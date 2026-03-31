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
