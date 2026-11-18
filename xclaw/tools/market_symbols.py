"""Symbol normalization helpers for market-specific data providers."""

from __future__ import annotations


def normalize_hk_yf_symbol(symbol: str) -> str:
    """Normalize a Hong Kong stock code into the yfinance symbol format.

    yfinance expects HK tickers like ``0700.HK`` and ``9992.HK`` instead of
    bare five-digit forms such as ``00700.HK`` or ``09992.HK``.
    """

    candidate = symbol.strip().upper()
    if not candidate:
        return candidate

    has_suffix = candidate.endswith(".HK")
    code = candidate[:-3] if has_suffix else candidate

    if code.isdigit():
        if len(code) < 4:
            code = code.zfill(4)
        elif len(code) == 5 and code.startswith("0"):
            code = code[1:]
        return f"{code}.HK"

    return candidate if has_suffix else f"{candidate}.HK"


_HK_INDEX_NAMES = {"恒生指数": "HSI", "恒生科技指数": "HSTECH", "国企指数": "HSCEI",
                   "恒生中国企业指数": "HSCEI"}
HK_HISTORY_INDICES = frozenset(_HK_INDEX_NAMES.values())


def hk_history_index(symbol: str, market: str) -> str | None:
    """Resolve exact HK index aliases. Only explicit caret aliases cross US routing."""
    alias = symbol.strip().upper()
    if market == "US" and alias.startswith("^") and alias[1:] in HK_HISTORY_INDICES:
        return alias[1:]
    if market != "HK":
        return None
    alias = alias.removesuffix(".HK").removeprefix("^")
    return alias if alias in HK_HISTORY_INDICES else _HK_INDEX_NAMES.get(alias)
