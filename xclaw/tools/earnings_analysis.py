"""Tool: earnings_analysis – preview, recap, and estimate-driven earnings analysis."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult
from xclaw.tools.market_symbols import normalize_hk_yf_symbol


class EarningsAnalysisTool(Tool):
    """Analyze upcoming or recent earnings events using yfinance data."""

    @property
    def name(self) -> str:
        return "earnings_analysis"

    @property
    def description(self) -> str:
        return (
            "分析美股/港股财报前瞻、财报回顾和盈利预期变化。"
            "适合回答“下次财报什么时候”“财报预期如何”“上一季财报表现怎样”这类问题。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "股票代码，如 AAPL、MSFT、00700。"},
                "market": {
                    "type": "string",
                    "enum": ["US", "HK"],
                    "default": "US",
                    "description": "市场（当前主要支持美股/港股）。",
                },
                "mode": {
                    "type": "string",
                    "enum": ["preview", "recap", "estimate"],
                    "default": "preview",
                    "description": "preview=财报前瞻，recap=财报回顾，estimate=预期分析。",
                },
            },
            "required": ["symbol"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        import asyncio

        symbol = str(params.get("symbol", "")).strip().upper()
        market = str(params.get("market", "US")).upper()
        mode = str(params.get("mode", "preview")).lower()
        if not symbol:
            return ToolResult(content="股票代码不能为空", is_error=True)

        try:
            import yfinance as yf  # type: ignore[import]

            yf_symbol = normalize_hk_yf_symbol(symbol) if market == "HK" else symbol
            ticker = yf.Ticker(yf_symbol)
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, lambda: ticker.info)
            calendar = await loop.run_in_executor(None, lambda: ticker.calendar)
            earnings_dates = await loop.run_in_executor(None, lambda: ticker.get_earnings_dates(limit=8))
            earnings_df = self._normalize_earnings_dates(earnings_dates)

            if mode == "preview":
                return ToolResult(content=self._format_preview(info, calendar, earnings_df, yf_symbol))
            if mode == "recap":
                return ToolResult(content=self._format_recap(info, earnings_df, yf_symbol))
            if mode == "estimate":
                return ToolResult(content=self._format_estimate(info, earnings_df, yf_symbol))
            return ToolResult(content=f"不支持的财报分析模式: {mode}", is_error=True)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"财报分析失败: {exc}", is_error=True)

    def _normalize_earnings_dates(self, earnings_dates: Any) -> pd.DataFrame:
        if earnings_dates is None:
            return pd.DataFrame()
        if isinstance(earnings_dates, pd.DataFrame):
            df = earnings_dates.copy()
        else:
            return pd.DataFrame()

        if "Earnings Date" not in df.columns:
            df = df.reset_index()
            first_col = df.columns[0]
            df = df.rename(columns={first_col: "Earnings Date"})

        df["Earnings Date"] = pd.to_datetime(df["Earnings Date"], errors="coerce")
        if "Reported EPS" not in df.columns:
            df["Reported EPS"] = pd.NA
        if "EPS Estimate" not in df.columns:
            df["EPS Estimate"] = pd.NA
        if "Surprise(%)" not in df.columns:
            df["Surprise(%)"] = pd.NA

        return df.dropna(subset=["Earnings Date"]).sort_values("Earnings Date", ascending=False)

    def _format_preview(
        self,
        info: dict[str, Any],
        calendar: Any,
        earnings_df: pd.DataFrame,
        symbol: str,
    ) -> str:
        company = info.get("shortName") or symbol
        upcoming_date = self._extract_next_earnings_date(calendar, earnings_df)
        upcoming_row = self._extract_upcoming_row(earnings_df)
        history_rows = self._extract_past_rows(earnings_df, limit=4)
        lines = [
            f"## {company} 财报前瞻",
            f"标的: {symbol}",
            f"下次财报时间: {upcoming_date or '暂无公开日期'}",
        ]
        if upcoming_row is not None and pd.notna(upcoming_row.get("EPS Estimate")):
            lines.append(f"EPS 一致预期: {float(upcoming_row['EPS Estimate']):.2f}")
        opinions = info.get("numberOfAnalystOpinions")
        if opinions:
            lines.append(f"分析师覆盖数: {opinions}")
        if info.get("recommendationKey"):
            lines.append(f"一致评级: {info['recommendationKey']}")
        if info.get("targetMeanPrice") and info.get("currentPrice"):
            upside = (float(info["targetMeanPrice"]) / float(info["currentPrice"]) - 1) * 100
            lines.append(f"一致目标价: {float(info['targetMeanPrice']):.2f}（相对现价 {upside:+.2f}%）")

        if not history_rows.empty:
            lines.extend(["", "近几次财报 surprise："])
            for _, row in history_rows.iterrows():
                dt = self._fmt_date(row["Earnings Date"])
                est = self._fmt_num(row.get("EPS Estimate"))
                rep = self._fmt_num(row.get("Reported EPS"))
                surprise = self._fmt_pct(row.get("Surprise(%)"))
                lines.append(f"- {dt}: 预期 {est} / 实际 {rep} / Surprise {surprise}")

        return "\n".join(lines)

    def _format_recap(
        self,
        info: dict[str, Any],
        earnings_df: pd.DataFrame,
        symbol: str,
    ) -> str:
        company = info.get("shortName") or symbol
        latest = self._extract_past_rows(earnings_df, limit=1)
        if latest.empty:
            return "\n".join(
                [f"## {company} 财报回顾", f"标的: {symbol}", "暂无最近一次财报回顾所需的数据。"]
            )
        row = latest.iloc[0]
        past_rows = self._extract_past_rows(earnings_df, limit=8)
        beat_rate = 0.0
        if not past_rows.empty:
            beat_rate = float((past_rows["Surprise(%)"].fillna(0) > 0).mean() * 100)
        current_price = info.get("currentPrice")
        prev_close = info.get("regularMarketPreviousClose")
        reaction = "N/A"
        if current_price not in (None, 0) and prev_close not in (None, 0):
            reaction = f"{(float(current_price) / float(prev_close) - 1) * 100:+.2f}%"
        return "\n".join(
            [
                f"## {company} 财报回顾",
                f"标的: {symbol}",
                f"最近财报日期: {self._fmt_date(row['Earnings Date'])}",
                f"EPS 一致预期: {self._fmt_num(row.get('EPS Estimate'))}",
                f"EPS 实际值: {self._fmt_num(row.get('Reported EPS'))}",
                f"Surprise: {self._fmt_pct(row.get('Surprise(%)'))}",
                f"历史 Beat 率: {beat_rate:.2f}%",
                f"价格反应: {reaction}",
                f"当前一致评级: {info.get('recommendationKey', 'N/A')}",
            ]
        )

    def _format_estimate(
        self,
        info: dict[str, Any],
        earnings_df: pd.DataFrame,
        symbol: str,
    ) -> str:
        company = info.get("shortName") or symbol
        forward_eps = info.get("forwardEps")
        trailing_eps = info.get("trailingEps")
        earnings_growth = info.get("earningsGrowth")
        revenue_growth = info.get("revenueGrowth")
        target_mean = info.get("targetMeanPrice")
        current_price = info.get("currentPrice")
        upcoming_row = self._extract_upcoming_row(earnings_df)

        lines = [f"## {company} 盈利预期分析", f"标的: {symbol}"]
        if forward_eps is not None:
            lines.append(f"Forward EPS: {float(forward_eps):.2f}")
        if trailing_eps is not None:
            lines.append(f"Trailing EPS: {float(trailing_eps):.2f}")
        if forward_eps is not None and trailing_eps not in (None, 0):
            eps_delta = (float(forward_eps) / float(trailing_eps) - 1) * 100
            lines.append(f"Forward vs Trailing EPS 变化: {eps_delta:+.2f}%")
        if earnings_growth is not None:
            lines.append(f"盈利增长预期: {float(earnings_growth) * 100:.2f}%")
        if revenue_growth is not None:
            lines.append(f"营收增长预期: {float(revenue_growth) * 100:.2f}%")
        if target_mean is not None and current_price not in (None, 0):
            upside = (float(target_mean) / float(current_price) - 1) * 100
            lines.append(f"一致目标价: {float(target_mean):.2f}（相对现价 {upside:+.2f}%）")
        if info.get("recommendationKey"):
            lines.append(f"一致评级: {info['recommendationKey']}")
        if info.get("numberOfAnalystOpinions"):
            lines.append(f"分析师覆盖数: {info['numberOfAnalystOpinions']}")
        if upcoming_row is not None and pd.notna(upcoming_row.get("EPS Estimate")):
            lines.append(f"下一次 EPS 一致预期: {float(upcoming_row['EPS Estimate']):.2f}")

        return "\n".join(lines)

    def _extract_next_earnings_date(self, calendar: Any, earnings_df: pd.DataFrame) -> str | None:
        candidate = None
        if isinstance(calendar, dict):
            value = calendar.get("Earnings Date")
            if isinstance(value, list) and value:
                candidate = value[0]
            elif value:
                candidate = value
        if candidate is None:
            upcoming = self._extract_upcoming_row(earnings_df)
            if upcoming is not None:
                candidate = upcoming["Earnings Date"]
        return self._fmt_date(candidate) if candidate is not None else None

    def _extract_upcoming_row(self, earnings_df: pd.DataFrame) -> pd.Series | None:
        if earnings_df.empty:
            return None
        now = pd.Timestamp.now("UTC").tz_localize(None)
        mask = earnings_df["Earnings Date"] >= now
        rows = earnings_df.loc[mask].sort_values("Earnings Date", ascending=True)
        if rows.empty:
            return None
        return rows.iloc[0]

    def _extract_past_rows(self, earnings_df: pd.DataFrame, limit: int) -> pd.DataFrame:
        if earnings_df.empty:
            return pd.DataFrame()
        rows = earnings_df.loc[earnings_df["Reported EPS"].notna()].sort_values("Earnings Date", ascending=False)
        return rows.head(limit)

    def _fmt_date(self, value: Any) -> str:
        dt = pd.to_datetime(value, errors="coerce")
        if pd.isna(dt):
            return "N/A"
        return dt.strftime("%Y-%m-%d")

    def _fmt_num(self, value: Any) -> str:
        if value is None or pd.isna(value):
            return "N/A"
        return f"{float(value):.2f}"

    def _fmt_pct(self, value: Any) -> str:
        if value is None or pd.isna(value):
            return "N/A"
        return f"{float(value):+.2f}%"
