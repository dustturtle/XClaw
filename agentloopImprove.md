# Agent Loop 优化方案

## 问题背景

用户问"上证指数最近20天的K线还有缺口没补吗"，从收到消息到最终回复耗时约 5 分钟。实际时间线：

| 时间 | 动作 | 问题 |
|------|------|------|
| 15:48:42 | stock_history（正确拿到上证指数 K 线） | ✅ 这一步已经够了 |
| 15:49:34 | stock_quote("000001") | ❌ 多余，且把 000001 当成平安银行 |
| 15:49:37 | stock_history("999999", asset_type="index") | ❌ 多余，触发 pytdx→yfinance→akshare 全部失败重试 |
| 15:50:03 | web_search | ❌ 数据已够，不需要搜索 |
| 15:50:15 | sub_agent | ❌ 嵌套循环，子 agent 又重新查了一次 stock_history |
| 15:53:36 | 最终回复 | 本应 1 步完成的任务走了 6 步 |

**核心结论**：不是单次 LLM 推理慢，而是"轮次多 + 工具调用绕路 + 失败重试 + 子 agent 嵌套" 累加导致。

## 根因分析（结合 learn-claude-code 设计理念）

learn-claude-code 的核心哲学：**"The model IS the agent, the code is the harness."** 不要用代码替模型做决策，而是给模型一个干净的环境让它自己做好决策。

XClaw 的 harness 存在三个关键缺陷：

### 缺陷 1：sub_agent 上下文污染（违反 s04 子智能体隔离原则）

learn-claude-code s04 的设计：子智能体以 `messages=[]` 启动，消息历史直接丢弃，父智能体只收到摘要文本。

XClaw 的 sub_agent 共享 `chat_id`，`agent_loop` 会从 DB 加载父级完整 session 历史，子循环的消息又写回同一个 session。导致：
- 子 agent 看到父级已查过的工具结果，但不知道已处理过
- 子 agent 的工具调用结果回写到父级 session，进一步膨胀上下文
- **这是 sub_agent "重复查询 stock_history" 的直接原因**

### 缺陷 2：缺少上下文压缩（缺失 s06 micro_compact 机制）

learn-claude-code s06 有三层压缩：
- Layer 1 (micro_compact)：每轮调 LLM 前，把旧 tool_result 替换为占位符
- Layer 2 (auto_compact)：token 超阈值时 LLM 做摘要
- Layer 3 (manual compact)：模型可主动触发压缩

XClaw 的 `_compact_messages` 只在消息数超 40 时触发，一次对话内多轮工具调用的大量 K 线数据全程留在 messages 里。模型看到一堆未整理的数据，分不清哪些已处理，就会"过度探索"。

### 缺陷 3：安全上限过高

`max_tool_iterations=50` + sub_agent 内部最多 10 轮 = 最坏 60 轮 LLM 调用。在上下文污染的情况下，模型会用满这个预算。

---

## 改进方案

### 方案 1：修复 sub_agent 上下文隔离（最关键）

**文件**: `xclaw/tools/sub_agent.py`

**改动**：让子 agent 使用全新的临时 chat_id / session，不读也不写父级的 session 数据。子 agent 完成后只返回摘要文本给父级。

**要点**：
- 在 DB 中为子 agent 创建临时 session（或完全跳过 session 持久化）
- 子 agent 的 `agent_loop` 不调用 `db.load_session` / `db.save_session`
- 子 agent 结束后，临时 session 可清理

**预期效果**：子 agent 不再看到父级已查过的数据，不会重复查询。

### 方案 2：加入 micro_compact（每轮工具调用前清理旧结果）

**文件**: `xclaw/agent_engine.py`

**改动**：在 agent_loop 的每次 LLM 调用前，把 N 轮以前的 `ToolResultBlock` 内容替换为简短占位符。

```python
KEEP_RECENT_TOOL_RESULTS = 3

def _micro_compact(messages: list[Message]) -> None:
    """Replace old tool_result content with placeholders (in-place)."""
    tool_result_positions = []
    for i, msg in enumerate(messages):
        if isinstance(msg.content, list):
            for j, block in enumerate(msg.content):
                if isinstance(block, ToolResultBlock):
                    tool_result_positions.append((i, j, block))
    if len(tool_result_positions) <= KEEP_RECENT_TOOL_RESULTS:
        return
    for i, j, block in tool_result_positions[:-KEEP_RECENT_TOOL_RESULTS]:
        if len(block.content) > 100:
            block.content = f"[已处理的工具结果，原内容已压缩]"
```

