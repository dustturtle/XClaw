# XClaw 接入 OpenAI Codex OAuth 的改造方案

## Summary

基于 PDF 里已验证可用的方案，我们把当前 XClaw 的“大模型接入 = provider + API key”模式，扩展成 **provider + credential source** 模式，并新增一个正式的 `openai-codex` provider。

这次改造不复用现有 `OpenAICompatibleProvider` 的认证方式，而是新增一条独立链路：

- OAuth 登录与 refresh token 持久化
- 运行时按需获取 / 刷新 access token
- 用 `chatgpt.com/backend-api` 跑 `openai-codex` 请求
- 通过 Web 管理端完成登录、手动粘贴回调、查看登录状态、退出登录
- 登录成功后无需重启服务即可生效

本方案明确沿用 PDF / OpenClaw 的关键协议参数：

- 固定 OAuth client id
- 固定 redirect URI `http://localhost:1455/auth/callback`
- PKCE S256
- `offline_access` refresh token
- local callback + manual paste fallback 双路径

## Execution Checklist

- [x] 配置层支持 `openai-codex` provider，并补充 `auth_profiles.json` 存储路径
- [x] 新增 provider-auth JSON store，带原子写入与文件锁
- [x] 新增 OpenAI Codex OAuth manager，覆盖：
  - [x] PKCE authorize URL
  - [x] localhost:1455 callback listener
  - [x] 手动粘贴 redirect URL / code
  - [x] authorization_code 换 token
  - [x] refresh token 自动刷新
  - [x] access token 身份解析
- [x] 新增 `OpenAICodexProvider`，运行时按需取 token、401 后强制 refresh 重试
- [x] runtime 接入 OAuth manager，登录后无需重启服务
- [x] Web 管理端新增 OAuth API 与管理页入口
- [x] 示例配置与 README 更新
- [x] 完成单元测试、Web API 测试、provider 行为测试

## Key Changes

### 1. Provider 体系改造

当前 `xclaw/llm.py` 只有：

- `AnthropicProvider`
- `OpenAICompatibleProvider`

这次新增独立的 `OpenAICodexProvider`，不混进 `OpenAICompatibleProvider`。

实现要求：

- `Settings.llm_provider` 增加 `openai-codex`
- `api_key` 对 `openai-codex` 变为可选，不再是必填
- `create_provider(...)` 改成支持注入一个 OAuth credential manager
- `OpenAICodexProvider.chat()` / `chat_stream()` 每次请求前都通过 credential manager 取当前 bearer token
- token 过期或接近过期时自动 refresh，不要求服务重启

约束：

- 保持现有 `anthropic/openai/deepseek/ollama` 行为不变
- `openai-codex` 单独走 `chatgpt.com/backend-api`
- 不把 refresh token 暴露给 `api/config`、日志或前端

### 2. 新增 OAuth 核心模块

新增一个独立的 OAuth 模块，建议放在新包里，例如：

- `xclaw/oauth/`
- 或 `xclaw/auth/`

它负责 4 件事：

#### 2.1 登录会话管理

- 创建 `login_id`
- 生成 `state`、`code_verifier`、`code_challenge`
- 记录开始时间、过期时间、当前阶段
- 这类登录会话只需要内存态 + TTL，不入数据库

#### 2.2 本地回调监听

- 按 PDF 的固定 redirect URI，在 `127.0.0.1:1455/auth/callback` 启一个临时 listener
- 如果能成功 bind，就支持自动回调完成
- 如果不能 bind，或者服务与用户浏览器不在同一台机器上，就走 manual paste

#### 2.3 token 交换与刷新

- `authorization_code -> access_token + refresh_token`
- `refresh_token -> new access_token (+ rotated refresh_token)`
- refresh 失败时给出明确错误，不吞异常
- refresh 要有进程内锁，避免并发请求同时刷新

#### 2.4 账号身份解析

- 从 access token/JWT 中提取 `account_id`、`email`、`display_name`
- 用于前端展示当前登录账号
- 解析失败不应影响基础 chat 能力，但要记录 warning

### 3. 持久化与安全策略

新增一个 provider-auth 存储文件，建议位置：

- `data_dir/auth_profiles.json`

建议结构：

- `profiles["openai-codex:default"] = { provider, type, access, refresh, expires, account_id, email, display_name, updated_at }`

要求：

- 读写都走文件锁
- refresh 后原地更新
- 文件权限尽量收紧到当前用户可读写
- 日志只记录 provider / profile / email / expiry，不记录 access/refresh token
- `/api/config` 和任何 Web 接口都不返回 token 原文

这层设计成 provider-agnostic，后面如果再加别的 OAuth provider 可以复用。

### 4. Web 管理端接入

当前系统已经有 Web 管理端，所以这次以 Web 为主，不把 CLI 作为 v1 主入口。

新增一组接口，风格对齐现有微信登录接口：

- `POST /api/llm/oauth/openai-codex/start`
  - 创建 login session
  - 返回 `login_id`、`authorize_url`、`expires_at`、`mode`
  - `mode` 至少区分：
    - `auto_callback`：本机 1455 listener 已启动，理论上可自动完成
    - `manual`：需要用户手动粘贴 redirect URL 或 code

- `GET /api/llm/oauth/openai-codex/status/{login_id}`
  - 返回当前状态：
    - `pending`
    - `awaiting_manual_input`
    - `completed`
    - `failed`
    - `expired`
  - 如已完成，带上账号摘要信息

- `POST /api/llm/oauth/openai-codex/complete`
  - 接收 `login_id` + `redirect_url_or_code`
  - 支持用户粘贴完整 redirect URL，也支持只粘贴 code
  - 完成 token exchange 并落盘

