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
    _micro_compact,
    _messages_from_serializable,
    _messages_to_serializable,
    _normalize_final_text,
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
from xclaw.tools import Tool, ToolContext, ToolRegistry, ToolResult
from xclaw.tools.sub_agent import SubAgentTool


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
async def test_reset_command_clears_session_without_calling_llm(db: Database, tmp_path: Path):
    chat_id = await db.get_or_create_chat("web", "reset_user")
    await db.save_session(chat_id, [{"role": "user", "content": "旧上下文"}])

    llm = MockLLMProvider([])  # Should not be called
    file_memory = FileMemory(tmp_path / "groups")
    struct_memory = StructuredMemory(db)
    tools = ToolRegistry()
    ctx = AgentContext(
        chat_id=chat_id, channel="web", db=db, llm=llm,
        tools=tools, file_memory=file_memory, structured_memory=struct_memory,
    )

    reply = await agent_loop(ctx, "/reset")
    assert "上下文已重置" in reply
    assert len(llm._calls) == 0
    assert await db.load_session(chat_id) is None


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


def test_build_system_prompt_includes_gap_routing_rule():
    ctx = MagicMock()
    ctx.settings = None
    prompt = _build_system_prompt(ctx)
    assert "stock_gap_analysis" in prompt
    assert "不要根据 stock_history 返回的K线自行手算缺口" in prompt


def test_normalize_final_text_fallback():
    assert _normalize_final_text("") == "抱歉，这一轮没有生成可展示的答复，请稍后重试。"
    assert _normalize_final_text("有内容") == "有内容"


def test_micro_compact_replaces_old_tool_results():
    messages = [
        Message(
            role="user",
            content=[
                ToolResultBlock(tool_use_id="t1", content="A" * 120),
                ToolResultBlock(tool_use_id="t2", content="B" * 120),
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(tool_use_id="t3", content="C" * 120),
                ToolResultBlock(tool_use_id="t4", content="D" * 120),
            ],
        ),
    ]

    _micro_compact(messages, keep_recent_tool_results=2)

    assert messages[0].content[0].content == "[已处理的工具结果，原内容已压缩]"
    assert messages[0].content[1].content == "[已处理的工具结果，原内容已压缩]"
    assert messages[1].content[0].content == "C" * 120
    assert messages[1].content[1].content == "D" * 120


@pytest.mark.asyncio
async def test_empty_end_turn_reply_uses_fallback(db: Database, tmp_path: Path):
    chat_id = await db.get_or_create_chat("web", "empty_reply_user")
    llm = MockLLMProvider([
        LLMResponse(
            stop_reason=StopReason.end_turn,
            content=[],
        )
    ])
    ctx = AgentContext(
        chat_id=chat_id,
        channel="web",
        db=db,
        llm=llm,
        tools=ToolRegistry(),
        file_memory=FileMemory(tmp_path / "groups"),
        structured_memory=StructuredMemory(db),
    )

    reply = await agent_loop(ctx, "测试空回复")

    assert reply == "抱歉，这一轮没有生成可展示的答复，请稍后重试。"


@pytest.mark.asyncio
async def test_agent_loop_forces_direct_answer_after_consecutive_tool_errors(db: Database, tmp_path: Path):
    chat_id = await db.get_or_create_chat("web", "error_stop_user")

    class AlwaysErrorTool(Tool):
        @property
        def name(self): return "always_error"
        @property
        def description(self): return "always error"
        @property
        def parameters(self): return {"type": "object", "properties": {}, "required": []}
        async def execute(self, params, context): return ToolResult(content="失败", is_error=True)

    llm = MockLLMProvider([
        LLMResponse(stop_reason=StopReason.tool_use, content=[ToolUseBlock(id="e1", name="always_error", input={})]),
        LLMResponse(stop_reason=StopReason.tool_use, content=[ToolUseBlock(id="e2", name="always_error", input={})]),
        LLMResponse(stop_reason=StopReason.tool_use, content=[ToolUseBlock(id="e3", name="always_error", input={})]),
        LLMResponse(stop_reason=StopReason.end_turn, content=[TextBlock(text="基于已有信息直接回答。")]),
    ])

    tools = ToolRegistry()
    tools.register(AlwaysErrorTool())
    ctx = AgentContext(
        chat_id=chat_id,
        channel="web",
        db=db,
        llm=llm,
        tools=tools,
        file_memory=FileMemory(tmp_path / "groups"),
        structured_memory=StructuredMemory(db),
    )

    reply = await agent_loop(ctx, "测试连续失败收口", max_iterations=10)

    assert reply == "基于已有信息直接回答。"
    assert len(llm._calls) == 4
    assert llm._calls[-1]["tools"] is None
    assert any(
        message.role == "user"
        and message.content == "多个工具已连续失败。请不要继续调用工具，基于当前已获得的信息直接回答用户；如果信息不足，请明确说明缺失点。"
        for message in llm._calls[-1]["messages"]
    )


@pytest.mark.asyncio
async def test_sub_agent_does_not_load_or_save_parent_session(db: Database):
    chat_id = await db.get_or_create_chat("web", "subagent_parent")
    parent_session = [
        {"role": "user", "content": "父级历史"},
        {"role": "assistant", "content": "父级答复"},
    ]
    await db.save_session(chat_id, parent_session)
    await db.save_message(chat_id, "user", "父级消息")

    llm = MockLLMProvider([
        LLMResponse(
            stop_reason=StopReason.end_turn,
            content=[TextBlock(text="子 Agent 完成")],
        )
    ])
    registry = ToolRegistry()
    sub_agent_tool = SubAgentTool(registry)

    result = await sub_agent_tool.execute(
        {"task": "只返回一个结论"},
        ToolContext(
            chat_id=chat_id,
            channel="web",
            llm=llm,
            db=db,
            settings=MagicMock(max_tokens=1024, max_tool_iterations=10, max_session_messages=40, compact_keep_recent=20),
            structured_memory=StructuredMemory(db),
        ),
    )

    assert result.content == "子 Agent 完成"
    assert len(llm._calls) == 1
    sent_messages = llm._calls[0]["messages"]
    assert sent_messages[0].role == "user"
    assert sent_messages[0].content == "只返回一个结论"
    assert all(getattr(message, "content", None) != "父级历史" for message in sent_messages)
    assert all(getattr(message, "content", None) != "父级答复" for message in sent_messages)
    assert await db.load_session(chat_id) == parent_session
    recent_messages = await db.get_recent_messages(chat_id, limit=10)
    assert len(recent_messages) == 1
    assert recent_messages[0]["content"] == "父级消息"
