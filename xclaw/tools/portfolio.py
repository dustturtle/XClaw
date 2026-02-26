"""Tool: portfolio_manage – manage a user's stock portfolio."""

from __future__ import annotations

from typing import Any

from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult


class PortfolioManageTool(Tool):
    """Add, remove, update, or view the user's stock portfolio with P&L."""

    @property
    def name(self) -> str:
        return "portfolio_manage"

    @property
    def description(self) -> str:
        return "管理持仓记录：买入（buy）、卖出/删除（sell）、查看持仓和盈亏（view）。持仓数据仅本地存储，无自动交易。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["buy", "sell", "view"],
                    "description": "操作类型：buy（买入/更新）、sell（卖出/删除）、view（查看）",
                },
                "symbol": {
                    "type": "string",
                    "description": "股票代码",
                },
                "market": {
                    "type": "string",
                    "enum": ["CN", "US", "HK"],
                    "default": "CN",
                },
                "shares": {
                    "type": "number",
                    "description": "股数（buy/sell 时必填）",
                },
                "price": {
                    "type": "number",
                    "description": "成交均价（buy 时必填）",
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

        if action == "buy":
            if not symbol:
                return ToolResult(content="买入需要提供股票代码", is_error=True)
            shares = float(params.get("shares", 0))
            price = float(params.get("price", 0))
            if shares <= 0 or price <= 0:
                return ToolResult(content="股数和价格必须大于 0", is_error=True)
            await context.db.upsert_portfolio(context.chat_id, symbol, market, shares, price)
            return ToolResult(
                content=f"已记录买入：{symbol}（{market}）{shares} 股 @ ¥{price:.2f}"
            )

        elif action == "sell":
            if not symbol:
                return ToolResult(content="卖出需要提供股票代码", is_error=True)
            removed = await context.db.remove_from_portfolio(context.chat_id, symbol, market)
            if removed:
                return ToolResult(content=f"已从持仓中删除 {symbol}（{market}）")
            return ToolResult(content=f"持仓中未找到 {symbol}（{market}）", is_error=True)

        elif action == "view":
            items = await context.db.get_portfolio(context.chat_id)
            if not items:
                return ToolResult(content="持仓列表为空")
            lines = ["=== 我的持仓 ==="]
            for item in items:
                sym = item["symbol"]
                mkt = item["market"]
                shares = item["shares"]
                avg_cost = item["avg_cost"]
                cost_total = shares * avg_cost
                lines.append(
                    f"• {sym} ({mkt}) | 持有 {shares} 股 | 均价 {avg_cost:.2f} | 成本 {cost_total:.2f}"
                )
            return ToolResult(content="\n".join(lines))

        else:
            return ToolResult(content=f"不支持的操作: {action}", is_error=True)
