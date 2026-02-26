"""Pydantic type system for LLM messages and tool definitions."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Union

from pydantic import BaseModel, Field


# ── Content block types ───────────────────────────────────────────────────────

class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


ContentBlock = Union[TextBlock, ToolUseBlock, ToolResultBlock]


# ── Messages ──────────────────────────────────────────────────────────────────

class Message(BaseModel):
    """A single message in the conversation."""

    role: Literal["user", "assistant", "system"]
    # content is either a plain string (shorthand) or a list of content blocks
    content: Union[str, list[ContentBlock]]

    def text_content(self) -> str:
        """Return the plain text extracted from this message."""
        if isinstance(self.content, str):
            return self.content
        parts: list[str] = []
        for block in self.content:
            if isinstance(block, TextBlock):
                parts.append(block.text)
            elif isinstance(block, ToolResultBlock):
                parts.append(block.content)
        return "\n".join(parts)


# ── Tool definitions ──────────────────────────────────────────────────────────

class ToolDefinition(BaseModel):
    """JSON-Schema-based tool description sent to the LLM."""

    name: str
    description: str
    input_schema: dict[str, Any]


# ── LLM Response ─────────────────────────────────────────────────────────────

class StopReason(str, Enum):
    end_turn = "end_turn"
    tool_use = "tool_use"
    max_tokens = "max_tokens"
    stop_sequence = "stop_sequence"


class UsageStats(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class LLMResponse(BaseModel):
    stop_reason: StopReason
    content: list[ContentBlock]
    usage: UsageStats = Field(default_factory=UsageStats)
    model: str = ""

    def text(self) -> str:
        """Return the combined text from all TextBlocks."""
        return "\n".join(b.text for b in self.content if isinstance(b, TextBlock))

    def tool_uses(self) -> list[ToolUseBlock]:
        """Return all ToolUseBlock entries in the response."""
        return [b for b in self.content if isinstance(b, ToolUseBlock)]


# ── Streaming events ──────────────────────────────────────────────────────────

class TextDeltaEvent(BaseModel):
    type: Literal["text_delta"] = "text_delta"
    text: str


class ToolUseDeltaEvent(BaseModel):
    type: Literal["tool_use_delta"] = "tool_use_delta"
    tool_use: ToolUseBlock


class DoneEvent(BaseModel):
    type: Literal["done"] = "done"
    response: LLMResponse


LLMEvent = Union[TextDeltaEvent, ToolUseDeltaEvent, DoneEvent]
