# 飞书 OpenClaw 插件实现研究及 XClaw 参考思路

## 一、社区主要项目概览

社区围绕飞书接入 OpenClaw 形成了三类方案，覆盖了从"最简 demo"到"生产级插件"的完整谱系：

| 项目 | Stars | 方案类型 | 语言 | 核心区别 |
|------|-------|---------|------|---------|
| AlexAnys/openclaw-feishu | 632 | OpenClaw Gateway 插件 | TypeScript（17 个源文件） | 标准方案：作为 OpenClaw 插件运行，支持 WebSocket + Webhook 双模式 |
| AlexAnys/feishu-openclaw | 326 | 独立桥接器 | JavaScript（单文件 bridge.mjs） | 隔离方案：独立进程，不受 Gateway 崩溃影响 |
| Futaoj/enable_openclaw_feishu_lark | 74 | 最小化示例 | JavaScript（单文件） | 参考实现：展示飞书 SDK WebSocket 长连接最简用法 |

---

## 二、核心技术架构分析

### 2.1 连接模式：WebSocket 长连接 vs Webhook

**这是与 XClaw 当前实现最大的区别。**

XClaw 当前飞书适配器使用的是 Webhook（HTTP POST 推送）模式，消息流向为：飞书 → POST /webhook/feishu → XClaw FastAPI Server。这种方式需要公网 IP 或域名、内网穿透工具（ngrok/frp）以及 HTTPS 证书。

OpenClaw 插件使用的是 WebSocket 长连接模式（飞书 SDK 内置能力），消息流向为：XClaw 主动通过 WebSocket 连接到飞书开放平台，飞书通过该长连接推送事件。这种方式无需公网 IP、无需域名、无需内网穿透、无需 HTTPS。

飞书官方 Node.js SDK（@larksuiteoapi/node-sdk）的 WSClient 类封装了全部 WebSocket 逻辑。在 Python 端，等价的是飞书官方 Python SDK lark-oapi 的 ws 模块。

### 2.2 OpenClaw 插件版核心代码解析

#### 项目结构（AlexAnys/openclaw-feishu）

- **src/channel.ts** — 核心模块，定义 ChannelPlugin 和 ChannelDock（OpenClaw 插件接口）
- **src/receive.ts** — 消息接收，WebSocket 和 Webhook 双模式 provider
- **src/send.ts** — 消息发送，支持文本和媒体（图片/视频/音频/文件）
- **src/dedup.ts** — 消息去重，10 分钟 TTL 的 Map
- **src/media.ts** — 媒体处理，下载、上传、类型识别
- **src/group-filter.ts** — 群聊过滤，仅在 @、提问、请求类动词时回复
- **src/accounts.ts** — 多账号管理
- **src/onboarding.ts** — 引导式配置（setup wizard）
- **src/probe.ts** — 连接探测（检查 appId/secret 是否有效）
- **src/types.ts** — 类型定义
- **src/runtime.ts** — OpenClaw Runtime 引用
- **src/config-json-schema.ts** — 配置 Schema

#### 关键技术点逐一拆解

**① 双模式连接（receive.ts）**

飞书国内版支持 WebSocket，Lark 国际版不支持 WebSocket，只能用 Webhook。插件自动检测域名：如果 domain 为 lark 且 connectionMode 为 websocket，自动降级为 webhook 模式。每个 account 可独立配置 connectionMode 和 webhookPort。

WebSocket 模式下，使用飞书 SDK 的 WSClient 建立长连接，注册 EventDispatcher 处理 im.message.receive_v1 事件。Webhook 模式下，使用飞书 SDK 的 adaptDefault 创建 HTTP 事件处理器，启动本地 HTTP 服务器监听回调。

**② 消息去重（dedup.ts）**

使用一个 Map 结构，以 messageId 为 key、时间戳为 value，TTL 为 10 分钟。每次检查时先 GC 过期条目，再判断是否已见过该 messageId。

对比 XClaw：XClaw 飞书适配器无去重机制，微信公众号适配器有基于 MsgId 的去重（限 500 条）。

**③ "正在思考…" 占位符（receive.ts）**

收到消息后设置一个定时器（默认 2500ms），如果 AI 在阈值时间内完成回复，直接发送最终回复；如果超过阈值，先发送"正在思考…"占位消息，记录其 message_id。AI 回复生成后，使用消息编辑 API（PATCH /im/v1/messages/{id}）将占位消息替换为最终回复内容。如果最终结果为 NO_REPLY，则删除占位消息。

对比 XClaw：XClaw 没有此机制。微信公众号适配器用被动回复返回"消息已收到"，但不会后续替换。

**④ 群聊智能过滤（group-filter.ts）**

群聊中不是所有消息都回复，只在以下条件满足时响应：

- 被 @ 了
- 消息以 ? 或 ？ 结尾（提问）
- 包含请求类动词（帮、请、分析、总结、写…）
- 用名字呼唤（可自定义 botNames 列表）

其他闲聊不会回复，避免刷屏。

对比 XClaw：XClaw 飞书适配器不区分群聊和私聊，所有消息都处理。

**⑤ 媒体支持（send.ts + media.ts）**

