"""Tool: stock_gap_analysis – deterministically analyze price gaps."""

from __future__ import annotations

from typing import Any

import pandas as pd

from xclaw.datasources.a_share import (
    _INDEX_BARE_TO_PREFIXED,
    fetch_cn_history_dataframe,
)
from xclaw.datasources.futures_cn import (
    fetch_cn_future_history_dataframe,
    normalize_cn_future_symbol,
)
from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult
from xclaw.tools.market_symbols import normalize_hk_yf_symbol


class StockGapAnalysisTool(Tool):
    """Analyze daily price gaps using deterministic rules instead of LLM math."""

    @property
    def name(self) -> str:
        return "stock_gap_analysis"

    @property
    def description(self) -> str:
        return (
            "精确分析股票、指数或国内商品期货最近一段时间的日线跳空缺口，并判断是否已回补。"
            "当用户询问“有没有缺口”“缺口是否回补”这类问题时，应优先使用本工具，"
            "不要自己根据K线手算。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "股票或指数代码",
                },
                "market": {
                    "type": "string",
                    "enum": ["CN", "US", "HK"],
                    "default": "CN",
                },
                "asset_type": {
                    "type": "string",
                    "enum": ["stock", "index", "future"],
                    "default": "stock",
                    "description": "资产类型：stock=个股，index=指数，future=国内商品期货。",
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
                    "description": "最多分析最近多少条日线（默认 30）",
                    "default": 30,
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
        asset_type = params.get("asset_type", "stock")
        limit = min(int(params.get("limit", 30)), 180)

        if asset_type == "index" and market == "CN":
            bare = symbol.upper().replace(".", "").replace("SH", "").replace("SZ", "")
            if bare in _INDEX_BARE_TO_PREFIXED:
                symbol = _INDEX_BARE_TO_PREFIXED[bare]

        end_date = params.get("end_date") or date.today().strftime("%Y-%m-%d")
        start_date = params.get("start_date") or (
            date.today() - timedelta(days=120)
        ).strftime("%Y-%m-%d")

        if not symbol:
            return ToolResult(content="标的代码不能为空", is_error=True)

        try:
            if market == "CN" and asset_type == "future":
                df = await fetch_cn_future_history_dataframe(
                    symbol,
                    start_date=start_date,
                    end_date=end_date,
                )
            elif market == "CN":
                df = await fetch_cn_history_dataframe(
                    symbol,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                )
            else:
                df = await self._yf_history(
                    symbol=symbol,
                    market=market,
                    start_date=start_date,
                    end_date=end_date,
                    loop=asyncio.get_event_loop(),
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"缺口分析失败: {exc}", is_error=True)

        if df is None or df.empty:
            return ToolResult(content=f"未找到 {symbol} 的历史数据", is_error=True)

        df = df.tail(limit).reset_index(drop=True)
        gaps = self._detect_gaps(df)
        display_symbol = normalize_cn_future_symbol(symbol).symbol if asset_type == "future" and market == "CN" else symbol.upper()
        source = df.attrs.get("source", "unknown")
        return ToolResult(content=self._format_gap_report(display_symbol, source, len(df), gaps))

    async def _yf_history(
        self,
        *,
        symbol: str,
        market: str,
        start_date: str,
        end_date: str,
        loop: Any,
    ) -> pd.DataFrame:
        import yfinance as yf  # type: ignore[import]

        yf_symbol = normalize_hk_yf_symbol(symbol) if market == "HK" else symbol
        ticker = yf.Ticker(yf_symbol)
        df = await loop.run_in_executor(
            None,
            lambda: ticker.history(start=start_date, end=end_date, interval="1d"),
        )
        if df is None or df.empty:
            raise RuntimeError("no data")
        df = df.reset_index()
        date_column = "Date" if "Date" in df.columns else df.columns[0]
        df["日期"] = pd.to_datetime(df[date_column]).dt.strftime("%Y-%m-%d")
        df["开盘"] = df["Open"]
        df["最高"] = df["High"]
        df["最低"] = df["Low"]
        df["收盘"] = df["Close"]
        df["成交量"] = df["Volume"]
        df.attrs["source"] = "yfinance"
        return df[["日期", "开盘", "收盘", "最高", "最低", "成交量"]]

    def _detect_gaps(self, df: pd.DataFrame) -> list[dict[str, Any]]:
        gaps: list[dict[str, Any]] = []
        rows = df.to_dict("records")
        for index in range(1, len(rows)):
            prev_row = rows[index - 1]
            row = rows[index]

            prev_high = float(prev_row["最高"])
            prev_low = float(prev_row["最低"])
            current_high = float(row["最高"])
            current_low = float(row["最低"])

            # 向上跳空：今日最低价 > 昨日最高价
            if current_low > prev_high:
                lower = prev_high
                upper = current_low
                filled_at = None
                for future_row in rows[index + 1 :]:
                    if float(future_row["最低"]) <= lower:
                        filled_at = future_row["日期"]
                        break
                gaps.append(
                    {
                        "type": "向上",
                        "date": row["日期"],
                        "lower": lower,
                        "upper": upper,
                        "size": upper - lower,
                        "status": "已回补" if filled_at else "未回补",
                        "filled_at": filled_at,
                    }
                )
                continue

            # 向下跳空：今日最高价 < 昨日最低价
            if current_high < prev_low:
                lower = current_high
                upper = prev_low
                filled_at = None
                for future_row in rows[index + 1 :]:
                    if float(future_row["最高"]) >= upper:
                        filled_at = future_row["日期"]
                        break
                gaps.append(
                    {
                        "type": "向下",
                        "date": row["日期"],
                        "lower": lower,
                        "upper": upper,
                        "size": upper - lower,
                        "status": "已回补" if filled_at else "未回补",
                        "filled_at": filled_at,
                    }
                )
        return gaps

    def _format_gap_report(
        self,
        symbol: str,
        source: str,
        row_count: int,
        gaps: list[dict[str, Any]],
    ) -> str:
        lines = [
            f"=== {symbol} 跳空缺口分析 ===",
            f"数据源: {source}",
            f"分析范围: 最近 {row_count} 个交易日",
        ]

        if not gaps:
            lines.extend(
                [
                    "",
                    "结果: 未发现任何跳空缺口。",
                    "说明: 相邻交易日的价格区间均有重叠，因此不存在向上或向下跳空。",
                ]
            )
            return "\n".join(lines)

        unfilled = [gap for gap in gaps if gap["status"] == "未回补"]
        lines.extend(
            [
                "",
                f"缺口总数: {len(gaps)}",
                f"未回补: {len(unfilled)}",
                "",
                "明细:",
            ]
        )

        for gap in gaps:
            filled_suffix = f"，回补日期: {gap['filled_at']}" if gap["filled_at"] else ""
            lines.append(
                f"- {gap['date']} {gap['type']}跳空 | 区间: {gap['lower']:.2f} - {gap['upper']:.2f} "
                f"| 大小: {gap['size']:.2f} | 状态: {gap['status']}{filled_suffix}"
            )

        if unfilled:
            lines.extend(["", "未回补缺口:"])
            for gap in unfilled:
                lines.append(
                    f"- {gap['date']} {gap['type']}跳空 | 区间: {gap['lower']:.2f} - {gap['upper']:.2f} "
                    f"| 大小: {gap['size']:.2f}"
                )
        else:
            lines.extend(["", "当前没有未回补的缺口。"])

        return "\n".join(lines)
