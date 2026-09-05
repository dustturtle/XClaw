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
- 空正文不是成功。默认最多一次仅正文补答，禁止修复轮执行工具；重复空正文进入失败终态。错误提示不会伪装成成功工具结果。
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

契约为 Service API `1` 与 MCP `2026-07-28`，要求包含 `max_turns` 和非空终态语义的本次 PowerCore
实现。Server/Worker 必须来自同一版本且 catalog 一致。详细镜像发布规则在独立 PowerCore 的
`docs/contracts/distribution-v1.md`，本机二进制验收不代表已发布镜像验收。

当前聊天页面和原 `/api/chat/stream` 保留“终答完成后分块输出”的行为，并非模型实时 Token 流。
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
