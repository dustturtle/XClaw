# XClaw 开发计划 (todolist)

> 基于 MicroClaw 思路的 Python 多功能 Agent 运行时，聚焦任务处理 & 股票投资助手
>
> **目标用户**：中国用户。通信渠道支持**飞书**（Feishu/Lark）、**企业微信**（WeCom）、**钉钉**（DingTalk）及 Web 界面，**不支持 Telegram**。

---

## Phase 1：核心骨架（2-3 周）

**目标**：最小可用系统，能通过 Web / 飞书对话并调用工具

- [x] 项目脚手架搭建（pyproject.toml, 目录结构）
- [x] 配置加载（pydantic-settings + YAML）
- [x] LLM Provider 抽象 + Anthropic / OpenAI-Compatible 实现（httpx）
- [x] Pydantic 消息类型系统（Message, ToolUse, ToolResult）
- [x] 核心 Agent 循环（agent_loop）
- [x] Tool 基类 + ToolRegistry + 风险分级（Low / Medium / High）
- [x] 路径守卫（path_guard）
- [x] 基础工具：`web_search`、`web_fetch`
- [x] SQLite 数据库层（chats, messages, sessions 表，aiosqlite）
- [x] 会话持久化 + 恢复
- [x] Web 渠道适配（FastAPI + SSE，绑定 127.0.0.1）
- [x] 速率限制
- [x] CLI 入口（`xclaw start`）
- [x] 基础日志（loguru）
- [x] **自动化测试**：test_config, test_db, test_tools, test_agent_engine

**验收标准**：通过 Web 界面与 Agent 对话，Agent 能搜索网页并回答问题，重启后会话可恢复。

---

## Phase 2：投资工具 + 记忆（2-3 周）

**目标**：投资助手核心功能可用

- [x] 投资工具集：`stock_quote`（akshare A股 / yfinance 美股）
- [x] 投资工具集：`stock_history`（历史 K 线）
- [x] 投资工具集：`stock_indicators`（MA/MACD/RSI/KDJ/BOLL，pandas-ta）
- [x] 投资工具集：`stock_fundamentals`（财务数据）
- [x] 投资工具集：`market_overview`（大盘指数、北向资金）
- [x] 投资工具集：`stock_news`（个股/市场新闻）
- [x] 自选股管理：`watchlist_manage`（watchlist 表）
- [x] 持仓管理：`portfolio_manage`（portfolio 表）
- [x] 文件记忆系统（AGENTS.md）
- [x] 结构化记忆（memories 表 + CRUD 工具）
- [x] 显式 "记住..." 快速路径
- [x] 上下文压缩（超限摘要）
- [x] 文件工具：`read_file`、`write_file`
- [x] System Prompt 增强（注入记忆 + 投资人设）
- [x] **自动化测试**：test_stock_tools, test_memory, test_watchlist, test_portfolio

**验收标准**：用户可查行情、管理自选股和持仓，Agent 能记住用户偏好。

---

## Phase 3：定时任务 + 渠道扩展（2-3 周）

**目标**：自动化 + 中国用户友好的渠道支持

- [x] 定时任务系统（APScheduler + scheduled_tasks 表）
- [x] 调度工具：`schedule_task`、`list_scheduled_tasks`、`cancel_scheduled_task`
- [x] 每日盘后自动推送（集成投资工具）
- [x] **飞书（Feishu/Lark）渠道适配**（机器人 webhook + 事件回调）
- [x] **企业微信（WeCom）渠道适配**（群机器人 webhook + 企业应用消息）
- [x] **钉钉（DingTalk）渠道适配**（webhook 机器人）
- [x] FastAPI Web 后端（/api/chat, /api/sessions, /api/config）
- [x] SSE 流式响应
- [x] Token 用量统计（llm_usage 表）
- [x] **自动化测试**：test_scheduler, test_channels

**验收标准**：定时任务可自动推送盘后分析，飞书/企业微信/钉钉机器人可正常收发消息。

---

## Phase 4：增强 + 打磨（2-3 周）

