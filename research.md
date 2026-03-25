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

---
---

# 微信个人号 iLink Bot API 研究及 XClaw 参考思路

## 一、背景：微信首次合法开放个人号 Bot API

2026 年，腾讯通过 OpenClaw（AI Gateway 框架）正式开放了微信个人账号的 Bot API。官方名称为**微信 ClawBot 插件功能**，底层协议名为 **iLink（智联）**，接入域名是 ilinkai.weixin.qq.com——腾讯的官方服务器。

**这是历史性的变化。** 在此之前，开发者想让程序控制微信，只有灰色地带的选择：

| 旧方案 | 典型实现 | 性质 |
|--------|---------|------|
| 逆向 iPad 协议 | WeChatPadPro、itchat | 灰色地带，违反协议，随时封号 |
| PC 客户端 Hook | 注入 DLL、内存读写 | 违法，高封号风险 |
| 企业微信 API | 官方开放，但只面向企业 | 合法，但不是"微信" |

现在，iLink Bot API 是腾讯的官方产品，有法律文件背书（《微信ClawBot功能使用条款》，签订地深圳市南山区，适用中国大陆法律）。

---

## 二、社区主要项目概览

围绕微信 iLink Bot API，目前社区有两个关键参考项目：

| 项目 | 方案类型 | 语言 | 核心特点 |
|------|---------|------|---------|
| Johnixr/claude-code-wechat-channel | Claude Code Channel 插件 | TypeScript（MCP Channel 协议） | 作为 Claude Code 的 Channel 运行，通过 MCP 协议桥接微信消息 |
| hao-ji-xing/openclaw-weixin | 独立桥接器 + 协议文档 | JavaScript（单文件） | 裸调 iLink API 的最简实现，附带完整协议分析文档 |

此外，腾讯官方在 npm 上发布了两个包（scope 为 @tencent-weixin）：

| npm 包 | 版本 | 功能 |
|--------|------|------|
| @tencent-weixin/openclaw-weixin-cli | v1.0.2 | CLI 安装工具，引导扫码登录 |
| @tencent-weixin/openclaw-weixin | v1.0.2 | 完整的 iLink Bot 协议实现，41 个 TypeScript 源文件 |

---

## 三、iLink Bot API 协议详解

### 3.1 协议概述

iLink Bot API 是标准的 HTTP/JSON 协议，所有接口在 https://ilinkai.weixin.qq.com 下，无需 SDK 即可直接调用。CDN 域名为 https://novac2c.cdn.weixin.qq.com/c2c。

### 3.2 完整 API 列表

| Endpoint | Method | 功能 |
|----------|--------|------|
| /ilink/bot/get_bot_qrcode | GET | 获取登录二维码（参数 bot_type=3） |
| /ilink/bot/get_qrcode_status | GET | 轮询扫码状态（参数 qrcode=xxx） |
| /ilink/bot/getupdates | POST | 长轮询收消息（核心接口） |
| /ilink/bot/sendmessage | POST | 发送消息（文字/图片/文件/视频/语音） |
| /ilink/bot/getuploadurl | POST | 获取 CDN 预签名上传地址 |
| /ilink/bot/getconfig | POST | 获取 typing_ticket |
| /ilink/bot/sendtyping | POST | 发送"正在输入"状态 |

### 3.3 鉴权流程

鉴权采用 QR 码扫码登录方式：

1. 开发者调用 GET get_bot_qrcode 获取二维码 URL
2. 用户用微信扫码并在手机端确认
3. 开发者轮询 GET get_qrcode_status（长轮询，最多 35 秒 hold）
4. 确认后服务器返回 bot_token、baseurl、ilink_bot_id 等信息
5. 持久化 bot_token，后续所有请求使用 Bearer Token 认证

**请求头固定格式：**

- Content-Type: application/json
- AuthorizationType: ilink_bot_token
- X-WECHAT-UIN: 随机 uint32 转十进制字符串再 base64 编码（每次请求变化，起防重放作用）
- Authorization: Bearer {bot_token}

### 3.4 消息收取：长轮询机制

与 Telegram Bot API 的 getUpdates 设计一致：

- POST /ilink/bot/getupdates，携带 get_updates_buf（游标，首次为空字符串）和 base_info.channel_version
- 服务器 hold 住连接最多 35 秒，有新消息才返回
- 响应包含 msgs 数组、新的 get_updates_buf 游标、longpolling_timeout_ms 等字段
- **get_updates_buf 是关键**，类似数据库 cursor，必须每次更新，否则会重复收到消息

