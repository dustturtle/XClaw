"""Tool: etf_premium_analysis – analyze ETF premium/discount vs NAV."""

from __future__ import annotations

from typing import Any

from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult
from xclaw.tools.market_symbols import normalize_hk_yf_symbol


class ETFPremiumAnalysisTool(Tool):
    @property
    def name(self) -> str:
        return "etf_premium_analysis"

    @property
    def description(self) -> str:
        return "分析 ETF 当前相对净值（NAV）的溢价或折价情况，适合回答 ETF 是否偏离净值、是否出现异常溢价。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "ETF 代码，如 QQQ、SPY、2800。"},
                "market": {
                    "type": "string",
                    "enum": ["US", "HK"],
                    "default": "US",
                    "description": "市场（当前优先支持美股/港股 ETF）。",
                },
            },
            "required": ["symbol"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        import asyncio

        import yfinance as yf  # type: ignore[import]

        symbol = str(params.get("symbol", "")).strip().upper()
        market = str(params.get("market", "US")).upper()
        if not symbol:
            return ToolResult(content="ETF 代码不能为空", is_error=True)

        yf_symbol = normalize_hk_yf_symbol(symbol) if market == "HK" else symbol
        loop = asyncio.get_event_loop()
        ticker = yf.Ticker(yf_symbol)
        try:
            info = await loop.run_in_executor(None, lambda: ticker.info)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"ETF 溢价/折价分析失败: {exc}", is_error=True)

        current_price = info.get("currentPrice") or info.get("regularMarketPrice")
        nav_price = info.get("navPrice")
        if current_price in (None, 0) or nav_price in (None, 0):
            return ToolResult(content=f"未找到 {yf_symbol} 的价格或 NAV 数据", is_error=True)

        premium_pct = (float(current_price) / float(nav_price) - 1) * 100
        state = "溢价" if premium_pct > 0 else "折价" if premium_pct < 0 else "平价"
        interpretation = self._interpret(abs(premium_pct), state)
        volume = info.get("regularMarketVolume", "N/A")

        return ToolResult(
            content="\n".join(
                [
                    f"## {info.get('shortName', yf_symbol)} ETF 溢价/折价分析",
                    f"标的: {yf_symbol}",
                    f"现价: {float(current_price):.2f}",
                    f"NAV: {float(nav_price):.2f}",
                    f"溢价/折价幅度: {premium_pct:+.2f}%（{state}）",
                    f"成交量: {volume}",
                    f"结论: {interpretation}",
                ]
            )
        )

    def _interpret(self, abs_pct: float, state: str) -> str:
        if abs_pct < 0.5:
            return "当前偏离净值不明显，交易价格基本贴近 NAV。"
        if abs_pct < 1.5:
            return f"当前存在中等{state}，适合结合盘中流动性继续观察。"
        return f"当前{state}较明显，需警惕情绪驱动或流动性导致的价格偏离。"

