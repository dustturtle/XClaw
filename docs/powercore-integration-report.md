# XClaw 接入 PowerCore Harness 实施与验证报告

日期：2026-09-05，Asia/Shanghai。

后续新模型与图片能力复验见 [GLM-5.3-Flash 验收报告](glm53flash-acceptance-report.md)；本文保留 GLM-4.7 的原始实施记录。

## 结论

已完成独立 PowerCore 到 XClaw 的可选执行后端接入，并完成本机真实 Web/HTTP 功能闭环验收。
不是仅探活，也不是把 Rust 包装成另一个未持久化循环：模型/工具执行进入 PowerCore Server、Worker
与 PostgreSQL，使用现有预算、Checkpoint、补充信息、工具收据和终态机制。

核心功能闭环通过，但这不等于生产发布、所有渠道验收或模型文本逐字准确。发现的模型输出质量问题
和未验收范围在末尾单独列出，未通过金融专用补丁或模糊成功提示掩盖。

## 范围与实现

本次经明确授权修改两个仓库，均未提交或推送：

| 仓库 | 验证基线 |
| --- | --- |
| `/Users/guanzhenwei/Desktop/gitProjects/XClaw` | `main`，`8f835419fe993e5ba56585cac6280fc26cc0052a` 加本次未提交修改 |
| `/Users/guanzhenwei/Desktop/gitProjects/PowerCore` | `main`，`59e06ce24ff309e97d79a2a2cca738af1bfba637` 加本次未提交修改 |

没有读取或集成 `poweragent/HTPowerCore` 历史副本的运行时代码，没有 FFI、代码复制、新 SDK 或新包依赖。
既有微信相关未提交修改保留未动。

### XClaw 侧

- `agent_backend=native|powercore` 显式选择，不做失败后静默回退。
- 统一入口通过 HTTP/SSE 创建/续接 Run，持久化 Session、Run、事件游标与输入请求；子 Agent 也使用 PowerCore。
- 沿用 ToolRegistry，增加工具自身的 `read_only` 元数据。低风险写操作仍按写入处理，不以工具名维护金融白名单。
- MCP 回调验证独立内部 Token、PowerCore principal 和 SQLite 中的 Run 绑定；从服务端恢复 `chat_id` 与工具权限。
- 文件工具限制在当前会话工作目录；定时任务只能取消自己的任务。只读命令不能通过“记住：”快捷分支绕过 Harness。
- 定时任务保持原会话所有者，但不共享前台 PowerCore Session；不会消费前台等待中的输入。子任务/后台失败抛出明确错误，不作为成功工具结果返回。
- 本地启动器复用模型配置，隔离演示数据和 PostgreSQL；验收脚本记录真实事件与 SQLite 回读证据。

### PowerCore 侧

- 成功出口拒绝空正文；默认允许一次补答。补答禁止工具调用，保留已完成结果和原有预算；再次为空则失败。
- 修复限制保存在 Checkpoint，模型即使在仅正文修复轮输出工具调用，也不会执行它。
- OpenAI-compatible Worker 支持可选 `temperature/max_tokens/thinking`，不硬编码 GLM 型号或服务地址。
- `mcp-controlled` 准入内置 `ask_user`，将追问落为持久化 `input_required`。
- Service API v1 新增可选 `max_turns`，参与幂等指纹、可信 Run context 与 Checkpoint；只能收紧服务端预算。
- MCP 失败保留失败标志，同时向模型提供带不可信标记的原始诊断，避免参数错误被统一抹成未知失败。
- 新增请求 schema snapshot、API/镜像发行契约和 Docker 镜像元数据标签。

主要入口：`xclaw/powercore.py`、`xclaw/powercore_mcp.py`、`xclaw/runtime.py`、
`scripts/run_powercore.py`、`scripts/verify_powercore.py`。具体配置见 [接入说明](powercore-integration.md)。

## 自动化验证

| 验证 | 当前结果 |
| --- | --- |
| XClaw 全量 `pytest -q` | 444 passed，1 条既有 pandas_ta 弃用警告，72.44 秒 |
| PowerCore `cargo fmt --all -- --check` | 通过 |
| PowerCore `cargo clippy --workspace --all-targets --all-features -- -D warnings` | 通过 |
| PowerCore `cargo test --workspace --all-features` | 333 passed，3 个 PostgreSQL 测试默认 ignored |
| 在专用测试数据库显式运行上述 3 个 ignored 测试 | 3 passed，演示数据库未受测试清理影响 |
| PowerCore `uv run python -m unittest discover` | 4 passed |
| PowerCore `cargo run -p powercore-cli -- doctor --format json` | status=ok；此命令为配置/本机检查，未冒充真实模型探测 |
| 两仓 `git diff --check`、新增 Python 入口编译检查 | 通过 |
| PowerCore Markdown 链接检查 | 38 份文档，0 个失效本地链接 |

