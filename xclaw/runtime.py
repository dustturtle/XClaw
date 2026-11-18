"""Application runtime: initialise all components and wire them together."""

from __future__ import annotations

import asyncio
import faulthandler
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
from loguru import logger

from xclaw.agent_engine import AgentContext, agent_loop
from xclaw.attachments import ImageAttachment
from xclaw.config import Settings
from xclaw.db import Database
from xclaw.llm import create_provider
from xclaw.model_routing import bind_request_model, resolve_request_model
from xclaw.memory import FileMemory, StructuredMemory
from xclaw.oauth import AuthProfileStore, OpenAICodexOAuthManager
from xclaw.skills import build_skill_registry
from xclaw.tools import ToolRegistry
from xclaw.tools.sub_agent import SubAgentTool
from xclaw.lifecycle import LifecycleJournal, exception_fields

@dataclass
class _TurnEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0
    owner: asyncio.Task | None = None


class ChatTurnGate:
    """Serialize native turns within this runtime, keyed by resolved chat identity.

    No DB transaction is held while waiting or calling a provider. This prevents
    in-process session lost updates, not cross-process writes or crash replay.
    Sub-agents run directly with persistence disabled and do not enter this gate.
    """
    def __init__(self):
        self._entries: dict[int, _TurnEntry] = {}

    @asynccontextmanager
    async def hold(self, chat_id: int):
        entry = self._entries.setdefault(chat_id, _TurnEntry())
        task = asyncio.current_task()
        if entry.owner is task:
            raise RuntimeError("Recursive persistent turn for the same chat is not supported")
        entry.users += 1  # Include waiters so cancellation cannot replace an active lock.
        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            entry.owner = task
            yield
        finally:
            if acquired:
                entry.owner = None
                entry.lock.release()
            entry.users -= 1
            if entry.users == 0:
                self._entries.pop(chat_id, None)


def _setup_logging(settings: Settings) -> None:
    settings.logs_path.mkdir(parents=True, exist_ok=True)
    logger.add(
        settings.logs_path / "xclaw.log",
        rotation="10 MB",
        retention="30 days",
        level="INFO",
        encoding="utf-8",
    )


def _build_tool_registry(settings: Settings) -> ToolRegistry:
    """Build tool registry using the Skills system."""
    registry = ToolRegistry()

    # Load all enabled skills (they register their tools)
    skill_registry = build_skill_registry(
        enabled_skills=settings.enabled_skills,
        skills_dir=settings.skills_dir or None,
    )
    skill_registry.load_tools(registry, settings)

    # SubAgentTool must be registered after other tools (it references the registry)
    try:
        registry.register(SubAgentTool(registry))
    except ValueError:
        pass  # already registered by a skill

    return registry


async def run(settings: Settings) -> None:
    journal = LifecycleJournal(settings.logs_path, "runtime")
    journal.emit("runtime_started")
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    def exception_handler(_loop, context):
        error = context.get("exception")
        journal.emit("async_exception", **exception_fields(error) if error else {"exception_type": "AsyncFailure"})

    loop.set_exception_handler(exception_handler)
    crash_log = (settings.logs_path / ("fatal-" + journal.boot_id + ".log")).open("a")
    fatal_was_enabled = faulthandler.is_enabled()
    faulthandler.enable(crash_log, all_threads=True)
    try:
        await _run(settings)
    except BaseException as exc:
        journal.emit("runtime_exception", **exception_fields(exc))
        raise
    finally:
        loop.set_exception_handler(previous_handler)
        if fatal_was_enabled:
            faulthandler.enable()
        else:
            faulthandler.disable()
        crash_log.close()
        journal.emit("runtime_stopped")


