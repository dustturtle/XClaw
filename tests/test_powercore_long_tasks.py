"""Long-running chat budget and observation boundary regressions."""

import asyncio
import json
import tomllib
from unittest.mock import AsyncMock

import httpx
import pytest

from xclaw.agent_engine import AgentContext
from xclaw.config import Settings
from xclaw.powercore import PowerCoreClient, PowerCoreError
from xclaw.tools import ToolRegistry


def event(sequence, kind, **fields):
    return {"schema_version": 1, "sequence": sequence, "run_id": "run_long", "type": kind, **fields}


def encoded(item):
    return f"data: {json.dumps(item)}\n\n".encode()


async def context(db, backend):
    chat_id = await db.get_or_create_chat("web", "long")
    return AgentContext(chat_id, "web", db, AsyncMock(), ToolRegistry(), settings=backend.settings, powercore=backend)


def accepted(wall_time_ms=600000):
    return httpx.Response(202, json={
        "schema_version": 1, "run_id": "run_long", "session_id": "session_long", "wall_time_ms": wall_time_ms,
    })


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_legacy_terminal_duration_is_unknown_not_time_since_creation(status):
    binding = {"run_id": "run_old", "status": status, "cursor": 2,
               "created_at": "2026-01-01 00:00:00", "progress_json": "{}"}
    assert PowerCoreClient._snapshot(binding)["elapsed_seconds"] is None
    binding["progress_json"] = json.dumps({"elapsed_seconds": 12.5})
    assert PowerCoreClient._snapshot(binding)["elapsed_seconds"] == 12.5


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,requested", [("standard", 180000), ("analysis", 600000)])
async def test_budget_is_explicit_and_effective_server_budget_is_saved(db, mode, requested):
    def server(request):
        if request.method == "POST":
            assert json.loads(request.content)["wall_time_ms"] == requested
            return accepted(120000)
        return httpx.Response(200, content=encoded(event(1, "run.completed", response="A useful answer.")))

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    ctx.execution_mode = mode
    assert await backend.run(ctx, "Review the supplied document") == "A useful answer."
    state = await backend.progress(ctx)
    assert state["wall_time_ms"] == 120000
    assert state["status"] == "completed"
    assert isinstance(state["elapsed_seconds"], float)
    assert state["reply"] == "A useful answer."
    await backend.close()


@pytest.mark.asyncio
async def test_old_server_cannot_silently_ignore_budget(db):
    def server(request):
        return httpx.Response(202, json={"schema_version": 1, "run_id": "run_long", "session_id": "session_long"})

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    reply = await backend.run(ctx, "Analyze")
    assert "预算" in reply and "升级" in reply
    await backend.close()


@pytest.mark.asyncio
async def test_disconnect_does_not_cancel_run_and_progress_is_sanitized(db):
    started, finish = asyncio.Event(), asyncio.Event()
    requests = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield encoded(event(1, "run.started", model="glm"))
            yield encoded(event(2, "tool.started", name="mcp_xclaw-mcp-controlled_stock_quote", arguments={"secret": "private"}))
            started.set()
            await finish.wait()
            yield encoded(event(3, "run.completed", response="Finished after reconnect."))

    def server(request):
        requests.append(request)
        return accepted() if request.method == "POST" else httpx.Response(200, stream=Stream())

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    caller = asyncio.create_task(backend.run(ctx, "Long input"))
    await asyncio.wait_for(started.wait(), 2)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    snapshot = await backend.progress(ctx, include_history=True)
    assert snapshot["status"] == "running"
    assert snapshot["stage"] == "tool"
    assert "private" not in json.dumps(snapshot)
    assert "arguments" not in json.dumps(snapshot)
    assert not any(request.url.path.endswith("/cancel") for request in requests)
    finish.set()
    for _ in range(100):
        snapshot = await backend.progress(ctx)
        if snapshot.get("reply"):
            break
        await asyncio.sleep(0.01)
    assert snapshot["status"] == "completed"
    assert snapshot["reply"] == "Finished after reconnect."
    await backend.progress(ctx)
    history = await db.get_recent_messages(ctx.chat_id)
    assert [row["role"] for row in history] == ["user", "assistant"]
    assert sum(request.method == "POST" for request in requests) == 1
    await backend.close()


