"""Core Agent loop: process_message → LLM → tool calls → response."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

from loguru import logger

from xclaw import stage_timing
from xclaw.attachments import ImageAttachment

from xclaw.db import Database
from xclaw.llm import LLMProvider
from xclaw.llm_types import (
    LLMResponse,
    TextDeltaEvent,
    DoneEvent,
    Message,
    StopReason,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from xclaw.memory import FileMemory, StructuredMemory
from xclaw.tools import ToolContext, ToolRegistry
from xclaw.responses import DirectReply

# Pattern to detect "记住..." / "remember..." quick-memory commands
_MEMORY_PATTERN = re.compile(r"^(?:记住|remember)[：:]\s*(.+)", re.IGNORECASE | re.DOTALL)
_RESET_PATTERN = re.compile(r"^/reset\s*$", re.IGNORECASE)
_EMPTY_REPLY_FALLBACK = "抱歉，这一轮没有生成可展示的答复，请稍后重试。"
_MICRO_COMPACT_KEEP_RECENT_TOOL_RESULTS = 3
_MICRO_COMPACT_PLACEHOLDER = "[已处理的工具结果，原内容已压缩]"
_MAX_CONSECUTIVE_TOOL_ERROR_ROUNDS = 3
_EARLY_STOP_INSTRUCTION = (
    "多个工具已连续失败。请不要继续调用工具，基于当前已获得的信息直接回答用户；"
    "如果信息不足，请明确说明缺失点。"
)


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
        scheduler: Any = None,
        skip_session_persistence: bool = False,
        skip_message_persistence: bool = False,
        powercore: Any = None,
        execution_mode: str = "analysis",
    ) -> None:
        self.chat_id = chat_id
        self.channel = channel
        self.db = db
        self.llm = llm
        self.tools = tools
        self.file_memory = file_memory
        self.structured_memory = structured_memory
        self.settings = settings
        self.scheduler = scheduler
        self.skip_session_persistence = skip_session_persistence
        self.skip_message_persistence = skip_message_persistence
        self.powercore = powercore
        self.execution_mode = execution_mode
        self.tool_evidence: list[dict] = []
        self.progress_callback = None


async def _report_progress(ctx: AgentContext, message: str) -> None:
    if ctx.progress_callback is not None:
        await ctx.progress_callback(message)


async def _execute_tool_batch(ctx: AgentContext, tool_uses, tool_context, round_index=0):
    """Parallelize only explicitly independent reads; writes are ordering barriers."""
    semaphore = asyncio.Semaphore(4)
    completed = 0

    async def execute(call):
        nonlocal completed
        async with semaphore:
            await _report_progress(ctx, f"正在查询业务数据（{completed}/{len(tool_uses)} 已完成）")
            from xclaw.tool_execution_journal import execute_with_journal, is_read_only_call
            from xclaw.tools.sub_agent import SubAgentTool
            target = ctx.tools.get(call.name)
            safe_read = (is_read_only_call(ctx.tools, call)
                         or type(target) is SubAgentTool and ctx.powercore is None
                         or call.name == "investment_report" and call.input.get("action") in {"latest","history"})
            with stage_timing.span("tool", round_index, getattr(target, "name", "unknown")) as timing_status:
                if ctx.execution_mode == "prepare" and not safe_read:
                    from xclaw.tools import ToolResult, ToolExecutionEffect
                    result = ToolResult("预览确认模式：该写操作未执行；请输出待确认计划，不要再次直接执行。", True,
                                        execution_effect=ToolExecutionEffect.NOT_APPLIED)
                    result.prepare_blocked = True
                elif (ctx.execution_mode == "prepare" and call.name == "investment_report"
                      and call.input.get("action") in {"latest", "history"}):
                    result = await ctx.tools.execute(call.name, call.input, tool_context)
                else:
                    result = await execute_with_journal(ctx, call, tool_context)
                if timing_status is not None and result.is_error:
                    timing_status[0] = "blocked" if getattr(result, "prepare_blocked", False) else "tool_error"
            completed += 1
            await _report_progress(ctx, f"业务数据已返回（{completed}/{len(tool_uses)}）")
            return result

    results = []
    batch = []
    for call in tool_uses:
        tool = ctx.tools.get(call.name)
        if tool and tool.read_only and tool.independent:
            batch.append(call)
            continue
        if batch:
            results.extend(await asyncio.gather(*(execute(item) for item in batch)))
            batch = []
        results.append(await execute(call))
    if batch:
        results.extend(await asyncio.gather(*(execute(item) for item in batch)))
    return results


def _build_system_prompt(
    ctx: AgentContext,
    memories: list[dict] | None = None,
    file_memory_content: str = "",
) -> str:
    """Assemble the system prompt from static instructions + injected memories."""
    _tz_sh = timezone(timedelta(hours=8))
    _now = datetime.now(_tz_sh)
    parts = [
        "你是 XClaw，一个智能投资助手和任务处理 Agent。",
        f"当前时间：{_now.strftime('%Y-%m-%d %H:%M')}（Asia/Shanghai，UTC+8）。"
        "涉及日期计算时请以此为准。",
        "你能够帮助用户查询股票、指数和国内商品期货行情，分析技术指标，管理自选股和持仓，以及处理通用任务。",
        "回答请使用中文，保持简洁专业。不提供投资建议，仅提供数据分析辅助。",
        (
            "## 工具使用规则\n"
            "- 先识别交付目标：分析用户给定材料不等于全面搜集实时行情。优先评审材料的假设、逻辑与缺失条件。\n"
            "- 仅当外部事实会影响结论、或用户要求核验时再查询；将材料内推论与外部核验明确区分。\n"
            "- 批量提出互不依赖的只读查询；已有工具证据可复用，不为凑齐数据重复查询。\n"
            "- 无结果不是事实不存在；相同信息缺口在有限查询后仍不可得时，应标明缺失，不反复扩张搜索。\n"
            "- 单只股票/指数/国内商品期货的行情查询：优先调用 stock_quote。\n"
            "- 单只股票/指数/国内商品期货的历史K线：优先调用 stock_history。\n"
            "- 单只股票/指数/国内商品期货的技术指标：优先调用 stock_indicators。\n"
            "- 多只股票/指数之间的联动性、相关矩阵、同涨同跌分析：优先调用 stock_correlation。\n"
            "- 美股/港股的财报前瞻、财报回顾、盈利预期分析：优先调用 earnings_analysis。\n"
            "- 用户询问 ETF 是否溢价/折价、价格是否偏离净值：优先调用 etf_premium_analysis。\n"
            "- 用户询问某只股票/ETF 的成交活跃度、流动性、冲击成本：优先调用 stock_liquidity。\n"
            "- 单只股票/指数/国内商品期货的跳空缺口/回补判断：优先调用 stock_gap_analysis，"
            "不要根据 stock_history 返回的K线自行手算缺口。\n"
            "- 用户询问单只股票的买点、卖点、策略分析、决策卡时：优先调用 strategy_scan。\n"
            "- 用户询问自选股日报、盘后简报、每日决策仪表盘时：优先调用 investment_report。\n"
            "- sub_agent 仅用于需要同时调用多个不同工具、交叉对比多只标的的复杂研究任务。"
            "简单的单工具查询+分析绝不要走 sub_agent。"
        ),
    ]
    if ctx.channel == "web" and getattr(ctx.settings, "openui_enabled", False):
        parts.append(
            "## Web 交互展示边界\n"
            "此 Web 界面启用了独立的 OpenUI 展示阶段，会在本轮文字答复后按需生成真实业务卡片与按钮。"
            "你负责事实、业务推理和已授权工具调用，不要伪造按钮链接/Markdown按钮，也不要断言界面不支持按钮。"
            "用户要求先展示待确认操作时，只查询并说明待确认的业务目标与参数，不提前执行写入；"
            "交互展示由后续阶段处理，不能保证每次生成成功。"
            "用户本轮明确要求图表、数据卡片或对比展示时，若未禁止重新查询，应实际调用所需的只读工具取得本轮可绑定结果；不能只引用旧答复文字就承诺本轮卡片。"
            "禁止为了图表重跑strategy_scan等有持久化副作用的工具；已有策略结果可用只读strategy_runs。用户禁止重查或没有合适的只读工具时，沿用旧信息必须注明原时点，只提供文字说明并如实说明本轮图表缺少可绑定证据，不伪造结果。"
            "对已有真实时间序列或财报数据的图表请求，保留结论、关键数值、来源和限制，不额外输出ASCII/Unicode字符画或Mermaid图表代码来代替组件；"
            "将相关解读分段，供真实组件插入对应段落。用户明确索要代码或纯文本示意时除外。"
            "当前历史/分时组件展示收盘价格折线，不是OHLC蜡烛图，也没有成交量副图；不得承诺会显示蜡烛K线或成交量图。"
            "历史组件可在已加载区间内筛选最近3/5/20个交易日，默认全部；这只是本地筛选，不承诺任意日期的新查询控件。"
            "财报组件展示原始币种和金额单位的多期表格，不是亿元柱状图；文字可换算亿元但应明确文字换算与原始表单位。"
            "展示阶段尚未完成时，只描述已获取数据与期望展示，不保证某按钮、图形或切换控件一定出现。"
        )
    if file_memory_content.strip():
        parts.append("\n## 记忆文件\n" + file_memory_content)
    if memories:
        from xclaw.memory import StructuredMemory as SM
        formatted = SM(None).format_for_prompt(memories)  # type: ignore[arg-type]
        if formatted:
            parts.append("\n" + formatted)
    return "\n\n".join(parts)


_MAX_PRESERVED_EXECUTION_CHARS = 200_000
_HISTORY_EXECUTION_MARKER = "[历史执行记录：仅上下文，禁止重放；缺失或冲突不代表成功]"


def _tool_occurrences(messages):
    calls, results = {}, {}
    for index, message in enumerate(messages):
        if not isinstance(message.content, list):
            continue
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                calls.setdefault(block.id, []).append((index, message.role, block))
            elif isinstance(block, ToolResultBlock):
                results.setdefault(block.tool_use_id, []).append((index, message.role, block))
    return calls, results


def _repair_orphan_tool_results(messages: list[Message]) -> list[Message]:
    """Demote ambiguous legacy tool groups to verbatim, non-executable evidence.

    Duplicate IDs, missing calls/results, inverted order and invalid roles do not
    establish execution success. Never manufacture calls/results or replay tools.
    """
    calls, results = _tool_occurrences(messages)
    valid = set()
    for index, message in enumerate(messages):
        if message.role != "assistant" or not isinstance(message.content, list):
            continue
        batch = [block.id for block in message.content if isinstance(block, ToolUseBlock)]
        if not batch or any(not identity or len(calls.get(identity, [])) != 1
                            or len(results.get(identity, [])) != 1 for identity in batch):
            continue
        pending = set(batch)
        cursor = index + 1
        while pending and cursor < len(messages):
            result_message = messages[cursor]
            if (result_message.role != "user" or not isinstance(result_message.content, list)
                    or not result_message.content
                    or any(not isinstance(block, ToolResultBlock) for block in result_message.content)):
                break
            identities = [block.tool_use_id for block in result_message.content]
            if len(set(identities)) != len(identities) or not set(identities) <= pending:
                break
            pending.difference_update(identities)
            cursor += 1
        if not pending:
            valid.update(batch)
    repaired = []
    for message in messages:
        if isinstance(message.content, str):
            repaired.append(message)
            continue
        blocks = []
        for block in message.content:
            identity = block.id if isinstance(block, ToolUseBlock) else block.tool_use_id if isinstance(block, ToolResultBlock) else None
            if isinstance(block, (ToolUseBlock, ToolResultBlock)) and identity not in valid:
                blocks.append(TextBlock(text=_HISTORY_EXECUTION_MARKER + "\n" + json.dumps(block.model_dump(), ensure_ascii=False)))
            else:
                blocks.append(block)
        repaired.append(Message(role=message.role, content=blocks))
    return repaired


def _is_read_only_tool(name, registry):
    tool = registry.get(name) if registry is not None else None
    return tool is not None and tool.read_only is True


def _preserved_execution_records(messages, registry):
    """Keep state-changing/unknown execution evidence out of lossy summaries."""
    calls, _ = _tool_occurrences(messages)
    protected = {identity for identity, uses in calls.items()
                 if any(not _is_read_only_tool(block.name, registry) for _, _, block in uses)}
    records = []
    for message in messages:
        if isinstance(message.content, str):
            if message.content.startswith(_HISTORY_EXECUTION_MARKER):
                records.append(message.content)
            continue
        for block in message.content:
            if isinstance(block, TextBlock) and block.text.startswith(_HISTORY_EXECUTION_MARKER):
                records.append(block.text)
            elif (isinstance(block, ToolUseBlock) and block.id in protected
                  or isinstance(block, ToolResultBlock) and block.tool_use_id in protected):
                records.append(_HISTORY_EXECUTION_MARKER + "\n" + json.dumps(block.model_dump(), ensure_ascii=False))
    return records


def _safe_compaction_cut(messages: list[Message], keep_recent: int) -> int:
    """Move a proposed cut backwards until it no longer splits any tool pair."""
    cut = max(0, len(messages) - max(1, keep_recent))
    calls = {}
    pairs = []
    for index, message in enumerate(messages):
        if not isinstance(message.content, list):
            continue
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                calls[block.id] = index
            elif isinstance(block, ToolResultBlock) and block.tool_use_id in calls:
                pairs.append((calls.pop(block.tool_use_id), index))
    while True:
        earlier = [start for start, end in pairs if start < cut <= end]
        if not earlier:
            return cut
        cut = min(earlier)


async def _compact_messages(
    messages: list[Any],
    ctx: AgentContext,
    keep_recent: int,
) -> list[Any]:
    """Summarise older messages to reduce context size."""
    messages = _repair_orphan_tool_results(messages)
    if len(messages) <= keep_recent:
        return messages

    if sum(map(len, _preserved_execution_records(messages, getattr(ctx, "tools", None)))) > _MAX_PRESERVED_EXECUTION_CHARS:
        raise ValueError("历史执行记录超过安全上限，请新建会话；不会删除记录或重放操作")
    cut = _safe_compaction_cut(messages, keep_recent)
    if cut == 0:
        return messages
    to_summarise = messages[:cut]
    recent = messages[cut:]

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
    records = _preserved_execution_records(to_summarise, getattr(ctx, "tools", None))
    preserved = [Message(role="user", content=record) for record in records]
    return [summary_msg] + preserved + recent


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


def _micro_compact(
    messages: list[Message],
    keep_recent_tool_results: int = _MICRO_COMPACT_KEEP_RECENT_TOOL_RESULTS,
    registry: ToolRegistry | None = None,
) -> None:
    """Shrink only confirmed read-only, uniquely paired evidence.

    Unknown/state-changing tools and damaged history retain exact execution
    evidence. Preserved records can grow; never trade execution safety for size.
    """
    if keep_recent_tool_results < 0:
        raise ValueError("keep_recent_tool_results must be nonnegative")
    calls, results = _tool_occurrences(messages)
    eligible = set()
    for identity, uses in calls.items():
        outputs = results.get(identity, [])
        if (len(uses) == len(outputs) == 1 and uses[0][1] == "assistant"
                and outputs[0][1] == "user" and uses[0][0] < outputs[0][0]
                and _is_read_only_tool(uses[0][2].name, registry)):
            eligible.add(identity)
    ordered = [block for message in messages if isinstance(message.content, list)
               for block in message.content if isinstance(block, ToolResultBlock)
               and block.tool_use_id in eligible]
    old = ordered[:-keep_recent_tool_results] if keep_recent_tool_results else ordered
    for block in old:
        if len(block.content) > 100:
            block.content = _MICRO_COMPACT_PLACEHOLDER


def _finalize_evidence_limits(final_text, messages, evidence):
    """Append source limitations, not model inference; synchronize stored final text."""
    from xclaw.responses import RejectedReply
    if isinstance(final_text, RejectedReply):
        return final_text
    unverified = False
    for item in evidence:
        if item.get("name") != "cn_fund_nav" or item.get("is_error"):
            continue
        try:
            payload = json.loads(item.get("content", ""))
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("identity_verified") is False:
            unverified = True
    if not unverified:
        return final_text
    note = "数据限制：本轮基金净值来源未回显基金代码，基金身份未独立核验；这些净值不能视为已核实对应所查询基金。"
    final_text = str(final_text) + "\n\n" + note
    # The last assistant is the final answer, not an earlier tool-use turn.
    if messages and messages[-1].role == "assistant" and not (
        isinstance(messages[-1].content, list)
        and any(isinstance(b, ToolUseBlock) for b in messages[-1].content)
    ):
        messages[-1] = Message(role="assistant", content=final_text)
    else:
        messages.append(Message(role="assistant", content=final_text))
    return final_text


async def _stream_agent_response(llm, on_text_event, round_index, inline=None, **kwargs):
    """Only draft text escapes before a fully validated terminal response.

    Tool deltas are never executable; caller gets a response only after EOF.
    """
    await on_text_event({"type": "reset", "round": round_index})
    final = None
    parts = []
    inline_error = None
    saw_delta = saw_text = False
    async for event in llm.chat_stream(**kwargs):
        if final is not None:
            raise ValueError("Event after model completion")
        if not saw_delta and not isinstance(event, DoneEvent):
            stage_timing.first_delta("first_delta")
            saw_delta = True
        if isinstance(event, TextDeltaEvent):
            if event.text and not saw_text:
                stage_timing.first_delta("first_text_delta")
                saw_text = True
            parts.append(event.text)
            if inline is None:
                await on_text_event({"type": "delta", "round": round_index, "text": event.text})
            elif inline_error is None:
                try:
                    previous_revision = inline.revision
                    inline.feed(event.text)
                    if inline.revision != previous_revision:
                        await on_text_event({"type": "ui_snapshot", "round": round_index,
                                             "text": inline.text, "presentation": inline.snapshot()})
                except (ValueError, TypeError, KeyError) as exc:
                    inline_error = exc
                    # A chunk can contain valid records before a bad record.
                    # Preserve only that parsed text prefix, never its cards.
                    await on_text_event({"type": "draft_failed", "round": round_index,
                                         "text": inline.text})
        elif isinstance(event, DoneEvent):
            final = event.response
    if final is None:
        raise ValueError("Model stream missing completion")
    if parts and "".join(parts) != final.text():
        raise ValueError("Model draft differs from completion")
    if final.stop_reason not in {StopReason.end_turn, StopReason.tool_use}:
        raise ValueError("Incomplete model response")
    if final.tool_uses():
        if final.stop_reason != StopReason.tool_use:
            raise ValueError("Invalid tool completion")
        ids = [call.id for call in final.tool_uses()]
        if len(set(ids)) != len(ids) or any(not call.id.strip() or not call.name.strip() for call in final.tool_uses()):
            raise ValueError("Invalid tool identity")
        await on_text_event({"type": "reset", "round": round_index})
    elif inline is not None:
        if inline_error is not None:
            raise inline_error
        inline.finish()
        await on_text_event({"type": "ui_snapshot", "round": round_index,
                             "text": inline.text, "presentation": inline.snapshot()})
        final = final.model_copy(update={"content": [TextBlock(text=inline.text)]})
    return final


async def agent_loop(
    ctx: AgentContext,
    user_message: str,
    max_iterations: int | None = None,
    attachments: list[ImageAttachment] | None = None,
    on_text_event=None,
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
    _max_iter = max_iterations or (settings.max_tool_iterations if settings else 10)
    _max_session = settings.max_session_messages if settings else 40
    _keep_recent = settings.compact_keep_recent if settings else 20

    # ── 1. Quick memory path ────────────────────────────────────────────────
    stripped_message = user_message.strip()
    if ctx.execution_mode == "prepare" and (ctx.powercore is not None
            or _RESET_PATTERN.match(stripped_message) or _MEMORY_PATTERN.match(stripped_message)):
        from xclaw.responses import RejectedReply
        return RejectedReply("预览确认模式未执行重置、记忆写入或委派操作；当前仅支持自选股和本地模拟持仓确认计划。")
    if attachments:
        if ctx.powercore is None:
            raise ValueError("当前执行后端未启用图片输入")
        return await ctx.powercore.run(ctx, user_message, max_iterations=max_iterations, attachments=attachments)
    if _RESET_PATTERN.match(stripped_message):
        reply = "当前会话上下文已重置。后续消息将从干净上下文重新开始。"
        if not ctx.skip_session_persistence:
            if ctx.powercore is not None:
                await ctx.powercore.reset(ctx.chat_id)
            await ctx.db.clear_session(ctx.chat_id)
        if not ctx.skip_message_persistence:
            await ctx.db.save_message(ctx.chat_id, "user", user_message)
            await ctx.db.save_message(ctx.chat_id, "assistant", reply)
        return DirectReply(reply)

    if ctx.powercore is not None:
        return await ctx.powercore.run(ctx, user_message, max_iterations=max_iterations)

    m = _MEMORY_PATTERN.match(stripped_message)
    if m and ctx.structured_memory and not ctx.skip_message_persistence:
        fact = m.group(1).strip()
        await ctx.structured_memory.add(ctx.chat_id, fact)
        return f"已记住：{fact}"

    # ── 2. Load session ──────────────────────────────────────────────────────
    raw_session = None
    if not ctx.skip_session_persistence:
        raw_session = await ctx.db.load_session(ctx.chat_id)
    messages: list[Message] = _messages_from_serializable(raw_session) if raw_session else []
    messages = _repair_orphan_tool_results(messages)
    if sum(map(len, _preserved_execution_records(messages, ctx.tools))) > _MAX_PRESERVED_EXECUTION_CHARS:
        from xclaw.responses import RejectedReply
        return RejectedReply("历史执行记录超过安全上限，请新建会话继续。原会话记录已保留，不会删除或重放操作。")

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
    if not ctx.skip_message_persistence:
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
        scheduler=ctx.scheduler,
    )

    final_text = ""
    verified_model = None
    current_turn_evidence = []
    consecutive_error_rounds = 0
    force_direct_answer = False
    empty_reply_retried = False
    for iteration in range(_max_iter):
        _micro_compact(messages, registry=ctx.tools)
        inline = None
        try:
            await _report_progress(ctx, "正在分析问题…" if iteration == 0 else "正在结合查询结果生成答复…")
            if (on_text_event is not None and ctx.channel == "web"
                    and getattr(settings, "openui_enabled", False)
                    and getattr(settings, "openui_inline_stream", False)):
                from xclaw.inline_ui_stream import InlineUIProtocol, current_evidence
                allow_actions = ctx.execution_mode == "prepare"
                if allow_actions:
                    from xclaw.tool_execution_journal import inspect_journal
                    journal = await inspect_journal(ctx.db, ctx.chat_id)
                    allow_actions = not any(row['state'] in {'started','unknown'} for row in journal)
                inline = InlineUIProtocol(await current_evidence(ctx), iteration, allow_actions=allow_actions)
            call_model = ctx.llm.chat
            if on_text_event is not None:
                async def call_model(**kwargs):
                    return await _stream_agent_response(ctx.llm, on_text_event, iteration, inline=inline, **kwargs)
            with stage_timing.span("model", iteration):
                response: LLMResponse = await call_model(
                    messages=messages,
                    tools=(tool_defs if tool_defs and not force_direct_answer else None),
                    system=system_prompt + (inline.prompt() if inline is not None else ""),
                    max_tokens=settings.max_tokens if settings else 4096,
                )
        except asyncio.CancelledError:
            if on_text_event is not None and inline is not None:
                await on_text_event({"type": "draft_failed", "round": iteration})
            raise
        except Exception as exc:
            if on_text_event is not None:
                from xclaw.llm_responses import ResponsesProtocolError
                if isinstance(exc, ResponsesProtocolError) and exc.code == "response_model_mismatch":
                    # A terminal identity contradiction revokes even draft prose,
                    # not only cards. Do not leave wrong-model text in the UI.
                    await on_text_event({"type": "reset", "round": iteration})
                await on_text_event({"type": "draft_failed" if inline is not None else "reset", "round": iteration})
            logger.error(f"LLM call failed: {exc}")
            from xclaw.responses import RejectedReply
            final_text = RejectedReply(f"所选模型 {settings.model} 调用失败，未切换到其他模型，请稍后重试。" if settings and getattr(settings, "web_model_options", []) else "AI 调用失败，请稍后重试。")
            messages.append(Message(role="assistant", content=str(final_text)))
            break

        if ctx.channel == "web" and getattr(ctx.llm, "require_model_identity", False) is True:
            verified_model = ctx.llm.model

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

        if force_direct_answer and response.tool_uses():
            final_text = _EMPTY_REPLY_FALLBACK
            messages.append(Message(role="assistant", content=final_text))
            break

        if not response.tool_uses() and not response.text().strip():
            if not empty_reply_retried and iteration + 1 < _max_iter:
                empty_reply_retried = True
                force_direct_answer = True
                messages.append(Message(
                    role="user",
                    content="上一轮没有返回正文。请根据已有信息给出最终答复，不要重复调用工具；信息不足时请明确说明。",
                ))
                continue
            final_text = _EMPTY_REPLY_FALLBACK
            messages.append(Message(role="assistant", content=final_text))
            break

        if response.stop_reason == StopReason.end_turn:
            if inline is not None:
                ctx.inline_presentation = inline.finish()
                ctx.inline_evidence = inline.evidence
                ctx.inline_round = iteration
                ctx.inline_actions = inline.actions
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
            results = await _execute_tool_batch(ctx, tool_uses, tool_context, iteration)
            for tool_use, result in zip(tool_uses, results):
                current_turn_evidence.append({"name": tool_use.name, "content": result.content, "is_error": result.is_error})
                if (ctx.channel == "web" and getattr(settings, "openui_enabled", False)
                        and not getattr(result, "prepare_blocked", False)):
                    ctx.tool_evidence.append({"name": tool_use.name, "arguments": tool_use.input,
                                              "content": result.content, "is_error": result.is_error})
                tool_result_blocks.append(ToolResultBlock(
                    tool_use_id=tool_use.id, content=result.content, is_error=result.is_error,
                ))

            # Append user message with tool results
            messages.append(
                Message(role="user", content=tool_result_blocks)  # type: ignore[arg-type]
            )

            if tool_result_blocks and all(block.is_error for block in tool_result_blocks):
                consecutive_error_rounds += 1
                if consecutive_error_rounds >= _MAX_CONSECUTIVE_TOOL_ERROR_ROUNDS:
                    force_direct_answer = True
                    messages.append(Message(role="user", content=_EARLY_STOP_INSTRUCTION))
            else:
                consecutive_error_rounds = 0
            continue

        # max_tokens or stop_sequence
        final_text = _normalize_final_text(response.text())
        messages.append(Message(role="assistant", content=response.content))
        break
    else:
        final_text = "已达到最大工具调用轮数，请缩短任务或分步执行。"

    final_text = _finalize_evidence_limits(final_text, messages, current_turn_evidence)
    if getattr(ctx, "inline_presentation", None) is not None:
        inline_text = "\n\n".join(b['source'] for b in ctx.inline_presentation['blocks'] if b['type']=='text')
        if inline_text != final_text:
            if str(final_text).startswith(inline_text + "\n\n"):
                ctx.inline_presentation['blocks'].append({"id":"server-notice", "type":"text",
                    "source":str(final_text)[len(inline_text)+2:], "server_notice":True})
            else:
                ctx.inline_presentation = None
                if on_text_event is not None:
                    await on_text_event({"type":"draft_failed", "round":ctx.inline_round})
    await _report_progress(ctx, "正在保存本轮答复…")

    # ── 8. Persist session and assistant message ─────────────────────────────
    if not ctx.skip_message_persistence:
        from xclaw.responses import RejectedReply
        stamp = ({"response_model": verified_model} if verified_model and not isinstance(final_text, RejectedReply) else {})
        if stamp:
            from xclaw.responses import VerifiedModelReply
            final_text = VerifiedModelReply(final_text, verified_model)
        message_id = await ctx.db.save_message(ctx.chat_id, "assistant", final_text, **stamp)
        ctx.assistant_message_id = message_id
        from xclaw.responses import RejectedReply
        if isinstance(final_text, RejectedReply):
            final_text.message_id = message_id
            final_text.chat_id = ctx.chat_id
            if ctx.channel == "web":
                await ctx.db.conn.execute("CREATE TABLE IF NOT EXISTS web_message_outcomes (message_id INTEGER PRIMARY KEY, status TEXT NOT NULL)")
                await ctx.db.conn.execute("INSERT OR REPLACE INTO web_message_outcomes VALUES(?,'failed')", (message_id,))
                await ctx.db.conn.commit()
    if not ctx.skip_session_persistence:
        await ctx.db.save_session(ctx.chat_id, _messages_to_serializable(messages))

    return final_text
