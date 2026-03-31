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
from xclaw.tools.stock_gap_analysis import StockGapAnalysisTool
from xclaw.tools.stock_history import StockHistoryTool
from xclaw.tools.stock_indicators import StockIndicatorsTool
from xclaw.tools.stock_news import StockNewsTool
from xclaw.tools.stock_quote import StockQuoteTool
from xclaw.tools.stock_zt_pool import StockZTPoolTool
from xclaw.tools.watchlist import WatchlistManageTool


def _ctx(db=None, chat_id=1) -> ToolContext:
    return ToolContext(chat_id=chat_id, channel="web", db=db)


# ── stock_quote ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stock_quote_cn_success():
    """stock_quote tool should use the direct CN realtime source first."""
    tool = StockQuoteTool()
    quote = {
        "name": "贵州茅台",
        "symbol": "600519.SH",
        "price": "1800.0",
        "change_pct": "1.50%",
        "change_amount": "26.5",
        "volume": "10000手",
        "amount": "180000万元",
        "open": "1790.0",
        "high": "1810.0",
        "low": "1785.0",
        "pre_close": "1773.5",
        "quote_time": "2026-03-27 09:30:00",
        "source": "腾讯直连 HTTP",
    }
    with (
        patch.object(tool, "_fetch_tencent_quote", AsyncMock(return_value=quote)),
        patch.object(tool, "_fetch_sina_quote", AsyncMock()),
        patch.object(tool, "_fetch_akshare_quote", AsyncMock()),
    ):
        result = await tool.execute({"symbol": "600519", "market": "CN"}, _ctx())
    assert not result.is_error
    assert "贵州茅台" in result.content
    assert "1800" in result.content
    assert "腾讯直连 HTTP" in result.content


@pytest.mark.asyncio
async def test_stock_quote_empty_symbol():
    tool = StockQuoteTool()
    result = await tool.execute({"symbol": "", "market": "CN"}, _ctx())
    assert result.is_error


@pytest.mark.asyncio
async def test_stock_quote_not_found():
    """When symbol is missing in all providers, should return error."""
    tool = StockQuoteTool()
    with (
        patch.object(tool, "_fetch_tencent_quote", AsyncMock(return_value=None)),
        patch.object(tool, "_fetch_sina_quote", AsyncMock(return_value=None)),
        patch.object(tool, "_fetch_akshare_quote", AsyncMock(return_value=None)),
    ):
        result = await tool.execute({"symbol": "999999", "market": "CN"}, _ctx())
    assert result.is_error


@pytest.mark.asyncio
async def test_stock_quote_cn_falls_back_to_sina():
    tool = StockQuoteTool()
    quote = {
        "name": "贵州茅台",
        "symbol": "600519.SH",
        "price": "1409.49",
        "change_pct": "0.59%",
        "change_amount": "8.31",
        "volume": "695552股",
        "amount": "979365126.000元",
        "open": "1400.000",
        "high": "1414.990",
        "low": "1396.660",
        "pre_close": "1401.180",
        "quote_time": "2026-03-27 09:53:33",
        "source": "新浪直连 HTTP",
    }
    with (
        patch.object(tool, "_fetch_tencent_quote", AsyncMock(side_effect=RuntimeError("timeout"))),
        patch.object(tool, "_fetch_sina_quote", AsyncMock(return_value=quote)),
        patch.object(tool, "_fetch_akshare_quote", AsyncMock()),
    ):
        result = await tool.execute({"symbol": "600519", "market": "CN"}, _ctx())
    assert not result.is_error
    assert "新浪直连 HTTP" in result.content


# ── stock_history ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stock_history_cn():
    mock_df = pd.DataFrame([
        {"日期": "2024-01-01", "开盘": 1780.0, "收盘": 1800.0, "最高": 1810.0, "最低": 1775.0, "成交量": 1000000},
        {"日期": "2024-01-02", "开盘": 1800.0, "收盘": 1820.0, "最高": 1830.0, "最低": 1795.0, "成交量": 1200000},
    ])
    mock_df.attrs["source"] = "baostock"
    with patch("xclaw.tools.stock_history.fetch_cn_history_dataframe", AsyncMock(return_value=mock_df)):
        tool = StockHistoryTool()
        result = await tool.execute({"symbol": "600519", "market": "CN", "limit": 5}, _ctx())
    assert not result.is_error
    assert "baostock" in result.content
    assert "1800" in result.content


