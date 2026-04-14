"""Tests for strategy scanning and trade point generation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from xclaw.config import Settings
from xclaw.investment.strategy_engine import TradePointEngine
from xclaw.tools import ToolContext
from xclaw.tools.strategy_scan import StrategyScanTool


def _ctx(db=None) -> ToolContext:
    return ToolContext(
        chat_id=1,
        channel="web",
        db=db,
        settings=Settings(),
    )


def _df_from_closes(closes: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    volumes = volumes or [1_000_000 + i * 10_000 for i in range(len(closes))]
    rows = []
    for i, close in enumerate(closes):
        rows.append(
            {
                "日期": f"2026-03-{i + 1:02d}",
                "开盘": close * 0.99,
                "收盘": close,
                "最高": close * 1.01,
                "最低": close * 0.98,
                "成交量": volumes[i],
            }
        )
    df = pd.DataFrame(rows)
    df.attrs["source"] = "mock"
    return df


@pytest.mark.asyncio
async def test_trade_point_engine_returns_all_strategies_for_cn_symbol():
    df = _df_from_closes(
        [10.0, 10.1, 10.2, 10.3, 10.5, 10.6, 10.8, 11.0, 11.2, 11.25, 11.3, 11.35]
    )

    with patch(
        "xclaw.investment.strategy_engine.fetch_cn_history_dataframe",
        AsyncMock(return_value=df),
    ):
        engine = TradePointEngine(Settings())
        results = await engine.scan_symbol("600519", market="CN")

    assert len(results) == 11
    assert any(r["strategy_id"] == "bull_trend" for r in results)
    assert any(r["strategy_id"] == "chan_theory" for r in results)


@pytest.mark.asyncio
async def test_trade_point_engine_marks_bull_trend_as_valuable_when_structure_is_strong():
    df = _df_from_closes(
        [10.0, 10.05, 10.1, 10.18, 10.25, 10.35, 10.45, 10.55, 10.7, 10.82, 10.9, 10.96]
    )

    with patch(
        "xclaw.investment.strategy_engine.fetch_cn_history_dataframe",
        AsyncMock(return_value=df),
    ):
        engine = TradePointEngine(Settings())
        results = await engine.scan_symbol("600519", market="CN")
        valuable = engine.filter_valuable_strategies(results)

    bull_trend = next(r for r in results if r["strategy_id"] == "bull_trend")
    assert bull_trend["signal_status"] in {"triggered", "near_trigger"}
    assert bull_trend["buy_zone"]
    assert bull_trend["stop_loss"]
    assert any(r["strategy_id"] == "bull_trend" for r in valuable)


@pytest.mark.asyncio
async def test_trade_point_engine_detects_recent_ma_golden_cross():
    closes = [10.0, 10.0, 10.0, 10.0, 10.0, 9.9, 9.8, 9.7, 9.8, 10.0, 10.4, 10.9]
    df = _df_from_closes(closes, volumes=[800_000] * 10 + [1_500_000, 1_800_000])

    with patch(
        "xclaw.investment.strategy_engine.fetch_cn_history_dataframe",
        AsyncMock(return_value=df),
    ):
        engine = TradePointEngine(Settings())
        results = await engine.scan_symbol("600519", market="CN")

    golden_cross = next(r for r in results if r["strategy_id"] == "ma_golden_cross")
    assert golden_cross["signal_status"] in {"triggered", "near_trigger"}
    assert golden_cross["trigger_condition"]


@pytest.mark.asyncio
async def test_strategy_scan_tool_persists_strategy_run(db):
    df = _df_from_closes(
        [10.0, 10.05, 10.1, 10.18, 10.25, 10.35, 10.45, 10.55, 10.7, 10.82, 10.9, 10.96]
    )
    tool = StrategyScanTool()
    chat_id = await db.get_or_create_chat("web", "strategy_scan_user")
    ctx = ToolContext(
        chat_id=chat_id,
        channel="web",
        db=db,
        settings=Settings(),
    )

    with patch(
        "xclaw.investment.strategy_engine.fetch_cn_history_dataframe",
        AsyncMock(return_value=df),
    ):
        result = await tool.execute(
            {"symbol": "600519", "market": "CN", "output_mode": "summary"},
            ctx,
        )

    assert not result.is_error
    assert "bull_trend" in result.content
    runs = await db.list_strategy_runs(chat_id, limit=5)
    assert len(runs) == 1
    assert runs[0]["symbol"] == "600519"
    assert runs[0]["valuable_strategies"]
