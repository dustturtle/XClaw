# GLM-5.3-Flash 功能与图片验收

日期：2026-09-05，Asia/Shanghai。

## 结论

已将本地模型配置切换为方舟 `glm-5-3-flash-260826`，并通过 XClaw + PowerCore 完整功能验收。
不是只测模型 API：图片从聊天 API/页面进入 PowerCore，普通聊天、业务工具、子 Agent、定时任务
也均使用新模型。自动验收首次完整执行通过，14 个独立 Run 全部完成，没有工具错误或失败终态。

另外通过实际浏览器文件选择、预览、发送和回复核对完成图片页面验收。
这只证明本轮有限用例通过，不是长期稳定性、金融结论准确性或所有多模态能力的保证。

## 配置与范围

- 模型：`glm-5-3-flash-260826`，沿用原方舟地址和已有 Key。
- 参数保持 `temperature=0.1`、`max_tokens=8192`、`thinking=true`、`timeout=240.0`；没有同时调参以混淆模型替换效果。
- 只修改本地忽略的 `xclaw.config.yaml` 中的模型行，没有重写或打印 Key。
- 测试目录为 `xclaw.data/powercore-glm53flash`；上一轮 GLM-4.7 的 `powercore-demo` 数据和报告保留。
- XClaw 基线为 `d3d97aab8316725409866e8f72cfd7ebe7e024d0` 加本轮修改。本轮没有新增 PowerCore Rust 改动，沿用已经接入的独立 Harness。
- 没有新增包依赖，没有提交或推送。

## 通用图片入口

新增 `ImageAttachment` 与 `attachments` 请求字段，不以 GLM 型号做分支。PowerCore 客户端把图片
转成已有 `input` 中的 `image` ContentBlock；沿用 Session、Run、MCP 鉴权及服务端会话绑定。
文字请求仍走原路径；未开启图片入口的后端会明确拒绝，而不是丢掉附件继续回答。

页面现在支持本地选择、缩略图预览、移除与发送 PNG/JPEG/WebP。
限制为最多 4 张、单张 5 MB、单张 2500 万像素、实际 HTTP 请求体 28 MiB，包含无 Content-Length
的分块上传限制；服务器验证 Base64、图片实际格式和尺寸。暂不接受远程图片 URL、文件路径、SVG、
音频、视频或其他文件类型。

聊天日志只记录文字和附件名称，不写 Base64。原图由 PowerCore 既有摄取/Artifact 机制保留；
校验错误不会回显完整上传数据。等待中的文字追问不能默默消费一张图片，会提示先回答或重置会话。

## 真实验收

命令：

```sh
.venv/bin/python scripts/verify_powercore.py \
  --data-dir xclaw.data/powercore-glm53flash \
  --expected-model glm-5-3-flash-260826 \
  --multimodal --numeric-transcription
```

执行时间：14:15:32 至 14:20:28，约 4 分 56 秒，包含等待一次性任务到点执行。
退出码为 0，证据文件为：

```text
xclaw.data/powercore-glm53flash/powercore/acceptance-dec074fedf.json
```

脚本检查配置和 Run 的模型名称；额外检查子 Agent 与后台任务事件，所有执行记录均为
`glm-5-3-flash-260826`。没有切回 native，也没有在失败后自动重跑整轮。

| 用例 | 实际结果 |
| --- | --- |
| 图片读取 | 随机挑战码、左右颜色/形状、两行数字全部与生成时的真值一致，无工具调用 |
| 图片续问 | 不重新上传图片，同 Session 正确返回此前挑战码与数值 |
| 行情与多轮聊天 | 实际行情工具成功，续问正确保留股票上下文 |
| 自选股/记忆/模拟持仓 | 实际写入并回读 SQLite，600519、2 股、成本价 1000 的模拟记录存在 |
| 会话隔离 | 第二会话没有第一会话的记忆、自选股或持仓 |
| 定时任务 | id=1，在 14:18:27+08:00 到点执行，最终 completed，结果写入原会话记忆 |
| 持久化追问 | 后台任务未消费前台问题；回答后同 Run 恢复完成 |
| 子 Agent | 独立只读 Run 完成两只股票行情对比，使用同一个新模型 |
| 历史行情/技术指标 | 20 行日 K 线及 MA5/MA20 成功返回，并通过严格数值转录校验 |
| 日报 | `investment_report generate` 实际执行，日报 id=1 落库 |
| `/reset` | 建立新 Session，但长期记忆、自选股和模拟持仓保留 |

### 图片事实

测试 PNG 由确定性绘图代码生成，挑战码和第二行数值随机生成，不包含在问题文本中，不能靠问题猜出。
模型收到的是图片本身，没有本地 OCR 预先把答案塞进提示词。

| 项目 | 真值及识别结果 |
| --- | --- |
| Challenge | `DA17BF8F` |
| 左侧 | 红色三角形 |
| 右侧 | 蓝色正方形 |
| ROW_A | `1288.00` |
| ROW_B | `1373.85` |

图片文件：`xclaw.data/powercore-glm53flash/powercore/vision-dec074fedf.png`。
自动图片 Run：`run_c88248e8ad974a04b378a732f0cfe42d`；续问 Run：
`run_b1d4310a8bd14a8b815047b172a8a1d9`。

浏览器额外验收 Run 为 `run_dafa8dc64d924996bf1e19b05b1391e0`，页面上传后同样识别出上述全部事实。
桌面布局已查看，390px 移动视口下未发现水平溢出；这不等于已经做过手机真机上传测试。

### 数值核对

`run_6e198faff33c4440a793876898abcadb` 的最终 JSON 与实际工具输出逐项对比：
20 行日期及开盘/收盘/最高/最低值、MA5 与 MA20 全部一致。数值要求以两位小数字符串输出，避免
忽略尾零或混入无法解析的文字。期望值从该 Run 的真实工具记录提取，没有硬编码当天行情。

上轮 GLM-4.7 出现过 `1288 fancied.00` 转述错误，本轮未复现。由于本轮增加了结构化输出要求与
精确断言，不能据单次通过推导模型在任意自由文本输出中都不会出错，也不能直接作模型优劣统计。

## 自动化与限制

全量 `.venv/bin/python -m pytest -q`：457 passed，1 条既有 pandas_ta 弃用警告，72.60 秒。
新增测试先复现旧入口忽略附件，再验证附件转发、鉴权会话、REST/SSE、格式/MIME/尺寸/数量限制、
分块请求体上限、图片单独发送、待回答 Run 的附件拒绝和不落 Base64 聊天日志。
日志：`xclaw.data/logs/glm53flash-tests.log`。Python 编译检查及 `git diff --check` 通过。

本轮图片用例不覆盖音视频、PDF/Office 附件、外部 IM 图片消息、生产鉴权部署或多副本入口。
`supports_images` 表示入口已接通，不保证任意后端模型都能理解图像。当前网页流式接口仍是终答后
分块输出，没有新增实时 Token 流。

模型配置、验收数据和原始图片均已核对处于 `.gitignore` 范围。现运行聊天页为
`http://127.0.0.1:8080`，可直接选择图片验证，不需要手工调用接口。
