"""Tool: web_search – search the web using DuckDuckGo (no API key required)."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
import httpx

from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult

_SEARCH_REQUEST_TIMEOUT_SECONDS = 4.0
_SEARCH_TOTAL_TIMEOUT_SECONDS = 8.0
_SEARCH_MAX_RESPONSE_BYTES = 512 * 1024
_SEARCH_ENDPOINTS = (
    ("DuckDuckGo HTML", "https://html.duckduckgo.com/html/"),
    ("DuckDuckGo Lite", "https://lite.duckduckgo.com/lite/"),
)


def _result_url(href: str) -> str | None:
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urlsplit(href)
        host = (parsed.hostname or "").lower()
        if parsed.path.startswith("/l/") and (not host or host.endswith(".duckduckgo.com") or host == "duckduckgo.com"):
            href = parse_qs(parsed.query).get("uddg", [""])[0]
            parsed = urlsplit(href)
            host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            return None
        if host == "duckduckgo.com" or host.endswith(".duckduckgo.com"):
            return None
        return href
    except ValueError:
        return None


def _parse_results(html: str, limit: int) -> list[tuple[str, str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one("#challenge-form, #anomaly-modal, .anomaly-modal"):
        return []
    results = []
    seen = set()
    # Support the HTML layout and Lite's table layout without treating arbitrary
    # navigation/captcha links as search results.
    for anchor in soup.select("a.result__a, a.result-link"):
        url = _result_url(str(anchor.get("href", "")))
        title = anchor.get_text(" ", strip=True)
        if not url or not title or url in seen:
            continue
        snippet = None
        result = anchor.find_parent(class_="result")
        if result is not None:
            snippet = result.select_one(".result__snippet")
        elif "result-link" in anchor.get("class", []):
            row = anchor.find_parent("tr")
            if row is not None:
                for sibling in row.find_next_siblings("tr", limit=3):
                    if sibling.select_one("a.result-link"):
                        break
                    snippet = sibling.select_one(".result-snippet")
                    if snippet is not None:
                        break
        seen.add(url)
        results.append((title[:300], url, snippet.get_text(" ", strip=True)[:1000] if snippet else ""))
        if len(results) >= limit:
            break
    return results


class WebSearchTool(Tool):
    """Search real webpages, with at most one alternate-layout fallback."""

    read_only = True
    # Per-call client and parser only, with no persistent state or file writes.
    independent = True

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
        query = params.get("query", "")
        if not isinstance(query, str) or not query.strip():
            return ToolResult(content="查询不能为空", is_error=True)
        query = query.strip()
        try:
            max_results = min(int(params.get("max_results", 5)), 10)
            if max_results < 1:
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            return ToolResult(content="max_results 必须为正整数", is_error=True)

        failures = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _SEARCH_TOTAL_TIMEOUT_SECONDS
        try:
            async with asyncio.timeout(_SEARCH_TOTAL_TIMEOUT_SECONDS):
                # Keep existing environment proxy behavior; do not override it.
                async with httpx.AsyncClient(timeout=_SEARCH_REQUEST_TIMEOUT_SECONDS) as client:
                    for source, endpoint in _SEARCH_ENDPOINTS:
                        if loop.time() >= deadline:
                            raise TimeoutError()
                        try:
                            async with asyncio.timeout(_SEARCH_REQUEST_TIMEOUT_SECONDS):
                                async with client.stream(
                                    "GET", endpoint, params={"q": query},
                                    headers={"User-Agent": "Mozilla/5.0 (compatible; XClaw/0.1)"},
                                ) as resp:
                                    resp.raise_for_status()
                                    chunks = []
                                    size = 0
                                    async for chunk in resp.aiter_bytes():
                                        size += len(chunk)
                                        if size > _SEARCH_MAX_RESPONSE_BYTES:
                                            raise ValueError("search response too large")
                                        chunks.append(chunk)
                                    html = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
                            results = _parse_results(html, max_results)
                            if loop.time() >= deadline:
                                raise TimeoutError()
                        except (httpx.TimeoutException, TimeoutError):
                            failures.append(f"{source}: 超时")
                            continue
                        except httpx.HTTPStatusError as exc:
                            failures.append(f"{source}: HTTP {exc.response.status_code}")
                            continue
                        except Exception as exc:  # noqa: BLE001
                            # Exception strings can contain proxy credentials.
                            failures.append(f"{source}: {type(exc).__name__}")
                            continue
                        if not results:
                            failures.append(f"{source}: 无可用结果（空结果、验证页面或解析失败）")
                            continue
                        lines = [f"搜索来源: {source}", "以下为搜索结果摘要，未读取或核验网页正文。"]
                        for title, url, snippet in results:
                            lines.extend([f"- {title}", f"  链接: {url}"])
                            if snippet:
                                lines.append(f"  摘要: {snippet}")
                        return ToolResult(content="\n".join(lines))
        except TimeoutError:
            failures.append("搜索总预算超时")
        except Exception as exc:  # noqa: BLE001
            failures.append(type(exc).__name__)
        return ToolResult(
            content=f"搜索数据缺失，无法据此确认相关事实。{' / '.join(failures)}",
            is_error=True,
        )
