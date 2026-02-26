# 微信公众号 / 小程序适配 设计方案

> **所属阶段**：Phase 5（可选进阶）  
> **状态**：设计稿 / 待实现  
> **关联文件**：`xclaw/channels/wechat_mp.py`、`xclaw/channels/web.py`、`xclaw/config.py`

---

## 1. 背景与边界

### 为什么不是 Phase 1-4？

| 渠道 | 需要条件 | 适合阶段 |
|------|---------|---------|
| 飞书 / 企业微信 / 钉钉 | 企业账号，内网部署即可 | Phase 3 ✅ |
| **微信公众号** | 需**已认证**服务号（个人号不支持客服消息 API）+ 公网域名 | Phase 5 |
| **微信小程序** | 需注册小程序 + 开发前端（WXML/WXSS）+ 公网域名 | Phase 5 |

公众号和小程序的接入门槛更高（需要工信部认证、公网服务器、HTTPS 域名备案），因此列为可选进阶。

---

## 2. 微信公众号（Official Account）适配

### 2.1 接入原理

```
用户在微信里发消息给公众号
        │
        ▼
  微信服务器 (POST XML)
        │
        ▼
XClaw POST /webhook/wechat_mp
        │
        ├─ 立即 ACK（被动回复 XML "消息已收到，处理中..."）
        │
        ▼
  agent_loop 异步处理
        │
        ▼
  微信客服消息 API（主动推送结果给用户）
```

### 2.2 两种回复模式对比

| 模式 | 说明 | 限制 | XClaw 采用 |
|------|------|------|---------|
| **被动回复**（Passive Reply） | 在 POST 响应 Body 中直接返回 XML | 必须 5 秒内返回；只能回复一条；不需要 API 权限 | 仅用于立即 ACK |
| **客服消息**（Customer Service Message） | 调用微信 API 异步推送 | 需要已认证服务号；有客服消息权限 | 用于返回 AI 结果 |

### 2.3 消息流程（时序）

```
用户     微信服务器       XClaw（POST /webhook/wechat_mp）     agent_loop
 │           │                      │                              │
 │─发消息──▶│                      │                              │
 │           │─POST XML────────────▶│                              │
 │           │                      │─验签                          │
 │           │                      │─立即被动回复 "处理中..."─────▶│
 │           │◀─XML ACK────────────│                              │
 │           │                      │─asyncio.create_task──────────▶│
 │           │                      │                              │─LLM + 工具
 │           │                      │                              │─生成回复
 │           │                      │◀─────────────────────────────│
 │           │◀─客服消息 API────────│                              │
 │◀─收到消息─│                      │                              │
```

### 2.4 URL 验证（服务器配置时）

```
GET /webhook/wechat_mp?signature=xxx&timestamp=yyy&nonce=zzz&echostr=abc
```
验证通过后，原样返回 `echostr` 字符串。

**签名算法**：
```python
import hashlib
items = sorted([token, timestamp, nonce])
sha1 = hashlib.sha1("".join(items).encode()).hexdigest()
assert sha1 == signature
```

### 2.5 消息 XML 格式

**接收**（微信推来的文本消息）：
```xml
<xml>
  <ToUserName><![CDATA[gh_xxx]]></ToUserName>
  <FromUserName><![CDATA[openid_of_user]]></FromUserName>
  <CreateTime>1409735669</CreateTime>
  <MsgType><![CDATA[text]]></MsgType>
  <Content><![CDATA[你好]]></Content>
  <MsgId>1234567890123456</MsgId>
</xml>
```

**被动回复**（立即 ACK）：
```xml
<xml>
  <ToUserName><![CDATA[openid_of_user]]></ToUserName>
  <FromUserName><![CDATA[gh_xxx]]></FromUserName>
  <CreateTime>1409735669</CreateTime>
  <MsgType><![CDATA[text]]></MsgType>
  <Content><![CDATA[消息已收到，AI 正在处理中，请稍候...]]></Content>
</xml>
```

### 2.6 客服消息 API（主动推送）

```http
POST https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token=TOKEN
Content-Type: application/json

{
  "touser": "OPENID",
  "msgtype": "text",
  "text": {"content": "AI 回复内容"}
}
```

获取 access_token：
```http
GET https://api.weixin.qq.com/cgi-bin/token
    ?grant_type=client_credential
    &appid=APPID
    &secret=APPSECRET
```

### 2.7 配置项

```yaml
# 微信公众号（服务号）
wechat_mp_enabled: false
wechat_mp_app_id: ""           # 公众号 AppID
wechat_mp_app_secret: ""       # 公众号 AppSecret
wechat_mp_token: ""            # 服务器配置中的 Token（用于签名验证）
wechat_mp_encoding_aes_key: "" # 消息加解密 Key（可选，启用加密模式时填写）
```

### 2.8 注意事项

- 微信公众号 **只支持 HTTPS**，需要有备案的公网域名和 SSL 证书
- 个人订阅号**不支持**客服消息 API，必须使用已认证的**服务号**
- 消息不重放：微信会在 5 秒内多次重试，需用 `MsgId` 去重
- 图片/语音等非文字消息目前仅回复提示，不做 AI 处理（可扩展）

---

## 3. 微信小程序（Mini Program）后端支持

### 3.1 设计思路

微信小程序的**前端**（WXML/JS）由用户/团队自行开发，XClaw 只负责**后端 API**。小程序通过 HTTP 调用 XClaw 的 FastAPI 接口。

