"""Daily investment report generation service."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from xclaw.config import Settings
from xclaw.datasources.a_share import fetch_cn_index_quotes, fetch_cn_sector_snapshots
from xclaw.investment.strategy_engine import TradePointEngine


class InvestmentReportService:
    """Generate and persist watchlist-based daily reports."""

    def __init__(
        self,
        *,
        db: Any,
        settings: Settings | None = None,
        llm: Any = None,
        engine: TradePointEngine | None = None,
    ) -> None:
        self._db = db
        self._settings = settings or Settings()
        self._llm = llm
        self._engine = engine or TradePointEngine(self._settings)

    async def generate_report(
        self,
        *,
        chat_id: int,
        trigger_source: str,
        symbols: list[str] | None = None,
        market: str = "CN",
    ) -> dict[str, Any]:
        rows = await self._resolve_symbols(chat_id=chat_id, symbols=symbols, market=market)
        if not rows:
            raise ValueError("当前没有可用于生成日报的股票，请先添加自选股。")

        overview = await self._build_market_overview()
        stock_sections: list[str] = []
        triggered_count = 0
        watch_count = 0

        for row in rows:
            strategy_results = await self._engine.scan_symbol(row["symbol"], market=row["market"])
            valuable = self._engine.filter_valuable_strategies(strategy_results)
            if valuable:
                primary = valuable[:3]
                triggered_count += 1
            else:
                primary = strategy_results[:2]
                watch_count += 1
            stock_sections.append(self._format_stock_section(row, primary, len(valuable)))

        report_date = datetime.now().strftime("%Y-%m-%d")
        title = f"{report_date} 自选股日报"
        summary = f"{len(rows)} 只股票，{triggered_count} 只出现高价值策略，{watch_count} 只以观察为主"
        content = self._render_markdown(title, summary, overview, stock_sections)

        report_id = await self._db.add_investment_report(
            chat_id=chat_id,
            report_type="daily_watchlist",
            title=title,
            summary=summary,
            content_markdown=content,
            symbol_count=len(rows),
            trigger_source=trigger_source,
        )
        latest = await self._db.get_latest_investment_report(chat_id)
        if latest is None:
            raise RuntimeError("日报写入成功后未能读取回记录")
        return latest | {"id": report_id}

    async def latest_report(self, chat_id: int) -> dict[str, Any] | None:
        return await self._db.get_latest_investment_report(chat_id)

    async def list_reports(self, chat_id: int, limit: int = 20) -> list[dict[str, Any]]:
        return await self._db.list_investment_reports(chat_id, limit=limit)

    async def _resolve_symbols(
        self,
        *,
        chat_id: int,
        symbols: list[str] | None,
        market: str,
    ) -> list[dict[str, str]]:
        if symbols:
            unique = []
            seen = set()
            for symbol in symbols:
                normalized = symbol.strip().upper()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    unique.append({"symbol": normalized, "market": market.upper(), "name": ""})
            return unique[: self._settings.strategy_report_max_symbols]

        watchlist = await self._db.get_watchlist(chat_id)
        rows = [
            {
                "symbol": item["symbol"],
                "market": item["market"],
                "name": item.get("name") or "",
            }
            for item in watchlist
            if item["market"] == market.upper()
        ]
        return rows[: self._settings.strategy_report_max_symbols]

    async def _build_market_overview(self) -> str:
        quotes = await fetch_cn_index_quotes()
        sectors = await fetch_cn_sector_snapshots()

        if not quotes:
            return "市场概览暂不可用。"

        lines = ["主要指数："]
        for quote in quotes[:3]:
            lines.append(f"- {quote['name']}: {quote['price']} ({quote['change_pct']}%)")
        if sectors:
            top = ", ".join(f"{row['name']} {row['change_pct']}%" for row in sectors["top"][:3])
            bottom = ", ".join(f"{row['name']} {row['change_pct']}%" for row in sectors["bottom"][:3])
            lines.append(f"领涨板块：{top}")
            lines.append(f"领跌板块：{bottom}")
        return "\n".join(lines)

    def _format_stock_section(
        self,
        row: dict[str, str],
        strategies: list[dict[str, Any]],
        valuable_count: int,
    ) -> str:
        header_name = f"{row['name']} " if row.get("name") else ""
        lines = [f"### {header_name}{row['symbol']}"]
        lines.append(
            f"高价值策略数：{valuable_count}" if valuable_count else "当前没有高价值策略触发，以观察为主。"
        )
        for strategy in strategies:
            lines.append(
                f"- {strategy['strategy_id']} | {strategy['signal_status']} | 买入区 {strategy['buy_zone']} | "
                f"止损 {strategy['stop_loss']} | 目标 {strategy['target_1']}"
            )
            lines.append(f"  条件：{strategy['trigger_condition']}")
        return "\n".join(lines)

    def _render_markdown(
        self,
        title: str,
        summary: str,
        overview: str,
        stock_sections: list[str],
    ) -> str:
        lines = [f"# {title}", "", f"摘要：{summary}", "", "## 市场概览", overview, "", "## 个股策略卡"]
        for section in stock_sections:
            lines.extend([section, ""])
        lines.extend(
            [
                "## 免责声明",
                "以下内容仅基于规则和公开市场数据生成，属于参考分析，不构成投资建议。",
            ]
        )
        return "\n".join(lines)
