"""Tool: stock_quote – get real-time/delayed stock quote."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any

import httpx

from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult
from xclaw.tools.market_symbols import normalize_hk_yf_symbol


class StockQuoteTool(Tool):
    """Fetch current stock quote (price, change, volume)."""

    _CN_HTTP_TIMEOUT_SECONDS = 3.0
    _BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0"}
    _SINA_HEADERS = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn",
    }

    @property
    def name(self) -> str:
        return "stock_quote"

    @property
    def description(self) -> str:
        return "获取股票实时行情，包括当前价格、涨跌幅、成交量等。A 股优先走腾讯/新浪直连，美股和港股走 yfinance。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "股票代码，如 '600519'（A股）、'00700'（港股）或 'AAPL'（美股）",
                },
                "market": {
                    "type": "string",
                    "enum": ["CN", "US", "HK"],
                    "description": "市场（默认 CN）",
                    "default": "CN",
                },
            },
            "required": ["symbol"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        symbol = params.get("symbol", "").strip()
        market = params.get("market", "CN").upper()
        if not symbol:
            return ToolResult(content="股票代码不能为空", is_error=True)
        try:
            if market == "CN":
                return await self._cn_quote(symbol)
            elif market in ("US", "HK"):
                return await self._yfinance_quote(symbol, market)
            else:
                return ToolResult(content=f"不支持的市场: {market}", is_error=True)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"获取行情失败: {exc}", is_error=True)

    async def _cn_quote(self, symbol: str) -> ToolResult:
        normalized = self._normalize_cn_symbol(symbol)
        providers = (
            ("tencent", self._fetch_tencent_quote),
            ("sina", self._fetch_sina_quote),
            ("akshare", self._fetch_akshare_quote),
        )
        errors: list[str] = []
        for source, fetcher in providers:
            try:
                quote = await fetcher(normalized)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{source}: {exc}")
                continue
            if quote is None:
                errors.append(f"{source}: 未找到股票 {normalized['code']}")
                continue
            return ToolResult(content=self._format_cn_quote(quote))
        return ToolResult(
            content=f"获取 A 股实时行情失败: {'; '.join(errors)}",
            is_error=True,
        )

    async def _yfinance_quote(self, symbol: str, market: str) -> ToolResult:
        import asyncio

        import yfinance as yf  # type: ignore[import]

        yf_symbol = normalize_hk_yf_symbol(symbol) if market == "HK" else symbol
        loop = asyncio.get_event_loop()
        ticker = await loop.run_in_executor(None, lambda: yf.Ticker(yf_symbol))
        info = await loop.run_in_executor(None, lambda: ticker.info)  # type: ignore[union-attr]

        current_price = info.get("currentPrice") or info.get("regularMarketPrice", "N/A")
        prev_close = info.get("previousClose", "N/A")
        if current_price == "N/A" and prev_close == "N/A" and not info.get("shortName"):
            return ToolResult(content=f"未找到 {yf_symbol} 的行情数据", is_error=True)
        change_pct = (
            f"{((current_price - prev_close) / prev_close * 100):.2f}%"
            if isinstance(current_price, (int, float)) and isinstance(prev_close, (int, float))
            else "N/A"
        )
        text = (
            f"股票: {info.get('shortName', yf_symbol)} ({yf_symbol})\n"
            f"当前价: {current_price}\n"
            f"涨跌幅: {change_pct}\n"
            f"市值: {info.get('marketCap', 'N/A')}\n"
            f"今开: {info.get('open', 'N/A')}\n"
            f"最高: {info.get('dayHigh', 'N/A')}\n"
            f"最低: {info.get('dayLow', 'N/A')}\n"
            f"52周最高: {info.get('fiftyTwoWeekHigh', 'N/A')}\n"
            f"52周最低: {info.get('fiftyTwoWeekLow', 'N/A')}"
        )
        return ToolResult(content=text)

    def _normalize_cn_symbol(self, symbol: str) -> dict[str, str]:
        candidate = symbol.strip().upper()
        if not candidate:
            raise ValueError("股票代码不能为空")

        suffix_match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", candidate)
        if suffix_match:
            code, exchange = suffix_match.groups()
        else:
            prefix_match = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", candidate)
            if prefix_match:
                exchange, code = prefix_match.groups()
            elif re.fullmatch(r"\d{6}", candidate):
                code = candidate
                exchange = self._infer_cn_exchange(code)
            else:
                raise ValueError(f"不支持的 A 股代码格式: {symbol}")

        return {
            "code": code,
            "exchange": exchange,
            "prefixed_code": f"{exchange.lower()}{code}",
            "display_symbol": f"{code}.{exchange}",
        }

    def _infer_cn_exchange(self, code: str) -> str:
        if code.startswith(("5", "6", "9")):
            return "SH"
        if code.startswith(("0", "1", "2", "3")):
            return "SZ"
        if code.startswith(("4", "8")):
            return "BJ"
        raise ValueError(f"无法根据代码推断交易所: {code}")

    async def _fetch_tencent_quote(self, normalized: dict[str, str]) -> dict[str, str] | None:
        url = f"http://qt.gtimg.cn/q={normalized['prefixed_code']}"
        async with httpx.AsyncClient(
            timeout=self._CN_HTTP_TIMEOUT_SECONDS,
            trust_env=False,
        ) as client:
            response = await client.get(url, headers=self._BROWSER_HEADERS)
            response.raise_for_status()

        text = response.content.decode("gb18030", errors="ignore").strip()
        if '="' not in text:
            raise ValueError("腾讯返回格式异常")
        payload = text.split('="', 1)[1].rsplit('";', 1)[0]
        parts = payload.split("~")
        if len(parts) < 38 or not parts[1].strip():
            return None

        return {
            "name": parts[1].strip(),
            "symbol": normalized["display_symbol"],
            "price": parts[3].strip() or "N/A",
            "change_pct": f"{parts[32].strip() or 'N/A'}%",
            "change_amount": parts[31].strip() or "N/A",
            "volume": f"{parts[36].strip() or parts[6].strip() or 'N/A'}手",
            "amount": f"{parts[37].strip() or 'N/A'}万元",
            "open": parts[5].strip() or "N/A",
            "high": parts[33].strip() or "N/A",
            "low": parts[34].strip() or "N/A",
            "pre_close": parts[4].strip() or "N/A",
            "quote_time": self._format_tencent_time(parts[30].strip()),
            "source": "腾讯直连 HTTP",
        }

    async def _fetch_sina_quote(self, normalized: dict[str, str]) -> dict[str, str] | None:
        url = f"http://hq.sinajs.cn/list={normalized['prefixed_code']}"
        async with httpx.AsyncClient(
            timeout=self._CN_HTTP_TIMEOUT_SECONDS,
            trust_env=False,
        ) as client:
            response = await client.get(url, headers=self._SINA_HEADERS)
            response.raise_for_status()

        text = response.content.decode("gb18030", errors="ignore").strip()
        if text.lower() == "forbidden":
            raise ValueError("新浪返回 forbidden")
        if '="' not in text:
            raise ValueError("新浪返回格式异常")
        payload = text.split('="', 1)[1].rsplit('";', 1)[0]
        fields = payload.split(",")
        if len(fields) < 32 or not fields[0].strip():
            return None

        price = self._safe_float(fields[3])
        pre_close = self._safe_float(fields[2])
        change_amount = "N/A"
        change_pct = "N/A"
        if price is not None and pre_close not in (None, 0):
            diff = price - pre_close
            change_amount = f"{diff:.2f}"
            change_pct = f"{(diff / pre_close * 100):.2f}%"

        return {
            "name": fields[0].strip(),
            "symbol": normalized["display_symbol"],
            "price": fields[3].strip() or "N/A",
            "change_pct": change_pct,
            "change_amount": change_amount,
            "volume": f"{fields[8].strip() or 'N/A'}股",
            "amount": f"{fields[9].strip() or 'N/A'}元",
            "open": fields[1].strip() or "N/A",
            "high": fields[4].strip() or "N/A",
            "low": fields[5].strip() or "N/A",
            "pre_close": fields[2].strip() or "N/A",
            "quote_time": self._format_sina_time(fields[30].strip(), fields[31].strip()),
            "source": "新浪直连 HTTP",
        }

    async def _fetch_akshare_quote(self, normalized: dict[str, str]) -> dict[str, str] | None:
        import asyncio

        import akshare as ak  # type: ignore[import]

        loop = asyncio.get_event_loop()
        df = await loop.run_in_executor(None, lambda: ak.stock_zh_a_spot_em())
        row = df[df["代码"] == normalized["code"]]
        if row.empty:
            return None
        r = row.iloc[0]
        return {
            "name": str(r.get("名称", normalized["code"])),
            "symbol": normalized["display_symbol"],
            "price": str(r.get("最新价", "N/A")),
            "change_pct": f"{r.get('涨跌幅', 'N/A')}%",
            "change_amount": str(r.get("涨跌额", "N/A")),
            "volume": str(r.get("成交量", "N/A")),
            "amount": str(r.get("成交额", "N/A")),
            "open": str(r.get("今开", "N/A")),
            "high": str(r.get("最高", "N/A")),
            "low": str(r.get("最低", "N/A")),
            "pre_close": str(r.get("昨收", "N/A")),
            "quote_time": "N/A",
            "source": "AKShare/Eastmoney",
        }

    def _format_cn_quote(self, quote: dict[str, str]) -> str:
        return (
            f"股票: {quote['name']} ({quote['symbol']})\n"
            f"当前价: {quote['price']}\n"
            f"涨跌幅: {quote['change_pct']}\n"
            f"涨跌额: {quote['change_amount']}\n"
            f"成交量: {quote['volume']}\n"
            f"成交额: {quote['amount']}\n"
            f"今开: {quote['open']}\n"
            f"最高: {quote['high']}\n"
            f"最低: {quote['low']}\n"
            f"昨收: {quote['pre_close']}\n"
            f"报价时间: {quote['quote_time']}\n"
            f"数据源: {quote['source']}"
        )

    def _safe_float(self, value: str) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _format_tencent_time(self, value: str) -> str:
        if not value:
            return "N/A"
        try:
            return datetime.strptime(value, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return value

    def _format_sina_time(self, date_value: str, time_value: str) -> str:
        if date_value and time_value:
            return f"{date_value} {time_value}"
        return date_value or time_value or "N/A"