### 3.5 消息结构

每条消息（WeixinMessage）的核心字段包括：

- from_user_id：发送者 ID，格式为 xxx@im.wechat
- to_user_id：接收者 ID，格式为 xxx@im.bot
- message_type：1 表示用户消息，2 表示 Bot 消息
- message_state：2 表示完整消息（FINISH）
- context_token：对话关联 token（回复时必须带上）
- group_id：群聊 ID（群消息时存在）
- item_list：消息内容数组

**消息内容类型（item_list 中的 type）：**

| type 值 | 含义 |
|---------|------|
| 1 | 文本（text_item.text） |
| 2 | 图片（CDN 加密存储，image_item） |
| 3 | 语音（silk 编码，附带语音转文字，voice_item） |
| 4 | 文件附件（file_item） |
| 5 | 视频（video_item） |

### 3.6 context_token：对话关联的核心

**这是整个协议里最关键也最容易踩坑的细节。**

每条收到的消息都带有 context_token，回复时**必须原样带上这个 token**，否则消息不会关联到正确的对话窗口。

发送消息时，POST /ilink/bot/sendmessage 的 body 中需要包含：to_user_id、message_type（2，BOT）、message_state（2，FINISH）、context_token（从收到的消息中取）、item_list（回复内容）。

对于群聊场景，context_token 需要按 group_id 缓存；对于私聊场景，按 sender_id 缓存。Johnixr/claude-code-wechat-channel 项目将 context_token 持久化到磁盘文件（context_tokens.json），使其在 Claude Code 会话重启后仍然有效。

### 3.7 媒体文件处理

微信 CDN 上的所有媒体文件经过 **AES-128-ECB** 加密：

**发送图片的完整流程：**
1. 生成随机 AES-128 key（16 字节）
2. 用 AES-128-ECB 加密文件
3. 调用 getuploadurl 获取预签名 URL（需要 media_id）
4. PUT 加密文件到 CDN
5. 在 sendmessage 中带上 aes_key（base64 编码）和 CDN 引用参数

**接收图片的流程：**
1. 从消息 item_list 中取出 image_item 的 CDN URL 和 aes_key
2. 下载加密文件
3. 用 AES-128-ECB 解密

### 3.8 "正在输入" 状态指示

iLink API 支持发送 typing 状态：

1. 先调用 getconfig 获取 typing_ticket
2. 再调用 sendtyping 发送"正在输入"状态
3. 该状态在微信端显示为"对方正在输入…"

---

## 四、两个社区项目的核心架构分析

### 4.1 Johnixr/claude-code-wechat-channel（Claude Code Channel 插件）

#### 项目定位

这是一个**基于 MCP Channel 协议**的 Claude Code 插件，将微信消息桥接到 Claude Code 会话中。它不是一个通用的微信机器人框架，而是专门为 Claude Code 设计的通道。

#### 架构

消息流向为：微信 iOS → WeChat ClawBot → iLink API → 本插件（长轮询） → MCP Channel 协议 → Claude Code Session。Claude Code 通过 MCP Tool（wechat_reply / wechat_send_image）发送回复。

#### 关键文件

- **wechat-channel.ts**（约 970 行）— 全部核心逻辑，包括 iLink API 封装、MCP Server 定义、长轮询循环、工具处理
- **setup.ts** — 独立的扫码登录工具
- **cli.mjs** — CLI 入口，支持 setup / install / start / help 命令

#### 关键技术点

**① MCP Channel 协议集成**

使用 @modelcontextprotocol/sdk 创建 MCP Server，声明 experimental claude/channel 能力。收到微信消息后，通过 mcp.notification 方法以 notifications/claude/channel 事件推送给 Claude Code。消息以 XML-like 的 channel 标签格式传递，包含 sender、sender_id、msg_type、can_reply、is_group 等元数据。

**② 双向工具**

暴露两个 MCP Tool 给 Claude Code：

- wechat_reply：发送纯文本回复，参数为 sender_id 和 text
- wechat_send_image：发送本地图片，参数为 sender_id 和 file_path（绝对路径）

Claude Code 在处理完消息后调用这些工具来回复微信用户。

**③ context_token 磁盘持久化**