覆盖空终答、有界修复、禁止重复工具、API 请求快照、续接、输入恢复、SSE 重连与去重、后台会话隔离、
越权写入/取消拒绝、文件目录逃逸、只读权限、预算收紧、并发 Session 追加与 PostgreSQL 恢复。

首次 XClaw 基线为 425 passed / 1 failed，失败来自财报测试固定日期已经过期。只把该测试的未来日期
改为相对当前时间，没有修改财报产品逻辑以迁就断言。

日志位置：

- `xclaw.data/logs/powercore-final-tests.log`
- `../PowerCore/.runtime/xclaw-integration-cargo-test.log`
- `../PowerCore/.runtime/xclaw-integration-clippy.log`
- `../PowerCore/.runtime/xclaw-integration-postgres-test.log`
- `../PowerCore/.runtime/xclaw-integration-python-test.log`
- `../PowerCore/.runtime/xclaw-integration-doctor.json`

## 真实功能验收

环境：本机 ARM64 macOS、XClaw Python 3.12、原生 Rust Server/Worker、独立 Docker PostgreSQL，
真实模型 `glm-4-7-251222`。原有 YAML 中的密钥仅用于已配置的模型服务，不在报告中复述。

完整脚本最终一轮从 13:34:18 至 13:38:06，返回 `status=passed`，该轮未做自动重试或 mock：

```text
xclaw.data/powercore-demo/powercore/acceptance-ea5ad2f8d8.json
```

验收会话为 `pc-acceptance-ea5ad2f8d8`；隔离会话为 `pc-isolation-ea5ad2f8d8`。

| 实际功能 | 执行与回读结果 | 关键 Run |
| --- | --- | --- |
| 实时行情 | 贵州茅台 600519，1330.00；时间 2026-09-04 16:14:33；腾讯直连 HTTP | `run_457a56da0d28489d8b61394c60b976dc` |
| 多轮上下文 | 无需重输代码，正确续接同一 Session，返回股票及昨收/涨幅 | `run_b62b0aaf40284e82a33a7b4a36d2b3e7` |
| 自选股/记忆/持仓 | 三个实际工具成功；SQLite 回读 600519、独立记忆标记、2 股/成本价 1000 的模拟记录 | `run_a20b2ecd0e5f4ccfa18f68e4cfa3a674` |
| 会话隔离 | 第二会话通过三个工具读取，未获得第一会话状态，SQLite 也无对应记录 | `run_cb958689c3f54b0ba5faa09195d82349` |
| 创建一次性任务 | task id=1，计划 13:36:26+08:00 | `run_428566df10ae40aab57437b2ed49884b` |
| 到点执行 | task 状态 completed；原所有者记忆中出现后台执行标记；使用独立 Session | `run_a44e611c2687440dba9cf7ca20cf2297` |
| 持久化追问 | 前台保持 waiting_input，不被定时消息替代；用户回复“蓝色”后同 Run 完成 | `run_a163ad7d5582490e9aa4030402052d65` |
| 只读子 Agent | 主 Run 委托两个股票行情对比，子 Run 实际使用 `mcp-readonly` | `run_d43eb5046a044f68b4593aaaeab9ade4` / `run_ebbd57fcfe7f4ae492b91c42ded5ba1b` |
| 历史/技术指标 | 两工具成功，源为 pytdx；原始数据包含最近 20 日 K 线，MA5=1305.09、MA20=1311.28 | `run_d549b41ef25a469a980de09367ff29cf` |
| 生成日报 | 实际生成并落库 id=1，标题“2026-09-05 自选股日报” | `run_a5576871abd6400e880fd155dd77052f` |
| 重置 | `/reset` 清除 Session 关联；新 Session 仍能读取自选股、记忆与模拟持仓 | `run_7251db2601a64a9c91b385b3f98f8c45` |

另外通过浏览器页面操作验证过行情、多轮、状态写入和服务重启后的续接。
`powercore-product-acceptance` 的 Session `session_7a5b1915eb2947de8033a05e330ac15a`
在进程重启后仍正确返回此前的 600519、2 股、成本价 1000，续接 Run 为
`run_612a7fe7db2a4fc38543cbb3f9ed8f4f`，未依靠重复查询重新获得对话事实。

最终浏览器页面还通过 `investment_report latest` 读取了已经落库的日报，返回标题和摘要，未重新生成；
Run 为 `run_0688573975b84ad78ac64e8900233167`。

直接回读 PostgreSQL：只读子 Run 的请求限制与 Checkpoint 均为 5 轮，实际 3 轮；后台 Run
与普通前台 Run 均为 10 轮硬上限。这不是提示词中的软约束。

