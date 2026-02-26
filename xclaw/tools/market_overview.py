"""Tool: market_overview – get A-share market index and sector overview."""

from __future__ import annotations

from typing import Any

from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult


class MarketOverviewTool(Tool):
    """Get major market indices and sector performance overview."""

    @property
    def name(self) -> str:
        return "market_overview"

    @property
    def description(self) -> str:
        return "获取大盘概览：上证/深证/创业板等主要指数、以及板块涨跌排名。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "include_sectors": {
                    "type": "boolean",
                    "description": "是否包含板块涨跌排名（默认 true）",
                    "default": True,
                },
            },
            "required": [],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        import asyncio

        include_sectors = bool(params.get("include_sectors", True))
        loop = asyncio.get_event_loop()

        try:
            import akshare as ak  # type: ignore[import]

            # Major indices
            df_index = await loop.run_in_executor(
                None, lambda: ak.stock_zh_index_spot_em()
            )

            lines = ["=== 大盘行情概览 ==="]
            target_indices = ["上证指数", "深证成指", "创业板指", "科创50", "沪深300", "中证500"]
            if df_index is not None and not df_index.empty:
                for name in target_indices:
                    row = df_index[df_index["名称"] == name]
                    if not row.empty:
                        r = row.iloc[0]
                        change = r.get("涨跌幅", 0)
                        arrow = "▲" if float(change or 0) >= 0 else "▼"
                        lines.append(
                            f"{arrow} {name}: {r.get('最新价', 'N/A')}  "
                            f"({r.get('涨跌幅', 'N/A')}%)"
                        )

            # Sector overview
            if include_sectors:
                try:
                    df_sector = await loop.run_in_executor(
                        None, lambda: ak.stock_board_industry_name_em()
                    )
                    if df_sector is not None and not df_sector.empty:
                        lines.append("\n=== 行业板块 TOP5 涨幅 ===")
                        top5 = df_sector.nlargest(5, "涨跌幅") if "涨跌幅" in df_sector.columns else df_sector.head(5)
                        for _, row in top5.iterrows():
                            lines.append(
                                f"• {row.get('板块名称', row.get('名称', 'N/A'))}: "
                                f"{row.get('涨跌幅', 'N/A')}%"
                            )
                        lines.append("\n=== 行业板块 TOP5 跌幅 ===")
                        bot5 = df_sector.nsmallest(5, "涨跌幅") if "涨跌幅" in df_sector.columns else df_sector.tail(5)
                        for _, row in bot5.iterrows():
                            lines.append(
                                f"• {row.get('板块名称', row.get('名称', 'N/A'))}: "
                                f"{row.get('涨跌幅', 'N/A')}%"
                            )
                except Exception:  # noqa: BLE001
                    lines.append("\n（板块数据获取失败）")

            return ToolResult(content="\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"获取大盘概览失败: {exc}", is_error=True)