context_token 缓存不仅保存在内存 Map 中，还同步写入 ~/.claude/channels/wechat/context_tokens.json。这样即使 Claude Code 会话重启，仍能回复之前的对话。

**④ 长轮询同步游标持久化**

get_updates_buf（轮询游标）也持久化到 sync_buf.txt，避免重启后重复收到旧消息。

**⑤ Typing 指示器**

收到消息后，在等待 Claude Code 处理期间自动发送"正在输入"状态，需要先获取 typing_ticket 再发送 sendtyping。

**⑥ 错误恢复**

连续失败计数 + 退避机制：连续失败 3 次后等待 30 秒，普通重试间隔 2 秒。

**⑦ 群聊支持**

原生支持群聊消息（通过 group_id 字段区分），群消息回复时 sender_id 使用 group_id。

**⑧ 媒体支持**

支持接收图片/语音/文件/视频消息（提取文本描述或转写），支持发送图片（AES-128-ECB 加密上传到 CDN）。

### 4.2 hao-ji-xing/openclaw-weixin（独立桥接器 + 协议文档）

#### 项目定位

这是一个**独立的微信-Claude 桥接器**，不依赖 Claude Code 的 Channel 协议，而是直接使用 @anthropic-ai/claude-agent-sdk 调用 Claude 的 Agent 能力。同时提供了详尽的 iLink Bot API 协议分析文档。

#### 架构

消息流向为：微信 → iLink API → 本桥接器（长轮询） → Claude Agent SDK → Claude API → 生成回复 → iLink API → 微信。

#### 关键文件

- **wechat-claude-bridge.mjs**（约 300 行）— 完整的桥接实现
- **weixin-bot-api.md** — 详尽的 iLink Bot API 协议分析文档
- **protocol.md** — 微信 ClawBot 功能使用条款原文

#### 关键技术点

**① 直接调用 Claude Agent SDK**

使用 @anthropic-ai/claude-agent-sdk 的 query 函数，传入用户消息，Claude 可以调用 Bash、文件读写、Web 搜索等内置工具。Agent 处理完成后提取最终结果文本，通过 iLink API 回复微信。

**② iTerm2 二维码渲染优化**

扫码登录时优先使用 iTerm2 的 imgcat 工具渲染高清二维码 PNG 图片，降级为 qrcode-terminal 的 ASCII art，再降级为纯 URL。

**③ 二维码过期自动刷新**

扫码超时后自动刷新二维码（最多 3 次），而非直接退出。

**④ 语音消息转文字**

语音消息（type=3）自带转写文本（voice_item.text），桥接器直接使用转写结果作为 Claude 的输入。

---

## 五、与 XClaw 现有微信公众号适配器的对比

XClaw 当前的微信适配器是 WeChatMPAdapter（微信公众号适配器），位于 xclaw/channels/wechat_mp.py。两者的本质差异在于：**公众号是企业服务号，iLink 是个人微信号**。

| 维度 | XClaw WeChatMPAdapter（公众号） | iLink Bot API（个人号） |
|------|-------------------------------|----------------------|
| 账号类型 | 微信服务号 / 认证订阅号 | 微信个人号（需 iOS 最新版） |
| 接入方式 | Webhook（HTTP POST 回调） | 长轮询（HTTP 长连接，类似 Telegram） |
| 公网需求 | 需要公网 IP / 域名 / HTTPS | 不需要，客户端主动拉取 |
| 鉴权方式 | app_id + app_secret + token | QR 码扫码 → bot_token |
| 消息格式 | XML（微信公众平台规范） | JSON（iLink 协议） |
| 回复机制 | 被动回复（5 秒内 XML 响应）+ 客服消息 API | 主动发送（sendmessage API） |
| 消息去重 | 有（基于 MsgId，500 条上限） | 无内置，需基于 get_updates_buf 游标 |
| 对话关联 | 基于 OpenID | 基于 context_token |
| 媒体支持 | 仅文本（当前实现） | 图片/语音/文件/视频（协议原生支持） |
| 群聊 | 不适用（公众号无群聊概念） | 原生支持（group_id 字段） |
| "正在输入" | 无（被动回复返回"消息已收到"文本） | 原生支持（sendtyping API） |
| 使用场景 | 品牌/企业的公众号 AI 助手 | 个人微信中的 AI 助手 |
| 合规性 | 标准微信开放平台能力 | 腾讯新开放的 ClawBot 功能，有独立使用条款 |

---

## 六、XClaw 接入 iLink Bot API 的实现思路