@pytest.mark.asyncio
async def test_cancel_is_bound_to_the_exact_current_run(db):
    requests = []

    def server(request):
        requests.append(request)
        return httpx.Response(202, json={"schema_version": 1, "status": "cancelling"})

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    await db.bind_powercore_run(ctx.chat_id, "run_long", "session_long", [])
    with pytest.raises(PowerCoreError):
        await backend.cancel_run(ctx.chat_id, "run_other")
    assert requests == []
    snapshot = await backend.cancel_run(ctx.chat_id, "run_long")
    assert requests[0].url.path == "/runs/run_long/cancel"
    assert snapshot["status"] != "cancelled"
    assert snapshot["stage"] == "cancelling"
    await backend.close()


@pytest.mark.asyncio
async def test_deadline_is_a_structured_failure_not_a_completed_answer(db):
    def server(request):
        if request.method == "POST":
            return accepted()
        return httpx.Response(200, content=encoded(event(1, "run.failed", error={"code": "deadline_exceeded", "message": "private diagnostics"})))

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    reply = await backend.run(ctx, "Analyze")
    snapshot = await backend.progress(ctx)
    assert "超时" in reply
    assert snapshot["status"] == "failed"
    assert snapshot["error_code"] == "deadline_exceeded"
    assert "private diagnostics" not in json.dumps(snapshot)
    await backend.close()


@pytest.mark.asyncio
async def test_new_client_observes_existing_run_without_resubmitting(db):
    requests = []

    def server(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.headers["Last-Event-ID"] == "3"
        return httpx.Response(200, content=encoded(event(4, "run.completed", response="Recovered existing run.")))

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    await db.bind_powercore_run(ctx.chat_id, "run_long", "session_long", [], wall_time_ms=600000)
    await db.update_powercore_run("run_long", "running", 3)
    await backend.progress(ctx)
    for _ in range(100):
        snapshot = await backend.progress(ctx)
        if snapshot.get("reply"):
            break
        await asyncio.sleep(0.01)
    assert snapshot["reply"] == "Recovered existing run."
    assert len(requests) == 1
    await backend.close()


@pytest.mark.asyncio
async def test_outer_wait_uses_admitted_run_budget_not_model_timeout(db, monkeypatch):
    waits = []
    original_timeout = asyncio.timeout

    def timeout(delay):
        waits.append(delay)
        return original_timeout(delay)

    def server(request):
        if request.method == "POST":
            return accepted(480000)
        return httpx.Response(200, content=encoded(event(1, "run.completed", response="OK")))

    backend = PowerCoreClient(Settings(timeout=240), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    monkeypatch.setattr(asyncio, "timeout", timeout)
    assert await backend.run(ctx, "Analyze") == "OK"
    assert waits == [510.0]
    await backend.close()


@pytest.mark.asyncio
async def test_terminal_reply_replay_is_idempotent(db):
    chat_id = await db.get_or_create_chat("web", "replay")
    await db.bind_powercore_run(chat_id, "run_long", "session_long", [])
    await db.update_powercore_run("run_long", "waiting_input", 3)
    await db.save_powercore_reply("run_long", "Question?")
    await db.save_powercore_reply("run_long", "Question?")
    await db.update_powercore_run("run_long", "completed", 6)
    await db.save_powercore_reply("run_long", "Final answer")
    await db.save_powercore_reply("run_long", "Final answer")
    assert [row["content"] for row in await db.get_recent_messages(chat_id)] == ["Question?", "Final answer"]


@pytest.mark.asyncio
async def test_progress_and_first_message_can_resolve_chat_concurrently(db):
    ids = await asyncio.gather(*(db.get_or_create_chat("web", "same") for _ in range(10)))
    assert len(set(ids)) == 1


def test_launcher_separates_model_timeout_and_service_run_policy(tmp_path):
    from scripts.run_powercore import powercore_config_text

    settings = Settings(api_key="private-key", timeout=240, powercore_run_timeout=600)
    text = powercore_config_text(settings, tmp_path, 18081)
    config = tomllib.loads(text)
    assert "private-key" not in text
    assert settings.timeout == 240
    assert config["model"]["timeout_seconds"] == 600
    assert config["server"]["run_default_wall_time_ms"] == 600000
    assert config["server"]["run_max_wall_time_ms"] == 600000
    assert config["server"]["run_finalization_reserve_ms"] == 120000


@pytest.mark.asyncio
async def test_real_web_progress_accepts_idle_runtime_snapshot(db):
    from xclaw.channels.web import create_web_app

    backend = PowerCoreClient(Settings(), db)
    ctx = await context(db, backend)

    async def progress(chat_id, include_history=False):
        return await backend.progress(ctx, include_history=include_history)

    app = create_web_app(message_handler=AsyncMock(), progress_handler=progress)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.get("/api/chat/progress", params={"include_history": "true"})
    assert response.status_code == 200
    assert response.json()["status"] == "idle"
    assert response.json()["messages"] == []
    await backend.close()


@pytest.mark.asyncio
async def test_reset_http_response_explicitly_finishes_without_a_run(db):
    from xclaw.agent_engine import agent_loop
    from xclaw.channels.web import create_web_app

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(
        lambda request: pytest.fail("reset of an idle chat must not create a model Run")))
    ctx = await context(db, backend)

    async def handle(chat_id, message, **options):
        return await agent_loop(ctx, message, attachments=options.get("attachments"))

    app = create_web_app(message_handler=handle, progress_handler=AsyncMock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/chat", json={"chat_id": "long", "message": " /RESET "})
    assert response.status_code == 200
    assert response.json().get("delivery") == "direct"
    assert "已重置" in response.json()["reply"]
    assert await db.get_powercore_session(ctx.chat_id) is None
    await backend.close()


@pytest.mark.asyncio
async def test_invalid_followup_does_not_replace_the_persisted_question(db):
    def server(request):
        if request.method == "POST":
            return accepted()
        return httpx.Response(200, content=encoded(event(
            1, "input_required", input_request_id="input_choice", prompt="Choose a value.",
            response_schema={"required": ["choice"]},
        )))

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    assert await backend.run(ctx, "Start") == "Choose a value."
    assert "JSON" in await backend.run(ctx, "not json")
    snapshot = await backend.progress(ctx)
    assert snapshot["status"] == "waiting_input"
    assert snapshot["reply"] == "Choose a value."
    await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,cursor,kind,fields", [
    ("completed", 6, "run.completed", {"response": "Recovered final"}),
    ("waiting_input", 3, "input_required", {"prompt": "Recovered question", "input_request_id": "input_test", "response_schema": {"required": ["answer"]}}),
])
async def test_crash_between_event_and_reply_recovers_by_cursor(db, status, cursor, kind, fields):
    def server(request):
        assert request.method == "GET"
        assert request.headers["Last-Event-ID"] == str(cursor - 1)
        return httpx.Response(200, content=encoded(event(cursor, kind, **fields)))

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    await db.bind_powercore_run(ctx.chat_id, "run_long", "session_long", [])
    if status == "completed":
        await db.update_powercore_run("run_long", "waiting_input", 3)
        await db.save_powercore_reply("run_long", "Old question")
    await db.update_powercore_run("run_long", status, cursor)
    for _ in range(100):
        snapshot = await backend.progress(ctx)
        if snapshot.get("reply"):
            break
        await asyncio.sleep(0.01)
    assert snapshot["reply"] == fields.get("response", fields.get("prompt"))
    await backend.close()


