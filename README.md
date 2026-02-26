# XClaw

基于 MicroClaw 思路的 Python 多功能 Agent 运行时，聚焦任务处理 & 股票投资助手

## 功能特性

- **多渠道支持**：飞书（Feishu/Lark）、企业微信（WeCom）、钉钉（DingTalk）、微信公众号（WeChat Official Account）、Web 浏览器（FastAPI + SSE）；微信小程序后端 API 支持
- **投资助手**：A股/美股行情查询、历史K线、技术指标（MA/MACD/RSI/KDJ/BOLL）、财务数据、市场概览、个股新闻
- **自选股 & 持仓管理**：本地 SQLite 存储，数据不上传
- **智能记忆**：文件记忆（AGENTS.md）+ 结构化记忆（含去重）
- **定时任务**：APScheduler 调度，支持 cron 表达式和一次性任务，内置每日盘后推送
- **会话持久化**：重启后自动恢复对话历史，支持上下文压缩
- **工具风险分级**：Low / Medium / High，Bash 工具默认关闭
- **安全设计**：路径守卫、Web 绑定本地、Auth Token、速率限制、SQL 参数化

## 快速开始

### 安装

```bash
# Python 3.11+ 必须
pip install -e .
```

### 配置

```bash
# 交互式向导
xclaw setup

# 或手动复制示例配置
cp xclaw.config.example.yaml xclaw.config.yaml
# 编辑填入 api_key 等配置
```

### 启动

```bash
xclaw start
# 默认访问 http://127.0.0.1:8080
```

### 诊断

```bash
xclaw doctor
```

## 配置说明

所有配置项见 `xclaw.config.example.yaml`。优先级：**环境变量** > **YAML 配置** > **默认值**。

环境变量前缀为 `XCLAW_`，例如：
```bash
export XCLAW_API_KEY="sk-..."
export XCLAW_LLM_PROVIDER="anthropic"
```

### 主要配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `llm_provider` | `anthropic` | `anthropic` / `openai` / `deepseek` / `ollama` |
| `api_key` | `""` | LLM API Key |
| `model` | `claude-opus-4-5` | 模型名称 |
| `web_host` | `127.0.0.1` | Web 服务绑定地址（不建议改为 0.0.0.0） |
| `web_port` | `8080` | Web 服务端口 |
| `web_auth_token` | `""` | Bearer Token 鉴权（留空不验证） |
| `bash_enabled` | `false` | 启用 Bash 工具（高风险，谨慎开启） |
| `data_dir` | `./xclaw.data` | 数据存储目录（SQLite + 日志） |
| `timezone` | `Asia/Shanghai` | 时区（影响定时任务） |
| `stock_market_default` | `CN` | 默认股票市场（`CN`/`US`/`HK`） |

## Web API

服务启动后访问 `http://127.0.0.1:8080/docs` 查看 Swagger 文档。

| 路径 | 方法 | 说明 |
|------|------|------|
| `GET /health` | GET | 健康检查 |
| `POST /api/chat` | POST | 非流式对话 |
| `POST /api/chat/stream` | POST | SSE 流式对话 |
| `GET /api/sessions` | GET | 查看会话列表 |
| `GET /api/sessions/{id}/messages` | GET | 查看会话消息 |
| `GET /api/config` | GET | 查看当前配置（已脱敏） |
| `POST /webhook/feishu` | POST | 飞书 webhook |
| `POST /webhook/wecom` | POST | 企业微信 webhook |
| `POST /webhook/dingtalk` | POST | 钉钉 webhook |
| `GET /webhook/wechat_mp` | GET | 微信公众号 URL 验证 |
| `POST /webhook/wechat_mp` | POST | 微信公众号消息接收 |
| `POST /api/wxmp/login` | POST | 微信小程序登录（code 换 chat_id） |

## 工具参考

### 基础工具

| 工具名 | 说明 | 风险级别 |
|--------|------|---------|
| `web_search` | 网页搜索 | Low |
| `web_fetch` | 抓取网页内容 | Low |
| `read_file` | 读取本地文件 | Low |
| `write_file` | 写入本地文件（路径守卫保护） | Medium |
| `bash` | 执行 Shell 命令（需 `bash_enabled: true`） | High |
| `sub_agent` | 委托子任务给受限子 Agent | Medium |

### 投资工具

| 工具名 | 说明 |
|--------|------|
| `stock_quote` | 查询股票实时行情（A股/美股/港股） |
| `stock_history` | 历史 K 线数据 |
| `stock_indicators` | 技术指标（MA/MACD/RSI/KDJ/BOLL） |
| `stock_fundamentals` | 财务基本面数据 |
| `stock_news` | 个股/市场新闻 |
| `market_overview` | 大盘指数概览 |
| `watchlist_manage` | 自选股管理（add/remove/list） |
| `portfolio_manage` | 持仓管理（buy/sell/view） |

