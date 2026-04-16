"""Tool: strategy_scan – strategy-based trade point analysis for a single stock."""

from __future__ import annotations

from typing import Any

from xclaw.config import Settings
from xclaw.investment.strategy_engine import TradePointEngine
from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult


class StrategyScanTool(Tool):
    @property
    def name(self) -> str:
        return "strategy_scan"

    @property
    def description(self) -> str:
        return "按内置策略扫描单只股票，输出参考买入区、止损位、目标位和风险提示。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "股票代码"},
                "market": {
                    "type": "string",
                    "enum": ["CN", "US", "HK"],
                    "default": "CN",
                },
                "strategies": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "指定策略列表，留空则扫描全部策略",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["summary", "full", "decision_card"],
                    "default": "summary",
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
        strategies = params.get("strategies")
        output_mode = params.get("output_mode", "summary")
        settings = context.settings if context.settings is not None else Settings()
        engine = TradePointEngine(settings)

        try:
            results = await engine.scan_symbol(symbol, market=market, strategies=strategies)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"策略扫描失败: {exc}", is_error=True)

        valuable = engine.filter_valuable_strategies(results)

        if context.db is not None:
            await context.db.add_strategy_run(
                chat_id=context.chat_id,
                symbol=symbol.upper(),
                market=market,
                strategies=results,
                valuable_strategies=valuable,
            )

        return ToolResult(content=self._format_result(symbol.upper(), market, results, valuable, output_mode))

    def _format_result(
        self,
        symbol: str,
        market: str,
        results: list[dict[str, Any]],
        valuable: list[dict[str, Any]],
        output_mode: str,
    ) -> str:
        lines = [f"=== {symbol} 策略扫描 ({market}) ==="]
        lines.append(f"高价值策略数: {len(valuable)} / {len(results)}")

        if output_mode == "decision_card":
            top = sorted(results, key=lambda item: int(item.get("bias_score", 0)), reverse=True)[0]
            if valuable:
                overall = "偏多，可分批关注" if any(
                    item.get("signal_status") == "triggered" for item in valuable
                ) else "偏观察，可等待触发确认"
            else:
                overall = "暂不出手，继续观察"
            lines = [f"## {symbol} 策略决策卡", f"市场: {market}", f"综合结论: {overall}"]
            lines.append(
                f"优先策略: {top['strategy_id']} | 状态: {top['signal_status']} | 分数: {top['bias_score']}"
            )
            lines.append(f"参考买入区: {top['buy_zone']}")
            lines.append(f"参考止损: {top['stop_loss']}")
            lines.append(f"第一目标位: {top['target_1']}")
            lines.append(f"触发条件: {top['trigger_condition']}")
            if top.get("risk_notes"):
                lines.append(f"风险提示: {top['risk_notes']}")
            if valuable:
                lines.append(
                    "高价值策略: " + ", ".join(item["strategy_id"] for item in valuable[:4])
                )
            return "\n".join(lines)

        rows = valuable if output_mode == "summary" and valuable else results
        for result in rows:
            lines.append(
                f"- {result['strategy_id']} | 状态: {result['signal_status']} | 分数: {result['bias_score']}"
            )
            lines.append(
                f"  买入区: {result['buy_zone']} | 止损: {result['stop_loss']} | 目标: {result['target_1']}"
            )
            lines.append(f"  条件: {result['trigger_condition']}")
            if result["why_not_trade"]:
                lines.append(f"  备注: {result['why_not_trade']}")

        if output_mode == "summary" and not valuable:
            lines.append("当前没有高价值策略触发，建议继续观察。")
        elif output_mode == "summary":
            quiet = [result["strategy_id"] for result in results if result not in valuable]
            if quiet:
                lines.append(f"无明显信号: {', '.join(quiet)}")

        return "\n".join(lines)