**预期效果**：模型每轮只看到最近几轮的完整工具结果，旧数据被压缩，不会干扰决策。

### 方案 3：降低 max_tool_iterations 兜底

**文件**: `xclaw/config.py`, `xclaw/tools/sub_agent.py`

**改动**：
- `config.py`: `max_tool_iterations` 默认值从 50 降为 **10**
- `sub_agent.py`: 内部 `max_iterations` 上限从 20 降为 **5**

**预期效果**：即使模型行为异常，最坏也只跑 10 + 5 = 15 轮，而不是 50 + 10 = 60 轮。

### 方案 4：连续错误 early-stop

**文件**: `xclaw/agent_engine.py`

**改动**：在 tool loop 中跟踪连续错误次数，达到阈值时提前退出。

```python
MAX_CONSECUTIVE_ERRORS = 3

consecutive_errors = 0
for iteration in range(_max_iter):
    # ... LLM 调用 ...
    # ... 工具执行 ...
    all_errors = all(r.is_error for r in tool_results_this_round)
    if all_errors:
        consecutive_errors += 1
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            # 注入提示让模型停止探索，直接用已有数据回答
            break
    else:
        consecutive_errors = 0
```

**预期效果**：避免 `stock_history("999999")` 那种 pytdx→yfinance→akshare 全失败后还继续的无效重试。

---

## 优先级与预期收益

| 优先级 | 方案 | 改动量 | 预期收益 |
|--------|------|--------|----------|
| P0 | 方案 1：sub_agent 上下文隔离 | 中等 | 消除子 agent 重复查询，减少 1-2 轮嵌套 LLM 调用 |
| P0 | 方案 2：micro_compact | 中等 | 模型决策更精准，减少"过度探索"轮次 |
| P1 | 方案 3：降低 max_iterations | 极小 | 兜底安全围栏，最坏情况从 5 分钟缩到 ~1.5 分钟 |
| P1 | 方案 4：连续错误 early-stop | 小 | 避免无效重试浪费时间 |

四个方案全部落地后，预期将"上证指数 K 线缺口分析"这类问题的响应时间从 ~5 分钟压缩到 **30 秒以内**。

---

## TODO

- [x] 方案 1：修改 `sub_agent.py`，子 agent 使用独立临时 session，不共享父级 chat_id 的 session 数据
- [x] 方案 1：修改 `agent_engine.py`，支持 sub_agent 传入 `skip_session_persistence` 标志（或新建临时 chat_id）
- [x] 方案 1：补充 sub_agent 隔离的单元测试
- [x] 方案 2：在 `agent_engine.py` 中实现 `_micro_compact` 函数
- [x] 方案 2：在 agent_loop 的 LLM 调用前插入 `_micro_compact(messages)` 调用
- [x] 方案 2：补充 micro_compact 的单元测试
- [x] 方案 3：`config.py` 中 `max_tool_iterations` 默认值从 50 改为 10
- [x] 方案 3：`sub_agent.py` 中 `max_iterations` 上限从 20 改为 5
- [x] 方案 4：在 `agent_engine.py` tool loop 中加入连续错误计数和 early-stop 逻辑
- [x] 方案 4：补充连续错误 early-stop 的单元测试
- [x] 全部改动完成后运行完整测试套件验证
- [ ] 部署到测试环境，用"上证指数最近20天的K线还有缺口没补吗"端到端验证

### 追加优化（2026-03-27 第二轮）

实测发现"华泰证券最近一个月有缺口没补吗？"仍被模型路由进 sub_agent，导致 3 次 LLM 调用（主 agent→sub_agent→主 agent）。根因是工具描述不够精确，模型无法正确判断何时该直接调工具、何时才需要委托。

**修复**：精准化工具描述（不改控制流，让模型自己做对决策）

- [x] `sub_agent` description 明确限定"仅用于多步骤、多工具协作的复杂研究任务"，单一数据查询+分析明确要求不要使用 sub_agent
- [x] `stock_history` description 补充"获取数据后可直接分析，无需再调其他工具"引导模型一步到位
- [ ] 部署验证"华泰证券最近一个月有缺口没补吗"不再走 sub_agent 路径