发送图片时，先上传到飞书获取 image_key，再用该 key 发送图片消息。发送视频和文件时，先上传获取 file_key。发送失败时降级为纯文本加 URL 的形式。支持图片、视频、音频、文件四种媒体类型。

对比 XClaw：XClaw 飞书适配器仅支持纯文本消息。

**⑥ 多账号管理（accounts.ts + types.ts）**

支持在一个 OpenClaw 实例中配置多个飞书应用，每个有独立的 appId/secret、连接模式、权限策略（pairing/allowlist/open/disabled）。

对比 XClaw：XClaw 每个 channel 只支持单账号。

### 2.3 独立桥接版核心代码解析

项目结构（AlexAnys/feishu-openclaw）为单文件 bridge.mjs（约 1500 行），外加 setup-service.mjs。

关键差异：

- 不依赖 OpenClaw 插件 SDK，直接通过本地 WebSocket 连接 Gateway
- 更完整的媒体处理（收图 → AI 看图、AI 生图 → 回传飞书）
- Device Identity 认证机制（Ed25519 密钥对 + 签名）
- macOS launchd 保活机制
- 本地文件发送白名单（默认仅允许 ~/.clawdbot/media、系统临时目录、/tmp）
- 调试模式（FEISHU_BRIDGE_DEBUG=1）

### 2.4 最小参考实现

Futaoj/enable_openclaw_feishu_lark 展示了飞书 WebSocket 长连接的最简用法：只需创建 Lark.Client 和 Lark.WSClient，注册 im.message.receive_v1 事件处理器，在回调中解析文本消息并调用 client.im.v1.message.create 发送回复。

注意事项：

- 飞书长连接有 3 秒处理时限，超时会触发重推
- 每个应用最多 50 个长连接
- 多客户端部署时，随机一个客户端收到消息（不支持广播）

---

## 三、XClaw 参考该方案的实现思路

### 3.1 总体策略

推荐方案：在现有 Webhook 模式基础上，新增 WebSocket 长连接模式，让用户按需选择。

| 维度 | 当前 XClaw | 改进目标 |
|------|-----------|---------|
| 连接模式 | 仅 Webhook | WebSocket（默认）+ Webhook（兼容） |
| 消息去重 | 无 | 有（10 分钟 TTL Map） |
| 思考提示 | 无 | "正在思考…" → 替换为最终回复 |
| 媒体支持 | 仅文本 | 图片和文件（可分阶段） |
| 群聊过滤 | 无 | 智能过滤（@、提问、请求动词） |
| 多账号 | 单账号 | 可后续扩展 |

### 3.2 Python 端对应的技术选型

飞书 OpenClaw 插件基于 Node.js 的 @larksuiteoapi/node-sdk，Python 端有对应的官方 SDK：

| Node.js 组件 | Python 对应 | 说明 |
|-------------|-----------|------|
| @larksuiteoapi/node-sdk | lark-oapi（PyPI） | 飞书官方 Python SDK |
| Lark.WSClient | lark_oapi.ws.Client | WebSocket 长连接客户端 |
| Lark.Client | lark_oapi.Client | API 调用客户端 |
| Lark.EventDispatcher | lark_oapi.EventDispatcher | 事件分发器 |

lark-oapi 是飞书官方维护的 Python SDK，可通过 pip 直接安装。

也可以选择不引入 SDK，直接用 websockets 库实现 WebSocket 连接（飞书长连接的 WebSocket 协议并不复杂），这样保持 XClaw 轻量依赖的风格，但需要自行处理连接管理、心跳、重连等逻辑。

### 3.3 具体实现流程

**Step 1：配置层扩展（config.py）**

新增三个配置项：连接模式（websocket/webhook，默认 websocket）、思考占位阈值（默认 2500ms，0 则禁用）、群聊中机器人别名列表。

**Step 2：新建 WebSocket 模式适配器（修改 feishu.py）**

核心改动包括四个方面：

- start() 方法不再是空操作，而是启动飞书 SDK 的 WebSocket 长连接客户端
- 新增去重逻辑：10 分钟 TTL 的字典
- 新增"正在思考…"机制：收到消息后设定时器，超时发占位符，AI 回复后用消息编辑 API 替换内容
- 群聊过滤：检测 mentions、问号、请求动词

同时保留 Webhook 兼容：当 connection_mode 为 webhook 时走现有路径。

**Step 3：运行时注册适配（runtime.py）**

WebSocket 模式下，适配器的 start() 会主动建立连接，不需要注册 Webhook 路由。只在 connection_mode 为 webhook 时才在 FastAPI 应用中注册 /webhook/feishu 路由。

**Step 4："正在思考…" 实现细节**

使用 asyncio.create_task 创建一个异步任务，在设定的阈值时间后发送占位消息并记录其 message_id。当 AI 回复生成后，取消定时任务。如果占位消息已发送（message_id 非空），则用消息编辑 API 替换内容；否则直接发送最终回复。出错时清理占位消息。

**Step 5：消息去重**

实现一个简单的 MessageDedup 类，内部用字典存储已见消息 ID 及其时间戳，TTL 为 10 分钟，每次检查时自动清理过期条目。