## 空回复与失败证据

原始空回复不是“数据源没配好”的同义词。诊断中观察到：

1. 行情工具已返回价格、时间和数据源，随后模型只返回 reasoning，`content=""`、无工具调用且
   `finish_reason=stop`，旧成功出口仍接受了它。
2. 同一请求重复发送，一次有正文，另一次只有推理内容；模型参数透传有助于保持配置一致，但不能
   据单个样本断言彻底消除上游空回复。
3. 当前以通用终态校验和一次仅正文修复收口，而不是针对某只股票、某个工具或某个 GLM 返回造答案。

原始诊断保留在 `xclaw.data/powercore-demo/probe-variants.json` 和同目录忽略的模型探测记录中。
临时诊断代理已关闭，最终验收直接访问原配置的模型服务。

中途未通过的记录也保留，没有把它们删掉后宣称全程无失败：

- `acceptance-d77ae756ef.json`：验收脚本最初误把公共 `is_error` 字段当成 `status`，并要求回复包含未要求输出的代码；已修正脚本契约断言，产品工具当时已成功。
- `acceptance-9cef88b141.json`：模型去掉 UTC 时区，任务被解释为过去时间；当时 MCP 又隐藏具体诊断。已改进时间参数说明及通用错误数据保留，最终一轮任务真实执行成功。
- `acceptance-3e7dca220e.json`：模型返回非法工具参数 JSON，`run_38f99a916d324be3ae9f387c977d4fa2` 在工具执行前以 `provider_protocol` 失败。没有执行未知副作用，也没有用 native 重跑掩盖。

## 版本、密钥与运行

Service API 为 `schema_version=1`，MCP 为 `2026-07-28`。软件版本与 API 版本分开管理；
API 请求快照包含 `max_turns`。正式发行要求不可覆盖版本 tag、源码 revision、镜像 digest，以及
Server/Worker 相同产物和 catalog。契约在独立 PowerCore 的 `docs/contracts/distribution-v1.md`。

本轮使用本机构建的 Rust 二进制；复用已有 `htpowercore-postgres:latest` 仅作为本机 PostgreSQL
开发镜像，没有覆盖该镜像。没有构建/推送 PowerCore 正式镜像，不能把本机验收说成镜像发布成功。

`xclaw.config.yaml` 与整个 `xclaw.data/` 已由 `.gitignore` 排除，`git ls-files` 未列出它们。
新增示例没有真实密钥，生成的 TOML 只包含 Secret 环境变量引用；运行时 Token 不进入版本文件。

重新运行已验证环境：

```sh
.venv/bin/python scripts/run_powercore.py --web-only \
  --data-dir xclaw.data/powercore-demo \
  --postgres-image htpowercore-postgres:latest --profile mcp-controlled

.venv/bin/python scripts/verify_powercore.py --data-dir xclaw.data/powercore-demo
```

聊天页：`http://127.0.0.1:8080`；PowerCore：`http://127.0.0.1:18080`。
PostgreSQL 容器/数据卷：`xclaw-powercore-postgres-55439`；集成测试使用另建的
`powercore_test_xclaw_integration` 数据库，不使用演示数据库做清理测试。

## 已知限制

- 模型仍可能输出非法工具参数或正文转录错误。人工核对发现历史表中 2026-08-28 的最低价，工具原始值为 `1288.00`，模型转述成了 `1288 fancied.00`。工具和持久化链路正确，但该文本质量问题没有被本次自动验收断言检测；不能称为金融数值逐格精度验收通过。
- 后续若提高事实呈现可靠性，优先直接展示结构化工具数据/产物，把模型分析与原始数据分开，而不是给 GLM 或股票代码堆字符串修补规则。本轮未引入新的事实校验框架。
- 当前单入口进程按会话加锁，不承诺多副本 XClaw 并发接入。生产用户鉴权、RBAC、TLS 与部署加固没有用本机开发模式替代验收。
- 微信、QQ、飞书等真实外部渠道未做现场联调；现有单测通过不等于所有渠道真实送达。Web 定时任务结果落在消息/业务存储中，没有新增主动页面推送。
- 原 Web SSE 接口仍是终答完成后分块返回，不是模型实时 Token 流。原 native 历史不会自动迁移到 PowerCore；更换执行 profile 后应 `/reset` 开新会话。
- 本机启动器目前面向 OpenAI-compatible 模型与内置/本地 Skills 工具；外部 MCP、OAuth、多模型路由及正式镜像发布需要独立配置与验收。
- 日报验收证明真实工具执行与产物落库，不评价策略有效性、投资收益或生成结论的金融准确性。