@pytest.mark.asyncio
async def test_cancel_intent_blocks_followup_and_cannot_overwrite_terminal(db):
    requests = []

    def server(request):
        requests.append(request.url.path)
        return httpx.Response(202, json={"schema_version": 1, "status": "cancelling"})

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    await db.bind_powercore_run(ctx.chat_id, "run_long", "session_long", [])
    await db.update_powercore_run("run_long", "waiting_input", 3, {"input_request_id": "input_test", "response_schema": {"required": ["answer"]}})
    await backend.cancel_run(ctx.chat_id, "run_long")
    assert "取消" in await backend.run(ctx, "my answer")
    assert requests == ["/runs/run_long/cancel"]
    await db.update_powercore_run("run_long", "completed", 6)
    await db.save_powercore_progress("run_long", {"stage": "completed", "message": "Complete"})
    await db.request_powercore_cancel("run_long")
    assert backend._snapshot(await db.get_powercore_run("run_long"))["stage"] == "completed"
    await backend.close()


@pytest.mark.asyncio
async def test_background_observation_timeout_requests_cancel(db):
    requests = []

    def server(request):
        requests.append(request.url.path)
        return accepted()

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    ctx.skip_session_persistence = True
    backend._wait = AsyncMock(side_effect=TimeoutError)
    with pytest.raises(PowerCoreError):
        await backend.run(ctx, "Background work")
    assert "/runs/run_long/cancel" in requests
    state = await db.get_powercore_run("run_long")
    assert state["cancel_requested"] == 1
    assert state["status"] != "cancelled"
    assert await db.get_powercore_session(ctx.chat_id) is None
    await backend.close()


