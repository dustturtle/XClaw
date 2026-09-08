"""Offline fixtures for real search results and bounded failure handling."""

import asyncio

import httpx
import pytest
import respx

from xclaw.tools import ToolContext
from xclaw.tools import web_search


HTML_URL = "https://html.duckduckgo.com/html/"
LITE_URL = "https://lite.duckduckgo.com/lite/"
HTML = """
<div class="result">
 <h2><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fresearch%3Fa%3D1%26b%3D2">Research <b>report</b></a></h2>
 <a class="result__snippet">A <b>real</b> result excerpt.</a>
</div>
<div class="result">
 <a class="result__a" href="https://example.org/research?a=1&amp;b=2">Duplicate</a>
</div>
<div class="result">
 <a class="result__a" href="https://example.net/notes">Second report</a>
</div>
<a href="/settings">Settings must not become a result</a>
"""
LITE = """
<table><tr><td><a class="result-link" href="https://example.org/lite">Lite title</a></td></tr>
<tr><td class="result-snippet">Lite excerpt.</td></tr></table>
"""


async def search(**params):
    return await web_search.WebSearchTool().execute(
        {"query": "example research", **params}, ToolContext(chat_id=1, channel="web"),
    )


@respx.mock
async def test_html_search_extracts_real_links_snippets_and_deduplicates():
    route = respx.get(HTML_URL).mock(return_value=httpx.Response(200, text=HTML))
    result = await search()
    assert not result.is_error
    assert "Research report" in result.content
    assert "https://example.org/research?a=1&b=2" in result.content
    assert "A real result excerpt." in result.content
    assert "Second report" in result.content
    assert "Duplicate" not in result.content
    assert "Settings" not in result.content
    assert len(respx.calls) == 1
    assert route.calls[0].request.url.params["q"] == "example research"


@respx.mock
@pytest.mark.parametrize("first_response", [
    httpx.Response(200, text="<html>markup changed</html>"),
    httpx.Response(200, text="<html><div class='no-results'>No results</div></html>"),
    httpx.Response(503),
])
async def test_one_lite_fallback_for_parse_empty_and_http_failure(first_response):
    respx.get(HTML_URL).mock(return_value=first_response)
    respx.get(LITE_URL).mock(return_value=httpx.Response(200, text=LITE))
    result = await search()
    assert not result.is_error
    assert "Lite title" in result.content
    assert "Lite excerpt." in result.content
    assert "DuckDuckGo Lite" in result.content
    assert len(respx.calls) == 2


@respx.mock
async def test_network_timeout_can_fall_back_once():
    respx.get(HTML_URL).mock(side_effect=httpx.ReadTimeout("fixture"))
    respx.get(LITE_URL).mock(return_value=httpx.Response(200, text=LITE))
    assert not (await search()).is_error
    assert len(respx.calls) == 2


@respx.mock
@pytest.mark.parametrize("page", [
    "<div class='no-results'>No results</div>",
    "<form id='challenge-form'>Captcha challenge</form>",
    '<a class="result__a" href="javascript:alert(1)">Not a web result</a>',
    '<a class="result__a" href="/settings">Not a web result</a>',
])
async def test_missing_data_is_error_not_success(page):
    respx.get(HTML_URL).mock(return_value=httpx.Response(200, text=page))
    respx.get(LITE_URL).mock(return_value=httpx.Response(200, text=page))
    result = await search()
    assert result.is_error
    assert "数据缺失" in result.content
    assert len(respx.calls) == 2


@respx.mock
async def test_total_search_deadline_bounds_stalled_http(monkeypatch):
    monkeypatch.setattr(web_search, "_SEARCH_TOTAL_TIMEOUT_SECONDS", 0.03)

    async def stalled(request):
        await asyncio.Event().wait()

    respx.get(HTML_URL).mock(side_effect=stalled)
    result = await search()
    assert result.is_error
    assert "超时" in result.content
    assert "数据缺失" in result.content
    assert len(respx.calls) <= 1


@respx.mock
async def test_search_limit_and_invalid_parameters():
    respx.get(HTML_URL).mock(return_value=httpx.Response(200, text=HTML))
    one = await search(max_results=1)
    assert not one.is_error
    assert "Second report" not in one.content
    for params in ({"query": " "}, {"query": None}, {"max_results": 0}, {"max_results": "bad"}):
        assert (await search(**params)).is_error
    assert len(respx.calls) == 1


@respx.mock
async def test_oversized_response_is_not_parsed_and_falls_back_once(monkeypatch):
    monkeypatch.setattr(web_search, "_SEARCH_MAX_RESPONSE_BYTES", 512)
    respx.get(HTML_URL).mock(return_value=httpx.Response(200, text=HTML * 10))
    respx.get(LITE_URL).mock(return_value=httpx.Response(200, text=LITE))
    result = await search()
    assert not result.is_error
    assert "Lite title" in result.content
    assert "Research report" not in result.content
    assert len(respx.calls) == 2


@respx.mock
async def test_transport_error_does_not_echo_proxy_credentials():
    respx.get(HTML_URL).mock(side_effect=httpx.ProxyError("https://fixture-user:fixture-secret@proxy.invalid"))
    respx.get(LITE_URL).mock(return_value=httpx.Response(503))
    result = await search()
    assert result.is_error
    assert "fixture-secret" not in result.content
    assert "ProxyError" in result.content


@respx.mock
async def test_external_cancellation_is_not_converted_to_search_failure():
    entered = asyncio.Event()

    async def stalled(request):
        entered.set()
        await asyncio.Event().wait()

    respx.get(HTML_URL).mock(side_effect=stalled)
    task = asyncio.create_task(search())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@respx.mock
async def test_synchronous_parser_cannot_return_success_after_deadline(monkeypatch):
    import time

    parse = web_search._parse_results
    monkeypatch.setattr(web_search, "_SEARCH_TOTAL_TIMEOUT_SECONDS", 0.02)

    def slow_parse(html, limit):
        time.sleep(0.03)
        return parse(html, limit)

    monkeypatch.setattr(web_search, "_parse_results", slow_parse)
    respx.get(HTML_URL).mock(return_value=httpx.Response(200, text=HTML))
    result = await search()
    assert result.is_error
    assert "超时" in result.content
