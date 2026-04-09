# 微信“输入中”状态实现说明

## 1. 目标

当用户在微信里发来文本消息后，如果原系统处理耗时较长，Channel 会尝试在微信侧展示“输入中”状态，减少用户等待时的空白感。

当前实现特点：

- 不阻塞主轮询链路
- broker 很快返回时不展示“输入中”
- 长任务会按固定间隔续发 typing，尽量避免中间空窗
- “输入中”接口调用失败不影响最终文本回复
- broker 成功或失败后，都会尝试清理“输入中”状态

---

## 2. 触发条件

当前只有满足以下条件时，才会尝试展示“输入中”：

1. 收到的是私聊文本消息
2. `broker_client` 已配置
3. 当前 active credential 能反查到 principal
4. 当前这条入站消息本身带有 `message.context_token`

注意：

- 这里依赖的是 `message.context_token`
- 不是 `runtime_state` 中历史保存的 `context_token`

所以存在一种情况：

- 这条消息可以正常回复
- 但由于消息本身没有 `context_token`
- 这次不会显示“输入中”

---

## 3. 核心调用位置

### 3.1 业务入口

入口在：

- [wechat_channel/manager.py](/Users/guanzhenwei/Desktop/gitProjects/task_wechat_channel/wechat_channel/manager.py)

核心逻辑在：

