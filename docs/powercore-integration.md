# PowerCore Harness 接入

## 边界

```text
Web / QQ / 微信等已有渠道
          |
XClaw 统一入口、可信会话绑定、SQLite 业务状态
          | HTTP / SSE Service API v1
PowerCore Server + PostgreSQL + Worker
          | 带服务鉴权和 Run 身份的 MCP HTTP 回调
XClaw ToolRegistry + 现有行情/记忆/持仓/任务工具
```

PowerCore 负责 Agent Loop、模型轮数/Token/时间预算、持久化 Checkpoint、工具编排、补充信息请求和终态。
XClaw 负责渠道、业务工具、用户状态与 APScheduler。普通 Agent 和只读子 Agent 都进入 PowerCore，
但报告工具内部的业务摘要等既有 LLM 调用仍属于工具实现，不是另一个 Agent Loop。
不通过 Rust FFI 嵌入，不复制 PowerCore 源码，不新增 Python/Rust 包依赖。

## 本机运行

先安装项目现有依赖，准备 Docker Desktop 与 PowerCore `rust-toolchain.toml` 声明的 Rust。
默认查找同级独立仓库 `../PowerCore`，也可使用 `--powercore-dir` 指定。
已有本地 `xclaw.config.yaml` 提供模型配置，启动器不会改写该文件。

```sh
.venv/bin/python scripts/run_powercore.py --web-only --data-dir xclaw.data/powercore-demo
```

打开 `http://127.0.0.1:8080` 直接聊天。PowerCore 监听 `127.0.0.1:18080`；专用 PostgreSQL
容器监听 `127.0.0.1:55439`。端口都可通过启动器参数调整。首次启动会构建 Rust 二进制和缺失的
PostgreSQL 镜像；已有 PostgreSQL 镜像可通过 `--postgres-image` 指定，启动器不会覆盖外部指定镜像。
Ctrl-C 会停止本次启动的 Server/Worker，保留专用 PostgreSQL 容器及数据卷。

先仅接只读能力时增加 `--profile mcp-readonly`。需要自选股、记忆、持仓、报告和定时任务时使用
`--profile mcp-controlled`。演示目录与原有业务数据目录分开，验收脚本只创建独立测试会话和模拟记录。

启动器目前读取 OpenAI-compatible 的 `openai/deepseek/ollama` 配置；其他模型协议、OAuth 或多模型路由
可由独立部署的 PowerCore 自行配置，不通过启动器隐式转换。此启动器生成内置工具及本地 Skills 的
catalog，不自动准入外部 `mcp_servers` 工具；外部 MCP 需要独立部署时显式配置、审计和契约验证。

## 接入已有服务

配置字段已列在 `xclaw.config.example.yaml`；`XCLAW_*` 环境变量优先于 YAML。
`agent_backend=powercore` 要求启用 Web 回调，以及独立的 `powercore_mcp_token`。
MCP catalog 的 Authorization 使用 `env:XCLAW_POWERCORE_MCP_AUTHORIZATION`，值为同一 Token 的 Bearer 形式。
`powercore_api_token` 用于访问 PowerCore；没有此 Token 时仅适用于 PowerCore 的本地开发鉴权模式。

模型 Key、API Token 和 MCP Token 不写入示例或生成的 TOML。正式部署须启用入口鉴权、PowerCore
生产 RBAC、内网/TLS 与 Secret 引用；本机无鉴权聊天页不等于生产多租户登录方案。
同一个 XClaw 数据目录应只运行一个接入进程；多副本入口调度不是本轮实现范围。

## 泛化策略

