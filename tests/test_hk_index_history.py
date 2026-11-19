import asyncio

import httpx
import pytest

from xclaw.datasources import hk_index_history as source
from xclaw.tools.market_symbols import hk_history_index, normalize_hk_yf_symbol
from xclaw.tools.stock_history import StockHistoryTool


@pytest.mark.parametrize("symbol,market,expected", [
    ("HSI", "HK", "HSI"), ("hstech.hk", "HK", "HSTECH"), ("恒生中国企业指数", "HK", "HSCEI"),
    ("^HSI", "US", "HSI"), ("HSI", "US", None), ("^GSPC", "US", None), ("HSI", "CN", None),
])
def test_exact_identity(symbol, market, expected):
    assert hk_history_index(symbol, market) == expected
    assert normalize_hk_yf_symbol("00700") == "0700.HK"


def test_explicit_unsupported_index():
    res = asyncio.run(StockHistoryTool().execute(dict(symbol="XYZ", asset_type="index", market="HK"), None))
    assert res.is_error and "暂不支持" in res.content and ".HK" not in res.content


def stub(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(source.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handler), **kw))


@pytest.mark.parametrize("wrong_identity", [True, False])
def test_native_index_route(monkeypatch, wrong_identity):
    def handler(req):
        assert req.url.params["param"] == "hkHSTECH,day,,2026-11-18,3,"
        return httpx.Response(200, json={"code": 0, "data": {"hkHSTECH": {
            "qt": {"hkHSTECH": ["100", "恒生科技指数", "HSI" if wrong_identity else "HSTECH"]},
            "day": [["2026-11-16", 100, 100, 100, 100, 99999], ["2026-11-17", 100, 101, 101, 100, 99999]],
        }}})
    stub(monkeypatch, handler)
    res = asyncio.run(StockHistoryTool().execute(dict(symbol="HSTECH", market="HK", start_date="2026-11-17",
                                                     end_date="2026-11-18", limit=2), None))
    assert res.is_error == wrong_identity
    if not wrong_identity:
        assert "资产类型: index" in res.content and "价格单位: 点" in res.content
        assert "2026-11-17 100.00 101.00 101.00 100.00 +1.00% 1.00% -" in res.content
        assert "2026-11-16" not in res.content and "99999" not in res.content


def test_timeout_cancels_inflight_request(monkeypatch):
    stopped = []
    async def slow(req):
        try:
            await asyncio.sleep(5)
        finally:
            stopped.append(True)
    stub(monkeypatch, slow)
    monkeypatch.setattr(source, "_TIMEOUT", .03)
    res = asyncio.run(StockHistoryTool().execute(dict(symbol="^HSI", market="US"), None))
    assert res.is_error and "超时" in res.content and stopped


@pytest.mark.parametrize("code,name,bar", [
    (False, "恒生指数", ["2026-11-17", 1, 1, 1, 1]),
    (0, "恒生指数", ["2026-11-17", True, True, True, True]),
    (0, "恒生指数", ["2999-01-01", 1, 1, 1, 1]),
    (0, "恒生指数", ["20261117", 1, 1, 1, 1]),
    (0, "恒生指数", ["2026-11-17", 2, 2, 1, 1]),
    (0, "国企指数", ["2026-11-17", 1, 1, 1, 1]),
    (0, "恒生指数", {"date": "2026-11-17"}),
])
def test_reject_invalid_provider_record(monkeypatch, code, name, bar):
    def handler(req):
        return httpx.Response(200, json={"code": code, "data": {"hkHSI": {
            "qt": {"hkHSI": ["100", name, "HSI"]}, "day": [bar]}}})
    stub(monkeypatch, handler)
    result = asyncio.run(StockHistoryTool().execute(dict(symbol="HSI", market="HK",
                         start_date="2026-11-11", end_date="2999-01-01"), None))
    assert result.is_error and "腾讯财经" in result.content