- [wechat_channel/manager.py](/Users/guanzhenwei/Desktop/gitProjects/task_wechat_channel/wechat_channel/manager.py#L505)

### 3.2 iLink 接口封装

接口封装在：

- [wechat_channel/ilink.py](/Users/guanzhenwei/Desktop/gitProjects/task_wechat_channel/wechat_channel/ilink.py)

核心方法：

- `show_typing_indicator(...)`
- `clear_typing_indicator(...)`
- `_get_typing_ticket(...)`
- `_send_typing(...)`
- `_clear_typing(...)`

---

## 4. 接口设计

当前“输入中”状态由两步完成：

1. 先拿 `typing_ticket`
2. 再发送 typing 状态

清理时再调用一次 typing 接口，状态设为 clear。

### 4.1 获取 typing_ticket

接口：

`POST {base_url}/ilink/bot/getconfig`

代码位置：

- [wechat_channel/ilink.py](/Users/guanzhenwei/Desktop/gitProjects/task_wechat_channel/wechat_channel/ilink.py#L373)

请求体：

```json
{
  "to_user_id": "<ilink_user_id>",
  "ilink_user_id": "<ilink_user_id>",
  "context_token": "<message.context_token>",
  "base_info": {
    "channel_version": "1.0.0"
  }
}
```

说明：

- 同时发送 `to_user_id` 和 `ilink_user_id`
- 是为了兼容不同 iLink 环境
- 成功后从响应里提取 `typing_ticket`

返回模型：

- [wechat_channel/models.py](/Users/guanzhenwei/Desktop/gitProjects/task_wechat_channel/wechat_channel/models.py#L75)

### 4.2 发送“输入中”

接口：

`POST {base_url}/ilink/bot/sendtyping`

代码位置：

- [wechat_channel/ilink.py](/Users/guanzhenwei/Desktop/gitProjects/task_wechat_channel/wechat_channel/ilink.py#L394)

请求体：

```json
{
  "to_user_id": "<ilink_user_id>",
  "ilink_user_id": "<ilink_user_id>",
  "context_token": "<message.context_token>",
  "typing_ticket": "<typing_ticket>",
  "base_info": {
    "channel_version": "1.0.0"
  }
}
```

### 4.3 清除“输入中”

接口：

`POST {base_url}/ilink/bot/sendtyping`

代码位置：

- [wechat_channel/ilink.py](/Users/guanzhenwei/Desktop/gitProjects/task_wechat_channel/wechat_channel/ilink.py#L414)

请求体：

```json
{
  "to_user_id": "<ilink_user_id>",
  "ilink_user_id": "<ilink_user_id>",
  "typing_ticket": "<typing_ticket>",
  "status": 2,
  "base_info": {
    "channel_version": "1.0.0"
  }
}
```

其中：

- `status = 2` 表示清除 typing 状态

---

## 5. 调用逻辑

### 5.1 延迟触发

当前不会在收到消息后立即打“输入中”，而是先延迟 0.5 秒。

常量定义：

- [wechat_channel/manager.py](/Users/guanzhenwei/Desktop/gitProjects/task_wechat_channel/wechat_channel/manager.py#L28)

```python
TYPING_TRIGGER_DELAY_SECONDS = 0.5
```

逻辑：

1. 收到可处理的文本消息
2. 创建一个后台任务 `_delayed_typing_indicator()`
3. 先 `sleep(0.5)`
4. 如果 broker 在 0.5 秒内已经返回，则跳过 typing
5. 如果 broker 超过 0.5 秒仍未返回，则开始调用 iLink 显示“输入中”

这样做的目的是：

- 避免快速请求也闪一下“输入中”
- 减少无意义的 iLink 调用

### 5.2 typing 状态机

当前内部有一个简单的状态控制：

- `idle`
- `scheduled`
- `sending`
- `done`
- `skipped`

作用：

- 区分 typing 任务是否还没开始
- 是否已经进入发送阶段
- broker 返回时该取消任务还是等待它跑完

### 5.3 broker 成功时的收尾

成功路径逻辑：

1. broker 返回文本
2. 标记 `broker_done = True`
3. 调用 `_finish_typing_indicator(...)`
4. 如果 typing 任务还没真正开始，直接取消
5. 如果已经拿到 `typing_ticket`，则先正常发微信文本
6. 文本发成功后，再调用 `clear_typing_indicator(...)`

代码位置：

- [wechat_channel/manager.py](/Users/guanzhenwei/Desktop/gitProjects/task_wechat_channel/wechat_channel/manager.py#L551)

### 5.4 broker 失败时的收尾

失败路径逻辑：

1. broker 抛异常
2. 标记 `broker_done = True`
3. 调用 `_finish_typing_indicator(...)`
4. 给用户发失败兜底文案
5. 如果之前拿到了 `typing_ticket`，则继续清理 typing 状态

兜底文案：

```text
我刚才处理这条消息时遇到了一点问题，请稍后再试。
```

代码位置：

- [wechat_channel/manager.py](/Users/guanzhenwei/Desktop/gitProjects/task_wechat_channel/wechat_channel/manager.py#L603)

---

## 6. 完整时序

```text
用户发送文本消息
  ↓
Channel 长轮询收到消息
  ↓
消息去重、更新 context_token、校验 credential / principal
  ↓
异步启动 broker 请求任务
  ↓
如果 message.context_token 存在
  → 并行启动 delayed typing task
  → 先 sleep 0.5 秒
  ↓
如果 broker 在 0.5 秒内返回
  → 取消 typing task
  → 不显示“输入中”
  → 直接回最终文本

如果 broker 超过 0.5 秒仍未返回
  → POST /bot/getconfig 获取 typing_ticket
  → POST /bot/sendtyping 显示“输入中”
  ↓
broker 返回成功
  → POST /bot/sendmessage 发送正式回复
  → POST /bot/sendtyping(status=2) 清除“输入中”

broker 返回失败
  → POST /bot/sendmessage 发送失败兜底文案
  → POST /bot/sendtyping(status=2) 清除“输入中”
```

---

## 7. 当前实现的边界与特点

### 7.1 “输入中”失败不影响主链路

无论是获取 `typing_ticket` 失败，还是发送 / 清理 typing 失败，都不会中断主消息流程。

当前策略是：

- 捕获异常
- 写 debug 日志
- 主流程继续发送正式回复或失败兜底文案

对应代码：

- [wechat_channel/ilink.py](/Users/guanzhenwei/Desktop/gitProjects/task_wechat_channel/wechat_channel/ilink.py#L230)
- [wechat_channel/ilink.py](/Users/guanzhenwei/Desktop/gitProjects/task_wechat_channel/wechat_channel/ilink.py#L260)

### 7.2 超时时间

typing 相关接口统一使用：

- `TYPING_TIMEOUT_SECONDS = 5.0`

定义位置：

- [wechat_channel/ilink.py](/Users/guanzhenwei/Desktop/gitProjects/task_wechat_channel/wechat_channel/ilink.py#L27)

### 7.3 base_url 兼容

所有 typing 接口都会先经过 `_resolve_ilink_api_root(base_url)` 归一化，因此：

- `http://host`
- `http://host/ilink`

这两种配置都能正确落到：

- `http://host/ilink/bot/...`

### 7.4 当前仍未实现的能力

当前“输入中”只支持：

- 单次展示
- 在正式文本回包后清理

尚未实现：

- 周期性续期
- broker 中间态阶段性 typing 管理
- 长任务分阶段不同状态展示
- typing 与流式回复混合协同

---

## 8. 一句话总结

当前“输入中”实现本质上是：

**对文本消息的 broker 异步处理，额外加了一层 0.5 秒延迟触发的 iLink typing 包装。**

如果 broker 很快返回，就完全不展示；
如果 broker 处理稍慢，就先显示“输入中”，等最终文本发出后再清掉。
