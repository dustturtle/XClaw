"""A-share datasource helpers with failover for current network conditions."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta
import threading
from typing import Any

import httpx
from loguru import logger
import pandas as pd


_CN_HTTP_TIMEOUT_SECONDS = 3.0
_BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0"}
_TDX_SERVERS = (
    ("218.75.126.9", 7709),
    ("119.147.212.81", 7709),
    ("183.60.224.178", 7709),
)
_INDEX_CODES = (
    ("上证指数", "sh000001"),
    ("深证成指", "sz399001"),
    ("创业板指", "sz399006"),
    ("科创50", "sh000688"),
    ("沪深300", "sh000300"),
    ("中证500", "sh000905"),
)
_BAOSTOCK_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class NormalizedCnSymbol:
    code: str
    exchange: str
    prefixed_code: str
    bs_code: str
    yf_symbol: str
    tdx_market: int
    display_symbol: str


def normalize_cn_symbol(symbol: str) -> NormalizedCnSymbol:
    candidate = symbol.strip().upper()
    if not candidate:
        raise ValueError("股票代码不能为空")

    if "." in candidate:
        code, exchange = candidate.split(".", 1)
    elif candidate[:2] in {"SH", "SZ", "BJ"} and len(candidate) == 8:
        exchange, code = candidate[:2], candidate[2:]
    elif len(candidate) == 6 and candidate.isdigit():
        code = candidate
        exchange = _infer_exchange(code)
    else:
        raise ValueError(f"不支持的 A 股代码格式: {symbol}")

    exchange = exchange.upper()
    if exchange not in {"SH", "SZ", "BJ"} or len(code) != 6 or not code.isdigit():
        raise ValueError(f"不支持的 A 股代码格式: {symbol}")

    return NormalizedCnSymbol(
        code=code,
        exchange=exchange,
        prefixed_code=f"{exchange.lower()}{code}",
        bs_code=f"{exchange.lower()}.{code}",
        yf_symbol=f"{code}.{'SS' if exchange == 'SH' else exchange}",
        tdx_market=1 if exchange == "SH" else 0,
        display_symbol=f"{code}.{exchange}",
    )


async def fetch_cn_history_dataframe(
    symbol: str,
    *,
    period: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    normalized = normalize_cn_symbol(symbol)
    loop = asyncio.get_running_loop()
    providers = (
        ("baostock", lambda: _history_from_baostock(normalized, period, start_date, end_date)),
        ("pytdx", lambda: _history_from_pytdx(normalized, period, start_date, end_date)),
        ("yfinance", lambda: _history_from_yfinance(normalized, period, start_date, end_date)),
        ("akshare", lambda: _history_from_akshare(normalized, period, start_date, end_date)),
    )
    errors: list[str] = []
    for source, producer in providers:
        try:
            df = await loop.run_in_executor(None, producer)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"A-share history provider {source} failed: {exc}")
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


async def fetch_cn_index_quotes() -> list[dict[str, str]]:
    code_list = ",".join(code for _, code in _INDEX_CODES)
    async with httpx.AsyncClient(
        timeout=_CN_HTTP_TIMEOUT_SECONDS,
        trust_env=False,
    ) as client:
        response = await client.get(
            f"http://qt.gtimg.cn/q={code_list}",
            headers=_BROWSER_HEADERS,
        )
        response.raise_for_status()

    parsed: dict[str, dict[str, str]] = {}
    text = response.content.decode("gb18030", errors="ignore")
    for chunk in text.split(";"):
        line = chunk.strip()
        if not line or '="' not in line:
            continue
        code = line.split('="', 1)[0].removeprefix("v_")
        payload = line.split('="', 1)[1].rstrip('"')
        parts = payload.split("~")
        if len(parts) < 33 or not parts[1].strip():
            continue
        parsed[code] = {
            "name": parts[1].strip(),
            "price": parts[3].strip() or "N/A",
            "change_pct": parts[32].strip() or "N/A",
        }

    return [
        {
            "name": label,
            "price": parsed[code]["price"],
            "change_pct": parsed[code]["change_pct"],
            "source": "腾讯直连 HTTP",
        }
        for label, code in _INDEX_CODES
        if code in parsed
    ]


async def fetch_cn_sector_snapshots(limit: int = 5) -> dict[str, list[dict[str, str]]] | None:
    loop = asyncio.get_running_loop()
    try:
        import akshare as ak  # type: ignore[import]

        df = await loop.run_in_executor(None, lambda: ak.stock_board_industry_name_em())
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"A-share sector snapshot failed: {exc}")
        return None

    if df is None or df.empty:
        return None
    frame = df.copy()
    if "涨跌幅" not in frame.columns:
        return None
    frame["涨跌幅"] = pd.to_numeric(frame["涨跌幅"], errors="coerce")
    frame = frame.dropna(subset=["涨跌幅"])
    if frame.empty:
        return None

    top = frame.nlargest(limit, "涨跌幅")
    bottom = frame.nsmallest(limit, "涨跌幅")
    return {
        "top": [
            {
                "name": str(row.get("板块名称", row.get("名称", "N/A"))),
                "change_pct": str(row.get("涨跌幅", "N/A")),
            }
            for _, row in top.iterrows()
        ],
        "bottom": [
            {
                "name": str(row.get("板块名称", row.get("名称", "N/A"))),
                "change_pct": str(row.get("涨跌幅", "N/A")),
            }
            for _, row in bottom.iterrows()
        ],
    }


def _infer_exchange(code: str) -> str:
    if code.startswith(("5", "6", "9")):
        return "SH"
    if code.startswith(("0", "1", "2", "3")):
        return "SZ"
    if code.startswith(("4", "8")):
        return "BJ"
    raise ValueError(f"无法根据代码推断交易所: {code}")


def _history_from_baostock(
    normalized: NormalizedCnSymbol,
    period: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    import baostock as bs  # type: ignore[import]

    frequency_map = {"daily": "d", "weekly": "w", "monthly": "m"}
    frequency = frequency_map.get(period, "d")
    with _BAOSTOCK_LOCK:
        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(login.error_msg or "baostock login failed")
        try:
            rs = bs.query_history_k_data_plus(
                normalized.bs_code,
                "date,open,high,low,close,volume,amount",
                start_date=_to_ymd(start_date),
                end_date=_to_ymd(end_date),
                frequency=frequency,
                adjustflag="2",
            )
            if rs.error_code != "0":
                raise RuntimeError(rs.error_msg or "baostock query failed")
            rows: list[list[str]] = []
            while rs.next():
                rows.append(rs.get_row_data())
        finally:
            with contextlib.suppress(Exception):
                bs.logout()
    return pd.DataFrame(rows, columns=["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"])


def _history_from_pytdx(
    normalized: NormalizedCnSymbol,
    period: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    from pytdx.hq import TdxHq_API  # type: ignore[import]

    category_map = {"daily": 9, "weekly": 5, "monthly": 6}
    category = category_map.get(period, 9)
    count = _estimate_bar_count(start_date, end_date, period)
    api = TdxHq_API()
    errors: list[str] = []
    for host, port in _TDX_SERVERS:
        try:
            if not api.connect(host, port, time_out=5):
                errors.append(f"{host}:{port} connect failed")
                continue
            raw = api.get_security_bars(category, normalized.tdx_market, normalized.code, 0, count)
            if not raw:
                errors.append(f"{host}:{port} no data")
                continue
            frame = pd.DataFrame(raw)
            frame["日期"] = pd.to_datetime(frame["datetime"]).dt.strftime("%Y-%m-%d")
            frame["开盘"] = frame["open"]
            frame["最高"] = frame["high"]
            frame["最低"] = frame["low"]
            frame["收盘"] = frame["close"]
            frame["成交量"] = pd.to_numeric(frame["vol"], errors="coerce") * 100
            frame["成交额"] = pd.to_numeric(frame["amount"], errors="coerce")
            frame = frame[["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"]]
            start_ts = pd.Timestamp(_to_ymd(start_date))
            end_ts = pd.Timestamp(_to_ymd(end_date))
            frame["日期_ts"] = pd.to_datetime(frame["日期"])
            frame = frame[(frame["日期_ts"] >= start_ts) & (frame["日期_ts"] <= end_ts)]
            frame = frame.drop(columns=["日期_ts"])
            if frame.empty:
                errors.append(f"{host}:{port} date filter empty")
                continue
            return frame
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{host}:{port} {exc}")
        finally:
            with contextlib.suppress(Exception):
                api.disconnect()
    raise RuntimeError(" / ".join(errors))


def _history_from_yfinance(
    normalized: NormalizedCnSymbol,
    period: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    import yfinance as yf  # type: ignore[import]

    interval_map = {"daily": "1d", "weekly": "1wk", "monthly": "1mo"}
    ticker = yf.Ticker(normalized.yf_symbol)
    end_exclusive = (pd.Timestamp(_to_ymd(end_date)) + timedelta(days=1)).strftime("%Y-%m-%d")
    frame = ticker.history(
        start=_to_ymd(start_date),
        end=end_exclusive,
        interval=interval_map.get(period, "1d"),
    )
    if frame is None or frame.empty:
        raise RuntimeError("no data")
    frame = frame.reset_index()
    date_column = "Date" if "Date" in frame.columns else frame.columns[0]
    frame["日期"] = pd.to_datetime(frame[date_column]).dt.strftime("%Y-%m-%d")
    frame["开盘"] = frame["Open"]
    frame["最高"] = frame["High"]
    frame["最低"] = frame["Low"]
    frame["收盘"] = frame["Close"]
    frame["成交量"] = frame["Volume"]
    frame["成交额"] = pd.NA
    return frame[["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"]]


def _history_from_akshare(
    normalized: NormalizedCnSymbol,
    period: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    import akshare as ak  # type: ignore[import]

    frame = ak.stock_zh_a_hist(
        symbol=normalized.code,
        period=period if period in {"daily", "weekly", "monthly"} else "daily",
        start_date=_to_compact_ymd(start_date),
        end_date=_to_compact_ymd(end_date),
        adjust="qfq",
    )
    if frame is None or frame.empty:
        raise RuntimeError("no data")
    columns = [c for c in ["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"] if c in frame.columns]
    return frame[columns]


def _normalize_history_frame(frame: pd.DataFrame) -> pd.DataFrame:
    df = frame.copy()
    required = ["日期", "开盘", "最高", "最低", "收盘", "成交量"]
    for column in required:
        if column not in df.columns:
            raise ValueError(f"missing column {column}")
    if "成交额" not in df.columns:
        df["成交额"] = pd.NA
    df["日期"] = pd.to_datetime(df["日期"]).dt.strftime("%Y-%m-%d")
    for column in ["开盘", "最高", "最低", "收盘", "成交量", "成交额"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["日期", "开盘", "最高", "最低", "收盘"])
    df = df.sort_values("日期").drop_duplicates(subset=["日期"], keep="last")
    return df[["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"]]


def _estimate_bar_count(start_date: str, end_date: str, period: str) -> int:
    start = pd.Timestamp(_to_ymd(start_date))
    end = pd.Timestamp(_to_ymd(end_date))
    delta_days = max((end - start).days, 30)
    if period == "weekly":
        estimate = delta_days // 5 + 20
    elif period == "monthly":
        estimate = delta_days // 21 + 12
    else:
        estimate = delta_days + 30
    return min(max(estimate, 80), 800)


def _to_ymd(value: str) -> str:
    if "-" in value:
        return value
    return datetime.strptime(value, "%Y%m%d").strftime("%Y-%m-%d")


def _to_compact_ymd(value: str) -> str:
    if "-" not in value:
        return value
    return value.replace("-", "")
