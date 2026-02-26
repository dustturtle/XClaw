"""Tool: stock_news – fetch latest news for a stock or market."""

from __future__ import annotations

from typing import Any

from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult


class StockNewsTool(Tool):
    """Fetch latest news for a stock or the overall market."""

    @property
    def name(self) -> str:
        return "stock_news"

    @property
    def description(self) -> str:
        return "获取个股或市场的最新新闻资讯，帮助了解市场动态和公司事件。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "股票代码（留空则获取市场大盘新闻）",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回新闻条数（默认 10）",
                    "default": 10,
                },
            },
            "required": [],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        import asyncio

        symbol = params.get("symbol", "").strip()
        limit = min(int(params.get("limit", 10)), 20)
        loop = asyncio.get_event_loop()

        try:
            import akshare as ak  # type: ignore[import]

            if symbol:
                df = await loop.run_in_executor(
                    None,
                    lambda: ak.stock_news_em(symbol=symbol),
                )
                title = f"{symbol} 个股新闻"
            else:
                df = await loop.run_in_executor(
                    None,
                    lambda: ak.stock_news_em(symbol="000001"),
                )
                title = "市场最新资讯"

            if df is None or df.empty:
                return ToolResult(content=f"未找到相关新闻", is_error=True)

            df = df.head(limit)
            lines = [f"=== {title} ==="]
            for _, row in df.iterrows():
                pub_time = row.get("发布时间", row.get("时间", ""))
                headline = row.get("新闻标题", row.get("标题", ""))
                url = row.get("新闻链接", row.get("链接", ""))
                lines.append(f"[{pub_time}] {headline}")
                if url:
                    lines.append(f"  链接: {url}")
            return ToolResult(content="\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"获取新闻失败: {exc}", is_error=True)