@pytest.mark.asyncio
async def test_acceptance_crash_recovers_original_idempotency_key(db, monkeypatch):
    requests, keys = [], set()

    def server(request):
        if request.method == "POST":
            payload = json.loads(request.content)
            requests.append(payload)
            keys.add(payload["idempotency_key"])
            return accepted()
        return httpx.Response(200, content=encoded(event(1, "run.completed", response="Exactly once")))

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    real_bind = db.bind_powercore_run

    async def crash(*args, **kwargs):
        raise RuntimeError("Simulated crash after remote acceptance")

    monkeypatch.setattr(db, "bind_powercore_run", crash)
    with pytest.raises(RuntimeError, match="Simulated crash"):
        await backend.run(ctx, "Original task")
    await backend.close()
    monkeypatch.setattr(db, "bind_powercore_run", real_bind)
    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx.powercore = backend
    for _ in range(100):
        snapshot = await backend.progress(ctx)
        if snapshot.get("reply"):
            break
        await asyncio.sleep(0.01)
    assert snapshot["reply"] == "Exactly once"
    assert len(requests) == 2 and requests[0] == requests[1]
    assert len(keys) == 1
    assert [row["role"] for row in await db.get_recent_messages(ctx.chat_id)] == ["user", "assistant"]
    assert await db.get_pending_powercore_submission(ctx.chat_id) is None
    await backend.close()


@pytest.mark.parametrize("status", [400, 401, 403, 413, 422])
async def test_definitive_submission_rejection_clears_intent_and_allows_reset(db, status):
    requests = []

    def server(request):
        requests.append(request)
        return httpx.Response(status, json={"schema_version": 1, "error": "private diagnostics"})

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    try:
        reply = await backend.run(ctx, "Rejected input")
        assert "private diagnostics" not in reply
        assert await db.get_pending_powercore_submission(ctx.chat_id) is None
        await backend.reset(ctx.chat_id)
        await backend.progress(ctx)
        assert len(requests) == 1
    finally:
        await backend.close()


@pytest.mark.parametrize("failure", [409, 503, "transport"])
async def test_ambiguous_submission_keeps_the_original_payload_and_key(db, failure):
    payloads = []

    def server(request):
        payloads.append(json.loads(request.content))
        if failure == "transport":
            raise httpx.ReadTimeout("fixture")
        return httpx.Response(failure)

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    try:
        await backend.run(ctx, "Original input")
        pending = await db.get_pending_powercore_submission(ctx.chat_id)
        assert pending is not None
        await backend.run(ctx, "Must not become a new execution")
        if ctx.chat_id in backend._inflight:
            await backend._inflight[ctx.chat_id]
        assert len({p["idempotency_key"] for p in payloads}) == 1
        assert all(p == json.loads(pending["request_json"]) for p in payloads)
    finally:
        await backend.close()


async def seed_question(db, ctx):
    await db.bind_powercore_run(ctx.chat_id, "run_long", "session_long", [])
    await db.update_powercore_run("run_long", "waiting_input", 3, {
        "input_request_id": "input_original", "response_schema": {"required": ["answer"]},
    })
    await db.save_powercore_reply("run_long", "Original question")


