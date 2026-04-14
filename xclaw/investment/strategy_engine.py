"""Strategy scanning engine for investment trade-point generation."""

from __future__ import annotations

from typing import Any

from xclaw.config import Settings
from xclaw.datasources.a_share import fetch_cn_history_dataframe
from xclaw.investment.strategy_models import ALL_STRATEGIES, FRAMEWORK_STRATEGIES, RULE_BASED_STRATEGIES
from xclaw.investment.strategy_rules import (
    build_context,
    evaluate_framework_strategy,
    evaluate_rule_strategy,
    is_valuable_strategy,
)


class TradePointEngine:
    """Build structured strategy scan results for a single stock."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()

    async def scan_symbol(
        self,
        symbol: str,
        market: str = "CN",
        strategies: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        symbol = symbol.strip().upper()
        market = market.upper()
        if not symbol:
            raise ValueError("symbol is required")

        selected = self._normalize_strategies(strategies)
        ctx = build_context(
            await self._load_history(symbol, market),
            self._settings.strategy_bias_threshold,
        )

        results: list[dict[str, Any]] = []
        for strategy_id in selected:
            if strategy_id in RULE_BASED_STRATEGIES:
                results.append(evaluate_rule_strategy(strategy_id, ctx).to_dict())
            else:
                results.append(evaluate_framework_strategy(strategy_id, ctx).to_dict())
        return results

    def filter_valuable_strategies(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [result for result in results if is_valuable_strategy(result)]

    async def _load_history(self, symbol: str, market: str):
        if market != "CN":
            raise ValueError(f"strategy scanning currently supports CN only, got {market}")
        df = await fetch_cn_history_dataframe(
            symbol,
            period="daily",
            start_date="2025-01-01",
            end_date="2099-12-31",
        )
        if df is None or df.empty:
            raise ValueError(f"未找到 {symbol} 的历史数据")
        return df.tail(120).reset_index(drop=True)

    def _normalize_strategies(self, strategies: list[str] | None) -> list[str]:
        if not strategies:
            return list(ALL_STRATEGIES)
        normalized = []
        for item in strategies:
            strategy_id = item.strip()
            if strategy_id in ALL_STRATEGIES and strategy_id not in normalized:
                normalized.append(strategy_id)
        return normalized or list(ALL_STRATEGIES)
