"""Tests for the tool base classes and basic tools (path_guard, file_tools, etc.)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from xclaw.tools import RiskLevel, ToolContext, ToolRegistry, ToolResult
from xclaw.tools.path_guard import assert_path_safe, is_path_safe
from xclaw.tools.file_tools import ReadFileTool, WriteFileTool
from xclaw.tools.memory_tools import (
    ReadMemoryTool,
    StructuredMemoryReadTool,
    StructuredMemoryUpdateTool,
    WriteMemoryTool,
)
from xclaw.tools.watchlist import WatchlistManageTool
from xclaw.tools.portfolio import PortfolioManageTool
from xclaw.tools.schedule import (
    CancelScheduledTaskTool,
    ListScheduledTasksTool,
    ScheduleTaskTool,
)


# ── Path guard ────────────────────────────────────────────────────────────────

def test_safe_path():
    assert is_path_safe("/home/user/documents/report.txt") is True
    assert is_path_safe("./data/notes.md") is True


def test_blocked_paths():
    assert is_path_safe("/home/user/.ssh/id_rsa") is False
    assert is_path_safe("/home/user/.aws/credentials") is False
    assert is_path_safe("../../.env") is False
    assert is_path_safe("/etc/shadow") is False
    assert is_path_safe("secrets.json") is False


def test_assert_path_safe_raises():
    with pytest.raises(PermissionError):
        assert_path_safe("/home/.ssh/id_rsa")


# ── Tool registry ─────────────────────────────────────────────────────────────

def test_registry_register_and_get():
    registry = ToolRegistry()
    tool = WebSearchToolStub()
    registry.register(tool)
    assert registry.get("stub_search") is tool


def test_registry_duplicate_raises():
    registry = ToolRegistry()
    tool = WebSearchToolStub()
    registry.register(tool)
    with pytest.raises(ValueError):
        registry.register(tool)


def test_registry_get_definitions():
    registry = ToolRegistry()
    registry.register(WebSearchToolStub())
    defs = registry.get_definitions()
    assert len(defs) == 1
    assert defs[0].name == "stub_search"


# ── File tools ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_and_read_file(tmp_path: Path):
    ctx = _make_ctx()
    write_tool = WriteFileTool()
    read_tool = ReadFileTool()
    target = str(tmp_path / "test.txt")

    result = await write_tool.execute({"path": target, "content": "Hello XClaw!"}, ctx)
    assert not result.is_error
    assert "成功写入" in result.content

    result2 = await read_tool.execute({"path": target}, ctx)
    assert not result2.is_error
    assert result2.content == "Hello XClaw!"


@pytest.mark.asyncio
async def test_read_nonexistent_file():
    ctx = _make_ctx()
    tool = ReadFileTool()
    result = await tool.execute({"path": "/nonexistent/path/file.txt"}, ctx)
    assert result.is_error


@pytest.mark.asyncio
async def test_write_blocked_path():
    ctx = _make_ctx()
    tool = WriteFileTool()
    result = await tool.execute({"path": "~/.ssh/authorized_keys", "content": "evil"}, ctx)
    assert result.is_error


# ── Memory tools ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_memory_tools_no_memory_system():
    """Memory tools should return error when file_memory is None."""
    ctx = _make_ctx()
    tool = ReadMemoryTool()
    result = await tool.execute({}, ctx)
    assert result.is_error


@pytest.mark.asyncio
async def test_write_read_memory(tmp_path: Path):
    from xclaw.memory import FileMemory

    file_mem = FileMemory(tmp_path / "groups")
    ctx = _make_ctx(file_memory=file_mem)

    write_tool = WriteMemoryTool()
    read_tool = ReadMemoryTool()

    await write_tool.execute({"content": "用户偏好：价值投资"}, ctx)
    result = await read_tool.execute({}, ctx)
    assert "价值投资" in result.content


@pytest.mark.asyncio
async def test_structured_memory_tools(db, tmp_path):
    from xclaw.memory import StructuredMemory

    chat_id = await db.get_or_create_chat("web", "mem_test")
    struct_mem = StructuredMemory(db)
    ctx = _make_ctx(db=db, structured_memory=struct_mem, chat_id=chat_id)

    # Add a memory
    update_tool = StructuredMemoryUpdateTool()
    result = await update_tool.execute({"content": "用户关注新能源板块", "category": "关注"}, ctx)
    assert not result.is_error

    # Read it back
    read_tool = StructuredMemoryReadTool()
    result2 = await read_tool.execute({}, ctx)
    assert "新能源" in result2.content


# ── Watchlist tools ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_watchlist_tool(db):
    chat_id = await db.get_or_create_chat("web", "wl_tool_user")
    ctx = _make_ctx(db=db, chat_id=chat_id)
    tool = WatchlistManageTool()

    # Add
    r = await tool.execute({"action": "add", "symbol": "600519", "market": "CN", "name": "贵州茅台"}, ctx)
    assert not r.is_error

    # List
    r2 = await tool.execute({"action": "list"}, ctx)
    assert "600519" in r2.content

    # Remove
    r3 = await tool.execute({"action": "remove", "symbol": "600519", "market": "CN"}, ctx)
    assert not r3.is_error


# ── Portfolio tools ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_portfolio_tool(db):
    chat_id = await db.get_or_create_chat("web", "port_tool_user")
    ctx = _make_ctx(db=db, chat_id=chat_id)
    tool = PortfolioManageTool()

    # Buy
    r = await tool.execute({"action": "buy", "symbol": "000001", "market": "CN", "shares": 100, "price": 10.5}, ctx)
    assert not r.is_error

    # View
    r2 = await tool.execute({"action": "view"}, ctx)
    assert "000001" in r2.content

    # Sell
    r3 = await tool.execute({"action": "sell", "symbol": "000001", "market": "CN"}, ctx)
    assert not r3.is_error


# ── Schedule tools ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_schedule_tool(db):
    chat_id = await db.get_or_create_chat("web", "sched_tool_user")
    ctx = _make_ctx(db=db, chat_id=chat_id)

    create_tool = ScheduleTaskTool()
    list_tool = ListScheduledTasksTool()
    cancel_tool = CancelScheduledTaskTool()

    r = await create_tool.execute(
        {"description": "每日行情", "prompt": "获取大盘今日行情", "cron_expression": "0 15 * * 1-5"},
        ctx,
    )
    assert not r.is_error
    assert "id=" in r.content

    r2 = await list_tool.execute({}, ctx)
    assert "每日行情" in r2.content

    # Extract task id
    import re
    match = re.search(r"id=(\d+)", r.content)
    assert match
    task_id = int(match.group(1))

    r3 = await cancel_tool.execute({"task_id": task_id}, ctx)
    assert not r3.is_error


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_ctx(db=None, chat_id=1, file_memory=None, structured_memory=None) -> ToolContext:
    return ToolContext(
        chat_id=chat_id,
        channel="web",
        db=db,
        settings=None,
        file_memory=file_memory,
        structured_memory=structured_memory,
    )


class WebSearchToolStub:
    """Minimal stub implementing Tool interface for registry tests."""
    name = "stub_search"
    description = "stub"
    risk_level = RiskLevel.LOW

    @property
    def parameters(self):
        return {"type": "object", "properties": {}, "required": []}

    def to_definition(self):
        from xclaw.llm_types import ToolDefinition
        return ToolDefinition(name=self.name, description=self.description, input_schema=self.parameters)

    async def execute(self, params, context):
        return ToolResult(content="stub result")
