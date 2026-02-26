"""Tests for the Agent Engine (agent_loop) using a mock LLM."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from xclaw.agent_engine import (
    AgentContext,
    _build_system_prompt,
    _messages_from_serializable,
    _messages_to_serializable,
    agent_loop,
)
from xclaw.db import Database
from xclaw.llm import LLMProvider
from xclaw.llm_types import (
    LLMEvent,
    LLMResponse,
    Message,
    StopReason,
    TextBlock,
    ToolDefinition,
    ToolUseBlock,
    ToolResultBlock,
)
from xclaw.memory import FileMemory, StructuredMemory
from xclaw.tools import ToolContext, ToolRegistry, ToolResult


# ── Mock LLM ──────────────────────────────────────────────────────────────────

class MockLLMProvider(LLMProvider):
    """LLM provider that returns pre-configured responses."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self._calls: list[dict] = []

    async def chat(
        self,
        messages,
        tools=None,
        system=None,
        max_tokens=4096,
    ) -> LLMResponse:
        self._calls.append({"messages": messages, "tools": tools, "system": system})
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(
            stop_reason=StopReason.end_turn,
            content=[TextBlock(text="默认回复")],
        )

    async def chat_stream(self, messages, tools=None, system=None, max_tokens=4096):
        response = await self.chat(messages, tools, system, max_tokens)
        yield response


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_agent_ctx(db: Database, llm: LLMProvider, tmp_path: Path) -> AgentContext:
    file_memory = FileMemory(tmp_path / "groups")
    struct_memory = StructuredMemory(db)
    tools = ToolRegistry()
    return AgentContext(
        chat_id=1,
        channel="web",
        db=db,
        llm=llm,
        tools=tools,
        file_memory=file_memory,
        structured_memory=struct_memory,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_simple_chat(db: Database, tmp_path: Path):
    """Agent loop should return the LLM text response."""
    await db.get_or_create_chat("web", "web_default")
    # Pre-create chat row with id=1
    # Reload with correct id
    chat_id = await db.get_or_create_chat("web", "agent_simple_test")

    llm = MockLLMProvider([
        LLMResponse(
            stop_reason=StopReason.end_turn,
            content=[TextBlock(text="你好！我是 XClaw。")],
        )
    ])
    file_memory = FileMemory(tmp_path / "groups")
    struct_memory = StructuredMemory(db)
    tools = ToolRegistry()
    ctx = AgentContext(
        chat_id=chat_id,
        channel="web",
        db=db,
        llm=llm,
        tools=tools,
        file_memory=file_memory,
        structured_memory=struct_memory,
    )
    reply = await agent_loop(ctx, "你好")
    assert "你好" in reply or "XClaw" in reply


@pytest.mark.asyncio
async def test_quick_memory_path(db: Database, tmp_path: Path):
    """'记住: ...' input should be stored without calling LLM."""
    chat_id = await db.get_or_create_chat("web", "mem_path_user")
    llm = MockLLMProvider([])  # Should not be called
    file_memory = FileMemory(tmp_path / "groups")
    struct_memory = StructuredMemory(db)
    tools = ToolRegistry()
    ctx = AgentContext(
        chat_id=chat_id, channel="web", db=db, llm=llm,
        tools=tools, file_memory=file_memory, structured_memory=struct_memory,
    )
    reply = await agent_loop(ctx, "记住: 用户偏好价值投资")
    assert "价值投资" in reply
    # LLM should NOT have been called
    assert len(llm._calls) == 0

    # Verify it was persisted
    memories = await struct_memory.get_all(chat_id)
    assert any("价值投资" in m["content"] for m in memories)


@pytest.mark.asyncio
async def test_tool_call_loop(db: Database, tmp_path: Path):
    """Agent loop should execute a tool and pass result back to LLM."""
    chat_id = await db.get_or_create_chat("web", "tool_loop_user")

    # First LLM response: tool_use
    first_response = LLMResponse(
        stop_reason=StopReason.tool_use,
        content=[
            TextBlock(text="让我查一下行情。"),
            ToolUseBlock(id="tool1", name="fake_tool", input={"symbol": "600519"}),
        ],
    )
    # Second LLM response: end_turn
    second_response = LLMResponse(
        stop_reason=StopReason.end_turn,
        content=[TextBlock(text="查询完成，当前价格为 1800 元。")],
    )
    llm = MockLLMProvider([first_response, second_response])

    # Register a fake tool
    from xclaw.tools import Tool

    class FakeTool(Tool):
        @property
        def name(self): return "fake_tool"
        @property
        def description(self): return "Fake tool"
        @property
        def parameters(self): return {"type": "object", "properties": {}, "required": []}
        async def execute(self, params, context): return ToolResult(content="价格: 1800")

    tools = ToolRegistry()
    tools.register(FakeTool())

    file_memory = FileMemory(tmp_path / "groups")
    struct_memory = StructuredMemory(db)
    ctx = AgentContext(
        chat_id=chat_id, channel="web", db=db, llm=llm,
        tools=tools, file_memory=file_memory, structured_memory=struct_memory,
    )
    reply = await agent_loop(ctx, "查一下贵州茅台")
    assert "1800" in reply or "查询" in reply
    # LLM should have been called twice
    assert len(llm._calls) == 2


@pytest.mark.asyncio
async def test_session_persistence(db: Database, tmp_path: Path):
    """Agent loop should save and restore sessions."""
    chat_id = await db.get_or_create_chat("web", "session_persist")

    llm = MockLLMProvider([
        LLMResponse(stop_reason=StopReason.end_turn, content=[TextBlock(text="第一条回复")]),
        LLMResponse(stop_reason=StopReason.end_turn, content=[TextBlock(text="第二条回复")]),
    ])
    file_memory = FileMemory(tmp_path / "groups")
    struct_memory = StructuredMemory(db)
    tools = ToolRegistry()
    ctx = AgentContext(
        chat_id=chat_id, channel="web", db=db, llm=llm,
        tools=tools, file_memory=file_memory, structured_memory=struct_memory,
    )

    await agent_loop(ctx, "第一条消息")
    # Second call – should restore session from DB
    llm2 = MockLLMProvider([
        LLMResponse(stop_reason=StopReason.end_turn, content=[TextBlock(text="第二条回复")]),
    ])
    ctx2 = AgentContext(
        chat_id=chat_id, channel="web", db=db, llm=llm2,
        tools=tools, file_memory=file_memory, structured_memory=struct_memory,
    )
    reply2 = await agent_loop(ctx2, "第二条消息")
    assert "第二条" in reply2

    # The second LLM call should have received at least 2 messages (from session)
    call_messages = llm2._calls[0]["messages"]
    assert len(call_messages) >= 2


def test_messages_serialisation_roundtrip():
    """_messages_to_serializable → _messages_from_serializable should be identity."""
    original = [
        Message(role="user", content="Hello"),
        Message(
            role="assistant",
            content=[
                TextBlock(text="I'll look that up."),
                ToolUseBlock(id="t1", name="stock_quote", input={"symbol": "AAPL"}),
            ],
        ),
        Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="t1", content="Price: 180")],
        ),
    ]
    serialised = _messages_to_serializable(original)
    restored = _messages_from_serializable(serialised)

    assert len(restored) == len(original)
    assert restored[0].role == "user"
    assert restored[0].text_content() == "Hello"


def test_build_system_prompt_includes_memories():
    from xclaw.agent_engine import AgentContext
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.settings = None
    memories = [{"content": "用户偏好价值投资", "category": "偏好"}]
    prompt = _build_system_prompt(ctx, memories=memories, file_memory_content="# Notes\n- 关注新能源")
    assert "价值投资" in prompt
    assert "关注新能源" in prompt
