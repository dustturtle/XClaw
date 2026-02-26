"""Tool: stock_fundamentals – fetch financial fundamentals for CN stocks."""

from __future__ import annotations

from typing import Any

from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult


class StockFundamentalsTool(Tool):
    """Get fundamental financial data for a stock (revenue, profit, PE/PB/ROE)."""

    @property
    def name(self) -> str:
        return "stock_fundamentals"

    @property
    def description(self) -> str:
        return "获取股票基本面数据，包括营收、净利润、市盈率（PE）、市净率（PB）、ROE 等财务指标。主要支持 A 股。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "股票代码（A股 6位代码）",
                },
                "market": {
                    "type": "string",
                    "enum": ["CN", "US"],
                    "default": "CN",
                },
            },
            "required": ["symbol"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        import asyncio

        symbol = params.get("symbol", "").strip()
        market = params.get("market", "CN").upper()

        if not symbol:
            return ToolResult(content="股票代码不能为空", is_error=True)

        loop = asyncio.get_event_loop()
        try:
            if market == "CN":
                return await self._cn_fundamentals(symbol, loop)
            else:
                return await self._yf_fundamentals(symbol, loop)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"获取基本面数据失败: {exc}", is_error=True)

    async def _cn_fundamentals(self, symbol: str, loop) -> ToolResult:
        import akshare as ak  # type: ignore[import]

        df = await loop.run_in_executor(
            None, lambda: ak.stock_zh_valuation_baidu(symbol=symbol, indicator="市盈率(TTM)")
        )
        if df is None or df.empty:
            return ToolResult(content=f"未找到 {symbol} 的基本面数据", is_error=True)

        latest = df.iloc[-1]
        lines = [
            f"=== {symbol} 基本面指标 ===",
            f"日期: {latest.get('date', latest.get('日期', 'N/A'))}",
            f"市盈率 PE(TTM): {latest.get('value', latest.get('数值', 'N/A'))}",
        ]
        return ToolResult(content="\n".join(lines))

    async def _yf_fundamentals(self, symbol: str, loop) -> ToolResult:
        import yfinance as yf  # type: ignore[import]

        ticker = yf.Ticker(symbol)
        info = await loop.run_in_executor(None, lambda: ticker.info)
        lines = [
            f"=== {info.get('shortName', symbol)} 基本面指标 ===",
            f"市盈率 PE(TTM): {info.get('trailingPE', 'N/A')}",
            f"远期 PE: {info.get('forwardPE', 'N/A')}",
            f"市净率 PB: {info.get('priceToBook', 'N/A')}",
            f"ROE: {info.get('returnOnEquity', 'N/A')}",
            f"营收增长率: {info.get('revenueGrowth', 'N/A')}",
            f"毛利率: {info.get('grossMargins', 'N/A')}",
            f"净利率: {info.get('profitMargins', 'N/A')}",
            f"市值: {info.get('marketCap', 'N/A')}",
            f"每股收益 EPS: {info.get('trailingEps', 'N/A')}",
        ]
        return ToolResult(content="\n".join(lines))
