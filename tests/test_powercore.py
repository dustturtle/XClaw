"""Exercise the XClaw boundary, including actual SQLite-backed business tools."""

import json
import tomllib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from xclaw.agent_engine import AgentContext, agent_loop
from xclaw.config import Settings, load_settings
from xclaw.memory import StructuredMemory
from xclaw.powercore import MCP_PROTOCOL_VERSION, PowerCoreClient, PowerCoreError, allowed_tools, mcp_catalog
from xclaw.powercore_mcp import create_powercore_mcp_router
from xclaw.runtime import resolve_message_chat
from xclaw.tools import Tool, ToolContext, ToolRegistry, ToolResult
from xclaw.tools.memory_tools import StructuredMemoryReadTool, StructuredMemoryUpdateTool
from xclaw.tools.file_tools import ReadFileTool, WriteFileTool
from xclaw.tools.schedule import CancelScheduledTaskTool, ScheduleTaskTool


def event(sequence, kind, run_id="run_test", **fields):
    return {"schema_version": 1, "sequence": sequence, "run_id": run_id, "type": kind, **fields}


def sse(*events):
    return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content="".join(
        f"data: {json.dumps(item)}\n\n" for item in events
    ))


async def context(db, backend, name="test"):
    chat_id = await db.get_or_create_chat("web", name)
    return AgentContext(
        chat_id=chat_id, channel="web", db=db, llm=AsyncMock(),
        tools=ToolRegistry(), structured_memory=StructuredMemory(db),
        settings=backend.settings, powercore=backend,
    )


