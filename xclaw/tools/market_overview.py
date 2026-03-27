"""Tool: market_overview – get A-share market index and sector overview."""

from __future__ import annotations

from typing import Any

from xclaw.datasources.a_share import fetch_cn_index_quotes, fetch_cn_sector_snapshots
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
        include_sectors = bool(params.get("include_sectors", True))

        try:
            lines = ["=== 大盘行情概览 ==="]
            quotes = await fetch_cn_index_quotes()
            if not quotes:
                return ToolResult(content="获取大盘概览失败: 指数数据不可用", is_error=True)
            for quote in quotes:
                change = quote.get("change_pct", "0")
                try:
                    arrow = "▲" if float(change or 0) >= 0 else "▼"
                except ValueError:
                    arrow = "•"
                lines.append(f"{arrow} {quote['name']}: {quote['price']}  ({quote['change_pct']}%)")
            lines.append("\n数据源: 腾讯直连 HTTP")

            if include_sectors:
                sectors = await fetch_cn_sector_snapshots()
                if sectors:
                    lines.append("\n=== 行业板块 TOP5 涨幅 ===")
                    for row in sectors["top"]:
                        lines.append(f"• {row['name']}: {row['change_pct']}%")
                    lines.append("\n=== 行业板块 TOP5 跌幅 ===")
                    for row in sectors["bottom"]:
                        lines.append(f"• {row['name']}: {row['change_pct']}%")
                else:
                    lines.append("\n（板块数据暂不可用，已降级为指数概览）")

            return ToolResult(content="\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"获取大盘概览失败: {exc}", is_error=True)