### 记忆工具

| 工具名 | 说明 |
|--------|------|
| `read_memory` | 读取文件记忆（AGENTS.md） |
| `write_memory` | 写入文件记忆 |
| `structured_memory_read` | 读取结构化记忆列表 |
| `structured_memory_update` | 添加/更新结构化记忆 |

快捷命令：发送 `记住：<内容>` 或 `remember: <内容>` 可快速记录，无需调用 Agent。

### 调度工具

| 工具名 | 说明 |
|--------|------|
| `schedule_task` | 创建定时任务（cron 或一次性） |
| `list_scheduled_tasks` | 查看活跃定时任务 |
| `cancel_scheduled_task` | 取消定时任务 |

## 渠道配置

### 飞书（Feishu/Lark）

1. 创建企业自建应用，获取 `App ID`、`App Secret`
2. 在"事件订阅"中设置回调地址：`http://your-host/webhook/feishu`
3. 获取"验证 Token" 和"加密 Key"（可选）
4. 在配置文件中填入 `feishu_*` 字段并设置 `feishu_enabled: true`

### 企业微信（WeCom）

1. 在企业微信管理后台创建应用，获取 `Corp ID`、`Agent ID`、`Secret`
2. 在"接收消息"设置回调：`http://your-host/webhook/wecom`
3. 设置 `Token` 和 `EncodingAESKey`
4. 在配置文件中填入 `wecom_*` 字段并设置 `wecom_enabled: true`

### 钉钉（DingTalk）

1. 在钉钉开放平台创建企业内部机器人，获取 `App Key`、`App Secret`、`Robot Code`
2. 设置消息接收地址：`http://your-host/webhook/dingtalk`
3. 在配置文件中填入 `dingtalk_*` 字段并设置 `dingtalk_enabled: true`

### 微信公众号（WeChat Official Account）

> 详细设计见 [`docs/wechat-design.md`](docs/wechat-design.md)

**前置条件**：已认证的服务号 + 公网 HTTPS 域名（个人订阅号功能受限）

1. 在微信公众平台 → 开发 → 基本配置，填写服务器 URL：`https://your-host/webhook/wechat_mp`
2. 设置 `Token`（自定义字符串，用于签名验证）
3. 获取 `AppID` 和 `AppSecret`
4. 在配置文件中填入 `wechat_mp_*` 字段并设置 `wechat_mp_enabled: true`

### 微信小程序（WeChat Mini Program）

> 详细设计见 [`docs/wechat-design.md`](docs/wechat-design.md)

XClaw 只提供后端 API，前端需自行开发微信小程序：

1. 注册微信小程序，获取 `AppID` 和 `AppSecret`（可与公众号共用）
2. 在配置文件中填入 `wechat_mp_app_id` 和 `wechat_mp_app_secret`
3. 小程序端调用 `wx.login()` 获取 `code`，发送到 `POST /api/wxmp/login` 换取 `chat_id`
4. 后续对话直接调用 `/api/chat`，使用 `chat_id` 标识用户

## 安全设计

| 措施 | 说明 |
|------|------|
| 路径守卫 | 拦截 `.ssh`、`.env`、`.aws`、`credentials` 等敏感路径 |
| 工具风险分级 | Low / Medium / High，High 级需管理员确认 |
| Bash 默认关闭 | 需显式在配置中启用 |
| Web 绑定本地 | 默认 `127.0.0.1`，不暴露公网 |
| Auth Token | Web API 可配置 Bearer Token 鉴权 |
| 速率限制 | Web API 每 IP 每分钟限流 |
| 持仓数据本地 | 仅存 SQLite，不上传云端，无自动交易 |
| SQL 参数化 | 所有数据库操作参数化查询 |

## 开发 & 测试

```bash
# 安装开发依赖
pip install -e '.[dev]'

# 运行测试
python -m pytest tests/ -v

# 查看覆盖率
python -m pytest tests/ --cov=xclaw --cov-report=term-missing
```

## 技术栈

| 层次 | 选型 |
|------|------|
| 语言 | Python 3.11+ |
| 异步 | asyncio |
| LLM 客户端 | httpx + pydantic v2 |
| 通信渠道 | 飞书 / 企业微信 / 钉钉 + Web |
| Web 框架 | FastAPI + SSE |
| 数据库 | aiosqlite (SQLite) |
| 股票数据 | akshare（A股）/ yfinance（美股） |
| 技术分析 | pandas-ta |
| 调度 | APScheduler |
| 日志 | loguru |
| CLI | click |
| 测试 | pytest + pytest-asyncio + respx |

---

> **注意**：不支持 Telegram（在中国大陆需翻墙）。本工具仅提供数据分析辅助，不提供投资建议，不支持自动交易。
