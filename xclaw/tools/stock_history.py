"""Tool: stock_history – fetch historical OHLCV K-line data."""

from __future__ import annotations

from typing import Any

from xclaw.datasources.a_share import (
    _INDEX_BARE_TO_PREFIXED,
    fetch_cn_history_dataframe,
)
from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult


class StockHistoryTool(Tool):
    """Fetch historical candlestick (OHLCV) data for a stock."""

    @property
    def name(self) -> str:
        return "stock_history"

    @property
    def description(self) -> str:
        return (
            "获取股票或指数历史 K 线数据（日/周/月线），包括开盘价、收盘价、最高价、最低价、成交量。"
            "查询指数（如上证指数、深证成指）时请将 asset_type 设为 index。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "股票代码",
                },
                "market": {
                    "type": "string",
                    "enum": ["CN", "US", "HK"],
                    "default": "CN",
                },
                "period": {
                    "type": "string",
                    "enum": ["daily", "weekly", "monthly"],
                    "description": "K 线周期（默认 daily）",
                    "default": "daily",
                },
                "start_date": {
                    "type": "string",
                    "description": "开始日期，格式 YYYY-MM-DD（默认 90 天前）",
                },
                "end_date": {
                    "type": "string",
                    "description": "结束日期，格式 YYYY-MM-DD（默认今天）",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回条数（默认 30）",
                    "default": 30,
                },
                "asset_type": {
                    "type": "string",
                    "enum": ["stock", "index"],
                    "default": "stock",
                    "description": "资产类型：stock=个股，index=指数。查询指数（如上证指数、沪深300）时必须传 index。",
                },
            },
            "required": ["symbol"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        import asyncio
        from datetime import date, timedelta

        symbol = params.get("symbol", "").strip()
        market = params.get("market", "CN").upper()
        period = params.get("period", "daily")
        asset_type = params.get("asset_type", "stock")
        limit = min(int(params.get("limit", 30)), 120)

        # When asset_type is index and a bare 6-digit code is given,
        # resolve it to the prefixed index code (e.g. 000001 → sh000001)
        if asset_type == "index" and market == "CN":
            bare = symbol.upper().replace(".", "").replace("SH", "").replace("SZ", "")
            if bare in _INDEX_BARE_TO_PREFIXED:
                symbol = _INDEX_BARE_TO_PREFIXED[bare]

        # Date defaults
        end_date = params.get("end_date") or date.today().strftime("%Y-%m-%d")
        start_date = params.get("start_date") or (
            date.today() - timedelta(days=90)
        ).strftime("%Y-%m-%d")

        if not symbol:
            return ToolResult(content="股票代码不能为空", is_error=True)

        try:
            if market == "CN":
                return await self._cn_history(symbol, period, start_date, end_date, limit, asyncio.get_event_loop())
            else:
                return await self._yf_history(symbol, market, period, start_date, end_date, limit, asyncio.get_event_loop())
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"获取历史数据失败: {exc}", is_error=True)

    async def _cn_history(self, symbol, period, start_date, end_date, limit, loop) -> ToolResult:
        df = await fetch_cn_history_dataframe(
            symbol,
            period=period,
            start_date=start_date,
            end_date=end_date,
        )
        if df is None or df.empty:
            return ToolResult(content=f"未找到 {symbol} 的历史数据", is_error=True)
        df = df.tail(limit)
        source = df.attrs.get("source", "unknown")
        lines = [
            f"数据源: {source}",
            f"{'日期':<12} {'开盘':>8} {'收盘':>8} {'最高':>8} {'最低':>8} {'成交量':>12}",
        ]
        for _, row in df.iterrows():
            lines.append(
                f"{str(row.get('日期', '')):<12} "
                f"{row.get('开盘', 0):>8.2f} "
                f"{row.get('收盘', 0):>8.2f} "
                f"{row.get('最高', 0):>8.2f} "
                f"{row.get('最低', 0):>8.2f} "
                f"{row.get('成交量', 0):>12,.0f}"
            )
        return ToolResult(content="\n".join(lines))

    async def _yf_history(self, symbol, market, period, start_date, end_date, limit, loop) -> ToolResult:
        import yfinance as yf  # type: ignore[import]

        yf_symbol = f"{symbol}.HK" if market == "HK" and not symbol.endswith(".HK") else symbol
        period_map = {"daily": "1d", "weekly": "1wk", "monthly": "1mo"}
        yf_interval = period_map.get(period, "1d")
        ticker = yf.Ticker(yf_symbol)
        df = await loop.run_in_executor(
            None,
            lambda: ticker.history(start=start_date, end=end_date, interval=yf_interval),
        )
        if df is None or df.empty:
            return ToolResult(content=f"未找到 {yf_symbol} 的历史数据", is_error=True)
        df = df.tail(limit)
        lines = [f"{'日期':<12} {'开盘':>8} {'收盘':>8} {'最高':>8} {'最低':>8} {'成交量':>12}"]
        for idx, row in df.iterrows():
            lines.append(
                f"{str(idx)[:10]:<12} "
                f"{row.get('Open', 0):>8.2f} "
                f"{row.get('Close', 0):>8.2f} "
                f"{row.get('High', 0):>8.2f} "
                f"{row.get('Low', 0):>8.2f} "
                f"{row.get('Volume', 0):>12,.0f}"
            )
        return ToolResult(content="\n".join(lines))
