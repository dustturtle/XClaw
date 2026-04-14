"""Tests for investment daily report generation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from xclaw.config import Settings
from xclaw.investment.report_service import InvestmentReportService
from xclaw.tools import ToolContext
from xclaw.tools.investment_report import InvestmentReportTool


@pytest.mark.asyncio
async def test_investment_report_service_generates_from_watchlist_and_persists(db):
    chat_id = await db.get_or_create_chat("web", "report_service_user")
    await db.add_to_watchlist(chat_id, "600519", "CN", name="贵州茅台")
    await db.add_to_watchlist(chat_id, "000001", "CN", name="平安银行")

    fake_scan = [
        {
            "strategy_id": "bull_trend",
            "display_name": "默认多头趋势",
            "signal_status": "triggered",
            "bias_score": 82,
            "buy_zone": "10.10-10.20",
            "stop_loss": "9.80",
            "target_1": "10.80",
            "trigger_condition": "回踩 MA5 企稳",
            "risk_notes": "跌破 MA20 失效",
            "why_not_trade": "",
            "tier": "rule",
        }
    ]

    service = InvestmentReportService(
        db=db,
        settings=Settings(strategy_report_max_symbols=10),
    )

    with (
        patch.object(service, "_build_market_overview", AsyncMock(return_value="市场概览：偏暖")),
        patch.object(service._engine, "scan_symbol", AsyncMock(return_value=fake_scan)),
    ):
        report = await service.generate_report(chat_id=chat_id, trigger_source="manual")

    assert report["symbol_count"] == 2
    assert "自选股日报" in report["title"]
    assert "市场概览：偏暖" in report["content_markdown"]
    assert "bull_trend" in report["content_markdown"]

    latest = await db.get_latest_investment_report(chat_id)
    assert latest is not None
    assert latest["id"] == report["id"]


@pytest.mark.asyncio
async def test_investment_report_tool_supports_latest_and_history(db):
    chat_id = await db.get_or_create_chat("web", "report_tool_user")
    await db.add_investment_report(
        chat_id=chat_id,
        report_type="daily_watchlist",
        title="2026-04-14 自选股日报",
        summary="1 只偏多",
        content_markdown="# 报告正文",
        symbol_count=1,
        trigger_source="manual",
    )

    tool = InvestmentReportTool()
    ctx = ToolContext(
        chat_id=chat_id,
        channel="web",
        db=db,
        settings=Settings(),
    )

    latest = await tool.execute({"action": "latest"}, ctx)
    history = await tool.execute({"action": "history", "limit": 5}, ctx)

    assert not latest.is_error
    assert "2026-04-14 自选股日报" in latest.content
    assert not history.is_error
    assert "1 只偏多" in history.content
