"""Tool: stock_indicators – calculate technical indicators (MA/MACD/RSI/KDJ/BOLL)."""

from __future__ import annotations

from typing import Any

from xclaw.datasources.a_share import fetch_cn_history_dataframe
from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult
from xclaw.tools.market_symbols import normalize_hk_yf_symbol


class StockIndicatorsTool(Tool):
    """Calculate technical indicators for a stock using pandas-ta."""

    @property
    def name(self) -> str:
        return "stock_indicators"

    @property
    def description(self) -> str:
        return "计算股票技术指标，支持 MA（移动平均）、MACD、RSI、KDJ、BOLL（布林带）等。"

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
                "indicators": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["MA", "MACD", "RSI", "KDJ", "BOLL"]},
                    "description": "需要计算的指标列表（默认 ['MA', 'MACD', 'RSI']）",
                    "default": ["MA", "MACD", "RSI"],
                },
                "period_days": {
                    "type": "integer",
                    "description": "计算所需历史数据天数（默认 120）",
                    "default": 120,
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
        indicators = params.get("indicators", ["MA", "MACD", "RSI"])
        period_days = int(params.get("period_days", 120))

        if not symbol:
            return ToolResult(content="股票代码不能为空", is_error=True)

        start_date = (date.today() - timedelta(days=period_days)).strftime("%Y%m%d")
        end_date = date.today().strftime("%Y%m%d")

        try:
            loop = asyncio.get_event_loop()
            df = await self._fetch_data(symbol, market, start_date, end_date, loop)
            if df is None or df.empty:
                return ToolResult(content=f"无法获取 {symbol} 的历史数据", is_error=True)
            return self._compute_indicators(df, indicators, symbol, df.attrs.get("source"))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"计算指标失败: {exc}", is_error=True)

    async def _fetch_data(self, symbol, market, start_date, end_date, loop):
        if market == "CN":
            return await fetch_cn_history_dataframe(
                symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
            )
        else:
            import yfinance as yf  # type: ignore[import]
            from datetime import date, timedelta

            yf_symbol = normalize_hk_yf_symbol(symbol) if market == "HK" else symbol
            s = start_date[:4] + "-" + start_date[4:6] + "-" + start_date[6:]
            e = end_date[:4] + "-" + end_date[4:6] + "-" + end_date[6:]
            ticker = yf.Ticker(yf_symbol)
            df = await loop.run_in_executor(None, lambda: ticker.history(start=s, end=e))
            if df is not None and not df.empty:
                df = df.rename(columns={"Open": "开盘", "Close": "收盘", "High": "最高", "Low": "最低", "Volume": "成交量"})
            return df

    def _compute_indicators(self, df, indicators: list[str], symbol: str, source: str | None = None) -> ToolResult:
        try:
            import pandas_ta as ta  # type: ignore[import]
        except ImportError:
            return ToolResult(content="pandas-ta 未安装，无法计算技术指标", is_error=True)

        close = df["收盘"] if "收盘" in df.columns else df.get("Close")
        high = df["最高"] if "最高" in df.columns else df.get("High")
        low = df["最低"] if "最低" in df.columns else df.get("Low")

        lines = [f"=== {symbol} 技术指标分析 ==="]
        if source:
            lines.append(f"历史数据源: {source}")
        latest_close = float(close.iloc[-1]) if close is not None else None
        if latest_close:
            lines.append(f"最新收盘价: {latest_close:.2f}")

        if "MA" in indicators:
            for n in [5, 10, 20, 60]:
                if len(close) >= n:
                    ma = close.rolling(n).mean().iloc[-1]
                    signal = "↑ 价格 > MA" if latest_close and latest_close > ma else "↓ 价格 < MA"
                    lines.append(f"MA{n}: {ma:.2f}  {signal}")

        if "MACD" in indicators and close is not None:
            macd = ta.macd(close)
            if macd is not None and not macd.empty:
                macd_val = float(macd.iloc[-1, 0])  # MACD_12_26_9
                signal_val = float(macd.iloc[-1, 1])  # MACDs_12_26_9
                hist_val = float(macd.iloc[-1, 2])  # MACDh_12_26_9
                cross = "金叉(MACD>Signal)" if macd_val > signal_val else "死叉(MACD<Signal)"
                lines.append(f"MACD: {macd_val:.4f}  Signal: {signal_val:.4f}  Hist: {hist_val:.4f}  {cross}")

        if "RSI" in indicators and close is not None:
            rsi = ta.rsi(close, length=14)
            if rsi is not None:
                rsi_val = float(rsi.iloc[-1])
                if rsi_val >= 70:
                    zone = "超买区间"
                elif rsi_val <= 30:
                    zone = "超卖区间"
                else:
                    zone = "中性区间"
                lines.append(f"RSI(14): {rsi_val:.2f}  {zone}")

        if "BOLL" in indicators and close is not None:
            boll = ta.bbands(close, length=20)
            if boll is not None and not boll.empty:
                upper = float(boll.iloc[-1, 0])
                mid = float(boll.iloc[-1, 1])
                lower = float(boll.iloc[-1, 2])
                lines.append(f"BOLL 上轨: {upper:.2f}  中轨: {mid:.2f}  下轨: {lower:.2f}")

        if "KDJ" in indicators and high is not None and low is not None and close is not None:
            stoch = ta.stoch(high, low, close)
            if stoch is not None and not stoch.empty:
                k_val = float(stoch.iloc[-1, 0])
                d_val = float(stoch.iloc[-1, 1])
                lines.append(f"KDJ K: {k_val:.2f}  D: {d_val:.2f}")

        return ToolResult(content="\n".join(lines))
