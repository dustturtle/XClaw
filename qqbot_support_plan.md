# QQBot 渠道支持实施计划

## 背景

目标是在 XClaw 中接入 QQBot 作为新渠道，参考腾讯官方实现 `openclaw-qqbot`，第一版覆盖：

- 多账号
- C2C 私聊
- QQ 群 @消息
- 输入指示器
- 语音输入
- 多媒体输出（图片、文件）
- 流式传输（C2C）

## 方案摘要

采用“多账号管理器 + API client + webhook 路由 + C2C 流式会话”的分层结构：

1. QQ 渠道统一由 `QQAdapter` 作为多账号入口管理。
2. 每个账号维护独立 token、gateway session、消息序列和回复上下文。
3. 私聊支持 typing 和 stream，群聊只做 @文本/语音收发，不做 typing / stream。
4. 语音输入优先使用平台自带转写文本；缺失时走可选 STT。
5. 图片 / 文件发送与现有微信出站能力保持一致，对接本地文件路径。

## 实施清单

- [x] 1. 写入计划文档并建立状态跟踪
- [x] 2. 为 QQ 多账号 / 私聊 / 群聊 / 语音 / 媒体 / stream 补失败测试
- [x] 3. 扩展配置模型，支持 `qq_accounts` 和旧字段兼容
- [x] 4. 重构 QQ 渠道为多账号 gateway 管理器，修正稳定 chat_id
- [x] 5. 实现 C2C 输入指示器与续期
- [x] 6. 实现 QQ 图片 / 文件发送（C2C + group）
- [x] 7. 实现 QQ 语音输入（平台转写优先，缺失时 STT）
- [x] 8. 实现 QQ C2C 流式输出与回退逻辑
- [x] 9. 完成 runtime / web 接线与回归测试
- [x] 10. 更新本文档状态与验证结果

## 关键默认值

- 多账号直接支持；运行时主动建立 gateway / websocket 连接。
- 旧 `qq_app_id / qq_app_secret` 自动兼容成一个默认账号。
- 群聊默认要求 @机器人 才处理。
- typing 和 streaming 仅在 C2C 私聊启用。
- 多媒体输出第一版只支持图片和文件，不做视频/TTS。
- 入站图片 / 文件暂不做解析，只保留后续扩展空间。

## 验证记录

- 2026-04-15：`python3 -m pytest -q tests/test_channels.py -k 'qq' tests/test_qqbot.py` -> `14 passed`
- 2026-04-15：`python3 -m pytest -q tests/test_channels.py tests/test_qqbot.py tests/test_config.py` -> `52 passed`
- 2026-04-15：`python3 -m pytest -q tests/test_agent_engine.py tests/test_investment_api.py tests/test_wechat_ilink.py tests/test_wechat_multitenant.py` -> `55 passed`
- 2026-04-15：`python3 -m pytest -q` -> `401 passed, 1 warning`
- 2026-04-15：切换到 QQ gateway / websocket 主路径后再次执行 `python3 -m pytest -q` -> `401 passed, 1 warning`
- 2026-04-15：使用真实 QQBot 账号 `1903076963` 完成 C2C 联调，确认私聊消息入站和流式输出正常。

## 实际落地结果

- 新增 QQ 多账号配置 `qq_accounts`，保留 `qq_app_id / qq_app_secret` 单账号兼容模式。
- 入站改为 gateway / websocket 主路径，不依赖 webhook。
- 私聊与群聊使用稳定 chat_id：
  - `qq:{account_key}:c2c:{user_openid}`
  - `qq:{account_key}:group:{group_openid}`
- C2C 私聊支持输入指示器续期。
- 语音输入支持：
  - 平台 `asr_refer_text`
  - 缺失时使用 `voice_wav_url/url` + STT fallback
- 出站媒体支持：
  - C2C 图片 / 文件
  - Group 图片 / 文件
- 群聊默认要求 @机器人 才处理。
- C2C 支持 QQ `stream_messages`。

## 已知边界

- 当前流式实现是“**最终回复文本的 transport-level chunk streaming**”：
  - `agent_loop` 仍先完成工具调用和最终回复生成
  - 然后通过 QQ `stream_messages` 分段推送给用户
  - 因此当前能提供流式展示，但**不会缩短工具执行阶段的等待时间**
- 入站图片 / 文件暂未接入理解链路，只保留了未来扩展空间。
- guild/channel、视频输出、TTS 输出未纳入本次实现。

## 当前状态

- 2026-04-15：QQBot 新渠道已完成实现、gateway 方向真实联调、测试和文档回填。
