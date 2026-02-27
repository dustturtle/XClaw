"""Tool: stock_zt_pool – fetch limit-up (涨停) stock pool data."""

from __future__ import annotations

from typing import Any

from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult


class StockZTPoolTool(Tool):
    """Fetch today's limit-up (涨停板) stock pool from East Money."""

    @property
    def name(self) -> str:
        return "stock_zt_pool"

    @property
    def description(self) -> str:
        return (
            "获取涨停板股票池数据，包括涨停股代码、名称、最新价、涨跌幅、"
            "连板数、所属行业、封板资金等。可按最新价排序筛选低价股。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "交易日期，格式 YYYYMMDD（如 '20250227'）。留空则使用最近交易日。",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数（默认 10，最大 100）",
                    "default": 10,
                },
                "sort_by_price": {
                    "type": "boolean",
                    "description": "是否按最新价从低到高排序（筛选低价股时设为 true）",
                    "default": False,
                },
            },
            "required": [],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        import asyncio
        from datetime import datetime

        date_str = params.get("date", "").strip()
        limit = min(int(params.get("limit", 10)), 100)
        sort_by_price = bool(params.get("sort_by_price", False))
        loop = asyncio.get_event_loop()

        if not date_str:
            date_str = datetime.now().strftime("%Y%m%d")

        try:
            import akshare as ak  # type: ignore[import]

            df = await loop.run_in_executor(
                None, lambda: ak.stock_zt_pool_em(date=date_str)
            )

            if df is None or df.empty:
                return ToolResult(content=f"未找到 {date_str} 的涨停板数据（可能非交易日）")

            if sort_by_price and "最新价" in df.columns:
                df = df.sort_values("最新价", ascending=True)

            df = df.head(limit)

            lines = [f"=== 涨停板股票池 ({date_str}) ==="]
            lines.append(f"共 {len(df)} 只涨停股：\n")

            for _, row in df.iterrows():
                code = row.get("代码", "")
                name = row.get("名称", "")
                price = row.get("最新价", "N/A")
                change_pct = row.get("涨跌幅", "N/A")
                industry = row.get("所属行业", "N/A")
                consec = row.get("连板数", "N/A")
                turnover = row.get("成交额", "N/A")
                seal_amount = row.get("封板资金", "N/A")
                first_seal = row.get("首次封板时间", "N/A")
                blast_count = row.get("炸板次数", "N/A")
                zt_stat = row.get("涨停统计", "N/A")

                # Format large numbers
                turnover_str = _format_amount(turnover)
                seal_str = _format_amount(seal_amount)

                lines.append(
                    f"• {name}（{code}）\n"
                    f"  最新价: {price}  涨跌幅: {change_pct}%\n"
                    f"  连板数: {consec}  所属行业: {industry}\n"
                    f"  成交额: {turnover_str}  封板资金: {seal_str}\n"
                    f"  首次封板: {first_seal}  炸板次数: {blast_count}\n"
                    f"  涨停统计: {zt_stat}"
                )

            return ToolResult(content="\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"获取涨停板数据失败: {exc}", is_error=True)


def _format_amount(value: Any) -> str:
    """Format large numbers to 万/亿 for readability."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.2f}万"
    return f"{v:.2f}"