**目标**：生产可用的质量

- [x] OpenAI-compatible Provider（支持 DeepSeek / Ollama / 通义千问 / 文心一言）
- [x] 记忆质量规则（去重、置信度、归档）
- [x] Bash 工具（可选，默认关闭）
- [x] Sub-agent 工具（受限工具集）
- [x] Web 认证（auth token）
- [x] 速率限制完善
- [x] 交互式 setup 向导（`xclaw setup`）
- [x] 错误处理 + 优雅降级
- [x] 完整单元测试 + 集成测试（pytest + pytest-asyncio）
- [x] 文档完善（README, 配置说明, 工具参考）

**验收标准**：系统稳定运行，安全措施到位，文档齐全，测试覆盖率 > 80%。

---

## Phase 5：可选进阶（按需）

- [x] **语义记忆（embedding + 向量检索）**
  - [x] 字符二元组 TF-IDF + 余弦相似度（无需额外依赖，纯 Python + math）
  - [x] `StructuredMemory.semantic_search()` 方法
  - [x] `SemanticMemorySearchTool` 工具
  - [x] 无相关记忆时返回提示，支持中英文混合查询
- [x] **MCP 工具联邦**
  - [x] `xclaw/mcp.py`：`MCPClient`（JSON-RPC 2.0 over HTTP）
  - [x] `MCPToolAdapter`：将 MCP 工具包装为 XClaw Tool
  - [x] `load_mcp_tools()`：启动时批量连接并注册工具
  - [x] 配置字段：`mcp_servers: [{name, url, timeout}]`
  - [x] 运行时接入（`xclaw/runtime.py`）
- [x] **Skills 系统（可扩展技能包）**
  - [x] `xclaw/skills/__init__.py`：`Skill` 抽象基类 + `SkillRegistry`
  - [x] 内置技能包：`investment`、`task_management`、`memory`、`system`
  - [x] 自定义技能目录（`skills_dir`）支持热加载 `.py` 文件
  - [x] 配置字段：`enabled_skills`（默认 `["all"]`）
  - [x] `runtime.py` 重构为 Skills-based 工具注册
- [x] **投资回测工具**
  - [x] `xclaw/tools/stock_backtest.py`：`StockBacktestTool`
  - [x] 支持策略：均线交叉（`sma_cross`）、RSI 策略（`rsi`）
  - [x] 绩效指标：总收益率、买入持有对比、最大回撤、Sharpe 比率、胜率
  - [x] 支持 A股（akshare）、美股/港股（yfinance）
- [x] **多用户隔离**
  - [x] `multi_user_mode: bool` 配置字段（默认 false）
  - [x] Web 渠道：开启时按 Bearer Token 哈希命名 session 空间
  - [x] 不同 token → 不同用户 → 不同 `chat_id` 空间，防止数据互访
  - [x] 无 token / 关闭模式：行为与原来完全兼容
- [x] **Docker 部署方案**
  - [x] `Dockerfile`（Python 3.11-slim + 非 root 用户 + HEALTHCHECK）
  - [x] `docker-compose.yml`（持久化 Volume + 环境变量覆盖）
  - [x] `.dockerignore`（排除 secrets、缓存、数据目录）
- [x] **微信公众号 / 小程序适配**（详见 [`docs/wechat-design.md`](docs/wechat-design.md)）
  - [x] 设计文档（`docs/wechat-design.md`）
  - [x] `WeChatMPAdapter`（`xclaw/channels/wechat_mp.py`）
    - [x] SHA1 签名验证（GET URL-verify 和 POST 消息验证）
    - [x] XML 消息解析（文本、非文本类型）
    - [x] MsgId 去重（防微信重试推送）
    - [x] 被动回复（Passive Reply）立即 ACK
    - [x] 异步客服消息 API（Customer Service API，主动推送 AI 结果）
    - [x] Access token 管理（自动获取 + 到期刷新）
    - [x] `code2session` API（支持小程序登录）
  - [x] 配置字段（`xclaw/config.py`：`wechat_mp_*`）
  - [x] 路由（`xclaw/channels/web.py`）
    - [x] `GET /webhook/wechat_mp`（URL 验证 echostr challenge）
    - [x] `POST /webhook/wechat_mp`（接收公众号消息）
    - [x] `POST /api/wxmp/login`（小程序 code 换 chat_id）
  - [x] 运行时接入（`xclaw/runtime.py`）
  - [x] 配置文件示例（`xclaw.config.example.yaml`）
  - [x] 自动化测试（`tests/test_wechat.py`，24 个测试）

