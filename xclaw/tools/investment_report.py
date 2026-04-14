"""Tool: investment_report – generate and review daily investment reports."""

from __future__ import annotations

from typing import Any

from xclaw.config import Settings
from xclaw.investment.report_service import InvestmentReportService
from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult


class InvestmentReportTool(Tool):
    @property
    def name(self) -> str:
        return "investment_report"

    @property
    def description(self) -> str:
        return "生成或查看自选股日报，支持 latest/history/generate。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["generate", "latest", "history"],
                    "default": "generate",
                },
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选，临时指定股票代码列表",
                },
                "market": {
                    "type": "string",
                    "enum": ["CN", "US", "HK"],
                    "default": "CN",
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                },
            },
            "required": [],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.db is None:
            return ToolResult(content="数据库未初始化", is_error=True)

        action = params.get("action", "generate")
        settings = context.settings if context.settings is not None else Settings()
        service = InvestmentReportService(
            db=context.db,
            settings=settings,
            llm=context.llm,
        )

        try:
            if action == "generate":
                report = await service.generate_report(
                    chat_id=context.chat_id,
                    trigger_source=context.channel,
                    symbols=params.get("symbols"),
                    market=params.get("market", "CN"),
                )
                return ToolResult(content=f"{report['title']}\n\n{report['summary']}\n\n{report['content_markdown']}")

            if action == "latest":
                report = await service.latest_report(context.chat_id)
                if report is None:
                    return ToolResult(content="当前还没有生成过日报", is_error=True)
                return ToolResult(content=f"{report['title']}\n\n{report['summary']}\n\n{report['content_markdown']}")

            if action == "history":
                rows = await service.list_reports(context.chat_id, limit=min(int(params.get("limit", 10)), 50))
                if not rows:
                    return ToolResult(content="当前还没有日报历史", is_error=True)
                lines = ["=== 日报历史 ==="]
                for row in rows:
                    lines.append(f"- [{row['id']}] {row['title']} | {row['summary']}")
                return ToolResult(content="\n".join(lines))

            return ToolResult(content=f"不支持的 action: {action}", is_error=True)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"日报操作失败: {exc}", is_error=True)