### 3.4 实现优先级建议

| 阶段 | 内容 | 价值 |
|------|------|------|
| Phase 1 | WebSocket 长连接模式 + 消息去重 | 最高。免去公网暴露需求，大幅降低部署难度 |
| Phase 2 | "正在思考…" 占位符 + 群聊智能过滤 | 高。提升用户体验 |
| Phase 3 | 图片和文件媒体支持 | 中。需要飞书 SDK 的文件上传 API |
| Phase 4 | 多账号管理 | 低。大多数用户单账号即可 |

### 3.5 关键风险点

| 风险 | 说明 | 缓解措施 |
|------|------|---------|
| lark-oapi Python SDK 的 WebSocket 实现成熟度 | Node SDK 已广泛验证，Python SDK 可能稍滞后 | 做充分测试；备选方案是直接用 websockets 库 |
| 3 秒处理时限 | 飞书 WebSocket 要求 3 秒内处理完 | "正在思考…" 机制 + asyncio.create_task 异步化 |
| 长连接稳定性 | 网络波动可能断连 | 实现自动重连（SDK 通常内置） |
| 与现有 Webhook 模式兼容 | 不能破坏已有用户的配置 | connection_mode 配置项，默认 websocket，可切回 webhook |

---

## 四、XClaw 现有飞书适配器分析

### 4.1 当前实现概况

XClaw 的飞书适配器位于 xclaw/channels/feishu.py，继承自 ChannelAdapter 抽象基类。

ChannelAdapter 抽象基类定义了三个必须实现的方法：start()（启动监听）、send_response(chat_id, text)（发送文本回复）、stop()（优雅停止）。

### 4.2 构造与初始化

构造函数接收五个参数：app_id、app_secret、verification_token、encrypt_key（可选）、message_handler（可选回调）。内部使用 httpx.AsyncClient（15 秒超时）作为 HTTP 客户端，缓存 tenant_access_token 用于消息发送。

### 4.3 Token 管理

使用飞书的 tenant_access_token/internal API 获取 token，POST 请求携带 app_id 和 app_secret。Token 采用懒加载策略：首次发送消息时刷新。遇到 401 响应时自动刷新并重试一次。没有主动刷新机制（不会在 token 到期前提前续期）。

### 4.4 消息发送

仅支持纯文本消息（msg_type 为 text）。消息内容是 JSON 格式的 text 字段。使用 Bearer Token 认证，receive_id_type 固定为 chat_id。遇到 401 会刷新 token 并重试一次。

### 4.5 签名验证

签名验证方法 verify_signature 已经实现：将 timestamp、nonce、verification_token 和 body 拼接后计算 SHA-256 哈希，使用 hmac.compare_digest 做常量时间比较防止时序攻击。但该方法定义后从未被调用——Webhook 路由直接解析 JSON 并调用 handle_event，未做签名校验。

### 4.6 事件处理

handle_event 方法处理两类事件：url_verification 挑战（回显 challenge 值以完成 URL 验证）和 im.message.receive_v1 消息接收。

消息接收流程为：提取 chat_id（优先用 message.chat_id，降级为 sender.sender_id.open_id，最终为 "unknown"）→ 解析 JSON 格式的 content 字段取出 text → 调用 message_handler 回调 → 发送回复。异常被捕获并记录日志，始终返回 200 OK 响应。

### 4.7 Webhook 路由

FastAPI 在 /webhook/feishu 注册 POST 路由，收到请求后解析 JSON、调用适配器的 handle_event、返回 JSON 响应。未在路由层做签名验证。

### 4.8 生命周期

start() 为空操作（仅打印日志），因为 webhook 路由由 web 模块注册。stop() 关闭 httpx 客户端。

### 4.9 现有实现的局限性

- 无 Webhook 签名验证（verify_signature 方法存在但未调用）
- encrypt_key 存储但未使用（无法解密加密 payload）
- 仅支持纯文本（无富文本、卡片消息、图片等）
- 无消息去重（重复 Webhook 会触发多次处理）
- 无群聊过滤（所有消息均处理）
- 无"正在思考…"等用户体验优化
- chat_id 降级到 "unknown" 可能导致消息路由失败
- 单次重试（401 仅重试一次，无指数退避）
- 全局 15 秒超时（对慢 LLM 响应可能不足）
- 无消息编辑和删除能力

---

## 五、总结

飞书 OpenClaw 插件方案的最大创新是使用 WebSocket 长连接替代 Webhook，极大降低了部署门槛。结合消息去重、"正在思考…"占位、群聊智能过滤、媒体支持等特性，形成了一个生产级的飞书 AI 机器人方案。

XClaw 可以分阶段参考该方案进行改进。Phase 1（WebSocket 长连接 + 去重）价值最高，能直接解决用户最大的部署痛点。Phase 2（思考占位 + 群聊过滤）提升体验，Phase 3/4（媒体支持 + 多账号）按需推进。

Python 端可以使用飞书官方 lark-oapi SDK 或者直接用 websockets 库实现长连接，两种路径各有取舍，需要根据 SDK 成熟度和 XClaw 的依赖策略做选择。