- 工具使用自身 `read_only` 声明准入，不维护金融工具名称白名单。未声明的工具默认可写；`LOW` 风险不代表只读。`HIGH` 风险工具不向 PowerCore 暴露。
- 每个 Run 保存 `run_id -> chat_id + allowed_tools`。回调同时校验内部 Bearer Token、PowerCore 租户/用户和 Run 绑定，模型参数不能指定或覆盖可信 `chat_id`。
- 文件读写限定在服务端绑定的当前会话目录，拒绝跨目录、绝对路径越界和符号链接逃逸。
- 前台会话持久化到 PowerCore Session；子 Agent 和定时任务使用独立 Run/Session，但仍绑定原用户。定时任务不会消费前台的 `input_required`。
- `/reset` 取消未结束的前台 Run 并清除会话关联，不删除自选股、持仓或长期记忆。切换 backend 不自动迁移旧 Agent Loop 历史。
- 空正文和已知输出截断都不是成功。默认共用最多一次仅正文补答，禁止修复轮执行工具；截断时重新生成精简完整正文，重复空白/截断进入失败终态。错误提示不会伪装成成功工具结果。
- 工具错误仍保留失败标记，诊断正文作为不可信工具数据交还模型；不隐藏可纠正的参数错误，也不允许错误正文提升权限。
- `max_tool_iterations` 和子 Agent 的 `max_iterations` 映射到 PowerCore `max_turns`，由 Harness 强制执行。子任务上限为 5；补答也消耗预算。

## 验证与版本

```sh
.venv/bin/python -m pytest -q
.venv/bin/python scripts/verify_powercore.py --data-dir xclaw.data/powercore-demo
```

第二个命令会消耗真实模型调用，查询真实行情，并在独立验收会话中创建记忆、自选股、模拟持仓、
一次性任务和日报。它等待任务实际执行，读回 SQLite 与 PowerCore 公共事件，将结果写入数据目录
`powercore/acceptance-*.json`；不只是健康检查或成功启动，也不会要求用户手工用 curl 验证。
验收还读取受保护的 `/admin/runs/{run_id}/audit`，核对续问数值是否来自上一轮工具且没有重复查询；
运行者须具备审计权限。工具原始结果不会因此进入普通聊天进度或公共 Session transcript。

契约为 Service API `1` 与 MCP `2026-07-28`，要求包含 `max_turns`、`wall_time_ms` 生效预算回读和非空终态语义的本次 PowerCore
实现。Server/Worker 必须来自同一版本且 catalog 一致。详细镜像发布规则在独立 PowerCore 的
`docs/contracts/distribution-v1.md`，本机二进制验收不代表已发布镜像验收。
长任务预算使用内部 PostgreSQL schema 2（公共 API 仍是 1）。升级前备份数据库并停止旧
Server/Worker，再同步升级两者；不能让 schema 1 的旧 Worker 与新服务混跑。

聊天页面通过 `/api/chat/progress` 观察真实执行阶段、耗时、预算和终态，并支持指定 Run 的取消与刷新后恢复观察。
成功的本地操作（例如无附件的 `/reset`）在 `/api/chat` 响应中返回 `delivery: "direct"`，
明确表示该操作直接完成、没有创建模型 Run。页面显示确认后重新读取会话状态；普通模型回复
不带此标记，仍必须核对真实 Run 终态，不能凭回复文字或没有 Run 就判定成功。
原 `/api/chat/stream` 仍保留“终答完成后分块输出”的行为；执行进度不等于模型实时 Token 流。
真实外部 IM 渠道、生产鉴权和镜像内运行需要各自独立验收，不能用本机 Web 验收替代。

本轮实测结果、Run 标识和已知限制见 [实施与验证报告](powercore-integration-report.md)。

## 图片输入

PowerCore 后端的聊天页可以选择并预览 PNG/JPEG/WebP，最多 4 张、每张 5 MB、2500 万像素。
`/api/chat` 和 `/api/chat/stream` 接受可选 `attachments` 数组，每项为
`{"name":"image.png","media_type":"image/png","data":"<base64>"}`；有附件时 `message` 可省略。
入口复用现有可信会话绑定，并转换为 PowerCore 已有的 image ContentBlock，不依赖某个模型名称。
模型本身必须具备图像理解能力。当前 native 后端、音频、视频、SVG、远程 URL 和其他文件类型
未开放该入口。文字追问等待期间如需新传图片，应先回答或发送 `/reset`。

可在真实验收命令增加 `--expected-model <模型 ID> --multimodal --numeric-transcription`，检查
实际 Run 模型、随机图像事实、图片续问及行情数值与原始工具输出的一致性。
新模型结果见 [GLM-5.3-Flash 验收报告](glm53flash-acceptance-report.md)。

## 长任务预算与观察