---

## 技术栈

| 层次 | 选型 |
|------|------|
| 语言 | Python 3.11+ |
| 异步 | asyncio |
| LLM 客户端 | httpx + pydantic v2 |
| 通信渠道 | 飞书 / 企业微信 / 钉钉（中国用户）+ Web |
| Web 框架 | FastAPI + SSE |
| 数据库 | aiosqlite |
| 股票数据 | akshare（A股）/ yfinance（美股） |
| 技术分析 | pandas-ta |
| 调度 | APScheduler |
| 日志 | loguru |
| CLI | click |
| 测试 | pytest + pytest-asyncio + respx（mock httpx） |

---

## 渠道适配说明（中国用户）

| 渠道 | 接入方式 | 适用场景 |
|------|---------|---------|
| 飞书（Feishu） | 企业自建应用 + 机器人 | 团队 / 企业内部使用 |
| 企业微信（WeCom） | 企业应用 + 群机器人 webhook | 已有企微环境的用户 |
| 钉钉（DingTalk） | 企业内部机器人 webhook | 已有钉钉环境的用户 |
| **微信公众号** | 已认证服务号 + 公网 HTTPS 域名 | 面向关注公众号的微信用户 |
| **微信小程序** | 后端 API + 小程序前端（自行开发） | 构建专属微信小程序 |
| Web（浏览器） | FastAPI + SSE | 个人本地使用，无需企业账号 |

> **注意**：不支持 Telegram（在中国大陆需翻墙）。

---

## 安全设计

| 措施 | 说明 |
|------|------|
| 路径守卫 | 拦截 `.ssh`、`.env`、`.aws`、`credentials` 等敏感路径 |
| 工具风险分级 | Low / Medium / High，High 级需管理员确认 |
| Bash 默认关闭 | 需显式在配置文件中启用 |
| Web 绑定本地 | 默认 `127.0.0.1`，不暴露公网 |
| Auth Token | Web API Bearer Token 鉴权 |
| 速率限制 | Web API 每会话限流 |
| 持仓数据本地 | 仅存 SQLite，不上传云端，无自动交易 |
| SQL 参数化 | 所有数据库操作参数化查询 |

---

## 自动化测试策略

```
tests/
├── conftest.py               # 共享 fixtures（临时 DB、mock LLM）
├── test_config.py            # 配置加载测试
├── test_db.py                # 数据库 CRUD 测试
├── test_agent_engine.py      # Agent 循环（mock LLM）
├── test_llm_types.py         # LLM 消息类型测试
├── test_tools.py             # 基础工具测试（含 bash_tool, sub_agent）
├── test_stock_tools.py       # 投资工具测试（mock akshare/yfinance）
├── test_memory.py            # 记忆系统测试
├── test_scheduler.py         # 定时任务测试
├── test_channels.py          # 渠道适配测试（Feishu/WeCom/DingTalk/Web）
├── test_wechat.py            # 微信公众号 / 小程序适配测试
└── test_phase5.py            # Phase 5 全量测试（语义记忆/MCP/Skills/回测/多用户/Docker）
```

- 使用 `pytest-asyncio` 支持异步测试
- 使用 `respx` mock httpx HTTP 请求（LLM API、行情 API）
- 使用 `pytest` fixtures 提供临时 SQLite 数据库
- 单元测试覆盖所有工具 execute 方法
- 集成测试验证 agent_loop 完整流程

---

*本计划根据中国用户需求调整：移除 Telegram，增加飞书、企业微信、钉钉三种渠道适配。*

