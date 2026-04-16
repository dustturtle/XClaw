"""Tool: stock_correlation – analyze co-movement between symbols."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from xclaw.config import Settings
from xclaw.datasources.a_share import fetch_cn_history_dataframe
from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult
from xclaw.tools.market_symbols import normalize_hk_yf_symbol


class StockCorrelationTool(Tool):
    """Analyze return correlations for multiple symbols."""

    @property
    def name(self) -> str:
        return "stock_correlation"

    @property
    def description(self) -> str:
        return (
            "分析多只股票或指数之间的联动性和收益率相关性，适合回答"
            "“谁和某只股票联动最强”“两只股票相关性如何”“一组股票的相关矩阵”这类问题。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要比较的标的代码列表，2-8 个为宜。",
                    "minItems": 2,
                },
                "market": {
                    "type": "string",
                    "enum": ["CN", "US", "HK"],
                    "default": "CN",
                    "description": "市场（默认 CN）。",
                },
                "asset_type": {
                    "type": "string",
                    "enum": ["stock", "index"],
                    "default": "stock",
                    "description": "资产类型，默认 stock。分析指数时请设为 index。",
                },
                "period": {
                    "type": "string",
                    "enum": ["daily", "weekly", "monthly"],
                    "default": "daily",
                    "description": "K 线周期（默认 daily）。",
                },
                "lookback_days": {
                    "type": "integer",
                    "default": 60,
                    "description": "回看天数（默认 60，自然日口径）。",
                },
                "visualize": {
                    "type": "boolean",
                    "default": False,
                    "description": "是否额外导出相关矩阵热力图 PNG。",
                },
            },
            "required": ["symbols"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        raw_symbols = params.get("symbols") or []
        symbols = [str(item).strip().upper() for item in raw_symbols if str(item).strip()]
        if len(symbols) < 2:
            return ToolResult(content="至少需要 2 个标的才能做相关性分析", is_error=True)
        if len(symbols) > 8:
            return ToolResult(content="单次相关性分析最多支持 8 个标的", is_error=True)

        market = str(params.get("market", "CN")).upper()
        asset_type = str(params.get("asset_type", "stock")).lower()
        period = str(params.get("period", "daily")).lower()
        lookback_days = max(20, min(int(params.get("lookback_days", 60)), 365))
        visualize = bool(params.get("visualize", False))
        end_date = date.today().strftime("%Y-%m-%d")
        start_date = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

        try:
            frames: list[pd.Series] = []
            normalized_symbols: list[str] = []
            for symbol in symbols:
                df = await self._fetch_history(
                    symbol,
                    market=market,
                    asset_type=asset_type,
                    period=period,
                    start_date=start_date,
                    end_date=end_date,
                )
                if df is None or df.empty:
                    return ToolResult(content=f"未找到 {symbol} 的历史数据", is_error=True)

                series = self._to_close_series(df, symbol)
                returns = series.pct_change().dropna()
                if returns.empty:
                    return ToolResult(content=f"{symbol} 的收益率数据不足，无法计算相关性", is_error=True)
                frames.append(returns.rename(symbol))
                normalized_symbols.append(symbol)

            merged = pd.concat(frames, axis=1, join="inner").dropna()
            if len(merged) < 3:
                return ToolResult(content="重叠交易日不足，无法可靠计算相关性", is_error=True)

            heatmap_path = None
            if visualize:
                heatmap_path = self._render_heatmap(
                    merged.corr(),
                    chat_id=context.chat_id,
                    settings=context.settings,
                )

            if len(normalized_symbols) == 2:
                return ToolResult(
                    content=self._format_pair_report(
                        merged,
                        normalized_symbols,
                        market,
                        period,
                        heatmap_path=heatmap_path,
                    )
                )

            return ToolResult(
                content=self._format_matrix_report(
                    merged,
                    normalized_symbols,
                    market,
                    period,
                    heatmap_path=heatmap_path,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"相关性分析失败: {exc}", is_error=True)

    async def _fetch_history(
        self,
        symbol: str,
        *,
        market: str,
        asset_type: str,
        period: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame | None:
        if market == "CN":
            return await fetch_cn_history_dataframe(
                symbol,
                period=period,
                start_date=start_date,
                end_date=end_date,
            )
        return await self._fetch_yfinance_history(symbol, market, period, start_date, end_date)

    async def _fetch_yfinance_history(
        self,
        symbol: str,
        market: str,
        period: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame | None:
        import asyncio

        import yfinance as yf  # type: ignore[import]

        yf_symbol = normalize_hk_yf_symbol(symbol) if market == "HK" else symbol
        interval = {"daily": "1d", "weekly": "1wk", "monthly": "1mo"}.get(period, "1d")
        ticker = yf.Ticker(yf_symbol)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: ticker.history(start=start_date, end=end_date, interval=interval),
        )

    def _to_close_series(self, df: pd.DataFrame, symbol: str) -> pd.Series:
        if "日期" in df.columns and "收盘" in df.columns:
            series = pd.Series(df["收盘"].values, index=pd.to_datetime(df["日期"]), name=symbol)
            return series.sort_index()
        if "Close" in df.columns:
            series = pd.Series(df["Close"].values, index=pd.to_datetime(df.index), name=symbol)
            return series.sort_index()
        raise ValueError(f"{symbol} 的历史数据缺少收盘价字段")

    def _format_pair_report(
        self,
        returns: pd.DataFrame,
        symbols: list[str],
        market: str,
        period: str,
        *,
        heatmap_path: Path | None = None,
    ) -> str:
        left, right = symbols
        corr = float(returns[left].corr(returns[right]))
        same_direction = (
            (returns[left] > 0) & (returns[right] > 0) | (returns[left] < 0) & (returns[right] < 0)
        ).mean()
        recent_window = min(20, len(returns))
        recent_corr = float(returns[left].tail(recent_window).corr(returns[right].tail(recent_window)))

        lines = [
                f"## {left} vs {right} 相关性分析",
                f"市场: {market} | 周期: {period} | 重叠样本: {len(returns)}",
                "",
                f"- 皮尔逊相关系数: {corr:.2f}",
                f"- 最近 {recent_window} 个样本相关系数: {recent_corr:.2f}",
                f"- 同涨同跌占比: {same_direction * 100:.2f}%",
                "",
                self._corr_interpretation(corr, left, right),
            ]
        if heatmap_path is not None:
            lines.extend(["", f"热力图路径: {heatmap_path}"])
        return "\n".join(lines)

    def _format_matrix_report(
        self,
        returns: pd.DataFrame,
        symbols: list[str],
        market: str,
        period: str,
        *,
        heatmap_path: Path | None = None,
    ) -> str:
        corr = returns.corr().round(2)
        ranked_pairs: list[tuple[tuple[str, str], float]] = []
        for left, right in combinations(symbols, 2):
            ranked_pairs.append(((left, right), float(corr.loc[left, right])))
        ranked_pairs.sort(key=lambda item: abs(item[1]), reverse=True)

        lines = [
            "## 多标的相关矩阵",
            f"市场: {market} | 周期: {period} | 重叠样本: {len(returns)}",
            "",
            "### 相关矩阵",
            self._format_matrix(corr),
            "",
            "### 相关性最强的组合",
        ]
        for (left, right), value in ranked_pairs[:3]:
            lines.append(f"- {left} / {right}: {value:.2f}")
        if heatmap_path is not None:
            lines.extend(["", f"热力图路径: {heatmap_path}"])
        return "\n".join(lines)

    def _render_heatmap(self, corr: pd.DataFrame, *, chat_id: int, settings: Any) -> Path:
        cfg = settings if settings is not None else Settings()
        base_dir = Path(getattr(cfg, "report_exports_path", Path("./xclaw.data/report_exports")))
        out_dir = base_dir / "correlation" / str(chat_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / f"corr_heatmap_{datetime.now().strftime('%Y%m%d%H%M%S')}.png"

        plt.figure(figsize=(6, 5))
        plt.imshow(corr.values, cmap="RdYlGn", vmin=-1, vmax=1)
        plt.colorbar(label="Correlation")
        plt.xticks(range(len(corr.columns)), corr.columns, rotation=45, ha="right")
        plt.yticks(range(len(corr.index)), corr.index)
        for row in range(len(corr.index)):
            for col in range(len(corr.columns)):
                plt.text(col, row, f"{corr.iloc[row, col]:.2f}", ha="center", va="center", fontsize=8)
        plt.title("Correlation Heatmap")
        plt.tight_layout()
        plt.savefig(output, format="png")
        plt.close()
        return output

    def _format_matrix(self, corr: pd.DataFrame) -> str:
        header = "标的".ljust(10) + "".join(symbol.rjust(10) for symbol in corr.columns)
        rows = [header]
        for symbol in corr.index:
            row = symbol.ljust(10) + "".join(f"{corr.loc[symbol, col]:>10.2f}" for col in corr.columns)
            rows.append(row)
        return "\n".join(rows)

    def _corr_interpretation(self, corr: float, left: str, right: str) -> str:
        if corr >= 0.8:
            return f"结论: {left} 和 {right} 的联动性很强，短期走势高度同步。"
        if corr >= 0.5:
            return f"结论: {left} 和 {right} 存在中等偏强的同向联动。"
        if corr <= -0.5:
            return f"结论: {left} 和 {right} 存在明显反向关系，可关注对冲或分散配置价值。"
        return f"结论: {left} 和 {right} 的联动性较弱，走势更多受各自因素驱动。"
