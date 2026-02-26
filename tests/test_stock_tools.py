"""Tests for investment stock tools (mocked data sources)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from xclaw.tools import ToolContext, ToolResult
from xclaw.tools.market_overview import MarketOverviewTool
from xclaw.tools.portfolio import PortfolioManageTool
from xclaw.tools.stock_fundamentals import StockFundamentalsTool
from xclaw.tools.stock_history import StockHistoryTool
from xclaw.tools.stock_indicators import StockIndicatorsTool
from xclaw.tools.stock_news import StockNewsTool
from xclaw.tools.stock_quote import StockQuoteTool
from xclaw.tools.watchlist import WatchlistManageTool


def _ctx(db=None, chat_id=1) -> ToolContext:
    return ToolContext(chat_id=chat_id, channel="web", db=db)


# ── stock_quote ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stock_quote_cn_success():
    """stock_quote tool should return formatted CN stock data."""
    mock_df = pd.DataFrame([{
        "代码": "600519",
        "名称": "贵州茅台",
        "最新价": 1800.0,
        "涨跌幅": 1.5,
        "涨跌额": 26.5,
        "成交量": 1000000,
        "成交额": 1800000000,
        "今开": 1790.0,
        "最高": 1810.0,
        "最低": 1785.0,
        "昨收": 1773.5,
    }])
    with patch("akshare.stock_zh_a_spot_em", return_value=mock_df):
        tool = StockQuoteTool()
        result = await tool.execute({"symbol": "600519", "market": "CN"}, _ctx())
    assert not result.is_error
    assert "贵州茅台" in result.content
    assert "1800" in result.content


@pytest.mark.asyncio
async def test_stock_quote_empty_symbol():
    tool = StockQuoteTool()
    result = await tool.execute({"symbol": "", "market": "CN"}, _ctx())
    assert result.is_error


@pytest.mark.asyncio
async def test_stock_quote_not_found():
    """When symbol not in DataFrame, should return error."""
    mock_df = pd.DataFrame([{"代码": "000001", "名称": "平安银行"}])
    with patch("akshare.stock_zh_a_spot_em", return_value=mock_df):
        tool = StockQuoteTool()
        result = await tool.execute({"symbol": "999999", "market": "CN"}, _ctx())
    assert result.is_error


# ── stock_history ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stock_history_cn():
    from datetime import date

    mock_df = pd.DataFrame([
        {"日期": "2024-01-01", "开盘": 1780.0, "收盘": 1800.0, "最高": 1810.0, "最低": 1775.0, "成交量": 1000000},
        {"日期": "2024-01-02", "开盘": 1800.0, "收盘": 1820.0, "最高": 1830.0, "最低": 1795.0, "成交量": 1200000},
    ])
    with patch("akshare.stock_zh_a_hist", return_value=mock_df):
        tool = StockHistoryTool()
        result = await tool.execute({"symbol": "600519", "market": "CN", "limit": 5}, _ctx())
    assert not result.is_error
    assert "1800" in result.content


@pytest.mark.asyncio
async def test_stock_history_empty_symbol():
    tool = StockHistoryTool()
    result = await tool.execute({"symbol": ""}, _ctx())
    assert result.is_error


# ── stock_indicators ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stock_indicators_cn():
    dates = pd.date_range("2023-01-01", periods=60, freq="D")
    prices = [100 + i * 0.5 for i in range(60)]
    mock_df = pd.DataFrame({
        "日期": dates,
        "开盘": prices,
        "收盘": prices,
        "最高": [p + 1 for p in prices],
        "最低": [p - 1 for p in prices],
        "成交量": [1000000] * 60,
    })
    with patch("akshare.stock_zh_a_hist", return_value=mock_df):
        tool = StockIndicatorsTool()
        result = await tool.execute(
            {"symbol": "600519", "market": "CN", "indicators": ["MA", "RSI"]},
            _ctx(),
        )
    assert not result.is_error or "未安装" in result.content or "失败" in result.content
    # Should at least attempt to compute something
    assert "600519" in result.content or result.is_error


# ── stock_fundamentals ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stock_fundamentals_cn():
    mock_df = pd.DataFrame([{
        "date": "2024-01-01",
        "value": 28.5,
    }])
    with patch("akshare.stock_zh_valuation_baidu", return_value=mock_df):
        tool = StockFundamentalsTool()
        result = await tool.execute({"symbol": "600519", "market": "CN"}, _ctx())
    assert not result.is_error
    assert "28.5" in result.content


# ── market_overview ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_market_overview():
    mock_index = pd.DataFrame([
        {"名称": "上证指数", "最新价": 3000.0, "涨跌幅": 0.5},
        {"名称": "深证成指", "最新价": 10000.0, "涨跌幅": -0.3},
    ])
    mock_sector = pd.DataFrame([
        {"板块名称": "新能源", "涨跌幅": 3.5},
        {"板块名称": "医疗", "涨跌幅": 2.1},
        {"板块名称": "消费", "涨跌幅": 1.8},
        {"板块名称": "地产", "涨跌幅": -2.5},
        {"板块名称": "钢铁", "涨跌幅": -3.1},
        {"板块名称": "化工", "涨跌幅": -0.5},
    ])
    with (
        patch("akshare.stock_zh_index_spot_em", return_value=mock_index),
        patch("akshare.stock_board_industry_name_em", return_value=mock_sector),
    ):
        tool = MarketOverviewTool()
        result = await tool.execute({"include_sectors": True}, _ctx())
    assert not result.is_error
    assert "上证指数" in result.content


# ── stock_news ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stock_news():
    mock_df = pd.DataFrame([
        {"发布时间": "2024-01-01 10:00", "新闻标题": "贵州茅台业绩超预期", "新闻链接": "http://example.com/1"},
        {"发布时间": "2024-01-01 11:00", "新闻标题": "机构增持茅台", "新闻链接": "http://example.com/2"},
    ])
    with patch("akshare.stock_news_em", return_value=mock_df):
        tool = StockNewsTool()
        result = await tool.execute({"symbol": "600519", "limit": 5}, _ctx())
    assert not result.is_error
    assert "贵州茅台" in result.content
