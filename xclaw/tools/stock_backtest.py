"""Tool: stock_backtest – simple moving average crossover backtesting."""

from __future__ import annotations

import math
from typing import Any

from xclaw.datasources.a_share import fetch_cn_history_dataframe
from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult


class StockBacktestTool(Tool):
    """Run a simple strategy backtest on historical stock data.

    Supported strategies:
    - ``sma_cross``:  Buy when fast SMA crosses above slow SMA; sell on reverse.
    - ``rsi``:        Buy when RSI < oversold threshold; sell when RSI > overbought.
    """

    # Number of extra warmup bars beyond the indicator period needed before signals are valid.
    _MIN_WARMUP_BARS = 5

    # Annualisation factor: approximate trading days per year
    _TRADING_DAYS_PER_YEAR = 252

    @property
    def name(self) -> str:
        return "stock_backtest"

    @property
    def description(self) -> str:
        return (
            "对股票历史数据运行策略回测，支持均线交叉（sma_cross）和 RSI 策略，"
            "返回总收益率、最大回撤、Sharpe 比率、胜率等绩效指标。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "股票代码（如 '600519'、'AAPL'）",
                },
                "market": {
                    "type": "string",
                    "enum": ["CN", "US", "HK"],
                    "default": "CN",
                    "description": "市场（CN/US/HK）",
                },
                "strategy": {
                    "type": "string",
                    "enum": ["sma_cross", "rsi"],
                    "default": "sma_cross",
                    "description": "回测策略：均线交叉或 RSI",
                },
                "start_date": {
                    "type": "string",
                    "description": "开始日期，格式 YYYY-MM-DD（默认 1 年前）",
                },
                "end_date": {
                    "type": "string",
                    "description": "结束日期，格式 YYYY-MM-DD（默认今天）",
                },
                "fast_period": {
                    "type": "integer",
                    "description": "快速均线周期（sma_cross 用，默认 10）",
                    "default": 10,
                },
                "slow_period": {
                    "type": "integer",
                    "description": "慢速均线周期（sma_cross 用，默认 30）",
                    "default": 30,
                },
                "rsi_period": {
                    "type": "integer",
                    "description": "RSI 计算周期（rsi 策略用，默认 14）",
                    "default": 14,
                },
                "rsi_oversold": {
                    "type": "number",
                    "description": "RSI 超卖阈值（默认 30）",
                    "default": 30,
                },
                "rsi_overbought": {
                    "type": "number",
                    "description": "RSI 超买阈值（默认 70）",
                    "default": 70,
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
        strategy = params.get("strategy", "sma_cross")
        fast_period = int(params.get("fast_period", 10))
        slow_period = int(params.get("slow_period", 30))
        rsi_period = int(params.get("rsi_period", 14))
        rsi_oversold = float(params.get("rsi_oversold", 30))
        rsi_overbought = float(params.get("rsi_overbought", 70))

        end_date = params.get("end_date") or date.today().strftime("%Y-%m-%d")
        start_date = params.get("start_date") or (
            date.today() - timedelta(days=365)
        ).strftime("%Y-%m-%d")

        if not symbol:
            return ToolResult(content="股票代码不能为空", is_error=True)

        try:
            loop = asyncio.get_event_loop()
            closes = await self._fetch_closes(symbol, market, start_date, end_date, loop)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"获取历史数据失败: {exc}", is_error=True)

        if len(closes) < max(
            slow_period + self._MIN_WARMUP_BARS,
            rsi_period + self._MIN_WARMUP_BARS,
            20,
        ):
            return ToolResult(
                content=f"历史数据不足（{len(closes)} 条），请扩大日期范围",
                is_error=True,
            )

        try:
            if strategy == "sma_cross":
                trades, equity = self._run_sma_cross(closes, fast_period, slow_period)
            else:
                trades, equity = self._run_rsi(closes, rsi_period, rsi_oversold, rsi_overbought)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"回测计算失败: {exc}", is_error=True)

        return ToolResult(content=self._format_result(
            symbol, market, strategy, start_date, end_date,
            closes, trades, equity,
        ))

    # ── Data fetching ──────────────────────────────────────────────────────────

    async def _fetch_closes(
        self, symbol: str, market: str, start_date: str, end_date: str, loop: Any
    ) -> list[float]:
        if market == "CN":
            df = await fetch_cn_history_dataframe(
                symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
            )
            if df is None or df.empty:
                raise ValueError(f"No data for {symbol}")
            return [float(v) for v in df["收盘"].dropna().tolist()]
        else:
            import yfinance as yf  # type: ignore[import]
            yf_symbol = f"{symbol}.HK" if market == "HK" and not symbol.endswith(".HK") else symbol
            ticker = yf.Ticker(yf_symbol)
            df = await loop.run_in_executor(
                None,
                lambda: ticker.history(start=start_date, end=end_date, interval="1d"),
            )
            if df is None or df.empty:
                raise ValueError(f"No data for {yf_symbol}")
            return [float(v) for v in df["Close"].tolist()]

    # ── Strategy: SMA crossover ────────────────────────────────────────────────

    @staticmethod
    def _sma(prices: list[float], period: int) -> list[float | None]:
        result: list[float | None] = [None] * (period - 1)
        for i in range(period - 1, len(prices)):
            result.append(sum(prices[i - period + 1 : i + 1]) / period)
        return result

    def _run_sma_cross(
        self,
        closes: list[float],
        fast: int,
        slow: int,
    ) -> tuple[list[dict], list[float]]:
        fast_sma = self._sma(closes, fast)
        slow_sma = self._sma(closes, slow)

        position = 0.0  # shares (0 = out)
        cash = 1.0       # normalised capital
        trades: list[dict] = []
        equity: list[float] = []

        prev_signal = 0  # +1 = fast above slow, -1 = fast below slow

        for i, price in enumerate(closes):
            f = fast_sma[i]
            s = slow_sma[i]
            if f is None or s is None or price <= 0:
                equity.append(cash + position * price)
                continue

            signal = 1 if f > s else -1

            if signal != prev_signal:
                if signal == 1 and position == 0:
                    # Buy signal
                    position = cash / price
                    cash = 0.0
                    trades.append({"action": "buy", "price": price, "day": i})
                elif signal == -1 and position > 0:
                    # Sell signal
                    cash = position * price
                    position = 0.0
                    trades.append({"action": "sell", "price": price, "day": i})
                prev_signal = signal

            equity.append(cash + position * price)

        # Close any open position at last price
        if position > 0 and closes:
            cash = position * closes[-1]
            position = 0.0
            trades.append({"action": "sell", "price": closes[-1], "day": len(closes) - 1, "note": "final"})

        return trades, equity

    # ── Strategy: RSI ─────────────────────────────────────────────────────────

    @staticmethod
    def _compute_rsi(closes: list[float], period: int) -> list[float | None]:
        if len(closes) < period + 1:
            return [None] * len(closes)
        rsi: list[float | None] = [None] * period
        gains = []
        losses = []
        for i in range(1, period + 1):
            delta = closes[i] - closes[i - 1]
            gains.append(max(delta, 0))
            losses.append(max(-delta, 0))
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        for i in range(period, len(closes)):
            if avg_loss == 0:
                rsi.append(100.0)
            else:
                rs = avg_gain / avg_loss
                rsi.append(100.0 - 100.0 / (1.0 + rs))
            delta = closes[i] - closes[i - 1]
            gain = max(delta, 0)
            loss = max(-delta, 0)
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
        return rsi

    def _run_rsi(
        self,
        closes: list[float],
        period: int,
        oversold: float,
        overbought: float,
    ) -> tuple[list[dict], list[float]]:
        rsi_values = self._compute_rsi(closes, period)

        position = 0.0
        cash = 1.0
        trades: list[dict] = []
        equity: list[float] = []

        for i, price in enumerate(closes):
            r = rsi_values[i]
            if r is not None and price > 0:
                if r < oversold and position == 0:
                    position = cash / price
                    cash = 0.0
                    trades.append({"action": "buy", "price": price, "day": i, "rsi": round(r, 2)})
                elif r > overbought and position > 0:
                    cash = position * price
                    position = 0.0
                    trades.append({"action": "sell", "price": price, "day": i, "rsi": round(r, 2)})
            equity.append(cash + position * price)

        if position > 0 and closes:
            cash = position * closes[-1]
            trades.append({"action": "sell", "price": closes[-1], "day": len(closes) - 1, "note": "final"})

        return trades, equity

    # ── Metrics calculation ────────────────────────────────────────────────────

    def _compute_metrics(
        self,
        equity: list[float],
        closes: list[float],
    ) -> dict[str, float]:
        if not equity:
            return {}

        total_return = (equity[-1] / equity[0] - 1) * 100 if equity[0] > 0 else 0.0

        # Buy-and-hold return
        bah_return = (closes[-1] / closes[0] - 1) * 100 if closes[0] > 0 else 0.0

        # Max drawdown
        peak = equity[0]
        max_dd = 0.0
        for v in equity:
            if v > peak:
                peak = v
            dd = (peak - v) / peak * 100 if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd

        # Daily returns for Sharpe
        daily_returns = [
            (equity[i] / equity[i - 1] - 1)
            for i in range(1, len(equity))
            if equity[i - 1] > 0
        ]
        if len(daily_returns) > 1:
            mean_r = sum(daily_returns) / len(daily_returns)
            variance = sum((r - mean_r) ** 2 for r in daily_returns) / len(daily_returns)
            std_r = math.sqrt(variance) if variance > 0 else 0.0
            sharpe = (mean_r / std_r * math.sqrt(self._TRADING_DAYS_PER_YEAR)) if std_r > 0 else 0.0
        else:
            sharpe = 0.0

        return {
            "total_return": round(total_return, 2),
            "bah_return": round(bah_return, 2),
            "max_drawdown": round(max_dd, 2),
            "sharpe": round(sharpe, 2),
        }

    @staticmethod
    def _win_rate(trades: list[dict]) -> tuple[int, int, float]:
        """Return (wins, total_round_trips, win_rate%)."""
        buys = [t for t in trades if t["action"] == "buy"]
        sells = [t for t in trades if t["action"] == "sell"]
        pairs = list(zip(buys, sells))
        wins = sum(1 for b, s in pairs if s["price"] > b["price"])
        total = len(pairs)
        rate = wins / total * 100 if total > 0 else 0.0
        return wins, total, round(rate, 1)

    def _format_result(
        self,
        symbol: str,
        market: str,
        strategy: str,
        start_date: str,
        end_date: str,
        closes: list[float],
        trades: list[dict],
        equity: list[float],
    ) -> str:
        metrics = self._compute_metrics(equity, closes)
        wins, total_trades, win_rate = self._win_rate(trades)

        strategy_name = {"sma_cross": "均线交叉（SMA Cross）", "rsi": "RSI 策略"}.get(
            strategy, strategy
        )

        lines = [
            f"📊 回测结果：{symbol} ({market})",
            f"策略：{strategy_name}",
            f"日期范围：{start_date} → {end_date}（共 {len(closes)} 个交易日）",
            "",
            "── 绩效指标 ──",
            f"  总收益率：{metrics.get('total_return', 0):+.2f}%",
            f"  买入持有：{metrics.get('bah_return', 0):+.2f}%",
            f"  最大回撤：{metrics.get('max_drawdown', 0):.2f}%",
            f"  Sharpe 比率：{metrics.get('sharpe', 0):.2f}",
            "",
            f"── 交易记录（共 {total_trades} 笔完整交易）──",
            f"  胜率：{win_rate:.1f}%（{wins}/{total_trades}）",
        ]

        # Show up to 10 trades
        shown = [t for t in trades if "note" not in t][:10]
        for t in shown:
            action_icon = "🟢买入" if t["action"] == "buy" else "🔴卖出"
            rsi_str = f"  RSI={t['rsi']}" if "rsi" in t else ""
            lines.append(f"  第{t['day']+1}日 {action_icon} @ {t['price']:.2f}{rsi_str}")
        if len([t for t in trades if "note" not in t]) > 10:
            lines.append("  ...")

        lines.append("")
        lines.append("⚠️ 免责声明：回测仅供参考，不构成投资建议，历史表现不代表未来。")
        return "\n".join(lines)
