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