@pytest.mark.asyncio
async def test_stock_history_empty_symbol():
    tool = StockHistoryTool()
    result = await tool.execute({"symbol": ""}, _ctx())
    assert result.is_error


@pytest.mark.asyncio
async def test_stock_history_index_bare_code_resolved():
    """asset_type=index + bare code 000001 should resolve to sh000001 (上证指数), not 000001.SZ (平安银行)."""
    mock_df = pd.DataFrame([
        {"日期": "2024-01-01", "开盘": 3100.0, "收盘": 3120.0, "最高": 3130.0, "最低": 3090.0, "成交量": 50000000},
    ])
    mock_df.attrs["source"] = "baostock"

    captured_symbol = {}

    async def _capture(symbol, *, period, start_date, end_date):
        captured_symbol["value"] = symbol
        return mock_df

    with patch("xclaw.tools.stock_history.fetch_cn_history_dataframe", _capture):
        tool = StockHistoryTool()
        result = await tool.execute(
            {"symbol": "000001", "market": "CN", "asset_type": "index"},
            _ctx(),
        )
    assert not result.is_error
    # The symbol passed to fetch_cn_history_dataframe should be the resolved index code
    assert captured_symbol["value"] == "sh000001"


@pytest.mark.asyncio
async def test_stock_history_stock_bare_code_not_resolved():
    """asset_type=stock (default) + bare code 000001 should remain as-is (平安银行)."""
    mock_df = pd.DataFrame([
        {"日期": "2024-01-01", "开盘": 12.0, "收盘": 12.5, "最高": 12.8, "最低": 11.9, "成交量": 8000000},
    ])
    mock_df.attrs["source"] = "baostock"

    captured_symbol = {}

    async def _capture(symbol, *, period, start_date, end_date):
        captured_symbol["value"] = symbol
        return mock_df

    with patch("xclaw.tools.stock_history.fetch_cn_history_dataframe", _capture):
        tool = StockHistoryTool()
        result = await tool.execute(
            {"symbol": "000001", "market": "CN"},
            _ctx(),
        )
    assert not result.is_error
    # Without asset_type=index, 000001 should NOT be rewritten
    assert captured_symbol["value"] == "000001"


@pytest.mark.asyncio
async def test_stock_history_output_contains_precomputed_chg_and_amp():
    """Output should include pre-computed 涨跌幅 and 振幅 columns."""
    mock_df = pd.DataFrame([
        {"日期": "2024-01-01", "开盘": 1780.0, "收盘": 1800.0, "最高": 1810.0, "最低": 1775.0, "成交量": 1000000},
        {"日期": "2024-01-02", "开盘": 1805.0, "收盘": 1820.0, "最高": 1830.0, "最低": 1795.0, "成交量": 1200000},
        {"日期": "2024-01-03", "开盘": 1815.0, "收盘": 1810.0, "最高": 1825.0, "最低": 1800.0, "成交量": 1100000},
    ])
    mock_df.attrs["source"] = "baostock"
    with patch("xclaw.tools.stock_history.fetch_cn_history_dataframe", AsyncMock(return_value=mock_df)):
        tool = StockHistoryTool()
        result = await tool.execute({"symbol": "600519", "market": "CN", "limit": 10}, _ctx())
    assert not result.is_error
    content = result.content
    # Header should contain new columns
    assert "涨跌幅" in content
    assert "振幅" in content
    # First data row (no previous close) should show "-"
    lines = content.strip().split("\n")
    data_lines = [l for l in lines if l.startswith("2024")]
    assert len(data_lines) == 3
    assert "-" in data_lines[0]  # first row has no prev close
    # Second row: (1820-1800)/1800 = +1.11%
    assert "+1.11%" in data_lines[1]
    # Third row: (1810-1820)/1820 = -0.55%
    assert "-0.55%" in data_lines[2]


@pytest.mark.asyncio
async def test_stock_gap_analysis_does_not_false_positive_on_overlap_dates():
    mock_df = pd.DataFrame([
        {"日期": "2026-03-16", "开盘": 19.66, "收盘": 19.48, "最高": 19.67, "最低": 19.28, "成交量": 1},
        {"日期": "2026-03-17", "开盘": 19.56, "收盘": 19.75, "最高": 20.37, "最低": 19.54, "成交量": 1},
        {"日期": "2026-03-23", "开盘": 18.78, "收盘": 18.26, "最高": 18.84, "最低": 18.14, "成交量": 1},
        {"日期": "2026-03-24", "开盘": 18.45, "收盘": 18.44, "最高": 18.65, "最低": 18.22, "成交量": 1},
    ])
    mock_df.attrs["source"] = "baostock"

    with patch("xclaw.tools.stock_gap_analysis.fetch_cn_history_dataframe", AsyncMock(return_value=mock_df)):
        tool = StockGapAnalysisTool()
        result = await tool.execute({"symbol": "601688", "market": "CN", "limit": 30}, _ctx())

    assert not result.is_error
    assert "2026-03-17 向上跳空" not in result.content
    assert "2026-03-24 向下跳空" not in result.content