@pytest.mark.parametrize("remote_status", ["running", "completed", "waiting_input"])
async def test_accepted_input_crash_is_reconciled_before_any_repost(db, monkeypatch, remote_status):
    requests = []
    input_accepted = False

    def server(request):
        nonlocal input_accepted
        requests.append(request)
        if request.url.path.endswith("/input"):
            assert not input_accepted, "An accepted input must never be posted again"
            input_accepted = True
            return httpx.Response(202, json={"schema_version": 1, "status": "queued"})
        if request.url.path == "/runs/run_long":
            return httpx.Response(200, json={
                "schema_version": 1, "run_id": "run_long",
                "status": remote_status if input_accepted else "waiting_input",
                "waiting_for_input": "input_new" if input_accepted else "input_original",
            })
        assert request.headers["Last-Event-ID"] == "3"
        return httpx.Response(200, content=encoded(event(6, "run.completed", response="Recovered answer")))

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    await seed_question(db, ctx)
    real_update = db.update_powercore_run

    async def crash(run_id, status, *args, **kwargs):
        if input_accepted and status == "queued":
            raise RuntimeError("crash after input acceptance")
        return await real_update(run_id, status, *args, **kwargs)

    monkeypatch.setattr(db, "update_powercore_run", crash)
    try:
        with pytest.raises(RuntimeError, match="crash after input"):
            await backend.run(ctx, "Original answer")
    finally:
        await backend.close()
        monkeypatch.setattr(db, "update_powercore_run", real_update)
    pending = await db.get_pending_powercore_submission(ctx.chat_id)
    assert pending is not None
    assert json.loads(pending["request_json"])["value"] == {"answer": "Original answer"}
    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx.powercore = backend
    try:
        await backend.progress(ctx)
        await backend._inflight[ctx.chat_id]
        assert (await backend.progress(ctx))["reply"] == "Recovered answer"
        assert sum(r.url.path.endswith("/input") for r in requests) == 1
        assert await db.get_pending_powercore_submission(ctx.chat_id) is None
    finally:
        await backend.close()


async def test_input_recovery_reposts_only_the_same_still_pending_answer(db):
    requests = []
    fail = True

    def server(request):
        requests.append(request)
        if request.url.path == "/runs/run_long":
            return httpx.Response(200, json={"schema_version": 1, "run_id": "run_long",
                "status": "waiting_input", "waiting_for_input": "input_original"})
        if request.url.path.endswith("/input"):
            if fail:
                raise httpx.ReadTimeout("request not delivered")
            return httpx.Response(202, json={"schema_version": 1, "status": "queued"})
        assert request.headers["Last-Event-ID"] == "3"
        return httpx.Response(200, content=encoded(event(4, "run.completed", response="OK")))

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    await seed_question(db, ctx)
    try:
        await backend.run(ctx, "Original answer")
        assert await db.get_pending_powercore_submission(ctx.chat_id) is not None
        fail = False
        await backend.run(ctx, "Different answer must not replace original")
        await backend._inflight[ctx.chat_id]
        inputs = [json.loads(r.content) for r in requests if r.url.path.endswith("/input")]
        assert inputs and all(p == {"input_request_id": "input_original", "value": {"answer": "Original answer"}} for p in inputs)
        for i, request in enumerate(requests):
            if request.url.path.endswith("/input"):
                assert requests[i - 1].method == "GET"
                assert requests[i - 1].url.path == "/runs/run_long"
    finally:
        await backend.close()


async def spin_until(predicate):
    async with asyncio.timeout(2):
        while not await predicate():
            await asyncio.sleep(0.005)


async def test_background_http_timeout_keeps_retrying_cancel_until_real_terminal(db, monkeypatch):
    from xclaw import powercore

    monkeypatch.setattr(powercore, "_BACKGROUND_POLL_SECONDS", 0.005, raising=False)
    monkeypatch.setattr(powercore, "_BACKGROUND_RETRY_SECONDS", 0.01, raising=False)
    requests = []
    failures = 0

    def server(request):
        requests.append(request.url.path)
        if request.url.path == "/runs":
            return accepted()
        if request.url.path.endswith("/cancel"):
            return httpx.Response(202, json={"schema_version": 1, "status": "cancelling"})
        return httpx.Response(200, content=encoded(event(6, "run.cancelled")))

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    ctx.skip_session_persistence = True
    await db.bind_powercore_run(ctx.chat_id, "run_foreground", "session_foreground", [])
    await db.update_powercore_run("run_foreground", "waiting_input", 2)
    real_wait = backend._wait

    async def interrupted(*args):
        nonlocal failures
        if failures < 2:
            failures += 1
            raise httpx.ReadTimeout("observation interrupted")
        return await real_wait(*args)

    monkeypatch.setattr(backend, "_wait", interrupted)
    try:
        with pytest.raises(PowerCoreError):
            await backend.run(ctx, "Background work")

        async def finished():
            row = await db.get_powercore_run("run_long")
            return row["status"] == "cancelled" and row["reply_cursor"] == 6

        await spin_until(finished)
        assert (await db.get_powercore_session(ctx.chat_id))["run_id"] == "run_foreground"
        assert requests.count("/runs") == 1
        assert requests.count("/runs/run_long/cancel") >= 2
    finally:
        await backend.close()


