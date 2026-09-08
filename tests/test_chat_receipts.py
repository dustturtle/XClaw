import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from xclaw.channels.web import create_web_app
from xclaw.receipts import ReceiptStore, current_receipt
from xclaw.responses import DirectReply
from xclaw.responses import RejectedReply


RID = "a" * 32


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")


async def settle(app):
    if app.state.chat_tasks:
        await asyncio.gather(*list(app.state.chat_tasks))


@pytest.mark.asyncio
async def test_concurrent_retry_and_restart_replay_one_durable_result(db):
    release = asyncio.Event()
    called = 0

    async def handler(cid, text):
        nonlocal called
        called += 1
        await release.wait()
        return DirectReply("reset complete")

    app = create_web_app(handler, db=db)
    payload = {"chat_id": "room", "message": "/reset", "request_id": RID}
    async with client(app) as c:
        responses = await asyncio.gather(*(c.post("/api/chat", json=payload) for _ in range(8)))
        assert {r.status_code for r in responses} == {202}
        assert called == 1
        assert (await c.get(f"/api/chat/receipts/{RID}?chat_id=room")).json()["status"] == "processing"
        changed = await c.post("/api/chat", json={**payload, "message": "different"})
        assert changed.status_code == 409
        release.set()
        await settle(app)
        assert (await c.get(f"/api/chat/receipts/{RID}?chat_id=room")).json()["reply"] == "reset complete"
    replacement = AsyncMock(return_value="must not run")
    restarted = create_web_app(replacement, db=db)
    async with client(restarted) as c:
        assert (await c.post("/api/chat", json=payload)).status_code == 202
        result = await c.get(f"/api/chat/receipts/{RID}?chat_id=room")
        assert result.json()["reply"] == "reset complete"
        assert result.headers["cache-control"] == "no-store"
    replacement.assert_not_awaited()


@pytest.mark.asyncio
async def test_receipts_are_scoped_to_credentials_and_chat(db):
    app = create_web_app(AsyncMock(return_value="private answer"), db=db)
    async with client(app) as c:
        payload = {"chat_id": "room", "message": "private text", "request_id": RID}
        assert (await c.post("/api/chat", json=payload, headers={"Authorization": "Bearer owner"})).status_code == 202
        await settle(app)
        for chat, token in [("room", "other"), ("other-room", "owner")]:
            response = await c.get(f"/api/chat/receipts/{RID}", params={"chat_id": chat},
                                   headers={"Authorization": "Bearer " + token})
            assert response.status_code == 404
            assert "private" not in response.text
    guarded = create_web_app(AsyncMock(), db=db, auth_token="real-token")
    async with client(guarded) as c:
        assert (await c.get(f"/api/chat/receipts/{RID}")).status_code == 401


@pytest.mark.asyncio
async def test_restart_without_effect_record_is_explicitly_interrupted_and_never_replayed(db):
    store = ReceiptStore(db)
    payload = {"chat_id": "room", "message": "do something", "attachments": [], "execution_mode": None}
    await store.admit(store.scope("room", ""), RID, payload)
    handler = AsyncMock()
    restarted = create_web_app(handler, db=db)
    async with client(restarted) as c:
        result = await c.get(f"/api/chat/receipts/{RID}?chat_id=room")
        assert result.json()["status"] == "interrupted"
        assert (await c.post("/api/chat", json={**payload, "request_id": RID})).status_code == 202
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_id_can_be_retried_once_with_same_id(db):
    handler = AsyncMock(return_value="answer")
    app = create_web_app(handler, db=db)
    async with client(app) as c:
        assert (await c.get(f"/api/chat/receipts/{RID}?chat_id=room")).status_code == 404
        for _ in range(2):
            await c.post("/api/chat", json={"chat_id": "room", "message": "test", "request_id": RID})
        await settle(app)
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_linked_run_survives_restart_and_uses_its_own_reply_not_latest_chat(db):
    store = ReceiptStore(db)
    receipt, _ = await store.admit(store.scope("room", ""), RID, {"message": "first"})
    chat = await db.get_or_create_chat("web", "room")
    token = current_receipt.set(receipt["id"])
    try:
        await db.save_powercore_submission(
            chat, "/runs", {"idempotency_key": "pc-first", "wall_time_ms": 600000}, [],
            conversational=True, persist_message=True,
        )
    finally:
        current_receipt.reset(token)
    await db.bind_powercore_run(chat, "run_first", "session_one", [], submission_id="pc-first")
    await db.clear_powercore_submission("pc-first")
    await db.update_powercore_run("run_first", "completed", 3)
    await db.save_powercore_reply("run_first", "first answer")
    await db.bind_powercore_run(chat, "run_later", "session_one", [])
    await db.update_powercore_run("run_later", "completed", 5)
    await db.save_powercore_reply("run_later", "unrelated answer")
    restarted = ReceiptStore(db)
    fresh = await restarted.lookup(store.scope("room", ""), RID)
    result = await restarted.project(fresh)
    assert result["reply"] == "first answer"
    assert result["progress"]["run_id"] == "run_first"
    # The receipt remains immutable even if the underlying Run later changes.
    await db.save_powercore_reply("run_first", "later input answer")
    assert (await restarted.project(await restarted.lookup(store.scope("room", ""), RID)))["reply"] == "first answer"