async def _run(settings: Settings) -> None:
    """Start the XClaw runtime."""
    _setup_logging(settings)
    logger.info("Starting XClaw runtime…")

    # Ensure data directories
    settings.data_path.mkdir(parents=True, exist_ok=True)
    settings.groups_path.mkdir(parents=True, exist_ok=True)

    # Database
    db = Database(settings.db_path)
    await db.connect()
    logger.info(f"Database connected: {settings.db_path}")

    oauth_manager = None
    if settings.llm_provider == "openai-codex":
        oauth_manager = OpenAICodexOAuthManager(
            store=AuthProfileStore(settings.auth_profiles_path),
            base_url=settings.base_url or "https://chatgpt.com/backend-api",
        )

    # LLM Provider
    llm = create_provider(
        settings.llm_provider,
        settings.api_key,
        settings.model,
        base_url=settings.base_url or None,
        temperature=settings.temperature,
        timeout=settings.timeout,
        thinking=settings.thinking,
        oauth_manager=oauth_manager,
    )

    # Memory
    file_memory = FileMemory(settings.groups_path)
    struct_memory = StructuredMemory(db)

    # Tool registry (skills-based)
    tools = _build_tool_registry(settings)

    # MCP tool federation
    mcp_clients: list[Any] = []
    if settings.mcp_servers:
        from xclaw.mcp import load_mcp_tools
        mcp_clients = await load_mcp_tools(settings.mcp_servers, tools)
        logger.info(f"MCP: loaded {len(mcp_clients)} server(s)")

    scheduler = None
    powercore = None
    if settings.agent_backend == "powercore":
        from xclaw.powercore import PowerCoreClient

        if not settings.web_enabled or not settings.powercore_mcp_token:
            raise ValueError("PowerCore requires web_enabled and powercore_mcp_token")
        powercore = PowerCoreClient(settings, db)
        await powercore.check_ready()
        logger.info("Agent backend: PowerCore Service API v1")

    async def context_for_chat(chat_id: int, model: str | None = None) -> AgentContext:
        chat = await db.get_chat(chat_id)
        if chat is None:
            raise ValueError("Chat does not exist")
        turn_llm, turn_settings = bind_request_model(llm, settings, model)
        return AgentContext(
            chat_id=chat_id, channel=chat["channel"], db=db, llm=turn_llm, tools=tools,
            file_memory=file_memory, structured_memory=struct_memory,
            settings=turn_settings, scheduler=scheduler, powercore=powercore,
        )

    async def tool_context_for_chat(chat_id: int):
        from xclaw.tools import ToolContext

        ctx = await context_for_chat(chat_id)
        return ToolContext(
            chat_id=ctx.chat_id, channel=ctx.channel, db=db, llm=ctx.llm,
            file_memory=file_memory, structured_memory=struct_memory,
            settings=ctx.settings, scheduler=scheduler, powercore=powercore,
            workspace_root=settings.groups_path / str(chat_id),
        )

    native_progress: dict[str, dict] = {}
    native_turns = ChatTurnGate()

    # ── Build message handler ─────────────────────────────────────────────────
    async def handle_message(
        external_chat_id: str,
        text: str,
        channel: str = "web",
        chat_type: str = "private",
        *, attachments: list[ImageAttachment] | None = None, execution_mode: str | None = None,
        model: str | None = None,
    ) -> str:
        # Bind the requested ID before the first await or chat/session mutation.
        model = resolve_request_model(settings, model)
        chat_id = await resolve_message_chat(db, external_chat_id, channel, chat_type)
        # The whole load/compact/tool/persist turn, including /reset, is serialized.
        # PowerCore owns its own execution protocol; do not add a network-duration lock.
        if powercore is not None:
            return await handle_resolved_message(chat_id, text, channel, attachments, execution_mode, model)
        async with native_turns.hold(chat_id):
            return await handle_resolved_message(chat_id, text, channel, attachments, execution_mode, model)

    async def handle_resolved_message(chat_id, text, channel, attachments, execution_mode, model):
        ctx = await context_for_chat(chat_id, model)
        if execution_mode is not None:
            if execution_mode not in {"standard", "analysis", "prepare"}:
                raise ValueError("Unsupported execution mode")
            if execution_mode == "prepare" and (channel != "web" or powercore is not None
                    or not settings.openui_enabled or not settings.openui_inline_stream):
                raise ValueError("Prepare mode requires native inline Web UI")
            ctx.execution_mode = execution_mode
        ctx.skip_session_persistence = channel == "scheduler"
        from xclaw.receipts import current_receipt
        receipt_id = current_receipt.get()
        if channel == "web" and powercore is None and receipt_id:
            native_progress[receipt_id] = {"started": time.monotonic(), "message": "正在准备执行…"}
            async def report(message):
                native_progress[receipt_id]["message"] = message
            ctx.progress_callback = report
        try:
            from xclaw.text_stream import current_text_sink
            sink = current_text_sink.get()
            options = {"on_text_event": sink} if sink is not None else {}
            reply = await agent_loop(ctx, text, attachments=attachments, **options)
            from xclaw.responses import RejectedReply
            if channel == "web" and settings.openui_enabled and not isinstance(reply, RejectedReply):
                try:
                    from xclaw.ui_evidence import save_evidence
                    if getattr(ctx, "inline_presentation", None) is not None:
                        from xclaw.ui_presentations_v2 import save_v2
                        from xclaw.responses import PresentationReply
                        import uuid
                        payload = await save_v2(
                            db, ctx.chat_id, ctx.assistant_message_id,
                            receipt_id or uuid.uuid4().hex, reply, text,
                            ctx.inline_evidence, ctx.inline_presentation, ctx.inline_actions)
                        response_model = getattr(reply, "response_model", None)
                        reply = PresentationReply(reply, presentation_id=payload['presentation_id'],
                                                  message_id=payload['message_id'])
                        reply.response_model = response_model
                        if sink is not None:
                            import json
                            presentation=json.loads(payload['source'])
                            await sink({"type": "ui_commit", "round": ctx.inline_round, "text": str(reply),
                                        "reply_hash": payload['reply_hash'],
                                        "presentation_id": payload['presentation_id'],
                                        "message_id": payload['message_id'], "presentation": {
                                            **presentation, "version": "xclaw-live-1",
                                            "message_id":payload['message_id'],
                                            "round": ctx.inline_round, "evidence": ctx.inline_evidence,
                                            "provisional": False, "validated": True}})
                    else:
                        await save_evidence(db, ctx.chat_id, reply, text, ctx.tool_evidence, model=ctx.settings.model)
                except Exception:
                    if sink is not None and getattr(ctx, "inline_presentation", None) is not None:
                        await sink({"type": "draft_failed", "round": ctx.inline_round})
                    logger.warning("UI evidence could not be saved; original reply preserved")
            return reply
        finally:
            if receipt_id:
                native_progress.pop(receipt_id, None)

    async def handle_message_stream(
        external_chat_id: str,
        text: str,
        channel: str = "web",
        chat_type: str = "private",
        *, attachments: list[ImageAttachment] | None = None, execution_mode: str | None = None,
        model: str | None = None,
    ):
        reply = await handle_message(external_chat_id, text, channel, chat_type, attachments=attachments, execution_mode=execution_mode, model=model)
        if not reply:
            return
        from xclaw.responses import RejectedReply
        if isinstance(reply, RejectedReply):
            yield reply
            return
        chunk_size = 120
        start = 0
        while start < len(reply):
            yield reply[start : start + chunk_size]
            start += chunk_size

    # ── Start channel adapters ────────────────────────────────────────────────
    adapters = []
    adapter_by_channel: dict[str, Any] = {}
    wechat_multi_tenant = None

    if settings.feishu_enabled:
        from xclaw.channels.feishu import FeishuAdapter

        feishu = FeishuAdapter(
            app_id=settings.feishu_app_id,
            app_secret=settings.feishu_app_secret,
            verification_token=settings.feishu_verification_token,
            encrypt_key=settings.feishu_encrypt_key,
            message_handler=lambda cid, text: handle_message(cid, text, "feishu"),
        )
        adapters.append(feishu)
        adapter_by_channel["feishu"] = feishu

    if settings.wecom_enabled:
        from xclaw.channels.wecom import WeComAdapter

        wecom = WeComAdapter(
            corp_id=settings.wecom_corp_id,
            agent_id=settings.wecom_agent_id,
            secret=settings.wecom_secret,
            token=settings.wecom_token,
            encoding_aes_key=settings.wecom_encoding_aes_key,
            message_handler=lambda cid, text: handle_message(cid, text, "wecom"),
        )
        adapters.append(wecom)
        adapter_by_channel["wecom"] = wecom

    if settings.dingtalk_enabled:
        from xclaw.channels.dingtalk import DingTalkAdapter

        dingtalk = DingTalkAdapter(
            app_key=settings.dingtalk_app_key,
            app_secret=settings.dingtalk_app_secret,
            robot_code=settings.dingtalk_robot_code,
            message_handler=lambda cid, text: handle_message(cid, text, "dingtalk"),
        )
        adapters.append(dingtalk)
        adapter_by_channel["dingtalk"] = dingtalk

    if settings.wechat_enabled:
        from xclaw.channels.wechat import WeChatAdapter
        from xclaw.channels.wechat_multi_tenant import WeChatMultiTenantService

        wechat = WeChatAdapter(
            base_url=settings.wechat_base_url,
            account_path=settings.wechat_account_path,
            state_path=settings.wechat_state_path,
            debug_log_path=settings.logs_path / "wechat_context_debug.jsonl",
            qr_total_timeout_seconds=settings.wechat_qr_total_timeout_seconds,
            qr_poll_interval_seconds=settings.wechat_qr_poll_interval_seconds,
            poll_timeout_ms=settings.wechat_poll_timeout_ms,
            max_reply_chars=settings.wechat_max_reply_chars,
            qr_poll_timeout_seconds=settings.wechat_qr_poll_timeout_seconds,
            message_handler=lambda cid, text: handle_message(cid, text, "wechat"),
        )
        adapters.append(wechat)
        adapter_by_channel["wechat"] = wechat
        wechat_multi_tenant = WeChatMultiTenantService(
            db=db,
            base_url=settings.wechat_base_url,
            qr_poll_interval_seconds=settings.wechat_qr_poll_interval_seconds,
            invite_refresh_seconds=settings.wechat_invite_refresh_seconds,
            invite_session_total_timeout_seconds=settings.wechat_invite_session_total_timeout_seconds,
            poll_timeout_ms=settings.wechat_poll_timeout_ms,
            max_reply_chars=settings.wechat_max_reply_chars,
            message_handler=lambda cid, text: handle_message(cid, text, "wechat"),
            debug_log_path=settings.logs_path / "wechat_context_debug.jsonl",
        )

    if settings.wechat_mp_enabled:
        from xclaw.channels.wechat_mp import WeChatMPAdapter

        wechat_mp = WeChatMPAdapter(
            app_id=settings.wechat_mp_app_id,
            app_secret=settings.wechat_mp_app_secret,
            token=settings.wechat_mp_token,
            encoding_aes_key=settings.wechat_mp_encoding_aes_key,
            message_handler=lambda cid, text: handle_message(cid, text, "wechat_mp"),
        )
        adapters.append(wechat_mp)
        adapter_by_channel["wechat_mp"] = wechat_mp

    if settings.qq_enabled:
        from xclaw.channels.qq import DefaultQQSTTClient, QQAdapter

        qq_stt_client = None
        if settings.api_key and (
            settings.base_url or settings.llm_provider in {"openai", "deepseek", "ollama"}
        ):
            default_urls = {
                "openai": "https://api.openai.com/v1",
                "deepseek": "https://api.deepseek.com/v1",
                "ollama": "http://localhost:11434/v1",
            }
            qq_stt_client = DefaultQQSTTClient(
                api_key=settings.api_key,
                base_url=(settings.base_url or default_urls.get(settings.llm_provider, "")).rstrip("/"),
                model="gpt-4o-mini-transcribe",
            )
        qq = QQAdapter(
            app_id=settings.qq_app_id,
            app_secret=settings.qq_app_secret,
            accounts=[account.model_dump() for account in settings.qq_accounts] if settings.qq_accounts else None,
            message_handler=lambda cid, text, chat_type="private": handle_message(cid, text, "qq", chat_type),
            stream_handler=lambda cid, text, chat_type="private": handle_message_stream(cid, text, "qq", chat_type),
            stt_client=qq_stt_client,
        )
        adapters.append(qq)
        adapter_by_channel["qq"] = qq

    async def deliver_scheduled_result(task: dict[str, Any], reply: str) -> None:
        chat_row = await db.get_chat(int(task["chat_id"]))
        if chat_row is None:
            logger.warning(f"Scheduled task {task['id']} target chat is missing")
            return

        channel = chat_row["channel"]
        external_chat_id = str(chat_row["external_chat_id"])
        if channel in {"web", "scheduler"}:
            logger.info(f"Scheduled task {task['id']} produced reply for channel={channel}; no push")
            return

        if channel == "wechat" and external_chat_id.startswith("tenant:"):
            if wechat_multi_tenant is None:
                raise RuntimeError("WeChat multi-tenant service is not available")
            await wechat_multi_tenant.send_response(external_chat_id, reply)
            return

        adapter = adapter_by_channel.get(channel)
        if adapter is None:
            logger.warning(f"No adapter available to deliver scheduled task {task['id']} reply")
            return
        await adapter.send_response(external_chat_id, reply)

    # ── Task scheduler ────────────────────────────────────────────────────────
    from xclaw.scheduler import TaskScheduler

    scheduler = TaskScheduler(
        message_handler=handle_message,
        result_handler=deliver_scheduled_result,
        db=db,
        timezone=settings.timezone,
    )

    # Start all adapters
    for adapter in adapters:
        await adapter.start()
    if wechat_multi_tenant is not None:
        await wechat_multi_tenant.start()
    scheduler.start()

    async def cleanup():
        scheduler.stop()
        try:
            if powercore is not None:
                await powercore.close()
            if wechat_multi_tenant is not None:
                await wechat_multi_tenant.stop()
            if oauth_manager is not None:
                await oauth_manager.close()
            for adapter in adapters:
                await adapter.stop()
                if hasattr(adapter, "close"):
                    await adapter.close()
            for client in mcp_clients:
                await client.close()
        finally:
            await db.close()

    # ── Web server ────────────────────────────────────────────────────────────
    if settings.web_enabled:
        from xclaw.channels.web import create_web_app

        async def handle_progress(external_chat_id: str, include_history: bool = False):
            chat_id = await resolve_message_chat(db, external_chat_id, "web", "private")
            return await powercore.progress(await context_for_chat(chat_id), include_history=include_history)

        async def handle_cancel(external_chat_id: str, run_id: str):
            chat_id = await resolve_message_chat(db, external_chat_id, "web", "private")
            return await powercore.cancel_run(chat_id, run_id)

        from xclaw.openui import OpenUITools

        async def openui_context(external_chat_id: str):
            chat_id = await resolve_message_chat(db, external_chat_id, "web", "private")
            return await tool_context_for_chat(chat_id)

        async def native_receipt_progress(receipt_id: str):
            state = native_progress.get(receipt_id)
            if not state:
                return None
            return {"message": state["message"],
                    "elapsed_seconds": max(0, time.monotonic() - state["started"])}

        web_app = create_web_app(
            receipt_progress_handler=native_receipt_progress if powercore is None else None,
            openui_service=OpenUITools(tools, openui_context) if settings.openui_enabled else None,
            message_handler=lambda cid, text, **options: handle_message(cid, text, "web", **options),
            stream_handler=lambda cid, text, **options: handle_message_stream(cid, text, "web", **options),
            supports_images=powercore is not None,
            progress_handler=handle_progress if powercore else None,
            cancel_handler=handle_cancel if powercore else None,
            startup_handler=(lambda: powercore.restore_background(context_for_chat)) if powercore else None,
            readiness_handler=powercore.check_ready if powercore else None,
            auth_token=settings.web_auth_token,
            rate_limit=settings.rate_limit_per_minute,
            db=db,
            settings=settings,
            multi_user_mode=settings.multi_user_mode,
            tool_registry=tools,
            oauth_manager=oauth_manager,
        )
        if powercore is not None:
            from xclaw.powercore_mcp import create_powercore_mcp_router

            web_app.include_router(
                create_powercore_mcp_router(tools, powercore, tool_context_for_chat),
                prefix="/_powercore/mcp",
            )
        # Register webhook adapters using isinstance checks for safety
        if settings.feishu_enabled:
            from xclaw.channels.feishu import FeishuAdapter
            for adapter in adapters:
                if isinstance(adapter, FeishuAdapter):
                    web_app.state.set_feishu_adapter(adapter)
                    break
        if settings.wechat_enabled:
            from xclaw.channels.wechat import WeChatAdapter
            for adapter in adapters:
                if isinstance(adapter, WeChatAdapter):
                    web_app.state.set_wechat_adapter(adapter)
                    break
            if wechat_multi_tenant is not None:
                web_app.state.set_wechat_multi_tenant_service(wechat_multi_tenant)
        if settings.wechat_mp_enabled:
            from xclaw.channels.wechat_mp import WeChatMPAdapter
            for adapter in adapters:
                if isinstance(adapter, WeChatMPAdapter):
                    web_app.state.set_wechat_mp_adapter(adapter)
                    break
        config = uvicorn.Config(
            web_app,
            host=settings.web_host,
            port=settings.web_port,
            log_level="info",
        )
        server = uvicorn.Server(config)
        logger.info(f"Web server starting on {settings.web_host}:{settings.web_port}")
        try:
            await server.serve()
        finally:
            await cleanup()
    else:
        await cleanup()


async def resolve_message_chat(
    db: Database, external_chat_id: str, channel: str, chat_type: str = "private",
) -> int:
    if channel == "scheduler":
        # Scheduled tasks already carry the owning internal chat ID.
        chat = await db.get_chat(int(external_chat_id))
        if chat is None:
            raise ValueError("Scheduled task chat does not exist")
        return int(chat["id"])
    return await db.get_or_create_chat(channel, external_chat_id, chat_type=chat_type)