async def test_background_restore_is_bounded_deduplicated_and_shutdown_safe(db, monkeypatch):
    from xclaw import powercore

    monkeypatch.setattr(powercore, "_BACKGROUND_POLL_SECONDS", 0.005, raising=False)
    release = asyncio.Event()
    entered = set()

    class Stream(httpx.AsyncByteStream):
        def __init__(self, run_id):
            self.run_id = run_id

        async def __aiter__(self):
            assert self.run_id not in entered, "duplicate concurrent observer"
            entered.add(self.run_id)
            await release.wait()
            yield b""

    def server(request):
        assert request.method == "GET"
        return httpx.Response(200, stream=Stream(request.url.path.split("/")[2]))

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    for i in range(12):
        await db.bind_powercore_run(ctx.chat_id, f"run_bg{i}", "session_bg", [], conversational=False)

    async def factory(chat_id):
        return await context(db, backend)

    try:
        await backend.restore_background(factory)
        await backend.restore_background(factory)

        async def has_workers():
            return bool(entered)

        await spin_until(has_workers)
        await asyncio.sleep(0.03)
        assert len(entered) <= 4
        assert len(backend._background) <= 4
    finally:
        await backend.close()
    assert not backend._background
    _, runs = await db.pending_powercore_background()
    assert len(runs) == 12


@pytest.mark.parametrize("status", [400, 401, 403, 413, 422, 409])
async def test_input_rejection_policy_preserves_question_and_handles_conflict(db, status):
    advanced = False
    inputs = []

    def server(request):
        if request.url.path == "/runs/run_long":
            return httpx.Response(200, json={"schema_version": 1, "run_id": "run_long",
                "status": "completed" if advanced else "waiting_input", "waiting_for_input": "input_original"})
        if request.url.path.endswith("/input"):
            inputs.append(json.loads(request.content))
            return httpx.Response(status)
        return httpx.Response(200, content=encoded(event(6, "run.completed", response="Recovered")))

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    await seed_question(db, ctx)
    try:
        await backend.run(ctx, "Answer")
        binding = await db.get_powercore_run("run_long")
        assert binding["reply"] == "Original question"
        pending = await db.get_pending_powercore_submission(ctx.chat_id)
        if status != 409:
            assert pending is None
        else:
            assert pending is not None
            advanced = True
            await backend.progress(ctx)
            await backend._inflight[ctx.chat_id]
            assert (await backend.progress(ctx))["reply"] == "Recovered"
            assert len(inputs) == 1
    finally:
        await backend.close()


async def test_shutdown_during_input_post_retains_original_intent(db):
    entered = asyncio.Event()
    requests = []

    async def server(request):
        requests.append(request.url.path)
        if request.url.path == "/runs/run_long":
            return httpx.Response(200, json={"schema_version": 1, "run_id": "run_long",
                "status": "waiting_input", "waiting_for_input": "input_original"})
        assert request.url.path.endswith("/input")
        entered.set()
        await asyncio.Event().wait()

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    await seed_question(db, ctx)
    caller = asyncio.create_task(backend.run(ctx, "Original answer"))
    await asyncio.wait_for(entered.wait(), 2)
    await backend.close()
    with pytest.raises(asyncio.CancelledError):
        await caller
    pending = await db.get_pending_powercore_submission(ctx.chat_id)
    assert pending["cursor"] == 3
    assert json.loads(pending["request_json"])["value"] == {"answer": "Original answer"}
    assert not any(path.endswith("/cancel") for path in requests)