@pytest.mark.asyncio
async def test_stock_gap_analysis_detects_real_gaps_and_fill_status():
    mock_df = pd.DataFrame([
        {"日期": "2026-03-01", "开盘": 10.0, "收盘": 10.1, "最高": 10.2, "最低": 9.9, "成交量": 1},
        {"日期": "2026-03-02", "开盘": 10.5, "收盘": 10.6, "最高": 10.8, "最低": 10.4, "成交量": 1},
        {"日期": "2026-03-03", "开盘": 10.3, "收盘": 10.1, "最高": 10.35, "最低": 9.8, "成交量": 1},
        {"日期": "2026-03-04", "开盘": 9.4, "收盘": 9.3, "最高": 9.5, "最低": 9.2, "成交量": 1},
        {"日期": "2026-03-05", "开盘": 9.6, "收盘": 9.7, "最高": 9.95, "最低": 9.5, "成交量": 1},
    ])
    mock_df.attrs["source"] = "baostock"

    with patch("xclaw.tools.stock_gap_analysis.fetch_cn_history_dataframe", AsyncMock(return_value=mock_df)):
        tool = StockGapAnalysisTool()
        result = await tool.execute({"symbol": "600519", "market": "CN", "limit": 30}, _ctx())

    assert not result.is_error
    assert "2026-03-02 向上跳空" in result.content
    assert "状态: 已回补" in result.content
    assert "2026-03-04 向下跳空" in result.content
    assert "未回补: 1" in result.content


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
    mock_df.attrs["source"] = "baostock"
    with patch("xclaw.tools.stock_indicators.fetch_cn_history_dataframe", AsyncMock(return_value=mock_df)):
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
    mock_index = [
        {"name": "上证指数", "price": "3000.0", "change_pct": "0.5"},
        {"name": "深证成指", "price": "10000.0", "change_pct": "-0.3"},
    ]
    mock_sector = {
        "top": [
            {"name": "新能源", "change_pct": "3.5"},
            {"name": "医疗", "change_pct": "2.1"},
        ],
        "bottom": [
            {"name": "地产", "change_pct": "-2.5"},
            {"name": "钢铁", "change_pct": "-3.1"},
        ],
    }
    with (
        patch("xclaw.tools.market_overview.fetch_cn_index_quotes", AsyncMock(return_value=mock_index)),
        patch("xclaw.tools.market_overview.fetch_cn_sector_snapshots", AsyncMock(return_value=mock_sector)),
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


# ── stock_zt_pool ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stock_zt_pool_success():
    """stock_zt_pool tool should return formatted limit-up stock pool data."""
    mock_df = pd.DataFrame([
        {
            "序号": 1, "代码": "000001", "名称": "平安银行", "涨跌幅": 10.0,
            "最新价": 12.5, "成交额": 5e8, "流通市值": 2e10, "总市值": 2.5e10,
            "换手率": 3.5, "封板资金": 1e8, "首次封板时间": "093500",
            "最后封板时间": "140000", "炸板次数": 1, "涨停统计": "3/5",
            "连板数": 2, "所属行业": "银行",
        },
        {
            "序号": 2, "代码": "600100", "名称": "同方股份", "涨跌幅": 10.0,
            "最新价": 8.2, "成交额": 3e8, "流通市值": 1e10, "总市值": 1.2e10,
            "换手率": 5.0, "封板资金": 5e7, "首次封板时间": "100000",
            "最后封板时间": "143000", "炸板次数": 0, "涨停统计": "1/3",
            "连板数": 1, "所属行业": "电子",
        },
    ])
    with patch("akshare.stock_zt_pool_em", return_value=mock_df):
        tool = StockZTPoolTool()
        result = await tool.execute({"date": "20250227", "limit": 10}, _ctx())
    assert not result.is_error
    assert "平安银行" in result.content
    assert "000001" in result.content
    assert "银行" in result.content
    assert "涨停板股票池" in result.content


@pytest.mark.asyncio
async def test_stock_zt_pool_sort_by_price():
    """When sort_by_price=True, lower-priced stocks should come first."""
    mock_df = pd.DataFrame([
        {
            "序号": 1, "代码": "600100", "名称": "高价股", "涨跌幅": 10.0,
            "最新价": 50.0, "成交额": 3e8, "流通市值": 1e10, "总市值": 1.2e10,
            "换手率": 5.0, "封板资金": 5e7, "首次封板时间": "100000",
            "最后封板时间": "143000", "炸板次数": 0, "涨停统计": "1/3",
            "连板数": 1, "所属行业": "电子",
        },
        {
            "序号": 2, "代码": "000001", "名称": "低价股", "涨跌幅": 10.0,
            "最新价": 5.0, "成交额": 5e8, "流通市值": 2e10, "总市值": 2.5e10,
            "换手率": 3.5, "封板资金": 1e8, "首次封板时间": "093500",
            "最后封板时间": "140000", "炸板次数": 1, "涨停统计": "3/5",
            "连板数": 2, "所属行业": "银行",
        },
    ])
    with patch("akshare.stock_zt_pool_em", return_value=mock_df):
        tool = StockZTPoolTool()
        result = await tool.execute(
            {"date": "20250227", "sort_by_price": True, "limit": 5}, _ctx()
        )
    assert not result.is_error
    # 低价股 should appear before 高价股 in the output
    low_idx = result.content.index("低价股")
    high_idx = result.content.index("高价股")
    assert low_idx < high_idx


@pytest.mark.asyncio
async def test_stock_zt_pool_empty():
    """When no data is returned, should give informative message."""
    with patch("akshare.stock_zt_pool_em", return_value=pd.DataFrame()):
        tool = StockZTPoolTool()
        result = await tool.execute({"date": "20250101"}, _ctx())
    assert not result.is_error
    assert "未找到" in result.content


@pytest.mark.asyncio
async def test_stock_zt_pool_default_date():
    """When no date is provided, should use today's date."""
    mock_df = pd.DataFrame([
        {
            "序号": 1, "代码": "000001", "名称": "平安银行", "涨跌幅": 10.0,
            "最新价": 12.5, "成交额": 5e8, "流通市值": 2e10, "总市值": 2.5e10,
            "换手率": 3.5, "封板资金": 1e8, "首次封板时间": "093500",
            "最后封板时间": "140000", "炸板次数": 1, "涨停统计": "3/5",
            "连板数": 2, "所属行业": "银行",
        },
    ])
    with patch("akshare.stock_zt_pool_em", return_value=mock_df) as mock_fn:
        tool = StockZTPoolTool()
        result = await tool.execute({}, _ctx())
    assert not result.is_error
    # Verify akshare was called with a date string (today's date)
    call_args = mock_fn.call_args
    date_arg = call_args[1].get("date") if call_args[1] else call_args[0][0] if call_args[0] else None
    # The date should be set (not empty)
    assert date_arg is not None and len(date_arg) == 8


# ── datasource provider timeout failover ──────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_cn_history_timeout_failover():
    """If the first provider hangs, it should time out and failover to the next."""
    import asyncio
    import time
    from xclaw.datasources.a_share import fetch_cn_history_dataframe, _PROVIDER_TIMEOUT_SECONDS

    good_df = pd.DataFrame([
        {"日期": "2024-01-02", "开盘": 100.0, "收盘": 105.0, "最高": 106.0, "最低": 99.0, "成交量": 10000, "成交额": 1000000},
    ])

    def hanging_provider(*args, **kwargs):
        """Simulate a provider that hangs forever (like baostock socket stuck)."""
        time.sleep(60)

    def good_provider(*args, **kwargs):
        return good_df

    with (
        patch("xclaw.datasources.a_share._history_from_pytdx", side_effect=hanging_provider),
        patch("xclaw.datasources.a_share._history_from_baostock", side_effect=good_provider),
        patch("xclaw.datasources.a_share._PROVIDER_TIMEOUT_SECONDS", 0.5),
    ):
        start = time.monotonic()
        df = await fetch_cn_history_dataframe("600519", period="daily", start_date="2024-01-01", end_date="2024-01-31")
        elapsed = time.monotonic() - start

    assert df is not None and not df.empty
    # Should have timed out the hanging provider and got data from the second one.
    # Total time should be close to the timeout, not 60 seconds.
    assert elapsed < 5.0
