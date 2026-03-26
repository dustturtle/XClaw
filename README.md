# XClaw

> 基于 MicroClaw 思路的 Python 多功能 **AI Agent 运行时**，聚焦**任务处理**与**A股/美股/港股投资助手**，面向中国用户打造。

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-passing-brightgreen.svg)](tests/)

---

## 目录

1. [功能特性](#功能特性)
2. [快速开始](#快速开始)
3. [配置详解](#配置详解)
4. [对话渠道](#对话渠道)
5. [Web API 参考](#web-api-参考)
6. [工具参考](#工具参考)
7. [Skills 系统](#skills-系统)
8. [MCP 工具联邦](#mcp-工具联邦)
9. [投资回测](#投资回测)
10. [智能记忆](#智能记忆)
11. [多用户隔离](#多用户隔离)
12. [定时任务](#定时任务)
13. [Docker 部署](#docker-部署)
14. [安全设计](#安全设计)
15. [开发与测试](#开发与测试)
16. [技术栈](#技术栈)

---

## 功能特性

| 分类 | 特性 |
|------|------|
| 🤖 **AI 引擎** | 支持 Anthropic Claude、OpenAI GPT、DeepSeek、Ollama（本地模型）|
| 💬 **多渠道** | 飞书、企业微信、钉钉、QQ 群、微信公众号/小程序、Web REST + SSE |
| 📈 **投资助手** | A股/美股/港股行情、历史K线、技术指标（MA/MACD/RSI/KDJ/BOLL）、财务数据、市场概览、个股新闻 |
| 📊 **策略回测** | 均线交叉（SMA Cross）/ RSI 策略回测，输出总收益、最大回撤、Sharpe 比率、胜率 |
| 🧠 **智能记忆** | 文件记忆（AGENTS.md）+ 结构化记忆 + **语义搜索**（字符二元组余弦相似度，纯 Python） |
| 🔌 **Skills 系统** | 可插拔技能包，支持 **SKILL.md 目录**（Claude Agent Skills 协议）、YAML 声明式、Python 编程式三种定义方式 |
| 🌐 **MCP 联邦** | JSON-RPC 2.0 连接外部 MCP 服务器，自动注册其工具到 Agent |
| 🔄 **协议兼容** | 支持 MCP Server 模式（暴露工具给外部 Agent）、OpenAI function calling 格式双向转换 |
| 👥 **多用户隔离** | `multi_user_mode` 按 Bearer Token 哈希隔离用户 session |
| ⏰ **定时任务** | APScheduler 调度，支持 cron 表达式和一次性任务 |
| 💾 **本地存储** | SQLite（aiosqlite），自选股/持仓数据永远不离本机 |
| 🐳 **Docker 部署** | 一键 `docker-compose up`，非 root 运行，内置健康检查 |
| 🔒 **安全设计** | 路径守卫、Bash 默认关闭、速率限制、Auth Token、SQL 参数化 |

---

## 快速开始

### 环境要求

- Python **3.11+**
- （可选）Docker & Docker Compose

### 1. 安装

```bash
# 克隆仓库
git clone https://github.com/dustturtle/XClaw.git
cd XClaw

# 安装（生产环境）
pip install -e .

# 安装（开发环境，含测试工具）
pip install -e '.[dev]'
```

### 2. 配置

**方式 A：交互式向导**

```bash
xclaw setup
# 按提示输入 LLM provider、API key、端口等
```

**方式 B：手动编辑**

```bash
cp xclaw.config.example.yaml xclaw.config.yaml
# 用编辑器打开 xclaw.config.yaml，至少填写：
#   api_key: "sk-..."
#   llm_provider: "anthropic"   # 或 openai / deepseek / ollama
```

**方式 C：纯环境变量（无配置文件）**

```bash
export XCLAW_LLM_PROVIDER="anthropic"
export XCLAW_API_KEY="sk-..."
export XCLAW_MODEL="claude-opus-4-5"
```

> 优先级：**环境变量** > **YAML 配置文件** > **默认值**  
> 所有环境变量以 `XCLAW_` 为前缀，例如 `XCLAW_WEB_PORT=9090`。

### 3. 诊断

```bash
xclaw doctor
# 检查 Python 版本、依赖、配置文件
```

### 4. 启动

```bash
xclaw start
# 默认在 http://127.0.0.1:8080 提供 Web API
# 访问 http://127.0.0.1:8080/docs 查看 Swagger 文档
```

### 5. 开始对话

```bash
# 发送第一条消息（需要另开终端或使用 curl）
curl -s -X POST http://127.0.0.1:8080/api/chat \
  -H "Content-Type: application/json" \
  -d '{"chat_id":"demo","message":"帮我查一下贵州茅台今天的股价"}'
```

---

## 配置详解

完整配置见 [`xclaw.config.example.yaml`](xclaw.config.example.yaml)。

### LLM 配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `llm_provider` | `anthropic` | `anthropic` / `openai` / `deepseek` / `ollama` |
| `api_key` | `""` | LLM API Key（建议通过环境变量设置） |
| `model` | `claude-opus-4-5` | 模型名称，可替换为 `gpt-4o`、`deepseek-chat`、`llama3` 等 |
| `max_tokens` | `4096` | 单次 LLM 回复最大 token 数 |
| `max_tool_iterations` | `50` | Agent 单轮工具调用最大轮次 |

### Web 服务配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `web_enabled` | `true` | 是否启用 Web 服务 |
| `web_host` | `127.0.0.1` | 绑定地址（**不建议**改为 `0.0.0.0`，应通过反向代理暴露） |
| `web_port` | `8080` | 服务端口 |
| `web_auth_token` | `""` | Bearer Token 鉴权，留空则不验证（生产环境**务必**设置） |
| `rate_limit_per_minute` | `20` | 每 IP 每分钟最大请求数 |

### 会话管理

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `max_session_messages` | `40` | 超过此数量触发上下文自动压缩 |
| `compact_keep_recent` | `20` | 压缩后保留的最近消息数 |
| `max_history_messages` | `50` | 数据库历史消息查询条数上限 |

### 投资配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `stock_market_default` | `CN` | 默认股票市场：`CN`（A股）/ `US`（美股）/ `HK`（港股） |
| `stock_data_source` | `akshare` | 数据源（`akshare` 用于 A股，`yfinance` 用于美港股） |

### 安全配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `bash_enabled` | `false` | 是否启用 Bash 工具（高风险，仅受信任环境开启） |
| `control_chat_ids` | `[]` | 管理员 chat ID 列表（High 风险操作需要） |
| `multi_user_mode` | `false` | 多用户隔离模式（详见[多用户隔离](#多用户隔离)） |

---

## 对话渠道

### Web（默认启用）

无需额外配置，启动后即可通过 REST API 或 SSE 流式接口对话。

```bash
# 非流式对话（返回完整回复）
curl -X POST http://127.0.0.1:8080/api/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-token>" \
  -d '{"chat_id":"user1","message":"今天 A股大盘怎么样？"}'

# 流式对话（Server-Sent Events）
curl -N http://127.0.0.1:8080/api/chat/stream \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-token>" \
  -d '{"chat_id":"user1","message":"分析一下茅台近30天走势"}'
```

### 飞书（Feishu/Lark）

1. 在[飞书开放平台](https://open.feishu.cn/)创建企业自建应用
2. 获取 `App ID`、`App Secret`
3. 在「事件订阅」设置回调地址：`https://your-host/webhook/feishu`
4. 获取「验证 Token」和「加密 Key」（可选）
5. 开启「接收消息」等事件权限
6. 配置文件：

```yaml
feishu_enabled: true
feishu_app_id: "cli_xxxx"
feishu_app_secret: "xxxx"
feishu_verification_token: "xxxx"
feishu_encrypt_key: ""         # 可选，开启加密后填写
```

### 企业微信（WeCom）

1. 在[企业微信管理后台](https://work.weixin.qq.com/)创建应用
2. 获取 `Corp ID`、`Agent ID`、`Secret`
3. 在「接收消息」设置回调：`https://your-host/webhook/wecom`
4. 记录 `Token` 和 `EncodingAESKey`
5. 配置文件：

```yaml
wecom_enabled: true
wecom_corp_id: "ww_xxxx"
wecom_agent_id: "1000001"
wecom_secret: "xxxx"
wecom_token: "xxxx"
wecom_encoding_aes_key: "xxxx"
```

### 钉钉（DingTalk）

1. 在[钉钉开放平台](https://open.dingtalk.com/)创建企业内部机器人
2. 获取 `App Key`、`App Secret`、`Robot Code`
3. 设置消息接收地址：`https://your-host/webhook/dingtalk`
4. 配置文件：

```yaml
dingtalk_enabled: true
dingtalk_app_key: "dingxxxx"
dingtalk_app_secret: "xxxx"
dingtalk_robot_code: "xxxx"
```

### QQ 群（QQ Group）

1. 在 [QQ 开放平台](https://q.qq.com) 注册并创建机器人应用
2. 获取 `AppID` 和 `AppSecret`
3. 设置消息回调地址：`https://your-host/webhook/qq`
4. 配置文件：

```yaml
qq_enabled: true
qq_app_id: "your_app_id"
qq_app_secret: "your_app_secret"
```

### 微信（WeChat / iLink 二维码登录）

这条链路适合已经接入 iLink 的微信 Bot 场景：

1. 在配置文件里启用 `wechat_enabled`
2. 启动 XClaw 后调用 `POST /api/auth/wechat/start` 获取二维码
3. 用户扫码确认后轮询 `GET /api/auth/wechat/status/{login_id}`
4. 确认成功后，XClaw 会自动开始通过 iLink 长轮询接收私聊消息

```yaml
wechat_enabled: true
wechat_base_url: "https://ilinkai.weixin.qq.com"
wechat_qr_total_timeout_seconds: 480
wechat_qr_poll_timeout_seconds: 35
wechat_qr_poll_interval_seconds: 1
wechat_poll_timeout_ms: 25000
wechat_max_reply_chars: 1500
```

常用接口：

- `POST /api/auth/wechat/start`：开始登录，返回 `login_id` 与二维码 SVG
- `GET /api/auth/wechat/status/{login_id}`：查询扫码状态
- `GET /api/auth/wechat/session`：查看当前是否已绑定微信 Bot
- `POST /api/auth/wechat/logout`：清除当前绑定
- `GET /api/wechat/bot/status`：查看轮询状态与最近错误

### 微信公众号（WeChat Official Account）

> 详细设计见 [`docs/wechat-design.md`](docs/wechat-design.md)

**前置条件**：已认证的服务号（订阅号功能受限）+ 公网 HTTPS 域名

1. 进入[微信公众平台](https://mp.weixin.qq.com/) → 开发 → 基本配置
2. 填写服务器 URL：`https://your-host/webhook/wechat_mp`
3. 自定义 `Token`（用于签名验证）
4. 获取 `AppID` 和 `AppSecret`
5. 配置文件：

```yaml
wechat_mp_enabled: true
wechat_mp_app_id: "wx_xxxx"
wechat_mp_app_secret: "xxxx"
wechat_mp_token: "your_token"
wechat_mp_encoding_aes_key: ""   # 可选，开启消息加密后填写
```

### 微信小程序（WeChat Mini Program）

XClaw 只提供后端 API，前端需自行开发微信小程序页面：

1. 注册微信小程序，获取 `AppID` 和 `AppSecret`（可与公众号共用）
2. 配置文件填入 `wechat_mp_app_id` 和 `wechat_mp_app_secret`
3. 小程序端调用登录流程：

```javascript
// 小程序端示例（JavaScript）
wx.login({
  success(res) {
    wx.request({
      url: 'https://your-host/api/wxmp/login',
      method: 'POST',
      data: { code: res.code },
      success(r) {
        const chatId = r.data.chat_id;  // 后续对话使用此 chat_id
        wx.setStorageSync('chat_id', chatId);
      }
    });
  }
});

// 发起对话
wx.request({
  url: 'https://your-host/api/chat',
  method: 'POST',
  data: {
    chat_id: wx.getStorageSync('chat_id'),
    message: '帮我看看茅台今天涨了多少'
  }
});
```

---

## Web API 参考

启动后访问 `http://127.0.0.1:8080/docs` 查看完整 Swagger 文档。

### 核心接口

| 路径 | 方法 | 鉴权 | 说明 |
|------|------|------|------|
| `GET /health` | GET | 无 | 健康检查，返回 `{"status":"ok"}` |
| `POST /api/chat` | POST | ✅ | 非流式对话，返回完整回复 |
| `POST /api/chat/stream` | POST | ✅ | SSE 流式对话，逐块返回回复 |
| `GET /api/sessions` | GET | ✅ | 查看会话列表 |
| `GET /api/sessions/{id}/messages` | GET | ✅ | 查看会话历史消息 |
| `GET /api/config` | GET | ✅ | 查看当前配置（已脱敏，不含 API Key）|
| `POST /api/auth/wechat/start` | POST | ✅ | 开始 iLink 微信二维码登录 |
| `GET /api/auth/wechat/status/{login_id}` | GET | ✅ | 查询微信二维码登录状态 |
| `GET /api/auth/wechat/session` | GET | ✅ | 查看当前微信 Bot 绑定状态 |
| `POST /api/auth/wechat/logout` | POST | ✅ | 解除当前微信 Bot 绑定 |
| `GET /api/wechat/bot/status` | GET | ✅ | 查看微信长轮询运行状态 |

### Webhook 接口

| 路径 | 方法 | 说明 |
|------|------|------|
| `POST /webhook/feishu` | POST | 飞书事件推送 |
| `POST /webhook/wecom` | POST | 企业微信消息接收 |
| `POST /webhook/dingtalk` | POST | 钉钉消息接收 |
| `POST /webhook/qq` | POST | QQ 群消息接收 |
| `GET /webhook/wechat_mp` | GET | 微信公众号 URL 验证（echostr challenge）|
| `POST /webhook/wechat_mp` | POST | 微信公众号消息接收 |
| `POST /api/wxmp/login` | POST | 微信小程序登录（`code` 换 `chat_id`）|

### 请求/响应示例

```jsonc
// POST /api/chat
// 请求
{
  "chat_id": "user_alice",   // 用户标识符（自定义字符串）
  "message": "茅台最近30天走势如何？"
}

// 响应
{
  "chat_id": "user_alice",
  "reply": "贵州茅台（600519）近30天：...\n最高价 1980，最低价 1850，整体震荡偏弱..."
}
```

---

## 工具参考

XClaw 内置以下工具，AI 会根据任务自动选择调用。

### 系统工具（`system` skill）

| 工具名 | 说明 | 风险级别 |
|--------|------|---------|
| `web_search` | 网页搜索 | Low |
| `web_fetch` | 抓取网页内容（HTML 转 Markdown）| Low |
| `read_file` | 读取本地文件（路径守卫保护）| Low |
| `write_file` | 写入本地文件 | Medium |
| `bash` | 执行 Shell 命令（需 `bash_enabled: true`）| **High** |
| `sub_agent` | 委托子任务给受限子 Agent（不支持 High 级工具）| Medium |

### 投资工具（`investment` skill）

| 工具名 | 说明 | 示例 |
|--------|------|------|
| `stock_quote` | 实时行情（A股/美股/港股）| "查 600519 的价格" |
| `stock_history` | 历史 K 线（日/周/月线）| "茅台近90天K线" |
| `stock_indicators` | 技术指标（MA/MACD/RSI/KDJ/BOLL）| "查茅台 RSI 指标" |
| `stock_fundamentals` | 财务基本面（PE/PB/营收/利润等）| "茅台财务数据" |
| `stock_news` | 个股或市场新闻 | "茅台最新新闻" |
| `market_overview` | 大盘指数概览（上证/深证/创业板等）| "今天大盘怎样？" |
| `watchlist_manage` | 自选股管理（add/remove/list）| "把茅台加入自选" |
| `portfolio_manage` | 持仓管理（buy/sell/view/pnl）| "记录买了100股茅台" |
| `stock_backtest` | 策略回测（见[投资回测](#投资回测)）| "回测茅台均线策略" |

### 记忆工具（`memory` skill）

| 工具名 | 说明 |
|--------|------|
| `read_memory` | 读取文件记忆（AGENTS.md）|
| `write_memory` | 写入/更新文件记忆 |
| `structured_memory_read` | 读取所有结构化记忆 |
| `structured_memory_update` | 添加结构化记忆条目（自动去重）|
| `semantic_memory_search` | **语义搜索**记忆（字符二元组余弦相似度）|

**快捷记忆命令**（无需 AI 处理，直接写入）：

```
记住：我喜欢价值投资，长期持有
remember: 我的风险偏好是保守型
```

### 调度工具（`task_management` skill）

| 工具名 | 说明 |
|--------|------|
| `schedule_task` | 创建定时任务（cron 表达式或一次性）|
| `list_scheduled_tasks` | 查看所有活跃定时任务 |
| `cancel_scheduled_task` | 取消指定定时任务 |

---

## Skills 系统

Skills（技能包）是 XClaw 的可扩展工具管理机制。每个 Skill 是一组相关工具的集合，可按需启用/禁用。

XClaw 支持三种自定义技能定义方式：

| 方式 | 适用场景 | 文件格式 |
|------|----------|----------|
| **SKILL.md 目录**（推荐） | 复杂 SOP 任务、带脚本/参考文档的技能 | 目录 + Markdown |
| **YAML 文件** | 简单 HTTP API 封装 | 单个 `.yaml` 文件 |
| **Python 文件** | 需要复杂编程逻辑的技能 | 单个 `.py` 文件 |

### 内置技能包

| Skill 名称 | 包含工具 |
|-----------|---------|
| `investment` | 所有投资类工具（行情/历史/指标/基本面/新闻/自选/持仓/回测/市场概览）|
| `memory` | 文件记忆 + 结构化记忆 + 语义搜索工具 |
| `system` | 网页搜索/抓取、文件读写、Bash（可选）|
| `task_management` | 定时任务创建/查询/取消 |

### 配置示例

```yaml
# 启用全部内置技能（默认）
enabled_skills:
  - all

# 按需启用（例如只需要投资功能）
enabled_skills:
  - investment
  - memory
  - system

# 自定义技能目录（放置 SKILL.md 目录、.yaml 文件、.py 文件）
skills_dir: "./xclaw.data/skills"
```

---

### 用 SKILL.md 编写自定义技能（推荐）

XClaw 兼容 **Claude Agent Skills 协议**，用户可以像写文档一样定义技能，无需编写任何 Python 代码。

#### 目录结构

在 `skills_dir` 下创建一个文件夹，包含 `SKILL.md` 入口文件：

```text
skills_dir/
└── code-reviewer/            # 技能目录（小写 + 短横线命名）
    ├── SKILL.md              # 核心定义与入口文件（必须）
    ├── references/           # 参考文档（可选）
    │   ├── rules.md
    │   └── api-docs.md
    ├── scripts/              # 可执行脚本（可选）
    │   ├── analyze.py
    │   └── validate.sh
    └── resources/            # 静态数据/资源（可选）
```

#### SKILL.md 格式

`SKILL.md` 由 **YAML Frontmatter**（元数据）和 **Markdown 正文**（指令）组成：

```markdown
---
name: code-reviewer
description: "代码审查助手，按照团队规范检查代码质量"
---

# Code Reviewer

## 执行步骤

1. 调用 `list_files` 查看技能目录结构
2. 使用 `read_reference` 读取 `rules.md` 了解审查规范
3. 让用户提供要审查的代码文件
4. 按照规范逐项检查，输出审查报告
5. 如果需要静态分析，使用 `run_script` 执行 `analyze.py`

## 输出格式

- 每个问题标注严重级别：🔴 严重 / 🟡 警告 / 🔵 建议
- 给出修复建议和示例代码
```

#### SKILL.md 规范

| 字段 | 说明 | 要求 |
|------|------|------|
| `name` | 技能唯一名称（小写 + 短横线） | 必须，最多 64 字符 |
| `description` | 技能描述（AI 据此判断何时启用） | 必须 |
| 正文 | 系统指令、执行步骤、引用指南 | 建议 500 行以内 |

#### 渐进式披露（Progressive Disclosure）

这是 SKILL.md 协议的核心设计，极大节省 Token 成本：

```text
┌─────────────────────────────────────────────┐
│  启动时（预加载）                              │
│  只加载 name + description → 极少 Token       │
└─────────────────────┬───────────────────────┘
                      │ 用户下达任务
                      ▼
┌─────────────────────────────────────────────┐
│  按需加载                                     │
│  AI 判断需要 → 调用 read_instructions         │
│  读取 SKILL.md 正文 → 获取执行步骤            │
└─────────────────────┬───────────────────────┘
                      │ 需要辅助信息
                      ▼
┌─────────────────────────────────────────────┐
│  无上下文惩罚执行                              │
│  run_script → 只返回输出，不占上下文           │
│  read_reference → 按需读取参考文档             │
└─────────────────────────────────────────────┘
```

#### 工具操作说明

每个 SKILL.md 技能自动注册为一个工具 `skill_<name>`（短横线转下划线），支持四种操作：

| 操作 | 说明 | 参数 |
|------|------|------|
| `read_instructions` | 读取 SKILL.md 正文（执行步骤） | 无 |
| `run_script` | 运行 `scripts/` 下的脚本，返回输出 | `filename`, `args`（可选） |
| `read_reference` | 读取 `references/` 下的参考文档 | `filename` |
| `list_files` | 列出技能目录下所有文件 | 无 |

脚本执行安全机制：
- 只能运行技能目录内 `scripts/` 下的文件
- 路径穿越自动拦截（`..`、`/` 等）
- 执行超时 30 秒
- `.py` 文件用 `python` 执行，`.sh` 文件用 `bash` 执行

#### 完整示例：投研分析技能

```text
xclaw.data/skills/
└── stock-research/
    ├── SKILL.md
    ├── scripts/
    │   └── sector_analysis.py
    └── references/
        └── analysis-framework.md
```

`SKILL.md`：

```markdown
---
name: stock-research
description: "A股行业研究助手，按照标准投研框架输出行业分析报告"
---

# 行业研究助手

## 执行步骤

1. 调用 `read_reference` 读取 `analysis-framework.md` 了解分析框架
2. 使用内置的 `stock_quote` 和 `market_overview` 工具获取行情数据
3. 使用 `run_script` 执行 `sector_analysis.py` 获取板块数据
4. 按照框架撰写研究报告

## 报告结构

- 行业概况与趋势
- 核心驱动因素
- 重点公司分析
- 风险提示
```

---

### 用 YAML 编写自定义技能

适合将外部 HTTP API 快速封装为技能。在 `skills_dir` 目录下创建 `.yaml` 文件：

```yaml
# xclaw.data/skills/weather.yaml
name: weather
description: "天气查询工具包"
tools:
  - name: get_weather
    description: "获取指定城市的当前天气"
    parameters:
      type: object
      properties:
        city:
          type: string
          description: "城市名"
      required: [city]
    http:
      method: GET
      url: "https://api.weatherapi.com/v1/current.json"
      query:
        key: "$WEATHER_API_KEY"
        q: "{{city}}"
      headers:
        User-Agent: "XClaw/0.1"
      response_path: "current.condition.text"
```

#### YAML 技能文件格式

| 字段 | 说明 |
|------|------|
| `name` | 技能唯一名称，用于 `enabled_skills` 列表 |
| `description` | 技能描述 |
| `tools` | 工具列表 |
| `tools[].name` | 工具名称（Agent 调用时使用）|
| `tools[].description` | 工具描述（AI 参考）|
| `tools[].parameters` | JSON Schema 格式的参数定义 |
| `tools[].http.method` | HTTP 方法（GET/POST/PUT/DELETE，默认 GET）|
| `tools[].http.url` | 请求 URL（支持模板变量）|
| `tools[].http.query` | URL 查询参数 |
| `tools[].http.headers` | 请求头 |
| `tools[].http.body` | POST/PUT 请求体（JSON 对象）|
| `tools[].http.response_path` | 从 JSON 响应中提取数据的路径（如 `results.0.text`）|
| `tools[].http.timeout` | 请求超时秒数（默认 20）|
| `tools[].http.max_chars` | 最大返回字符数（默认 8000）|

#### 模板变量语法

- `{{param_name}}` – 替换为工具调用时的参数值
- `$ENV_VAR` – 替换为环境变量值（适合存放 API Key 等敏感信息）

#### 更多 YAML 技能示例

**POST 请求 + JSON Body：**

```yaml
name: notification
description: "通知推送工具"
tools:
  - name: send_notification
    description: "发送通知消息"
    parameters:
      type: object
      properties:
        title:
          type: string
          description: "通知标题"
        message:
          type: string
          description: "通知内容"
      required: [title, message]
    http:
      method: POST
      url: "https://api.example.com/notify"
      headers:
        Authorization: "Bearer $NOTIFY_TOKEN"
      body:
        title: "{{title}}"
        text: "{{message}}"
```

---

### 导入别人的技能

可以通过 URL 直接安装别人分享的 YAML 技能文件：

```bash
# 从 URL 安装技能
xclaw skill install https://example.com/skills/weather.yaml

# 从 GitHub 安装（使用 raw 文件地址）
xclaw skill install https://raw.githubusercontent.com/user/xclaw-skills/main/weather.yaml
```

安装后需在 `enabled_skills` 中启用：

```yaml
enabled_skills:
  - all
  - weather
skills_dir: "./xclaw.data/skills"
```

### 技能管理命令

```bash
# 查看所有技能（内置 + 自定义）
xclaw skill list

# 安装远程技能
xclaw skill install <url>

# 删除自定义技能
xclaw skill remove <name>
```

### 用 Python 编写自定义技能

对于需要复杂逻辑的技能，仍然可以在 `skills_dir` 目录下创建 `.py` 文件：

```python
# xclaw.data/skills/my_crm.py
from xclaw.skills import Skill
from xclaw.tools import Tool, ToolContext, ToolResult, ToolRegistry

class GetCustomerInfoTool(Tool):
    @property
    def name(self) -> str: return "get_customer_info"
    @property
    def description(self) -> str: return "查询 CRM 客户信息"
    @property
    def parameters(self) -> dict: return {"type":"object","properties":{"id":{"type":"string"}},"required":["id"]}
    async def execute(self, params, context) -> ToolResult:
        # 调用内部 CRM API
        return ToolResult(content=f"客户 {params['id']} 的信息: ...")

class MyCRMSkill(Skill):
    name = "my_crm"
    description = "企业 CRM 集成工具"
    def register_tools(self, registry: ToolRegistry, settings) -> None:
        registry.register(GetCustomerInfoTool())
```

然后在配置中启用：

```yaml
enabled_skills:
  - all
  - my_crm
skills_dir: "./xclaw.data/skills"
```

---

## MCP 工具联邦

XClaw 支持通过 [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) 连接外部工具服务器，自动将其工具注册到 Agent 中。

### 配置 MCP 客户端（连接外部 MCP 服务器）

```yaml
mcp_servers:
  - name: "my_internal_tools"     # 标识符，用于日志
    url: "http://localhost:3000"   # MCP 服务器地址（HTTP JSON-RPC 2.0）
    timeout: 10                    # 请求超时（秒）
  - name: "analytics_server"
    url: "http://10.0.0.5:4000"
    timeout: 30
```

XClaw 在启动时：
1. 向每个 MCP 服务器发送 `initialize` 握手
2. 调用 `tools/list` 获取工具列表
3. 将每个工具包装为 `MCPToolAdapter` 注册到 ToolRegistry
4. AI 调用工具时转发 `tools/call` 请求

### MCP Server 模式（将 XClaw 工具暴露给外部客户端）

启用 `mcp_server_enabled` 后，XClaw 在 `/mcp` 端点提供标准 MCP 服务，
其他 MCP 兼容客户端（如 Claude Desktop、其他 Agent 框架）可以发现并调用 XClaw 注册的工具。

```yaml
mcp_server_enabled: true   # 在 /mcp 端点暴露 MCP 服务
```

MCP Server 遵循 MCP `2024-11-05` 版本协议，支持以下方法：

| 方法 | 说明 |
|------|------|
| `initialize` | 协议握手，返回版本和能力声明 |
| `tools/list` | 列出所有可用工具（自动排除高风险工具）|
| `tools/call` | 调用指定工具并返回结果 |

### MCP 服务器接口规范

XClaw 遵循 MCP 协议 `2024-11-05` 版本，服务器需实现：

```
POST / (Content-Type: application/json)
  - initialize      → {"result": {"protocolVersion": ..., "capabilities": {}}}
  - tools/list      → {"result": {"tools": [{"name": "...", "description": "...", "inputSchema": {...}}]}}
  - tools/call      → {"result": {"content": [{"type": "text", "text": "..."}], "isError": false}}
```

---

## 协议兼容性

XClaw 的 Skills 系统兼容以下开放标准协议：

| 协议 | 支持方式 | 说明 |
|------|----------|------|
| **Claude Agent Skills** | 原生支持 | SKILL.md 目录结构 + 渐进式披露 + 脚本执行 |
| **MCP (Model Context Protocol)** | 客户端 + 服务端 | 连接外部 MCP 服务器 / 将工具暴露为 MCP 服务 |
| **OpenAI Function Calling** | 格式转换 | `ToolDefinition.to_openai_function()` / `from_openai_function()` 双向转换 |
| **JSON Schema** | 原生支持 | 工具参数定义使用标准 JSON Schema |

### OpenAI Function Calling 格式转换

```python
from xclaw.llm_types import ToolDefinition

# XClaw → OpenAI 格式
td = ToolDefinition(name="search", description="搜索", input_schema={...})
openai_format = td.to_openai_function()
# → {"type": "function", "function": {"name": "search", ...}}

# OpenAI → XClaw 格式
td = ToolDefinition.from_openai_function(openai_format)

# XClaw → MCP 格式
mcp_format = td.to_mcp_tool()
# → {"name": "search", "description": "搜索", "inputSchema": {...}}

# MCP → XClaw 格式
td = ToolDefinition.from_mcp_tool(mcp_format)
```

### ToolRegistry 批量导出

```python
# 导出所有工具为 OpenAI 格式
openai_tools = registry.get_openai_definitions()

# 导出所有工具为 MCP 格式
mcp_tools = registry.get_mcp_definitions()

# 排除高风险工具
safe_tools = registry.get_openai_definitions(exclude_high_risk=True)
```

---

## 投资回测

`stock_backtest` 工具支持对历史数据运行简单策略回测。

### 支持策略

| 策略 | 参数 | 逻辑 |
|------|------|------|
| `sma_cross` | `fast_period`（默认10）、`slow_period`（默认30）| 快线上穿慢线买入，下穿卖出 |
| `rsi` | `rsi_period`（默认14）、`rsi_oversold`（默认30）、`rsi_overbought`（默认70）| RSI 低于超卖买入，高于超买卖出 |

### 对话示例

```
用户：帮我回测茅台过去一年的均线交叉策略
Agent：调用 stock_backtest({symbol:"600519", strategy:"sma_cross", fast_period:10, slow_period:30})

📊 回测结果：600519 (CN)
策略：均线交叉（SMA Cross）
日期范围：2024-02-26 → 2025-02-26（共 244 个交易日）

── 绩效指标 ──
  总收益率：+12.35%
  买入持有：+8.20%
  最大回撤：6.42%
  Sharpe 比率：1.23

── 交易记录（共 8 笔完整交易）──
  胜率：62.5%（5/8）
  第22日 🟢买入 @ 1862.30
  第45日 🔴卖出 @ 1920.10
  ...

⚠️ 免责声明：回测仅供参考，不构成投资建议，历史表现不代表未来。
```

---

## 智能记忆

XClaw 具备两层记忆系统：

### 1. 文件记忆（AGENTS.md）

自由格式的 Markdown 笔记，存储在 `<data_dir>/groups/<chat_id>/AGENTS.md`。

```
用户：记住：我是价值投资者，偏好消费和医疗赛道，风险承受能力中等
Agent：已记住：我是价值投资者，偏好消费和医疗赛道，风险承受能力中等
（后续对话中 Agent 会自动读取此记忆并应用到回复中）
```

### 2. 结构化记忆

SQLite 表存储，支持分类、置信度、自动去重（Jaccard 相似度阈值 0.7）。

```
用户：记住：持有茅台300股，成本价1800元
Agent：已记忆（id=5）：持有茅台300股，成本价1800元
```

### 3. 语义搜索

无需精确关键词，用自然语言查找相关记忆：

```
用户：我以前有没有提过关于投资偏好的事情？
Agent：调用 semantic_memory_search({query:"投资偏好风格", top_k:5})
→ 找到相关记忆：
  (0.82) [偏好] 我是价值投资者，偏好消费和医疗赛道，风险承受能力中等
  (0.61) [持仓] 持有茅台300股，成本价1800元
```

语义搜索使用字符二元组余弦相似度，**无需任何额外依赖**，支持中英文混合查询。

---

## 多用户隔离

在单机多用户场景（如团队共用一个 XClaw 实例），开启 `multi_user_mode` 可防止用户间数据互访。

```yaml
web_auth_token: "shared-server-secret"
multi_user_mode: true
```

**原理**：每个用户使用各自的 Bearer Token 调用 API，XClaw 对 Token 取 SHA-256 哈希前 16 位，作为该用户的 session 命名空间前缀，不同 Token 对应完全隔离的对话历史和记忆。

```bash
# 用户 Alice（自己的 token）
curl -X POST http://server/api/chat \
  -H "Authorization: Bearer alice_private_token" \
  -d '{"chat_id":"main","message":"..."}'
# → 实际使用 chat_id: "web_3f7a12b4c9d8e501_main"

# 用户 Bob（自己的 token）
curl -X POST http://server/api/chat \
  -H "Authorization: Bearer bob_private_token" \
  -d '{"chat_id":"main","message":"..."}'
# → 实际使用 chat_id: "web_9a2c45d1e8f7b3c6_main"（完全隔离）
```

> 如果 `web_auth_token` 为空或 `multi_user_mode: false`，`chat_id` 直接透传，行为与之前完全兼容。

---

## 定时任务

通过对话自然语言创建定时任务：

```
用户：每天下午3点盘后给我推送上证指数涨跌情况
Agent：已创建定时任务（每日 15:00）：查询大盘指数并推送摘要

用户：每周一早9点提醒我查看上周报告
Agent：已创建定时任务（每周一 09:00 cron: 0 9 * * 1）

用户：明天上午10点帮我查一下茅台的最新消息
Agent：已创建一次性任务（明日 10:00）
```

### 管理定时任务

```
用户：查看我的定时任务列表
用户：取消任务 3
```

---

## Docker 部署

### 方式一：docker-compose（推荐）

```bash
# 1. 准备配置文件
cp xclaw.config.example.yaml xclaw.config.yaml
# 编辑 xclaw.config.yaml 填入 api_key 等配置

# 2. 一键启动
docker-compose up -d

# 3. 查看日志
docker-compose logs -f xclaw

# 4. 停止
docker-compose down
```

数据持久化在 Docker Volume `xclaw_data`（SQLite 数据库、日志、记忆文件）。

### 方式二：docker run

```bash
# 构建镜像
docker build -t xclaw:latest .

# 运行（挂载配置文件和数据目录）
docker run -d \
  --name xclaw \
  -p 8080:8080 \
  -v $(pwd)/xclaw.config.yaml:/app/xclaw.config.yaml:ro \
  -v xclaw_data:/data \
  -e XCLAW_DATA_DIR=/data \
  -e XCLAW_WEB_HOST=0.0.0.0 \
  xclaw:latest
```

### 环境变量覆盖（Docker 推荐方式）

```bash
docker run -d \
  --name xclaw \
  -p 8080:8080 \
  -v xclaw_data:/data \
  -e XCLAW_LLM_PROVIDER=anthropic \
  -e XCLAW_API_KEY=sk-... \
  -e XCLAW_MODEL=claude-opus-4-5 \
  -e XCLAW_WEB_AUTH_TOKEN=my-secret-token \
  -e XCLAW_MULTI_USER_MODE=true \
  -e XCLAW_DATA_DIR=/data \
  -e XCLAW_WEB_HOST=0.0.0.0 \
  xclaw:latest
```

### 反向代理（生产环境）

建议通过 Nginx 或 Caddy 在前端做 HTTPS 终止，将 XClaw 的 `0.0.0.0:8080` 代理出去：

```nginx
# nginx.conf 示例
server {
    listen 443 ssl;
    server_name xclaw.example.com;

    ssl_certificate     /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        # SSE 需要禁用缓冲
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
}
```

---

## 安全设计

| 措施 | 详情 |
|------|------|
| **路径守卫** | 拦截所有涉及 `.ssh`、`.env`、`.aws`、`credentials`、`/etc/passwd` 等敏感路径的文件操作 |
| **Bash 默认关闭** | `bash_enabled` 默认 `false`，需显式开启，生产环境强烈建议保持关闭 |
| **工具风险分级** | Low / Medium / **High**，High 级工具（如 Bash）可限制为管理员专用 |
| **Web 绑定本地** | 默认 `127.0.0.1`，通过反向代理暴露，避免直接暴露给公网 |
| **Auth Token** | Web API 支持 Bearer Token 鉴权，生产环境强烈建议设置 |
| **速率限制** | 每 IP 每分钟 `rate_limit_per_minute`（默认20）次，防止滥用 |
| **投资数据本地** | 自选股/持仓仅存本地 SQLite，不上传任何云端，不支持自动交易 |
| **SQL 参数化** | 所有数据库操作使用参数化查询，防止 SQL 注入 |
| **多用户隔离** | `multi_user_mode` 模式下用户 session 互相隔离 |
| **Docker 非 root** | 容器内以 `xclaw`（UID 1000）用户运行 |

---

## 开发与测试

```bash
# 安装开发依赖
pip install -e '.[dev]'

# 运行全部测试
python -m pytest tests/ -v

# 运行单个测试文件
python -m pytest tests/test_phase5.py -v

# 查看覆盖率报告
python -m pytest tests/ --cov=xclaw --cov-report=term-missing

# 运行诊断
xclaw doctor
```

### 测试结构

```
tests/
├── conftest.py               # 共享 fixtures（临时 DB）
├── test_config.py            # 配置加载
├── test_db.py                # 数据库 CRUD
├── test_agent_engine.py      # Agent 循环（mock LLM）
├── test_llm_types.py         # LLM 消息类型
├── test_tools.py             # 基础工具（bash / sub_agent 等）
├── test_stock_tools.py       # 投资工具（mock akshare/yfinance）
├── test_memory.py            # 记忆系统
├── test_scheduler.py         # 定时任务
├── test_channels.py          # 渠道适配（Feishu/WeCom/DingTalk/QQ/Web）
├── test_wechat_ilink.py      # 微信 iLink 二维码登录 / 长轮询 Bot
├── test_wechat.py            # 微信公众号/小程序（24 个测试）
└── test_phase5.py            # Phase 5 全量测试（65 个测试）
```

### 添加自定义工具

所有工具继承 `xclaw.tools.Tool`，实现 4 个属性和 1 个方法：

```python
from xclaw.tools import Tool, ToolContext, ToolResult, RiskLevel

class MyTool(Tool):
    @property
    def name(self) -> str: return "my_tool"
    @property
    def description(self) -> str: return "工具说明（LLM 会读到）"
    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "查询内容"}
            },
            "required": ["query"]
        }
    @property
    def risk_level(self) -> RiskLevel: return RiskLevel.LOW

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        result = f"查询结果：{params['query']}"
        return ToolResult(content=result)
```

然后将工具打包进一个 Skill 并放入 `skills_dir` 即可（参见 [Skills 系统](#skills-系统)）。

---

## 技术栈

| 层次 | 选型 | 说明 |
|------|------|------|
| 语言 | Python 3.11+ | 使用 `asyncio` 全异步架构 |
| LLM 客户端 | httpx + pydantic v2 | 支持 Anthropic / OpenAI / DeepSeek / Ollama |
| Web 框架 | FastAPI + uvicorn | REST API + SSE 流式推送 |
| 速率限制 | slowapi | 基于 IP 的请求频率控制 |
| 数据库 | aiosqlite（SQLite）| 轻量本地存储，无服务器依赖 |
| A 股数据 | akshare | 免费 A股/ETF/指数行情 |
| 美港股数据 | yfinance | 美股/港股历史数据 |
| 技术分析 | pandas-ta | MA/MACD/RSI/KDJ/BOLL 等指标 |
| 调度 | APScheduler | cron 和一次性定时任务 |
| 通信渠道 | httpx（异步 HTTP）| 飞书 / 企业微信 / 钉钉 / QQ 群 / 微信 |
| 日志 | loguru | 彩色控制台 + 文件轮转 |
| CLI | click | `xclaw start / setup / doctor` |
| 测试 | pytest + pytest-asyncio + respx | mock httpx 请求，全异步测试 |
| 容器 | Docker + docker-compose | Python 3.11-slim，非 root 运行 |

---

## 免责声明

- 本工具**仅提供数据分析辅助**，不提供投资建议
- 不支持自动下单或自动交易
- 回测结果基于历史数据，**不代表未来收益**
- 不支持 Telegram（在中国大陆需翻墙）
- 使用前请确保遵守所在地相关法律法规

---

## License

MIT License — 详见 [LICENSE](LICENSE)