@pytest.mark.asyncio
async def test_chat_uses_powercore_session_after_client_restart(db):
    paths = []
    turns = 0

    def server(request):
        nonlocal turns
        paths.append(request.url.path)
        if request.method == "POST":
            turns += 1
            assert "idempotency_key" in json.loads(request.content)
            assert json.loads(request.content)["max_turns"] == settings.max_tool_iterations
            return httpx.Response(202, json={
                "schema_version": 1, "run_id": f"run_turn{turns}", "session_id": "session_test",
            })
        return sse(event(1, "run.started", run_id=f"run_turn{turns}", model="glm"), event(2, "usage", run_id=f"run_turn{turns}", usage={
            "input_tokens": 10, "output_tokens": 2,
        }), event(3, "run.completed", run_id=f"run_turn{turns}", response="结果是 323。"))

    settings = Settings()
    backend = PowerCoreClient(settings, db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    assert await agent_loop(ctx, "计算") == "结果是 323。"
    ctx.llm.chat.assert_not_awaited()
    await backend.close()
    backend = PowerCoreClient(settings, db, transport=httpx.MockTransport(server))
    ctx.powercore = backend
    assert await agent_loop(ctx, "再解释一下") == "结果是 323。"
    assert paths == ["/runs", "/runs/run_turn1/events", "/sessions/session_test/turns", "/runs/run_turn2/events"]
    history = await db.get_recent_messages(ctx.chat_id)
    assert [message["role"] for message in history] == ["user", "assistant", "user", "assistant"]
    await backend.close()


@pytest.mark.asyncio
async def test_input_required_resumes_the_same_run(db):
    requests = []
    reads = 0

    def server(request):
        nonlocal reads
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(202, json={
                "schema_version": 1, "run_id": "run_test", "session_id": "session_test",
            })
        reads += 1
        if reads == 1:
            return sse(event(1, "input_required", input_request_id="input_test", prompt="你的偏好是什么？",
                             response_schema={"type": "object", "required": ["answer"]}))
        assert request.headers["Last-Event-ID"] == "1"
        return sse(event(2, "run.completed", response="已了解偏好。"))

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    assert await agent_loop(ctx, "开始") == "你的偏好是什么？"
    assert await agent_loop(ctx, "低风险") == "已了解偏好。"
    assert requests[2].url.path == "/runs/run_test/input"
    assert json.loads(requests[2].content)["value"] == {"answer": "低风险"}
    await backend.close()


@pytest.mark.asyncio
async def test_quick_memory_command_cannot_bypass_readonly_harness(db):
    requests = []

    def server(request):
        requests.append(request)
        if request.method == "POST":
            assert json.loads(request.content)["profile"] == "mcp-readonly"
            return httpx.Response(202, json={"schema_version": 1, "run_id": "run_test", "session_id": "session_test"})
        return sse(event(1, "run.completed", response="本次只能读取，未修改记忆。"))

    backend = PowerCoreClient(Settings(powercore_profile="mcp-readonly"), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    assert "未修改" in await agent_loop(ctx, "记住：不应写入")
    assert requests[0].url.path == "/runs"
    assert await ctx.structured_memory.get_all(ctx.chat_id) == []
    await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [
    event(1, "run.completed", response=" \n"),
    event(1, "run.failed", error={"code": "provider_protocol"}),
])
async def test_empty_or_failed_response_is_not_saved_as_success(db, terminal):
    def server(request):
        if request.method == "POST":
            return httpx.Response(202, json={"schema_version": 1, "run_id": "run_test", "session_id": "session_test"})
        return sse(terminal)

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    reply = await agent_loop(ctx, "测试空回复")
    assert "本轮" in reply
    assert (await db.get_powercore_session(ctx.chat_id))["status"] == "failed"
    assert (await db.get_recent_messages(ctx.chat_id))[-1]["content"] == reply
    ctx.llm.chat.assert_not_awaited()
    await backend.close()


@pytest.mark.asyncio
async def test_sse_reconnect_uses_cursor_and_does_not_double_record_usage(db):
    calls = []
    usage = event(1, "usage", usage={"input_tokens": 10, "output_tokens": 2})

    def server(request):
        if request.method == "POST":
            return httpx.Response(202, json={"schema_version": 1, "run_id": "run_test", "session_id": "session_test"})
        calls.append(request.headers["Last-Event-ID"])
        if len(calls) == 1:
            return sse(usage)
        return sse(usage, event(2, "run.completed", response="已完成"))

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    assert await agent_loop(ctx, "开始") == "已完成"
    assert calls == ["0", "1"]
    async with db.conn.execute("SELECT COUNT(*) FROM llm_usage") as cursor:
        assert (await cursor.fetchone())[0] == 1
    await backend.close()


@pytest.mark.asyncio
async def test_reset_cancels_pending_run_and_forgets_remote_session(db):
    paths = []

    def server(request):
        paths.append(request.url.path)
        return httpx.Response(202, json={"schema_version": 1, "run_id": "run_test", "status": "cancelled"})

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    await db.bind_powercore_run(ctx.chat_id, "run_test", "session_test", [])
    assert "重置" in await agent_loop(ctx, "/reset")
    assert paths == ["/runs/run_test/cancel"]
    assert await db.get_powercore_session(ctx.chat_id) is None
    await backend.close()


def rpc(method, *, name=None, arguments=None, run_id="run_bound", user="user_xclaw"):
    return {
        "jsonrpc": "2.0", "id": 1, "method": method,
        "params": {"name": name, "arguments": arguments or {}, "_meta": {
            "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
            "io.powercore/requestContext": {"runId": run_id, "tenantId": "tenant_xclaw", "userId": user},
        }},
    }


@pytest.mark.asyncio
async def test_mcp_binds_tools_to_the_saved_chat_and_enforces_readonly(db):
    settings = Settings(powercore_mcp_token="test-mcp-secret")
    backend = SimpleNamespace(settings=settings, db=db)
    memory = StructuredMemory(db)
    first = await db.get_or_create_chat("web", "first")
    second = await db.get_or_create_chat("web", "second")
    registry = ToolRegistry()
    registry.register(StructuredMemoryUpdateTool())
    registry.register(StructuredMemoryReadTool())

    async def factory(chat_id):
        return ToolContext(chat_id=chat_id, channel="web", db=db, structured_memory=memory)

    app = FastAPI()
    app.include_router(create_powercore_mcp_router(registry, backend, factory), prefix="/mcp")
    await db.bind_powercore_run(first, "run_bound", "session_first", ["structured_memory_update"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        assert (await client.post("/mcp", json=rpc("tools/list"))).status_code == 401
        client.headers["Authorization"] = "Bearer test-mcp-secret"
        discovery = (await client.post("/mcp", json=rpc("server/discover"))).json()
        assert discovery["result"]["supportedVersions"] == [MCP_PROTOCOL_VERSION]
        reply = (await client.post("/mcp", json=rpc("tools/call", name="structured_memory_update", arguments={
            "content": "只属于第一位用户", "chat_id": second,
        }))).json()
        assert reply["result"]["isError"] is False
        assert len(await memory.get_all(first)) == 1
        assert await memory.get_all(second) == []
        forged = (await client.post("/mcp", json=rpc("tools/call", name="structured_memory_update", user="user_forged"))).json()
        assert forged["error"]["code"] == -32001
        await db.bind_powercore_run(second, "run_readonly", "session_second", ["structured_memory_read"])
        denied = (await client.post("/mcp", json=rpc("tools/call", run_id="run_readonly", name="structured_memory_update"))).json()
        assert denied["error"]["code"] == -32001
        await db.update_powercore_run("run_bound", "completed", 3)
        ended = (await client.post("/mcp", json=rpc("tools/call", name="structured_memory_update"))).json()
        assert ended["error"]["code"] == -32001


@pytest.mark.asyncio
async def test_scheduled_messages_keep_owner_and_cannot_cancel_another_chat(db):
    first = await db.get_or_create_chat("web", "first")
    second = await db.get_or_create_chat("web", "second")
    assert await resolve_message_chat(db, str(first), "scheduler") == first
    task = await db.add_scheduled_task(second, "other", "other", cron_expression="0 15 * * 1-5")
    result = await CancelScheduledTaskTool().execute({"task_id": task}, ToolContext(chat_id=first, channel="web", db=db))
    assert result.is_error
    assert (await db.get_active_tasks())[0]["id"] == task


@pytest.mark.asyncio
async def test_background_run_does_not_answer_or_replace_foreground_question(db):
    requests = []

    def server(request):
        requests.append(request)
        if request.method == "POST":
            assert request.url.path == "/runs"
            assert json.loads(request.content)["profile"] == "mcp-controlled"
            return httpx.Response(202, json={
                "schema_version": 1, "run_id": "run_test", "session_id": "session_background",
            })
        return sse(event(1, "run.completed", response="定时任务完成。"))

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    ctx.tools.register(ScheduleTaskTool())
    ctx.skip_session_persistence = True
    await db.bind_powercore_run(ctx.chat_id, "run_foreground", "session_foreground", [])
    pending = {"input_request_id": "input_foreground", "response_schema": {"required": ["answer"]}}
    await db.update_powercore_run("run_foreground", "waiting_input", 5, pending)
    assert await agent_loop(ctx, "后台任务") == "定时任务完成。"
    foreground = await db.get_powercore_session(ctx.chat_id)
    assert foreground["run_id"] == "run_foreground"
    assert foreground["status"] == "waiting_input"
    assert json.loads(foreground["input_request_json"]) == pending
    assert (await db.get_powercore_run("run_test"))["chat_id"] == ctx.chat_id
    await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["run.failed", "input_required"])
async def test_background_failure_or_question_is_not_reported_as_success(db, kind):
    def server(request):
        if request.method == "POST":
            return httpx.Response(202, json={
                "schema_version": 1, "run_id": "run_test", "session_id": "session_test",
            })
        return sse(event(1, kind, prompt="请补充参数", error={"code": "provider_protocol"}))

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    ctx.skip_session_persistence = True
    with pytest.raises(PowerCoreError):
        await agent_loop(ctx, "后台任务")
    assert all(message["role"] != "assistant" for message in await db.get_recent_messages(ctx.chat_id))
    assert (await db.get_powercore_run("run_test"))["status"] in {"failed", "cancelled"}
    await backend.close()


def test_catalog_treats_low_risk_state_mutations_as_writes():
    registry = ToolRegistry()
    registry.register(ScheduleTaskTool())
    registry.register(StructuredMemoryReadTool())
    assert allowed_tools(registry, "mcp-readonly") == ["structured_memory_read"]
    catalog = tomllib.loads(mcp_catalog(registry, Settings(), "http://127.0.0.1:8080/_powercore/mcp"))
    controlled = next(server for server in catalog["servers"] if server["profile"] == "mcp-controlled")
    schedule = next(tool for tool in controlled["tools"] if tool["name"] == "schedule_task")
    assert schedule["risk"] == "write"
    assert schedule["read_only"] is False
    assert schedule["idempotent"] is False


def test_new_tool_is_admitted_by_metadata_without_adapter_name_changes():
    class CustomLookup(Tool):
        name = "arbitrary_business_lookup"
        description = "A new independently registered business tool"
        parameters = {"type": "object", "properties": {}}
        read_only = True

        async def execute(self, params, context):
            return ToolResult("result")

    tool = CustomLookup()
    registry = ToolRegistry()
    registry.register(tool)
    assert allowed_tools(registry, "mcp-readonly") == [tool.name]
    tool.read_only = False
    assert allowed_tools(registry, "mcp-readonly") == []
    assert allowed_tools(registry, "mcp-controlled") == [tool.name]


def test_backend_env_override_wins_over_yaml(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("agent_backend: native\n", encoding="utf-8")
    monkeypatch.setenv("XCLAW_AGENT_BACKEND", "powercore")
    assert load_settings(path).agent_backend == "powercore"


@pytest.mark.asyncio
async def test_file_tools_respect_the_server_bound_workspace(tmp_path):
    root = tmp_path / "first"
    root.mkdir()
    other = tmp_path / "second.txt"
    other.write_text("other user's data")
    ctx = ToolContext(chat_id=1, channel="web", workspace_root=root)
    assert not (await WriteFileTool().execute({"path": "own.txt", "content": "own data"}, ctx)).is_error
    assert (await ReadFileTool().execute({"path": "own.txt"}, ctx)).content == "own data"
    assert (await ReadFileTool().execute({"path": "../second.txt"}, ctx)).is_error
    assert (await WriteFileTool().execute({"path": str(other), "content": "overwrite"}, ctx)).is_error
    (root / "link.txt").symlink_to(other)
    assert (await ReadFileTool().execute({"path": "link.txt"}, ctx)).is_error
    assert other.read_text() == "other user's data"