@pytest.mark.asyncio
async def test_input_receipt_does_not_replay_old_waiting_reply(db):
    store = ReceiptStore(db)
    receipt, _ = await store.admit("scope", RID, {})
    chat = await db.get_or_create_chat("web", "room")
    await db.bind_powercore_run(chat, "run_input", "session_one", [])
    await db.update_powercore_run("run_input", "waiting_input", 3)
    await db.save_powercore_reply("run_input", "old question")
    token = current_receipt.set(receipt["id"])
    try:
        await db.save_powercore_submission(
            chat, "/runs/run_input/input", {}, [], conversational=True, persist_message=True,
            kind="input", run_id="run_input", cursor=3, submission_id="input:run_input:question",
        )
    finally:
        current_receipt.reset(token)
    result = await store.project(await store.lookup("scope", RID))
    assert result["status"] == "processing"
    assert "reply" not in result
    await db.update_powercore_run("run_input", "completed", 6)
    await db.save_powercore_reply("run_input", "new answer")
    assert (await store.project(await store.lookup("scope", RID)))["reply"] == "new answer"


@pytest.mark.asyncio
async def test_logs_and_receipt_fingerprints_never_store_payload_or_exception_secrets(db):
    async def broken(*args):
        raise RuntimeError("private-secret-body")

    app = create_web_app(broken, db=db)
    async with client(app) as c:
        await c.post("/api/chat", json={"request_id": RID, "message": "private-question"})
        await settle(app)
        response = await c.get(f"/api/chat/receipts/{RID}")
        assert "private-" not in response.text
    logs = app.state.receipts.journal.path.read_text()
    assert "private-" not in logs
    async with db.conn.execute("SELECT * FROM web_receipts") as cur:
        records = await cur.fetchall()
    assert "private-" not in str([dict(r) for r in records])


@pytest.mark.asyncio
async def test_readiness_detects_dependency_failure_without_leaking_details(db):
    app = create_web_app(AsyncMock(), db=db,
                         readiness_handler=AsyncMock(side_effect=RuntimeError("private-secret")))
    async with client(app) as c:
        assert (await c.get("/health")).status_code == 200
        result = await c.get("/health/ready")
        assert result.status_code == 503
        assert result.json() == {"status": "unavailable"}


@pytest.mark.asyncio
async def test_known_rejection_is_not_a_completed_receipt(db):
    app = create_web_app(AsyncMock(return_value=RejectedReply("previous run busy")), db=db)
    async with client(app) as c:
        await c.post("/api/chat", json={"request_id": RID, "message": "another"})
        await settle(app)
        assert (await c.get(f"/api/chat/receipts/{RID}")).json()["status"] == "failed"


@pytest.mark.asyncio
async def test_receipt_visible_before_task_dispatch_is_not_misclassified_as_interrupted(db):
    store = ReceiptStore(db)
    receipt, _ = await store.admit("scope", RID, {})
    store.active.clear()
    assert (await store.project(receipt))["status"] == "processing"
    assert (await ReceiptStore(db).project(receipt))["status"] == "interrupted"


@pytest.mark.asyncio
async def test_real_powercore_adapter_links_and_replays_receipt_without_new_model_run(db):
    from xclaw.agent_engine import AgentContext, agent_loop
    from xclaw.config import Settings
    from xclaw.powercore import PowerCoreClient
    from xclaw.tools import ToolRegistry

    calls = []

    def remote(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(202, json={
                "schema_version": 1, "run_id": "run_receipt", "session_id": "session_receipt",
                "wall_time_ms": 600000,
            })
        body = {"schema_version": 1, "sequence": 3, "run_id": "run_receipt",
                "type": "run.completed", "response": "durable model answer"}
        return httpx.Response(200, content=f"data: {json.dumps(body)}\n\n")

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(remote))
    chat = await db.get_or_create_chat("web", "room")
    ctx = AgentContext(chat, "web", db, AsyncMock(), ToolRegistry(), settings=backend.settings, powercore=backend)
    app = create_web_app(lambda cid, text: agent_loop(ctx, text), db=db,
                         progress_handler=lambda cid, **kw: backend.progress(ctx, **kw))
    payload = {"chat_id": "room", "request_id": RID, "message": "question"}
    try:
        async with client(app) as c:
            await c.post("/api/chat", json=payload)
            await settle(app)
            response = await c.get(f"/api/chat/receipts/{RID}?chat_id=room")
            assert response.json()["reply"] == "durable model answer"
            assert response.json()["progress"]["run_id"] == "run_receipt"
            await c.post("/api/chat", json=payload)
        assert sum(r.method == "POST" for r in calls) == 1
        history = await db.get_recent_messages(chat)
        assert [r["role"] for r in history] == ["user", "assistant"]
    finally:
        await backend.close()