async def test_background_backoff_survives_restart_then_records_real_deadline(db, monkeypatch):
    import time
    from xclaw import powercore

    monkeypatch.setattr(powercore, "_BACKGROUND_POLL_SECONDS", 0.005)
    monkeypatch.setattr(powercore, "_BACKGROUND_RETRY_SECONDS", 10)
    requests = []
    healthy = False

    def server(request):
        requests.append(request.url.path)
        assert request.url.path != "/runs"
        if request.url.path.endswith("/cancel"):
            if not healthy:
                raise httpx.ReadTimeout("cancel status unknown")
            return httpx.Response(202, json={"schema_version": 1, "status": "cancelling"})
        return httpx.Response(200, content=encoded(event(6, "run.failed", error={"code": "deadline_exceeded"})))

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    await db.bind_powercore_run(ctx.chat_id, "run_long", "session_bg", [], conversational=False)
    await db.request_powercore_cancel("run_long")
    await backend.restore_background()

    async def backed_off():
        return (await db.get_powercore_run("run_long"))["observe_attempts"] >= 1

    await spin_until(backed_off)
    retry_at = (await db.get_powercore_run("run_long"))["observe_retry_at"]
    assert retry_at > time.time()
    await backend.close()
    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    before = len(requests)
    try:
        await backend.restore_background()
        await asyncio.sleep(0.03)
        assert len(requests) == before
        assert (await db.get_powercore_run("run_long"))["observe_retry_at"] == retry_at
        healthy = True
        await db.defer_powercore_observation("run_long", 0)

        async def terminal():
            row = await db.get_powercore_run("run_long")
            return row["status"] == "failed" and row["reply_cursor"] == 6

        await spin_until(terminal)
        row = await db.get_powercore_run("run_long")
        assert json.loads(row["progress_json"])["error_code"] == "deadline_exceeded"
        assert await db.get_powercore_session(ctx.chat_id) is None
        assert len(await db.get_recent_messages(ctx.chat_id)) == 1
    finally:
        await backend.close()


async def test_stalled_background_cancel_has_a_bounded_attempt(db, monkeypatch):
    from xclaw import powercore

    monkeypatch.setattr(powercore, "_BACKGROUND_POLL_SECONDS", 0.005)
    monkeypatch.setattr(powercore, "_CANCEL_TIMEOUT_SECONDS", 0.02, raising=False)

    async def server(request):
        assert request.url.path.endswith("/cancel")
        await asyncio.Event().wait()

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    await db.bind_powercore_run(ctx.chat_id, "run_long", "session_bg", [], conversational=False)
    await db.request_powercore_cancel("run_long")
    try:
        await backend.restore_background()

        async def retried():
            return (await db.get_powercore_run("run_long"))["observe_attempts"] > 0

        await spin_until(retried)
        assert (await db.get_powercore_run("run_long"))["status"] == "queued"
    finally:
        await backend.close()


async def test_old_foreground_and_background_submission_binding_are_not_double_observed(db, monkeypatch):
    from xclaw import powercore

    monkeypatch.setattr(powercore, "_BACKGROUND_POLL_SECONDS", 0.005)
    requests = []

    def server(request):
        requests.append(request)
        if request.method == "POST":
            return accepted()
        return httpx.Response(200, content=encoded(event(1, "run.completed", response="Once")))

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    ctx = await context(db, backend)
    await db.bind_powercore_run(ctx.chat_id, "run_old_front", "session_front", [])
    await db.clear_powercore_session(ctx.chat_id)
    request = {"idempotency_key": "original-key", "prompt": "background", "wall_time_ms": 600000}
    await db.save_powercore_submission(ctx.chat_id, "/runs", request, [], conversational=False, persist_message=True)
    await db.bind_powercore_run(ctx.chat_id, "run_long", "session_long", [], conversational=False, submission_id="original-key")
    try:
        await backend.restore_background()

        async def terminal():
            return (await db.get_powercore_run("run_long"))["reply_cursor"] == 1

        await spin_until(terminal)
        assert sum(r.method == "GET" for r in requests) == 1
        assert not any("run_old_front" in r.url.path for r in requests)
        assert len(await db.get_recent_messages(ctx.chat_id)) == 1
    finally:
        await backend.close()


async def test_terminal_replay_transport_diagnostics_never_become_the_final_reply(db, monkeypatch):
    from xclaw import powercore

    monkeypatch.setattr(powercore, "_BACKGROUND_POLL_SECONDS", 0.005)
    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(lambda request: accepted()))
    ctx = await context(db, backend)
    await db.bind_powercore_run(ctx.chat_id, "run_long", "session_bg", [], conversational=False)
    await db.update_powercore_run("run_long", "completed", 6)
    monkeypatch.setattr(backend, "_wait", AsyncMock(side_effect=PowerCoreError("Observer schema failure")))
    try:
        await backend.restore_background()

        async def attempted():
            row = await db.get_powercore_run("run_long")
            return row["observe_attempts"] > 0 or row["reply_cursor"] == 6

        await spin_until(attempted)
        row = await db.get_powercore_run("run_long")
        assert row["reply"] is None
        assert row["observe_attempts"] > 0
        assert await db.get_recent_messages(ctx.chat_id) == []
    finally:
        await backend.close()
