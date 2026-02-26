"""Tool: web_search – search the web using DuckDuckGo (no API key required)."""

from __future__ import annotations

from typing import Any

import httpx

from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult


class WebSearchTool(Tool):
    """Search the web via DuckDuckGo Instant Answer API."""

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return "搜索网络，返回与查询相关的摘要和链接。使用 DuckDuckGo。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最多返回结果数（默认 5）",
                    "default": 5,
                },
            },
            "required": ["query"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        query = params.get("query", "").strip()
        max_results = min(int(params.get("max_results", 5)), 10)
        if not query:
            return ToolResult(content="查询不能为空", is_error=True)

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://api.duckduckgo.com/",
                    params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
                    headers={"User-Agent": "XClaw/0.1"},
                )
                resp.raise_for_status()
                data = resp.json()

            lines: list[str] = []
            # AbstractText is the main answer
            if data.get("AbstractText"):
                lines.append(f"摘要: {data['AbstractText']}")
                if data.get("AbstractURL"):
                    lines.append(f"来源: {data['AbstractURL']}")

            # RelatedTopics as additional results
            for topic in data.get("RelatedTopics", [])[:max_results]:
                if isinstance(topic, dict) and topic.get("Text"):
                    lines.append(f"- {topic['Text']}")
                    if topic.get("FirstURL"):
                        lines.append(f"  链接: {topic['FirstURL']}")

            if not lines:
                lines.append(f"未找到与 '{query}' 相关的结果。")

            return ToolResult(content="\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"搜索失败: {exc}", is_error=True)
