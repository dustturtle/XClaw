"""Application runtime: initialise all components and wire them together."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import uvicorn
from loguru import logger

from xclaw.agent_engine import AgentContext, agent_loop
from xclaw.config import Settings
from xclaw.db import Database
from xclaw.llm import create_provider
from xclaw.memory import FileMemory, StructuredMemory
from xclaw.oauth import AuthProfileStore, OpenAICodexOAuthManager
from xclaw.skills import build_skill_registry
from xclaw.tools import ToolRegistry
from xclaw.tools.sub_agent import SubAgentTool

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

    # ── Build message handler ─────────────────────────────────────────────────
    async def handle_message(
        external_chat_id: str,
        text: str,
        channel: str = "web",
        chat_type: str = "private",
    ) -> str:
        chat_id = await db.get_or_create_chat(channel, external_chat_id, chat_type=chat_type)
        ctx = AgentContext(
            chat_id=chat_id,
            channel=channel,
            db=db,
            llm=llm,
            tools=tools,
            file_memory=file_memory,
            structured_memory=struct_memory,
            settings=settings,
            scheduler=scheduler,
        )
        return await agent_loop(ctx, text)

    async def handle_message_stream(
        external_chat_id: str,
        text: str,
        channel: str = "web",
        chat_type: str = "private",
    ):
        reply = await handle_message(external_chat_id, text, channel, chat_type)
        if not reply:
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

    # ── Web server ────────────────────────────────────────────────────────────
    if settings.web_enabled:
        from xclaw.channels.web import create_web_app

        web_app = create_web_app(
            message_handler=lambda cid, text: handle_message(cid, text, "web"),
            stream_handler=lambda cid, text: handle_message_stream(cid, text, "web"),
            auth_token=settings.web_auth_token,
            rate_limit=settings.rate_limit_per_minute,
            db=db,
            settings=settings,
            multi_user_mode=settings.multi_user_mode,
            tool_registry=tools,
            oauth_manager=oauth_manager,
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
        await server.serve()

    # Cleanup
    scheduler.stop()
    if wechat_multi_tenant is not None:
        await wechat_multi_tenant.stop()
    await db.close()
    if oauth_manager is not None:
        await oauth_manager.close()
    for adapter in adapters:
        await adapter.stop()
        if hasattr(adapter, "close"):
            await adapter.close()
    for client in mcp_clients:
        await client.close()
