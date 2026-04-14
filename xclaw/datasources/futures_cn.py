"""Domestic commodity futures datasource helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
from typing import Any

import pandas as pd

_PROVIDER_TIMEOUT_SECONDS = 15.0
_COMMODITY_MARKET = "CF"
_PRODUCT_TO_EXCHANGE: dict[str, str] = {
    # SHFE
    "AG": "SHFE", "AL": "SHFE", "AO": "SHFE", "AU": "SHFE", "BR": "SHFE", "BU": "SHFE",
    "CU": "SHFE", "FU": "SHFE", "HC": "SHFE", "NI": "SHFE", "PB": "SHFE", "RB": "SHFE",
    "RU": "SHFE", "SN": "SHFE", "SP": "SHFE", "SS": "SHFE", "WR": "SHFE", "ZN": "SHFE",
    # INE
    "BC": "INE", "EC": "INE", "LU": "INE", "NR": "INE", "SC": "INE",
    # DCE
    "A": "DCE", "B": "DCE", "BB": "DCE", "C": "DCE", "CS": "DCE", "EB": "DCE",
    "EG": "DCE", "FB": "DCE", "I": "DCE", "J": "DCE", "JD": "DCE", "JM": "DCE",
    "L": "DCE", "LH": "DCE", "M": "DCE", "P": "DCE", "PG": "DCE", "PP": "DCE",
    "RR": "DCE", "V": "DCE", "Y": "DCE",
    # CZCE
    "AP": "CZCE", "CF": "CZCE", "CJ": "CZCE", "CY": "CZCE", "FG": "CZCE", "JR": "CZCE",
    "LR": "CZCE", "MA": "CZCE", "OI": "CZCE", "PF": "CZCE", "PK": "CZCE", "PM": "CZCE",
    "PX": "CZCE", "RI": "CZCE", "RM": "CZCE", "RO": "CZCE", "RS": "CZCE", "SA": "CZCE",
    "SF": "CZCE", "SH": "CZCE", "SM": "CZCE", "SR": "CZCE", "TA": "CZCE", "UR": "CZCE",
    "WH": "CZCE", "ZC": "CZCE",
    # GFEX
    "LC": "GFEX", "SI": "GFEX",
}


@dataclass(frozen=True, slots=True)
class NormalizedCnFutureSymbol:
    symbol: str
    product: str
    exchange: str
    is_main_continuous: bool


def normalize_cn_future_symbol(symbol: str) -> NormalizedCnFutureSymbol:
    candidate = symbol.strip().upper()
    if not candidate:
        raise ValueError("期货合约代码不能为空")

    match = re.fullmatch(r"([A-Z]{1,3})(\d{0,4})", candidate)
    if not match:
        raise ValueError(f"不支持的国内期货代码格式: {symbol}")

    product, suffix = match.groups()
    suffix = suffix or "0"
    exchange = _PRODUCT_TO_EXCHANGE.get(product)
    if exchange is None:
        raise ValueError(f"暂不支持的国内商品期货品种: {product}")

    normalized_symbol = f"{product}{suffix}"
    return NormalizedCnFutureSymbol(
        symbol=normalized_symbol,
        product=product,
        exchange=exchange,
        is_main_continuous=suffix == "0",
    )


async def fetch_cn_future_quote(symbol: str) -> dict[str, str]:
    normalized = normalize_cn_future_symbol(symbol)
    loop = asyncio.get_running_loop()
    providers = (
        ("akshare:futures_zh_spot", lambda: _quote_from_spot(normalized)),
        ("akshare:futures_main_sina", lambda: _quote_from_daily_fallback(normalized)),
    )
    errors: list[str] = []
    for source, producer in providers:
        try:
            quote = await asyncio.wait_for(
                loop.run_in_executor(None, producer),
                timeout=_PROVIDER_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source}: {exc}")
            continue
        if quote:
            quote["source"] = source
            return quote
        errors.append(f"{source}: no data")
    raise RuntimeError(" / ".join(errors))


async def fetch_cn_future_history_dataframe(
    symbol: str,
    *,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    normalized = normalize_cn_future_symbol(symbol)
    loop = asyncio.get_running_loop()
    providers: tuple[tuple[str, Any], ...]
    if normalized.is_main_continuous:
        providers = (
            ("akshare:futures_main_sina", lambda: _history_from_main_sina(normalized, start_date, end_date)),
            ("akshare:futures_zh_daily_sina", lambda: _history_from_daily_sina(normalized, start_date, end_date)),
        )
    else:
        providers = (
            ("akshare:get_futures_daily", lambda: _history_from_exchange_daily(normalized, start_date, end_date)),
            ("akshare:futures_zh_daily_sina", lambda: _history_from_daily_sina(normalized, start_date, end_date)),
        )

    errors: list[str] = []
    for source, producer in providers:
        try:
            df = await asyncio.wait_for(
                loop.run_in_executor(None, producer),
                timeout=_PROVIDER_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source}: {exc}")
            continue
        if df is None or df.empty:
            errors.append(f"{source}: no data")
            continue
        normalized_df = _normalize_history_frame(df)
        if normalized_df.empty:
            errors.append(f"{source}: invalid data")
            continue
        normalized_df.attrs["source"] = source
        return normalized_df
    raise RuntimeError(" / ".join(errors))


def _quote_from_spot(normalized: NormalizedCnFutureSymbol) -> dict[str, str] | None:
    import akshare as ak  # type: ignore[import]

    df = ak.futures_zh_spot(symbol=normalized.symbol, market=_COMMODITY_MARKET, adjust="0")
    if df is None or df.empty:
        return None
    row = df.iloc[0]
    price = _safe_float(row.get("current_price"))
    pre_close = _safe_float(row.get("last_close"))
    change_amount = "N/A"
    change_pct = "N/A"
    if price is not None and pre_close not in (None, 0):
        diff = price - pre_close
        change_amount = f"{diff:.2f}"
        change_pct = f"{(diff / pre_close * 100):.2f}%"

    return {
        "name": str(row.get("symbol", normalized.symbol)),
        "symbol": normalized.symbol,
        "price": _to_text(row.get("current_price")),
        "change_pct": change_pct,
        "change_amount": change_amount,
        "volume": _to_text(row.get("volume")),
        "amount": "N/A",
        "open": _to_text(row.get("open")),
        "high": _to_text(row.get("high")),
        "low": _to_text(row.get("low")),
        "pre_close": _to_text(row.get("last_close")),
        "quote_time": _format_intraday_time(row.get("time")),
    }


def _quote_from_daily_fallback(normalized: NormalizedCnFutureSymbol) -> dict[str, str] | None:
    history = _history_from_main_sina(normalized, "19900101", "22220101")
    if history is None or history.empty:
        history = _history_from_daily_sina(normalized, "19900101", "22220101")
    if history is None or history.empty:
        return None
    row = history.iloc[-1]
    close = _safe_float(row.get("收盘"))
    open_price = _safe_float(row.get("开盘"))
    change_amount = "N/A"
    change_pct = "N/A"
    if close is not None and open_price not in (None, 0):
        diff = close - open_price
        change_amount = f"{diff:.2f}"
        change_pct = f"{(diff / open_price * 100):.2f}%"

    return {
        "name": normalized.symbol,
        "symbol": normalized.symbol,
        "price": _to_text(row.get("收盘")),
        "change_pct": change_pct,
        "change_amount": change_amount,
        "volume": _to_text(row.get("成交量")),
        "amount": "N/A",
        "open": _to_text(row.get("开盘")),
        "high": _to_text(row.get("最高")),
        "low": _to_text(row.get("最低")),
        "pre_close": "N/A",
        "quote_time": str(row.get("日期", "N/A")),
    }


def _history_from_exchange_daily(
    normalized: NormalizedCnFutureSymbol,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    import akshare as ak  # type: ignore[import]

    df = ak.get_futures_daily(
        start_date=_digits_date(start_date),
        end_date=_digits_date(end_date),
        market=normalized.exchange,
    )
    if df is None or df.empty:
        return pd.DataFrame()
    frame = df.copy()
    frame["symbol"] = frame["symbol"].astype(str).str.upper()
    frame = frame[frame["symbol"] == normalized.symbol]
    if frame.empty:
        return pd.DataFrame()
    return frame.rename(
        columns={
            "date": "日期",
            "open": "开盘",
            "high": "最高",
            "low": "最低",
            "close": "收盘",
            "volume": "成交量",
        }
    )[["日期", "开盘", "收盘", "最高", "最低", "成交量"]]


def _history_from_main_sina(
    normalized: NormalizedCnFutureSymbol,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    import akshare as ak  # type: ignore[import]

    df = ak.futures_main_sina(
        symbol=normalized.symbol,
        start_date=_digits_date(start_date),
        end_date=_digits_date(end_date),
    )
    if df is None or df.empty:
        return pd.DataFrame()
    return df.rename(
        columns={
            "开盘价": "开盘",
            "最高价": "最高",
            "最低价": "最低",
            "收盘价": "收盘",
        }
    )[["日期", "开盘", "收盘", "最高", "最低", "成交量"]]


def _history_from_daily_sina(
    normalized: NormalizedCnFutureSymbol,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    import akshare as ak  # type: ignore[import]

    df = ak.futures_zh_daily_sina(symbol=normalized.symbol)
    if df is None or df.empty:
        return pd.DataFrame()
    frame = df.rename(
        columns={
            "date": "日期",
            "open": "开盘",
            "high": "最高",
            "low": "最低",
            "close": "收盘",
            "volume": "成交量",
        }
    )[["日期", "开盘", "收盘", "最高", "最低", "成交量"]]
    start = _iso_date(start_date)
    end = _iso_date(end_date)
    frame["日期"] = frame["日期"].astype(str)
    return frame[(frame["日期"] >= start) & (frame["日期"] <= end)]


def _normalize_history_frame(df: pd.DataFrame) -> pd.DataFrame:
    frame = df.copy()
    frame["日期"] = frame["日期"].astype(str).str[:10]
    for column in ("开盘", "收盘", "最高", "最低", "成交量"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["日期", "开盘", "收盘", "最高", "最低", "成交量"])
    frame = frame.sort_values("日期").reset_index(drop=True)
    return frame[["日期", "开盘", "收盘", "最高", "最低", "成交量"]]


def _digits_date(value: str) -> str:
    return value.replace("-", "")


def _iso_date(value: str) -> str:
    digits = _digits_date(value)
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def _format_intraday_time(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) == 6 and text.isdigit():
        return f"{text[:2]}:{text[2:4]}:{text[4:6]}"
    return text or "N/A"


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_text(value: Any) -> str:
    if value is None:
        return "N/A"
    return str(value)
