"""Verified Hong Kong index price histories from Tencent's native HK index route."""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from math import isfinite
from zoneinfo import ZoneInfo

import httpx

_ENDPOINT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_INTERVAL = {"daily": "day", "weekly": "week", "monthly": "month"}
_NAMES = {"HSI": {"恒生指数", "恒生指數"}, "HSTECH": {"恒生科技指数", "恒生科技指數"},
          "HSCEI": {"国企指数", "國企指數", "恒生中国企业指数", "恒生中國企業指數"}}
_TIMEOUT = 5.0
HK_TZ = ZoneInfo("Asia/Hong_Kong")


async def history_text(symbol: str, period: str, start: str, end: str, limit: int) -> str:
    interval = _INTERVAL.get(period)
    if symbol not in _NAMES:
        raise ValueError("不支持的港股指数")
    start = date.fromisoformat(start).isoformat()
    end = min(date.fromisoformat(end), datetime.now(HK_TZ).date()).isoformat()
    if not interval or start > end or limit < 1:
        raise ValueError("无效周期、日期区间或条数")
    key = f"hk{symbol}"
    # Query end + bounded lookback: we only return the latest <=120 bars.
    try:
        async with asyncio.timeout(_TIMEOUT), httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_ENDPOINT, params={"param": f"{key},{interval},,{end},{limit+1},"})
            resp.raise_for_status()
            payload = resp.json()
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise ValueError(f"腾讯财经 {symbol} 请求超时，未取得指数数据") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    record = data.get(key) if isinstance(data, dict) else None
    quotes = record.get("qt") if isinstance(record, dict) else None
    quote = quotes.get(key) if isinstance(quotes, dict) else None
    if (not isinstance(quote, list) or len(quote) < 3 or quote[2] != symbol
            or not isinstance(quote[1], str) or quote[1] not in _NAMES[symbol]
            or type(payload.get("code")) is not int or payload["code"] != 0):
        raise ValueError(f"腾讯财经未返回可核实的 {symbol} 指数")
    rows = []
    previous = last_date = None
    bars = record.get(interval)
    if not isinstance(bars, list):
        raise ValueError(f"腾讯财经 {symbol} 行情格式无效")
    for bar in bars:
        if (not isinstance(bar, list) or len(bar) < 5 or not isinstance(bar[0], str)
                or any(type(v) not in (str, int, float) for v in bar[1:5])):
            raise ValueError(f"腾讯财经 {symbol} 行情格式无效")
        day = date.fromisoformat(bar[0]).isoformat()
        if day != bar[0]:
            raise ValueError(f"腾讯财经 {symbol} 行情日期无效")
        op, close, high, low = map(float, bar[1:5])
        if (day > datetime.now(HK_TZ).date().isoformat() or (last_date and day <= last_date) or not all(isfinite(p) and p > 0 for p in (op, close, high, low))
                or not low <= min(op, close) <= max(op, close) <= high):
            raise ValueError(f"腾讯财经 {symbol} 行情校验失败")
        if start <= day <= end:
            change = f"{(close / previous - 1) * 100:+.2f}%" if previous else "-"
            amplitude = f"{(high-low) / previous * 100:.2f}%" if previous else "-"
            rows.append(f"{day} {op:.2f} {close:.2f} {high:.2f} {low:.2f} {change} {amplitude} -")
        last_date, previous = day, close
    if not rows:
        raise ValueError(f"腾讯财经 {symbol} 指定区间内无指数数据")
    return "\n".join([
        f"数据源: 腾讯财经 ({key})", f"标的: {symbol} | 资产类型: index | 市场: HK | 周期: {period}",
        "价格单位: 点 | 时区: Asia/Hong_Kong | 成交量: 未提供可核实单位，不展示",
        f"数据截至: {rows[-1][:10]}（K 线日期，末根可能尚未收盘）",
        f"获取时间: {datetime.now(HK_TZ).isoformat(timespec='seconds')}",
        "日期 开盘 收盘 最高 最低 涨跌幅 振幅 成交量", *rows[-limit:],
    ])
