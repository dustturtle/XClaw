"""Tool: stock_quote – get real-time/delayed stock quote."""

from __future__ import annotations

from typing import Any

from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult


class StockQuoteTool(Tool):
    """Fetch current stock quote (price, change, volume)."""

    @property
    def name(self) -> str:
        return "stock_quote"

    @property
    def description(self) -> str:
        return "获取股票实时行情，包括当前价格、涨跌幅、成交量等。支持 A 股（akshare）和美股（yfinance）。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "股票代码，如 '600519'（A股）、'00700'（港股）或 'AAPL'（美股）",
                },
                "market": {
                    "type": "string",
                    "enum": ["CN", "US", "HK"],
                    "description": "市场（默认 CN）",
                    "default": "CN",
                },
            },
            "required": ["symbol"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        symbol = params.get("symbol", "").strip()
        market = params.get("market", "CN").upper()
        if not symbol:
            return ToolResult(content="股票代码不能为空", is_error=True)
        try:
            if market == "CN":
                return await self._cn_quote(symbol)
            elif market in ("US", "HK"):
                return await self._yfinance_quote(symbol, market)
            else:
                return ToolResult(content=f"不支持的市场: {market}", is_error=True)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"获取行情失败: {exc}", is_error=True)

    async def _cn_quote(self, symbol: str) -> ToolResult:
        import asyncio

        import akshare as ak  # type: ignore[import]

        loop = asyncio.get_event_loop()
        df = await loop.run_in_executor(None, lambda: ak.stock_zh_a_spot_em())
        row = df[df["代码"] == symbol]
        if row.empty:
            return ToolResult(content=f"未找到股票 {symbol}", is_error=True)
        r = row.iloc[0]
        text = (
            f"股票: {r.get('名称', symbol)} ({symbol})\n"
            f"当前价: {r.get('最新价', 'N/A')}\n"
            f"涨跌幅: {r.get('涨跌幅', 'N/A')}%\n"
            f"涨跌额: {r.get('涨跌额', 'N/A')}\n"
            f"成交量: {r.get('成交量', 'N/A')}\n"
            f"成交额: {r.get('成交额', 'N/A')}\n"
            f"今开: {r.get('今开', 'N/A')}\n"
            f"最高: {r.get('最高', 'N/A')}\n"
            f"最低: {r.get('最低', 'N/A')}\n"
            f"昨收: {r.get('昨收', 'N/A')}"
        )
        return ToolResult(content=text)

    async def _yfinance_quote(self, symbol: str, market: str) -> ToolResult:
        import asyncio

        import yfinance as yf  # type: ignore[import]

        # Adjust symbol for HK market
        yf_symbol = f"{symbol}.HK" if market == "HK" and not symbol.endswith(".HK") else symbol
        loop = asyncio.get_event_loop()
        ticker = await loop.run_in_executor(None, lambda: yf.Ticker(yf_symbol))
        info = await loop.run_in_executor(None, lambda: ticker.info)  # type: ignore[union-attr]

        current_price = info.get("currentPrice") or info.get("regularMarketPrice", "N/A")
        prev_close = info.get("previousClose", "N/A")
        change_pct = (
            f"{((current_price - prev_close) / prev_close * 100):.2f}%"
            if isinstance(current_price, (int, float)) and isinstance(prev_close, (int, float))
            else "N/A"
        )
        text = (
            f"股票: {info.get('shortName', yf_symbol)} ({yf_symbol})\n"
            f"当前价: {current_price}\n"
            f"涨跌幅: {change_pct}\n"
            f"市值: {info.get('marketCap', 'N/A')}\n"
            f"今开: {info.get('open', 'N/A')}\n"
            f"最高: {info.get('dayHigh', 'N/A')}\n"
            f"最低: {info.get('dayLow', 'N/A')}\n"
            f"52周最高: {info.get('fiftyTwoWeekHigh', 'N/A')}\n"
            f"52周最低: {info.get('fiftyTwoWeekLow', 'N/A')}"
        )
        return ToolResult(content=text)