页面可以选择普通模式（默认 180 秒）和分析模式（默认 600 秒，初始选中）。API 请求可携带
`execution_mode: standard | analysis`；省略时使用分析模式。不按文本长度或股票代码猜测任务类型。
`powercore_standard_timeout`、`powercore_run_timeout` 为两种模式的整轮预算；`powercore_model_timeout`
独立控制本地 PowerCore 的模型单次请求上限，默认 600 秒，仍会被整轮预算和收尾边界提前约束。
原 `timeout` 保留给 XClaw native/工具内部模型调用与旧客户端配置，不再隐式成为长任务的 240 秒重试边界。
本机启动器在 PowerCore `[server]` 中设置 `run_default_wall_time_ms`、
`run_max_wall_time_ms` 和 `run_finalization_reserve_ms`，不会改写带 Key 的本机 YAML。
收尾默认预留 120 秒；它是整轮预算的一部分，不会在硬截止后额外增加时间。该分配来自真实长文
验收中首轮 240 秒重试与 45 秒收尾不足的证据，不依赖某个模型名称做代码分支。

独立服务可收紧请求预算，XClaw 必须使用 accepted 响应中的 `wall_time_ms`，不能假设请求值已生效。
旧服务不返回预算时会明确提示升级并请求取消，不会静默退回 180 秒。观察等待使用生效预算加传输余量；
观察超时/断线不等于执行失败，不会自动重新提交用户请求。预算、恢复计数和最终完成判定仍由 PowerCore 管理。

`GET /api/chat/progress?chat_id=...&include_history=true` 返回当前前台 Run 的安全投影，历史默认不返回；
旧终态没有记录耗时时会省略 `elapsed_seconds`，页面显示“耗时未记录”，不把任务年龄当成执行耗时。
`POST /api/chat/cancel` 使用 `{chat_id, run_id}`，只能取消该会话当前绑定的 Run，服务端确认之前显示取消中。
两者沿用聊天鉴权和用户命名空间。刷新页面重新观察同一 Run；浏览器断线不取消执行。单 XClaw 进程重启后，
可根据 SQLite 绑定继续读取 PowerCore 事件，而不是新建 Run。终答使用执行事件键去重；子任务和后台任务不会
覆盖前台会话关联。多副本入口协调仍不在本地部署契约内。

PowerCore 的私有模型历史保留已配对的历史工具调用/结果，续问可以读取未出现在上一轮终答里的数据，
但公共 Session transcript 仍只返回用户输入和最终答复。历史证据不会计入新 Run 的成功收据。
保留上限为 32 轮、256 条消息；工具证据使用独立 256 KiB 预算，对话/图片另有 32 MiB 预算，
模型实际上下文仍受 ContextEngine 限制。超限会标记省略，不伪造数据。已 checkpoint 的旧 Run
保持原快照，不会被本次升级暗中回填或将旧失败改成成功。

行情工具的 `independent` 默认为 false，经过审查的独立只读查询才导出 true。历史数据层使用有界同参合并、
短 TTL 缓存和失败源退避，短暂忙碌的数据源在查询预算内等待，不立即被视为故障；第三方同步 SDK 超时后仍保留
实际线程名额，最多每源一个线程，不能通过重复超时无限积累线程。卡死 SDK 无法强制中断，但不会阻塞进程退出。
工具输出携带来源、获取时间与数据截至日期；缓存不是实时行情保证。搜索返回网页摘要与链接，不将摘要伪装成已核验正文。

长文验收不缩短原始输入，在新会话执行，不覆盖用户历史：

```sh
.venv/bin/python scripts/verify_long_tasks.py \
  --source-db xclaw.data/powercore-glm53flash/xclaw.db --message-id 35 \
  --data-dir xclaw.data/powercore-long-tasks --case all --cancel
```

源消息 ID 属于本机已复现的长文，其他环境应指定自己的真实用例。运行会消耗模型和数据服务调用；
输出在忽略的数据目录 `powercore/long-task-acceptance-*.json`。实施计划见 [长任务优化方案](long-task-optimization-plan.md)。
分轮验收证据与未完成项见 [长任务验收报告](long-task-optimization-report.md)。
