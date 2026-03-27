"""Core Agent loop: process_message → LLM → tool calls → response."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from loguru import logger

from xclaw.db import Database
from xclaw.llm import LLMProvider
from xclaw.llm_types import (
    LLMResponse,
    Message,
    StopReason,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from xclaw.memory import FileMemory, StructuredMemory
from xclaw.tools import ToolContext, ToolRegistry

# Pattern to detect "记住..." / "remember..." quick-memory commands
_MEMORY_PATTERN = re.compile(r"^(?:记住|remember)[：:]\s*(.+)", re.IGNORECASE | re.DOTALL)
_EMPTY_REPLY_FALLBACK = "抱歉，这一轮没有生成可展示的答复，请稍后重试。"


class AgentContext:
    """Carries all runtime dependencies needed by the agent loop."""

    def __init__(
        self,
        chat_id: int,
        channel: str,
        db: Database,
        llm: LLMProvider,
        tools: ToolRegistry,
        file_memory: FileMemory | None = None,
        structured_memory: StructuredMemory | None = None,
        settings: Any = None,
    ) -> None:
        self.chat_id = chat_id
        self.channel = channel
        self.db = db
        self.llm = llm
        self.tools = tools
        self.file_memory = file_memory
        self.structured_memory = structured_memory
        self.settings = settings


def _build_system_prompt(
    ctx: AgentContext,
    memories: list[dict] | None = None,
    file_memory_content: str = "",
) -> str:
    """Assemble the system prompt from static instructions + injected memories."""
    parts = [
        "你是 XClaw，一个智能投资助手和任务处理 Agent。",
        "你能够帮助用户查询股票行情、分析技术指标、管理自选股和持仓、以及处理通用任务。",
        "回答请使用中文，保持简洁专业。不提供投资建议，仅提供数据分析辅助。",
        (
            "涉及股票涨跌颜色或 emoji 展示时，必须遵守市场约定："
            "A 股默认 🔴=涨、🟢=跌，即红涨绿跌；"
            "港股/美股默认 🟢=涨、🔴=跌，即绿涨红跌。"
            "不要混用这些规则。"
        ),
    ]
    if file_memory_content.strip():
        parts.append("\n## 记忆文件\n" + file_memory_content)
    if memories:
        from xclaw.memory import StructuredMemory as SM
        formatted = SM(None).format_for_prompt(memories)  # type: ignore[arg-type]
        if formatted:
            parts.append("\n" + formatted)
    return "\n\n".join(parts)


async def _compact_messages(
    messages: list[Any],
    ctx: AgentContext,
    keep_recent: int,
) -> list[Any]:
    """Summarise older messages to reduce context size."""
    if len(messages) <= keep_recent:
        return messages

    to_summarise = messages[: len(messages) - keep_recent]
    recent = messages[len(messages) - keep_recent :]

    # Build a simple text digest of old messages
    digest_parts: list[str] = []
    for msg in to_summarise:
        if isinstance(msg, Message):
            digest_parts.append(f"{msg.role}: {msg.text_content()[:200]}")
        elif isinstance(msg, dict):
            digest_parts.append(f"{msg.get('role', '?')}: {str(msg.get('content', ''))[:200]}")

    digest_text = "\n".join(digest_parts)
    try:
        summary_response = await ctx.llm.chat(
            messages=[
                Message(
                    role="user",
                    content=f"请用中文简洁地总结以下对话的要点（100字以内）：\n\n{digest_text}",
                )
            ],
            max_tokens=256,
        )
        summary = summary_response.text()
    except Exception:  # noqa: BLE001
        summary = "（历史对话已压缩）"

    summary_msg = Message(role="user", content=f"[历史摘要] {summary}")
    return [summary_msg] + recent


def _messages_to_serializable(messages: list[Message]) -> list[dict]:
    """Convert Message objects to JSON-serializable dicts for DB storage."""
    result = []
    for msg in messages:
        if isinstance(msg.content, str):
            result.append({"role": msg.role, "content": msg.content})
        else:
            blocks = []
            for b in msg.content:
                blocks.append(b.model_dump())
            result.append({"role": msg.role, "content": blocks})
    return result


def _messages_from_serializable(data: list[dict]) -> list[Message]:
    """Restore Message objects from JSON-serializable dicts."""
    messages = []
    for item in data:
        role = item.get("role", "user")
        content = item.get("content", "")
        if isinstance(content, str):
            messages.append(Message(role=role, content=content))
        else:
            blocks = []
            for b in content:
                btype = b.get("type", "text")
                if btype == "text":
                    blocks.append(TextBlock(text=b.get("text", "")))
                elif btype == "tool_use":
                    blocks.append(
                        ToolUseBlock(
                            id=b.get("id", str(uuid.uuid4())),
                            name=b.get("name", ""),
                            input=b.get("input", {}),
                        )
                    )
                elif btype == "tool_result":
                    blocks.append(
                        ToolResultBlock(
                            tool_use_id=b.get("tool_use_id", ""),
                            content=b.get("content", ""),
                            is_error=b.get("is_error", False),
                        )
                    )
            messages.append(Message(role=role, content=blocks))
    return messages


def _normalize_final_text(text: str) -> str:
    """Prevent blank assistant replies from surfacing to end users."""
    return text if text.strip() else _EMPTY_REPLY_FALLBACK


async def agent_loop(
    ctx: AgentContext,
    user_message: str,
    max_iterations: int | None = None,
) -> str:
    """Core Agent loop.

    1. Quick-memory path: detect "记住: …" and write directly.
    2. Load session from DB.
    3. Build system prompt (file memory + structured memory + tool list).
    4. Context compaction if needed.
    5. Call LLM (with tools).
    6. Tool loop: execute tools, append results, re-call LLM.
    7. Persist session.
    8. Return final text response.
    """
    settings = ctx.settings
    _max_iter = max_iterations or (settings.max_tool_iterations if settings else 50)
    _max_session = settings.max_session_messages if settings else 40
    _keep_recent = settings.compact_keep_recent if settings else 20

    # ── 1. Quick memory path ────────────────────────────────────────────────
    m = _MEMORY_PATTERN.match(user_message.strip())
    if m and ctx.structured_memory:
        fact = m.group(1).strip()
        await ctx.structured_memory.add(ctx.chat_id, fact)
        return f"已记住：{fact}"

    # ── 2. Load session ──────────────────────────────────────────────────────
    raw_session = await ctx.db.load_session(ctx.chat_id)
    messages: list[Message] = (
        _messages_from_serializable(raw_session) if raw_session else []
    )

    # ── 3. Build system prompt ───────────────────────────────────────────────
    file_mem_content = ""
    if ctx.file_memory:
        file_mem_content = ctx.file_memory.read(ctx.chat_id)
    memories = []
    if ctx.structured_memory:
        memories = await ctx.structured_memory.get_all(ctx.chat_id)
    system_prompt = _build_system_prompt(ctx, memories, file_mem_content)

    # ── 4. Context compaction ────────────────────────────────────────────────
    if len(messages) >= _max_session:
        messages = await _compact_messages(messages, ctx, _keep_recent)

    # ── 5. Append user message ───────────────────────────────────────────────
    messages.append(Message(role="user", content=user_message))

    # ── 6. Save user message to history ─────────────────────────────────────
    await ctx.db.save_message(ctx.chat_id, "user", user_message)

    # ── 7. Tool loop ─────────────────────────────────────────────────────────
    tool_defs = ctx.tools.get_definitions()
    tool_context = ToolContext(
        chat_id=ctx.chat_id,
        channel=ctx.channel,
        llm=ctx.llm,
        db=ctx.db,
        settings=settings,
        file_memory=ctx.file_memory,
        structured_memory=ctx.structured_memory,
    )

    final_text = ""
    for iteration in range(_max_iter):
        try:
            response: LLMResponse = await ctx.llm.chat(
                messages=messages,
                tools=tool_defs if tool_defs else None,
                system=system_prompt,
                max_tokens=settings.max_tokens if settings else 4096,
            )
        except Exception as exc:
            logger.error(f"LLM call failed: {exc}")
            final_text = f"AI 调用失败，请稍后重试。"
            break

        # Record usage
        if ctx.db and response.usage:
            try:
                await ctx.db.record_usage(
                    ctx.chat_id,
                    response.model or "unknown",
                    response.usage.input_tokens,
                    response.usage.output_tokens,
                )
            except Exception:  # noqa: BLE001
                pass

        if response.stop_reason == StopReason.end_turn:
            final_text = _normalize_final_text(response.text())
            messages.append(Message(role="assistant", content=response.content))
            break

        if response.stop_reason == StopReason.tool_use:
            tool_uses = response.tool_uses()
            if not tool_uses:
                final_text = _normalize_final_text(response.text())
                messages.append(Message(role="assistant", content=response.content))
                break

            # Append assistant message with tool_use blocks
            messages.append(Message(role="assistant", content=response.content))

            # Execute each tool and collect results
            tool_result_blocks = []
            for tool_use in tool_uses:
                logger.debug(f"Executing tool: {tool_use.name} params={tool_use.input}")
                result = await ctx.tools.execute(tool_use.name, tool_use.input, tool_context)
                tool_result_blocks.append(
                    ToolResultBlock(
                        tool_use_id=tool_use.id,
                        content=result.content,
                        is_error=result.is_error,
                    )
                )

            # Append user message with tool results
            messages.append(
                Message(role="user", content=tool_result_blocks)  # type: ignore[arg-type]
            )
            continue

        # max_tokens or stop_sequence
        final_text = _normalize_final_text(response.text())
        messages.append(Message(role="assistant", content=response.content))
        break
    else:
        final_text = "已达到最大工具调用轮数，请缩短任务或分步执行。"

    # ── 8. Persist session and assistant message ─────────────────────────────
    await ctx.db.save_message(ctx.chat_id, "assistant", final_text)
    await ctx.db.save_session(ctx.chat_id, _messages_to_serializable(messages))

    return final_text
