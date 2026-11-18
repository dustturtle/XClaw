"""Legacy formatter cache must not cross turns with identical assistant text."""
import hashlib
from types import SimpleNamespace

import pytest

from xclaw.ui_evidence import save_evidence, save_presentation, load_presentation


@pytest.mark.asyncio
async def test_repeated_text_from_another_model_invalidates_cached_layout(tmp_path):
    db = SimpleNamespace(_path=tmp_path / 'test.db')
    text = 'Answer reused verbatim.'
    digest = hashlib.sha256(text.encode()).hexdigest()
    await save_evidence(db, 41, text, 'first', [], model='codex/fixture-a')
    await save_presentation(db, 41, digest, 'first layout')
    assert await load_presentation(db, 41, digest) == 'first layout'
    await save_evidence(db, 41, text, 'second', [], model='codex/fixture-b')
    assert await load_presentation(db, 41, digest) is None


@pytest.mark.asyncio
async def test_legacy_generate_does_not_rebind_between_admission_and_sse(db):
    from xclaw.openui import GenerateRequest, OpenUITools, create_openui_router
    from unittest.mock import AsyncMock
    async with db.conn.execute("INSERT INTO chats(channel,external_chat_id) VALUES('web','race')") as cur:
        cid = cur.lastrowid
    text = 'same body'
    digest = hashlib.sha256(text.encode()).hexdigest()
    ctx = SimpleNamespace(db=db, chat_id=cid)
    service = OpenUITools(None, AsyncMock(return_value=ctx))
    router = create_openui_router(service, lambda: None, lambda chat, auth: chat)
    endpoint = next(r.endpoint for r in router.routes if r.path.endswith('/generate'))
    await db.save_message(cid, 'assistant', text, response_model='codex/fixture-a')
    await save_evidence(db, cid, text, 'a', [], model='codex/fixture-a')
    response = await endpoint(GenerateRequest(chat_id='race', reply_hash=digest), '')
    await db.save_message(cid, 'assistant', text, response_model='codex/fixture-b')
    await save_evidence(db, cid, text, 'b', [], model='codex/fixture-b')
    await save_presentation(db, cid, digest, 'WRONG-TURN')
    events = [e async for e in response.body_iterator]
    assert 'WRONG-TURN' not in str(events)
    assert any(e['event'] == 'error' for e in events)
