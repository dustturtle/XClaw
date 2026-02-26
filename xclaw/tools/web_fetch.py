"""Tool: web_fetch – fetch a URL and return cleaned text content."""

from __future__ import annotations

from typing import Any

import httpx

from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult

try:
    from bs4 import BeautifulSoup

    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False


def _clean_html(html: str) -> str:
    """Strip HTML tags and return readable plain text."""
    if _HAS_BS4:
        soup = BeautifulSoup(html, "lxml")
        # Remove script/style elements
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    # Fallback: naive tag stripping
    import re

    text = re.sub(r"<[^>]+>", "", html)
    return re.sub(r"\s{2,}", "\n", text).strip()


class WebFetchTool(Tool):
    """Fetch the content of a URL and return cleaned text."""

    MAX_CHARS = 8000  # Limit returned text to avoid overflowing context

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return "抓取指定 URL 的页面内容，返回去除 HTML 标签后的纯文本。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要抓取的网页 URL",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "返回的最大字符数（默认 8000）",
                    "default": 8000,
                },
            },
            "required": ["url"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        url = params.get("url", "").strip()
        max_chars = min(int(params.get("max_chars", self.MAX_CHARS)), self.MAX_CHARS)

        if not url:
            return ToolResult(content="URL 不能为空", is_error=True)

        try:
            async with httpx.AsyncClient(
                timeout=20.0,
                follow_redirects=True,
                headers={"User-Agent": "XClaw/0.1 (web fetcher)"},
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")

            if "html" in content_type or "text" in content_type:
                text = _clean_html(resp.text)
            else:
                text = resp.text

            if len(text) > max_chars:
                text = text[:max_chars] + f"\n... [内容被截断，已显示前 {max_chars} 字符]"

            return ToolResult(content=text or "页面内容为空")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"抓取失败: {exc}", is_error=True)