### 6.1 总体策略

推荐方案：在 XClaw 中新增一个 WeChatPersonalAdapter（微信个人号适配器），与现有的 WeChatMPAdapter（公众号适配器）并列。两者面向不同用户群体，互不冲突。

| 维度 | 实现目标 |
|------|---------|
| 连接模式 | 长轮询（无需公网 IP） |
| 鉴权 | QR 码扫码登录 → bot_token 持久化 |
| 消息收发 | 文本消息收发 + context_token 管理 |
| 对话关联 | context_token 内存缓存 + 磁盘持久化 |
| "正在输入" | typing 状态指示 |
| 媒体支持 | 可分阶段：Phase 1 纯文本，Phase 2 图片 |
| 群聊 | 原生支持 |

### 6.2 Python 端对应的技术选型

iLink Bot API 是标准的 HTTP/JSON 协议，不需要任何特定 SDK。XClaw 已有的 httpx 库完全可以满足需求：

| 功能 | 技术选择 | 说明 |
|------|---------|------|
| HTTP 请求 | httpx.AsyncClient（已有依赖） | 所有 iLink API 调用 |
| 长轮询 | httpx + 超时控制（35-38 秒 timeout） | getupdates 接口 |
| AES 加密 | pycryptodome 或 cryptography | 媒体文件 AES-128-ECB 加解密（Phase 2） |
| QR 码显示 | qrcode 库（终端渲染）或直接输出 URL | 扫码登录 |
| 持久化 | JSON 文件（与社区方案一致） | token、context_token、sync_buf |

### 6.3 具体实现流程

**Step 1：新建适配器类（xclaw/channels/wechat_personal.py）**

创建 WeChatPersonalAdapter 类，继承 ChannelAdapter 抽象基类，实现 start()、send_response()、stop() 三个必要方法。

与现有适配器最大的不同：start() 方法不再是空操作，而是启动一个长轮询异步任务，主动从 iLink 服务器拉取消息。

**Step 2：实现扫码登录流程**

- 调用 GET /ilink/bot/get_bot_qrcode 获取二维码
- 在终端或日志中输出二维码 URL（或 ASCII 渲染）
- 轮询 GET /ilink/bot/get_qrcode_status 等待用户扫码确认
- 获取 bot_token 后持久化到配置文件
- 后续启动时直接读取已保存的 token

**Step 3：实现长轮询主循环**

- 使用 asyncio.create_task 在 start() 中启动后台轮询任务
- 调用 POST /ilink/bot/getupdates，设置 38 秒超时（服务器最多 hold 35 秒）
- 维护 get_updates_buf 游标，每次更新并持久化
- 解析收到的消息，提取文本内容和 context_token
- 调用 message_handler 回调处理消息
- 实现错误恢复：连续失败计数 + 指数退避

**Step 4：实现消息发送**

- send_response 方法调用 POST /ilink/bot/sendmessage
- 必须带上正确的 context_token
- context_token 的管理策略：收到消息时缓存（按 sender_id 或 group_id），发送时查找

**Step 5：实现 context_token 管理**

这是整个实现中最关键的部分：

- 内存中使用字典缓存：key 为 sender_id（私聊）或 group_id（群聊），value 为 context_token
- 同步持久化到磁盘 JSON 文件，保证进程重启后仍能回复
- 每次收到新消息时更新缓存
- 发送消息前查找缓存，找不到则无法回复（需等待用户先发消息）

**Step 6：实现请求头构造**

每个请求需要固定的 Header 格式：

- Content-Type: application/json
- AuthorizationType: ilink_bot_token
- X-WECHAT-UIN: 每次请求随机生成（uint32 → 十进制字符串 → base64）
- Authorization: Bearer {bot_token}

**Step 7：实现 Typing 指示**

收到消息后异步发送"正在输入"状态：先调用 getconfig 获取 typing_ticket，再调用 sendtyping。

**Step 8：配置层扩展（config.py）**

新增配置项：token 存储路径（默认 ~/.xclaw/wechat/）、长轮询超时时间（默认 38 秒）、是否启用 typing 指示（默认开启）。

**Step 9：运行时注册（runtime.py）**

长轮询模式下，适配器的 start() 会主动建立连接并开始收消息，不需要注册任何 Webhook 路由。FastAPI 应用中无需为此适配器添加路由。

### 6.4 与现有 WeChatMP 适配器的关系

