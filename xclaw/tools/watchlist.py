"""Tool: watchlist_manage – manage a user's watchlist."""

from __future__ import annotations

from typing import Any

from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult


class WatchlistManageTool(Tool):
    """Add, remove, or view the user's stock watchlist."""

    @property
    def name(self) -> str:
        return "watchlist_manage"

    @property
    def description(self) -> str:
        return "管理股票自选股列表：添加（add）、删除（remove）或查看（list）自选股。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "remove", "list"],
                    "description": "操作类型",
                },
                "symbol": {
                    "type": "string",
                    "description": "股票代码（add/remove 时必填）",
                },
                "market": {
                    "type": "string",
                    "enum": ["CN", "US", "HK"],
                    "default": "CN",
                },
                "name": {
                    "type": "string",
                    "description": "股票名称（可选，用于 add）",
                },
                "notes": {
                    "type": "string",
                    "description": "备注（可选）",
                },
            },
            "required": ["action"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        action = params.get("action", "").lower()
        symbol = params.get("symbol", "").strip().upper()
        market = params.get("market", "CN").upper()

        if context.db is None:
            return ToolResult(content="数据库未初始化", is_error=True)

        if action == "add":
            if not symbol:
                return ToolResult(content="添加自选股需要提供股票代码", is_error=True)
            await context.db.add_to_watchlist(
                context.chat_id,
                symbol,
                market,
                name=params.get("name"),
                notes=params.get("notes"),
            )
            return ToolResult(content=f"已将 {symbol}（{market}）添加到自选股列表")

        elif action == "remove":
            if not symbol:
                return ToolResult(content="删除自选股需要提供股票代码", is_error=True)
            removed = await context.db.remove_from_watchlist(context.chat_id, symbol, market)
            if removed:
                return ToolResult(content=f"已从自选股中删除 {symbol}（{market}）")
            return ToolResult(content=f"自选股中未找到 {symbol}（{market}）", is_error=True)

        elif action == "list":
            items = await context.db.get_watchlist(context.chat_id)
            if not items:
                return ToolResult(content="自选股列表为空")
            lines = ["=== 我的自选股 ==="]
            for item in items:
                name = item.get("name") or ""
                notes = f" - {item['notes']}" if item.get("notes") else ""
                lines.append(f"• {item['symbol']} ({item['market']}) {name}{notes}")
            return ToolResult(content="\n".join(lines))

        else:
            return ToolResult(content=f"不支持的操作: {action}", is_error=True)