- `GET /api/llm/provider/session`
  - 返回当前 provider 的认证状态
  - 对 `openai-codex` 至少包含：
    - `authenticated`
    - `provider`
    - `email`
    - `display_name`
    - `expires_at`
    - `account_id`

- `POST /api/llm/oauth/openai-codex/logout`
  - 删除 `openai-codex:default` profile
  - 让后续请求立即变成未登录状态

前端管理页同步增加：

- “使用 ChatGPT / Codex 登录”按钮
- 当前登录账号卡片
- 手动粘贴回调输入框
- 注销按钮
- 明确提示：如果服务和浏览器不在同一台机器，需要手动粘贴回调 URL

### 5. Runtime 集成方式

当前 `runtime.run()` 在启动时创建一个 provider 实例并一路传下去。这次保留这个结构，但 provider 内部不再持有静态 token，而是持有一个 live credential manager。

要求：

- `runtime.run()` 初始化：
  - auth store
  - OAuth manager
  - `OpenAICodexProvider`
- Web 登录成功后，provider 不需要重建
- 新请求自然读取到最新 token
- refresh 失败时：
  - Web `/api/chat` 返回明确的“provider 未认证/认证失效”错误
  - 渠道对话返回可理解的失败提示
  - 同时在日志里给 operator 明确原因

### 6. 与 PDF 方案对齐但适配 XClaw 的边界

这次明确不做两件事，避免实现跑偏：

- 不使用任意自定义回调 URL
  - 因为 PDF / 上游方案绑定的是固定 localhost redirect
  - 我们不能擅自把它改成 `https://your-server/callback`

- 不把 CLI 作为第一优先级入口
  - v1 先把 Web 管理端产品化跑通
  - CLI 登录可作为第二阶段补齐
  - 但内部 OAuth manager 设计要能复用到 CLI

## Implementation Plan

### Phase 1. 基础设施与配置

- 在 `config.py` 中新增 `openai-codex` provider 支持
- 为 auth store 增加 `data_dir/auth_profiles.json` 路径访问器
- 让 `api_key` 在 `openai-codex` 场景下不再必填
- 在 `README` / 示例配置里明确：
  - `llm_provider: openai-codex`
  - `model: gpt-5.4`
  - 不需要填写 `api_key`

### Phase 2. OAuth 核心与持久化

- 新建 OAuth manager、login session manager、auth profile store
- 端到端实现：
  - PKCE 生成
  - authorize URL 拼装
  - local callback listener
  - manual paste 完成
  - token refresh
  - 身份解析
- 先把这一层做成独立单元，不和 `llm.py` 强耦合

### Phase 3. `OpenAICodexProvider` 落地

- 在 `llm.py` 或独立模块新增 provider
- provider 内部通过 OAuth manager 获取/刷新 token
- `chat()` 和 `chat_stream()` 都走 Codex 后端
- 现有 `LLMProvider` 抽象保持不变
- `create_provider()` 新增 `openai-codex` 分支

### Phase 4. Web 接口与管理页

- 在 `channels/web.py` 新增 OAuth start/status/complete/logout/session 接口
- 管理页增加登录状态显示和手动粘贴流程
- 把 provider 当前认证状态加入管理端展示
- 登录成功后可直接在 Web 聊天里验证，无需重启服务

### Phase 5. 错误处理与运维体验

- 增加 TLS preflight，遇到本机证书链问题时给出可执行提示
- 为 OAuth 请求显式接入全局代理/直连策略，避免“浏览器能开、后端换 token 失败”
- 所有日志统一做 token 脱敏
- provider 失效时返回可诊断、可操作的错误文案，而不是裸 traceback

## Test Plan

### Unit tests

- PKCE 生成合法，authorize URL 包含固定参数
- `state` 校验成功 / 失败
- token exchange 成功 / 失败
- refresh 成功 / 失败
- refresh 并发锁只允许一次真实刷新
- JWT 身份解析成功 / 解析失败
- auth store 读写、覆盖、锁保护正常

### Provider tests

- `openai-codex` provider 创建成功
- 未登录时 `chat()` 给出明确错误
- 已登录时 `chat()` 带上 bearer token
- access token 过期时自动 refresh
- refresh 后下一次请求不需要重启 provider
- `chat_stream()` 在已登录状态下可工作

### Web API tests

- `start` 返回 `login_id + authorize_url`
- `status` 正确反映 pending/completed/failed/expired
- `complete` 支持完整 redirect URL 与 raw code 两种输入
- `logout` 会清掉 profile
- `session` 不泄漏 access/refresh token

### End-to-end validation

- 本机浏览器 + 本机服务：
  - 1455 自动回调成功
  - 登录后立刻跑通 `/api/chat`
- 远程/无法自动回调场景：
  - manual paste 成功
  - 登录后无需重启服务即可对话
- access token 过期后：
  - 自动 refresh
  - 对话继续可用

## Assumptions

- v1 只支持一个 `openai-codex:default` profile，不做多账号切换。
- v1 主入口是 Web 管理端；CLI 登录不在本轮主范围内。
- 固定沿用 PDF/上游方案中的 localhost redirect，不设计新的公网 callback 变体。
- `openai-codex` 默认模型使用 `gpt-5.4`，其他模型仍允许通过现有 `model` 配置显式指定。
- 运行时请求与 streaming 的具体 wire shape 直接对齐 OpenClaw 上游 `extensions/openai/openai-codex-provider.*` 的实现，不重新发明协议。
