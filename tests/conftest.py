"""Shared pytest fixtures for XClaw tests."""

from __future__ import annotations

import asyncio
import pytest
import pytest_asyncio
from pathlib import Path
import tempfile

from xclaw.db import Database


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """Return a temporary data directory."""
    return tmp_path


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> Database:
    """Provide a connected in-memory-like Database for each test."""
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def saved_legacy_button():
    """Create a stored presentation for tests exercising confirmed write receipts."""
    import hashlib
    import json
    from xclaw.legacy_ui_actions import legacy_request_id
    from xclaw.openui import ToolRequest
    from xclaw.ui_evidence import save_evidence, save_presentation

    async def save(db, external_id, arguments, label="确认添加"):
        token_hash = hashlib.sha256(b"acceptance-token").hexdigest()
        reply = "Synthetic action: " + json.dumps(arguments, sort_keys=True)
        reply_hash = hashlib.sha256(reply.encode()).hexdigest()
        message_id = await db.save_message(1, "assistant", reply)
        await save_evidence(db, 1, reply, "fixture", [])
        source = ('m=Mutation("watchlist_manage",' + json.dumps(arguments) + ')\n'
                  'root=Col([Button(' + json.dumps(label, ensure_ascii=False) + ',Action([@Run(m)]))])')
        await save_presentation(db, 1, reply_hash, source)
        request = ToolRequest(chat_id=external_id, message_id=message_id, name="watchlist_manage",
                              arguments=arguments, reply_hash=reply_hash, action_label=label,
                              request_id=legacy_request_id(token_hash, external_id, reply_hash,
                                                           "watchlist_manage", arguments))
        return request, token_hash

    return save
