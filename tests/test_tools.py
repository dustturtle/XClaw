"""Tests for the tool base classes and basic tools (path_guard, file_tools, etc.)."""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock

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
    scheduler = MagicMock()
    ctx = _make_ctx(
        db=db,
        chat_id=chat_id,
        settings=SimpleNamespace(timezone="Asia/Shanghai"),
        scheduler=scheduler,
    )

    create_tool = ScheduleTaskTool()
    list_tool = ListScheduledTasksTool()
    cancel_tool = CancelScheduledTaskTool()

    r = await create_tool.execute(
        {"description": "每日行情", "prompt": "获取大盘今日行情", "cron_expression": "0 15 * * 1-5"},
        ctx,
    )
    assert not r.is_error
    assert "id=" in r.content
    scheduler.schedule_from_db_row.assert_called_once()

    r2 = await list_tool.execute({}, ctx)
    assert "每日行情" in r2.content

    # Extract task id
    import re
    match = re.search(r"id=(\d+)", r.content)
    assert match
    task_id = int(match.group(1))

    r3 = await cancel_tool.execute({"task_id": task_id}, ctx)
    assert not r3.is_error
    scheduler.remove_task.assert_called_once_with(task_id)


@pytest.mark.asyncio
async def test_schedule_tool_normalizes_once_run_time(db):
    chat_id = await db.get_or_create_chat("web", "sched_tool_once_user")
    scheduler = MagicMock()
    ctx = _make_ctx(
        db=db,
        chat_id=chat_id,
        settings=SimpleNamespace(timezone="Asia/Shanghai"),
        scheduler=scheduler,
    )
    create_tool = ScheduleTaskTool()

    result = await create_tool.execute(
        {
            "description": "收盘提醒",
            "prompt": "查询收盘价",
            "run_once_at": "2099-01-01 15:00",
        },
        ctx,
    )

    assert not result.is_error
    tasks = await db.get_active_tasks()
    created = [t for t in tasks if t["chat_id"] == chat_id]
    assert len(created) == 1
    assert created[0]["next_run_at"] == "2099-01-01T15:00:00+08:00"
    scheduler.schedule_from_db_row.assert_called_once()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_ctx(
    db=None,
    chat_id=1,
    file_memory=None,
    structured_memory=None,
    settings=None,
    scheduler=None,
) -> ToolContext:
    return ToolContext(
        chat_id=chat_id,
        channel="web",
        db=db,
        settings=settings,
        file_memory=file_memory,
        structured_memory=structured_memory,
        scheduler=scheduler,
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


# ── Bash tool ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bash_tool_disabled_by_default():
    """BashTool should refuse to run when bash_enabled is False."""
    from xclaw.tools.bash_tool import BashTool

    tool = BashTool()
    assert tool.risk_level == RiskLevel.HIGH

    ctx = _make_ctx()  # settings=None
    result = await tool.execute({"command": "echo hello"}, ctx)
    assert result.is_error
    assert "未启用" in result.content


@pytest.mark.asyncio
async def test_bash_tool_disabled_via_settings():
    from types import SimpleNamespace
    from xclaw.tools.bash_tool import BashTool

    settings = SimpleNamespace(bash_enabled=False)
    ctx = _make_ctx(settings=settings)
    result = await BashTool().execute({"command": "echo hi"}, ctx)
    assert result.is_error


@pytest.mark.asyncio
async def test_bash_tool_runs_command():
    from types import SimpleNamespace
    from xclaw.tools.bash_tool import BashTool

    settings = SimpleNamespace(bash_enabled=True)
    ctx = _make_ctx(settings=settings)
    result = await BashTool().execute({"command": "echo xclaw_test"}, ctx)
    assert not result.is_error
    assert "xclaw_test" in result.content


@pytest.mark.asyncio
async def test_bash_tool_exit_nonzero():
    from types import SimpleNamespace
    from xclaw.tools.bash_tool import BashTool

    settings = SimpleNamespace(bash_enabled=True)
    ctx = _make_ctx(settings=settings)
    result = await BashTool().execute({"command": "exit 1"}, ctx)
    assert result.is_error


@pytest.mark.asyncio
async def test_bash_tool_empty_command():
    from types import SimpleNamespace
    from xclaw.tools.bash_tool import BashTool

    settings = SimpleNamespace(bash_enabled=True)
    ctx = _make_ctx(settings=settings)
    result = await BashTool().execute({"command": ""}, ctx)
    assert result.is_error


# ── Sub-agent tool ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sub_agent_no_llm():
    """SubAgentTool should return error when LLM is not available."""
    from xclaw.tools.sub_agent import SubAgentTool

    registry = ToolRegistry()
    tool = SubAgentTool(registry)
    ctx = _make_ctx()  # no llm attribute
    result = await tool.execute({"task": "查大盘"}, ctx)
    assert result.is_error


@pytest.mark.asyncio
async def test_sub_agent_empty_task():
    from xclaw.tools.sub_agent import SubAgentTool

    registry = ToolRegistry()
    tool = SubAgentTool(registry)
    ctx = _make_ctx()
    result = await tool.execute({"task": ""}, ctx)
    assert result.is_error


@pytest.mark.asyncio
async def test_sub_agent_restricted_tools():
    """SubAgentTool should only include allowed tools in the sub-registry."""
    from xclaw.tools.sub_agent import SubAgentTool, _ALLOWED_SUB_AGENT_TOOLS
    from xclaw.tools.web_search import WebSearchTool
    from xclaw.tools.portfolio import PortfolioManageTool

    registry = ToolRegistry()
    registry.register(WebSearchTool())
    registry.register(PortfolioManageTool())  # NOT in allowed set

    tool = SubAgentTool(registry)
    # The tool should be registered without error
    assert tool.name == "sub_agent"
    # portfolio_manage should not be in allowed set
    assert "portfolio_manage" not in _ALLOWED_SUB_AGENT_TOOLS
    assert "web_search" in _ALLOWED_SUB_AGENT_TOOLS
