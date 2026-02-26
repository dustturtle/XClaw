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
from xclaw.tools import ToolRegistry
from xclaw.tools.file_tools import ReadFileTool, WriteFileTool
from xclaw.tools.market_overview import MarketOverviewTool
from xclaw.tools.memory_tools import (
    ReadMemoryTool,
    StructuredMemoryReadTool,
    StructuredMemoryUpdateTool,
    WriteMemoryTool,
)
from xclaw.tools.portfolio import PortfolioManageTool
from xclaw.tools.schedule import (
    CancelScheduledTaskTool,
    ListScheduledTasksTool,
    ScheduleTaskTool,
)
from xclaw.tools.stock_fundamentals import StockFundamentalsTool
from xclaw.tools.stock_history import StockHistoryTool
from xclaw.tools.stock_indicators import StockIndicatorsTool
from xclaw.tools.stock_news import StockNewsTool
from xclaw.tools.stock_quote import StockQuoteTool
from xclaw.tools.watchlist import WatchlistManageTool
from xclaw.tools.web_fetch import WebFetchTool
from xclaw.tools.web_search import WebSearchTool
from xclaw.tools.bash_tool import BashTool
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
    registry = ToolRegistry()
    registry.register(WebSearchTool())
    registry.register(WebFetchTool())
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(ReadMemoryTool())
    registry.register(WriteMemoryTool())
    registry.register(StructuredMemoryReadTool())
    registry.register(StructuredMemoryUpdateTool())
    registry.register(ScheduleTaskTool())
    registry.register(ListScheduledTasksTool())
    registry.register(CancelScheduledTaskTool())
    # Investment tools
    registry.register(StockQuoteTool())
    registry.register(StockHistoryTool())
    registry.register(StockIndicatorsTool())
    registry.register(StockFundamentalsTool())
    registry.register(StockNewsTool())
    registry.register(WatchlistManageTool())
    registry.register(PortfolioManageTool())
    registry.register(MarketOverviewTool())
    # Optional tools
    if settings.bash_enabled:
        registry.register(BashTool())
    registry.register(SubAgentTool(registry))
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

    # LLM Provider
    llm = create_provider(settings.llm_provider, settings.api_key, settings.model)

    # Memory
    file_memory = FileMemory(settings.groups_path)
    struct_memory = StructuredMemory(db)

    # Tool registry
    tools = _build_tool_registry(settings)

    # ── Build message handler ─────────────────────────────────────────────────
    async def handle_message(external_chat_id: str, text: str, channel: str = "web") -> str:
        chat_id = await db.get_or_create_chat(channel, external_chat_id)
        ctx = AgentContext(
            chat_id=chat_id,
            channel=channel,
            db=db,
            llm=llm,
            tools=tools,
            file_memory=file_memory,
            structured_memory=struct_memory,
            settings=settings,
        )
        return await agent_loop(ctx, text)

    # ── Task scheduler ────────────────────────────────────────────────────────
    from xclaw.scheduler import TaskScheduler

    scheduler = TaskScheduler(
        message_handler=handle_message,
        db=db,
        timezone=settings.timezone,
    )
    scheduler.start()

    # ── Start channel adapters ────────────────────────────────────────────────
    adapters = []

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

    if settings.dingtalk_enabled:
        from xclaw.channels.dingtalk import DingTalkAdapter

        dingtalk = DingTalkAdapter(
            app_key=settings.dingtalk_app_key,
            app_secret=settings.dingtalk_app_secret,
            robot_code=settings.dingtalk_robot_code,
            message_handler=lambda cid, text: handle_message(cid, text, "dingtalk"),
        )
        adapters.append(dingtalk)

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

    # Start all adapters
    for adapter in adapters:
        await adapter.start()

    # ── Web server ────────────────────────────────────────────────────────────
    if settings.web_enabled:
        from xclaw.channels.web import create_web_app

        web_app = create_web_app(
            message_handler=lambda cid, text: handle_message(cid, text, "web"),
            auth_token=settings.web_auth_token,
            rate_limit=settings.rate_limit_per_minute,
            db=db,
            settings=settings,
        )
        # Register webhook adapters using isinstance checks for safety
        if settings.feishu_enabled:
            from xclaw.channels.feishu import FeishuAdapter
            for adapter in adapters:
                if isinstance(adapter, FeishuAdapter):
                    web_app.state.set_feishu_adapter(adapter)
                    break
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
    await db.close()
    for adapter in adapters:
        await adapter.stop()
