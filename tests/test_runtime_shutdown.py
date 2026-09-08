"""The runtime must close durable observers and SQLite on server cancellation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from xclaw import runtime
from xclaw.config import Settings
from xclaw.db import Database
from xclaw.tools import ToolRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_runtime_closes_resources_when_web_server_exits(tmp_path, monkeypatch, cancelled):
    settings = Settings(data_dir=str(tmp_path), agent_backend="powercore", powercore_mcp_token="fixture-token")
    database = Database(settings.db_path)
    backend = SimpleNamespace(settings=settings, db=database, check_ready=AsyncMock(), close=AsyncMock(), restore_background=AsyncMock())
    scheduler = SimpleNamespace(start=Mock(), stop=Mock())
    monkeypatch.setattr(runtime, "_setup_logging", lambda settings: None)
    monkeypatch.setattr(runtime, "Database", lambda path: database)
    monkeypatch.setattr(runtime, "create_provider", Mock())
    monkeypatch.setattr(runtime, "_build_tool_registry", lambda settings: ToolRegistry())
    monkeypatch.setattr("xclaw.powercore.PowerCoreClient", lambda settings, db: backend)
    monkeypatch.setattr("xclaw.scheduler.TaskScheduler", lambda **kwargs: scheduler)
    server = SimpleNamespace(serve=AsyncMock(side_effect=asyncio.CancelledError if cancelled else None))
    monkeypatch.setattr(runtime.uvicorn, "Server", lambda config: server)
    try:
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await runtime.run(settings)
        else:
            await runtime.run(settings)
        backend.close.assert_awaited_once()
        scheduler.stop.assert_called_once()
        assert database._conn is None
    finally:
        await database.close()
