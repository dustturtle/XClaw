"""Tests for the LLM type system."""

from __future__ import annotations

import pytest
from xclaw.llm_types import (
    DoneEvent,
    LLMResponse,
    Message,
    StopReason,
    TextBlock,
    TextDeltaEvent,
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
    UsageStats,
)


def test_text_block():
    b = TextBlock(text="Hello")
    assert b.type == "text"
    assert b.text == "Hello"


def test_tool_use_block():
    b = ToolUseBlock(id="id1", name="stock_quote", input={"symbol": "600519"})
    assert b.type == "tool_use"
    assert b.name == "stock_quote"
    assert b.input["symbol"] == "600519"


def test_tool_result_block():
    b = ToolResultBlock(tool_use_id="id1", content="result text")
    assert b.type == "tool_result"
    assert not b.is_error


def test_message_text_content_string():
    msg = Message(role="user", content="Hello")
    assert msg.text_content() == "Hello"


def test_message_text_content_blocks():
    msg = Message(
        role="assistant",
        content=[TextBlock(text="Part 1"), TextBlock(text="Part 2")],
    )
    assert "Part 1" in msg.text_content()
    assert "Part 2" in msg.text_content()


def test_message_text_content_with_tool_result():
    msg = Message(
        role="user",
        content=[
            ToolResultBlock(tool_use_id="x", content="tool output"),
        ],
    )
    assert "tool output" in msg.text_content()


def test_llm_response_text():
    response = LLMResponse(
        stop_reason=StopReason.end_turn,
        content=[TextBlock(text="Hello"), TextBlock(text="world")],
    )
    assert "Hello" in response.text()
    assert "world" in response.text()


def test_llm_response_tool_uses():
    response = LLMResponse(
        stop_reason=StopReason.tool_use,
        content=[
            TextBlock(text="Calling tool…"),
            ToolUseBlock(id="1", name="stock_quote", input={"symbol": "AAPL"}),
        ],
    )
    uses = response.tool_uses()
    assert len(uses) == 1
    assert uses[0].name == "stock_quote"


def test_usage_stats_defaults():
    u = UsageStats()
    assert u.input_tokens == 0
    assert u.output_tokens == 0


def test_tool_definition():
    td = ToolDefinition(
        name="stock_quote",
        description="Get quote",
        input_schema={"type": "object", "properties": {}},
    )
    assert td.name == "stock_quote"


def test_stop_reason_enum():
    assert StopReason.end_turn == "end_turn"
    assert StopReason.tool_use == "tool_use"


def test_llm_event_types():
    e1 = TextDeltaEvent(text="delta")
    assert e1.type == "text_delta"

    response = LLMResponse(stop_reason=StopReason.end_turn, content=[])
    e2 = DoneEvent(response=response)
    assert e2.type == "done"
