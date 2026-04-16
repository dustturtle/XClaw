"""Tool: stock_liquidity – analyze trading liquidity of a stock or ETF."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd

from xclaw.datasources.a_share import fetch_cn_history_dataframe
from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult
from xclaw.tools.market_symbols import normalize_hk_yf_symbol


class StockLiquidityTool(Tool):
    @property
    def name(self) -> str:
        return "stock_liquidity"

    @property
    def description(self) -> str:
        return "分析股票或 ETF 的流动性，包括日均成交额、波动幅度和 Amihud 冲击成本代理指标。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "标的代码。"},
                "market": {
                    "type": "string",
                    "enum": ["CN", "US", "HK"],
                    "default": "CN",
                },
                "lookback_days": {
                    "type": "integer",
                    "default": 60,
                    "description": "回看自然日天数。",
                },
            },
            "required": ["symbol"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        symbol = str(params.get("symbol", "")).strip().upper()
        market = str(params.get("market", "CN")).upper()
        lookback_days = max(20, min(int(params.get("lookback_days", 60)), 365))
        if not symbol:
            return ToolResult(content="标的代码不能为空", is_error=True)

        end_date = date.today().strftime("%Y-%m-%d")
        start_date = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        try:
            if market == "CN":
                df = await fetch_cn_history_dataframe(
                    symbol,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                )
            else:
                df = await self._fetch_yfinance_history(symbol, market, start_date, end_date)
            if df is None or df.empty:
                return ToolResult(content=f"未找到 {symbol} 的流动性数据", is_error=True)
            return ToolResult(content=self._format_report(df, symbol, market))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"流动性分析失败: {exc}", is_error=True)

    async def _fetch_yfinance_history(
        self,
        symbol: str,
        market: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame | None:
        import asyncio

        import yfinance as yf  # type: ignore[import]

        yf_symbol = normalize_hk_yf_symbol(symbol) if market == "HK" else symbol
        ticker = yf.Ticker(yf_symbol)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: ticker.history(start=start_date, end=end_date, interval="1d"))

    def _format_report(self, df: pd.DataFrame, symbol: str, market: str) -> str:
        normalized = self._normalize_dataframe(df)
        returns = normalized["close"].pct_change().abs()
        traded_value = normalized["close"] * normalized["volume"]
        amplitude = (normalized["high"] - normalized["low"]) / normalized["close"].shift(1)
        amihud = (returns / traded_value.replace(0, pd.NA)).dropna()

        avg_value = traded_value.mean()
        avg_volume = normalized["volume"].mean()
        avg_amp = amplitude.dropna().mean() * 100 if not amplitude.dropna().empty else 0.0
        amihud_value = amihud.mean() if not amihud.empty else 0.0

        return "\n".join(
            [
                f"## {symbol} 流动性分析",
                f"市场: {market} | 样本数: {len(normalized)}",
                f"日均成交量: {avg_volume:,.0f}",
                f"日均成交额: {avg_value:,.2f}",
                f"平均振幅: {avg_amp:.2f}%",
                f"Amihud 冲击成本代理: {amihud_value:.8f}",
                f"结论: {self._interpret_liquidity(avg_value, amihud_value)}",
            ]
        )

    def _normalize_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        if {"收盘", "最高", "最低", "成交量"}.issubset(df.columns):
            return pd.DataFrame(
                {
                    "close": pd.to_numeric(df["收盘"], errors="coerce"),
                    "high": pd.to_numeric(df["最高"], errors="coerce"),
                    "low": pd.to_numeric(df["最低"], errors="coerce"),
                    "volume": pd.to_numeric(df["成交量"], errors="coerce"),
                }
            ).dropna()
        return pd.DataFrame(
            {
                "close": pd.to_numeric(df["Close"], errors="coerce"),
                "high": pd.to_numeric(df["High"], errors="coerce"),
                "low": pd.to_numeric(df["Low"], errors="coerce"),
                "volume": pd.to_numeric(df["Volume"], errors="coerce"),
            }
        ).dropna()

    def _interpret_liquidity(self, avg_value: float, amihud: float) -> str:
        if avg_value >= 50_000_000 and amihud < 1e-7:
            return "流动性较好，大额交易的冲击成本相对可控。"
        if avg_value >= 10_000_000 and amihud < 5e-7:
            return "流动性中等，常规仓位进出问题不大，但仍需关注成交时段。"
        return "流动性偏弱，建议控制下单规模并关注盘口冲击。"