```
微信小程序（前端）          XClaw 后端
      │                        │
      │─wx.login()─▶ 微信服务器─▶ code
      │                        │
      │─POST /api/wxmp/login───▶│
      │   body: {code: "xxx"}  │─code2session API─▶ 微信服务器
      │                        │◀─ openid + session_key
      │                        │─创建/查找 chat 记录
      │◀── {session_token} ────│
      │                        │
      │─POST /api/chat ────────▶│（携带 session_token 作为 chat_id）
      │   message: "查大盘"    │─agent_loop
      │◀── reply ──────────────│
```

### 3.2 登录端点

```
POST /api/wxmp/login
Body: {"code": "wx_login_code"}
Response: {"chat_id": "wxmp_openid_xxx", "session_key": "..."}
```

**流程**：
1. 小程序调用 `wx.login()` 获取 `code`（有效期 5 分钟）
2. 发送 `code` 给 XClaw 的 `/api/wxmp/login`
3. XClaw 向微信调用 `code2session` API 换取 `openid`
4. 用 `openid` 作为 `external_chat_id` 在数据库 `chats` 表创建记录（channel="wechat_mp"）
5. 返回 `chat_id`（前端存储，后续对话使用）

**code2session API**：
```http
GET https://api.weixin.qq.com/sns/jscode2session
    ?appid=APPID
    &secret=APPSECRET
    &js_code=CODE
    &grant_type=authorization_code
```

### 3.3 聊天端点（复用现有接口）

登录后，小程序直接使用现有的 `/api/chat` 或 `/api/chat/stream` 端点：

```json
POST /api/chat
{
  "chat_id": "wxmp_openid_xxx",
  "message": "查一下贵州茅台最新价格"
}
```

**无需改动** `agent_loop`，只需在 `chat_id` 前加 `wxmp_` 前缀区分渠道。

### 3.4 CORS 配置更新

微信小程序要求后端在 **微信公众平台 → 开发 → 开发设置 → 服务器域名** 中填写 XClaw 的域名。同时 FastAPI 的 CORS 设置需要允许小程序访问（微信小程序不走浏览器 CORS，但出于安全考虑仍需配置）。

### 3.5 配置项

```yaml
# 微信小程序
wechat_mp_app_id: ""       # 与公众号共用，或填写小程序独立 AppID
wechat_mp_app_secret: ""   # 与公众号共用，或填写小程序 AppSecret
```

### 3.6 前端示例代码（参考）

```javascript
// 小程序端 app.js（示例）
wx.login({
  success(res) {
    wx.request({
      url: 'https://your-xclaw-host/api/wxmp/login',
      method: 'POST',
      data: { code: res.code },
      success(r) {
        wx.setStorageSync('xclaw_chat_id', r.data.chat_id);
      }
    });
  }
});

// 发送消息
function sendMessage(text) {
  const chatId = wx.getStorageSync('xclaw_chat_id');
  wx.request({
    url: 'https://your-xclaw-host/api/chat',
    method: 'POST',
    data: { chat_id: chatId, message: text },
    success(r) {
      console.log('AI 回复:', r.data.reply);
    }
  });
}
```

---

## 4. 实现计划（Phase 5 拆解）

### 4.1 公众号适配子任务

| # | 任务 | 文件 | 说明 |
|---|------|------|------|
| 1 | `WeChatMPAdapter` 类 | `xclaw/channels/wechat_mp.py` | 签名验证、XML 解析、被动回复、客服 API |
| 2 | 配置字段 | `xclaw/config.py` | `wechat_mp_*` 字段 |
| 3 | webhook 路由 | `xclaw/channels/web.py` | `GET/POST /webhook/wechat_mp` |
| 4 | 运行时接入 | `xclaw/runtime.py` | 创建适配器实例 |
| 5 | 配置文件示例 | `xclaw.config.example.yaml` | 填写示例 |
| 6 | 自动化测试 | `tests/test_wechat.py` | 签名验证、XML 解析、API mock |

### 4.2 小程序后端子任务

| # | 任务 | 文件 | 说明 |
|---|------|------|------|
| 1 | 登录端点 | `xclaw/channels/web.py` | `POST /api/wxmp/login` |
| 2 | code2session 调用 | `xclaw/channels/wechat_mp.py` | 通过 httpx 调用微信 API |
| 3 | openid → chat_id 映射 | `xclaw/db.py` | 使用现有 `get_or_create_chat("wechat_mp", openid)` |
| 4 | 测试 | `tests/test_wechat.py` | mock code2session 响应 |

---

## 5. 技术限制与替代方案

| 限制 | 说明 | 替代方案 |
|------|------|---------|
| 需要 HTTPS 域名 | 微信不接受 HTTP 或 IP | 使用 Nginx + Let's Encrypt；或 Cloudflare Tunnel |
| 需要服务号认证 | 个人公众号功能受限 | 改用小程序（门槛更低） |
| 5 秒回复超时 | AI 处理时间可能超过 5 秒 | 被动 ACK + 异步客服消息（本方案已采用） |
| 小程序需前端开发 | XClaw 只提供后端 | 参考本文档的前端示例代码 |
| 消息去重 | 微信会重试推送 | 用 MsgId 去重（适配器内实现） |

---

## 6. 与现有架构的兼容性

公众号和小程序适配**完全遵循**现有 `ChannelAdapter` 抽象基类，可以直接插入 `runtime.py` 的适配器列表。所有消息最终都经过统一的 `agent_loop` 处理，与其他渠道无任何区别。

```
WeChatMPAdapter
    │
    ├─ handle_event(xml_body)   ← 公众号消息
    │       └─ 调用 message_handler(openid, text)
    │
    ├─ handle_wxmp_login(code)  ← 小程序登录
    │       └─ 调用 code2session → 返回 chat_id
    │
    └─ send_response(openid, text) ← 客服消息 API
```