两个适配器完全独立，面向不同场景：

- WeChatMPAdapter：面向企业/品牌，需要微信服务号资质，Webhook 模式
- WeChatPersonalAdapter：面向个人用户，只需微信个人号 + iOS 最新版，长轮询模式

配置中通过不同的 channel 类型区分。用户根据自身需求选择使用哪一个。

### 6.5 实现优先级建议

| 阶段 | 内容 | 价值 |
|------|------|------|
| Phase 1 | 扫码登录 + 长轮询 + 文本消息收发 + context_token 管理 | 最高。实现基本可用的微信个人号 AI 助手 |
| Phase 2 | Typing 指示 + 错误恢复 + 游标持久化 | 高。提升稳定性和用户体验 |
| Phase 3 | 图片收发（AES-128-ECB 加解密 + CDN 上传下载） | 中。需要引入加密库 |
| Phase 4 | 语音转文字处理 + 文件/视频支持 | 低。语音已自带转写文本 |
| Phase 5 | 群聊支持 + 群聊智能过滤 | 低。按需推进 |

### 6.6 关键风险点

| 风险 | 说明 | 缓解措施 |
|------|------|---------|
| bot_token 过期 | 目前无文档说明 token 有效期 | 检测 session timeout 错误码（如 -14），自动提示重新扫码 |
| 服务稳定性 | 腾讯声明可随时终止服务（条款 7.2） | XClaw 保留公众号适配器作为备选方案 |
| iOS 限制 | 目前仅支持 iOS 最新版微信 | 关注腾讯后续是否开放 Android 支持 |
| 内容审核 | 腾讯有权拦截/阻断内容（条款 4.7） | 遵守使用条款，不发送违规内容 |
| 速率限制 | 官方未公开限速策略 | 实现自适应退避，实测摸索限制 |
| bot_type=3 含义 | 源码硬编码，可能对应特定账号类型 | 直接沿用，关注官方更新 |
| AES-128-ECB 安全性 | ECB 模式不安全（不推荐），但这是腾讯的协议要求 | 仅用于 CDN 媒体加密，遵循协议规范 |

---

## 七、官方使用条款要点

### 7.1 腾讯只是"管道"

条款明确（3.2）：腾讯仅提供微信 ClawBot 插件与第三方 AI 服务的信息收发，不存储输入内容与输出结果，不提供 AI 相关服务。AI 服务的责任由开发者自行承担。

### 7.2 腾讯保留控制权

条款（4.7）：腾讯有权决定可连接的第三方 AI 服务的类型、范围、信息收发规模或频率，有权对输入输出进行识别，并根据安全或风险情况采取风险提示、拦截、阻断等措施。

### 7.3 数据隐私

- 消息内容（文字/图片/语音/视频/文件）：转发给第三方 AI，不在腾讯服务器存储
- AI 返回的输出结果：转发给用户，不在腾讯服务器存储
- IP 地址、操作记录、设备信息：会被收集，用于安全审计

### 7.4 禁止行为

- 利用本功能绕过、破解微信软件的技术保护措施
- 违反国家法律法规
- 危害网络安全、数据安全及微信产品安全
- 侵犯他人合法权益

### 7.5 服务可终止

条款（7.2）：腾讯有权根据业务发展需要，自行决定变更、中断、中止或终止本功能服务。这意味着不应将核心业务完全依赖这套 API。

---

## 八、总结

微信 iLink Bot API 的开放是一个里程碑事件。对于 XClaw 来说，这意味着可以在合法合规的前提下接入微信个人号，与现有的公众号适配器形成互补。

两个社区项目提供了不同层次的参考价值：
- hao-ji-xing/openclaw-weixin 提供了最详尽的协议分析和最简实现范例，是理解 iLink API 的最佳入口
- Johnixr/claude-code-wechat-channel 展示了完整的 Channel 插件架构，其 context_token 持久化、typing 指示、错误恢复等工程实践值得参考

XClaw 的实现路径清晰：新建 WeChatPersonalAdapter，基于长轮询模式，复用现有的 httpx 依赖，Phase 1 实现文本消息收发即可达到基本可用状态。后续按需扩展媒体支持和群聊能力。

关键注意事项：context_token 的正确管理是整个实现的核心，必须确保每次回复时携带正确的 token；bot_token 的过期处理需要通过实测验证；腾讯可能随时调整服务范围，应保留降级方案。
