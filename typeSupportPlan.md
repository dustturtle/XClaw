# 微信 Bot Typing Support Plan

## Summary

目标是在用户给微信 bot 发送文本消息后、正式回复返回前，通过 iLink 的 `getconfig -> sendtyping` 两步接口，让微信聊天顶部出现“对方正在输入...”。

这次实现只覆盖现有“微信入站文本消息 -> broker task agent -> 微信文本回复”链路，不改绑定页，不新增对外 REST API，不扩展外部 outbound 主动推送场景。

## Key Changes

- 在 `wechat_channel/models.py` 增加 `IlinkTypingConfigResponse`，用于解析 `getconfig` 返回的 `typing_ticket`。
- 在 `wechat_channel/ilink.py` 为 `IlinkClient` / `HttpIlinkClient` 增加 `show_typing_indicator(...)` 能力。
- `show_typing_indicator(...)` 内部顺序执行：
  - `POST /ilink/bot/getconfig`
  - 若 `typing_ticket` 非空，再 `POST /ilink/bot/sendtyping`
- typing 请求使用与现有业务 API 相同的认证头，并固定 `base_info.channel_version = "0.2.0"`。
- typing 请求超时固定 5 秒，任一步失败都静默吞掉，不能阻塞正式回复。
- 在 `wechat_channel/manager.py` 的入站文本消息异步处理链路中，拿到 `principal` 后、发起 broker 请求前，fire-and-forget 调用 `show_typing_indicator(...)`。
- typing 只使用当前入站消息自带的 `context_token`，不回退到 runtime 中缓存的旧 token。

## Test Plan

- 校验 `show_typing_indicator(...)` 成功时会先取 ticket，再发 typing。
- 校验 `typing_ticket` 为空时不会调用 `sendtyping`。
- 校验 typing 两步任一步失败都不会向上抛异常。
- 校验文本消息且带当前 `context_token` 时会触发 typing。
- 校验缺少当前消息 `context_token` 时，即使 runtime 里有旧 token，也不会触发 typing，但正式回复仍可继续发送。

## Assumptions

- 以文档第 10 节为准，typing indicator 是真实微信会话顶部状态，不是自定义网页提示文案。
- 不引入新的数据库字段，不对 `outbound` 主动推送补做 typing 能力。
