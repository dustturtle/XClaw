"""Shared models and constants for investment strategy scans."""

from __future__ import annotations

from dataclasses import asdict, dataclass


RULE_BASED_STRATEGIES: tuple[str, ...] = (
    "bull_trend",
    "ma_golden_cross",
    "shrink_pullback",
    "volume_breakout",
    "bottom_volume",
    "one_yang_three_yin",
    "box_oscillation",
)

FRAMEWORK_STRATEGIES: tuple[str, ...] = (
    "chan_theory",
    "wave_theory",
    "emotion_cycle",
    "dragon_head",
)

ALL_STRATEGIES: tuple[str, ...] = RULE_BASED_STRATEGIES + FRAMEWORK_STRATEGIES

STRATEGY_DISPLAY_NAMES: dict[str, str] = {
    "bull_trend": "默认多头趋势",
    "ma_golden_cross": "均线金叉",
    "shrink_pullback": "缩量回踩",
    "volume_breakout": "放量突破",
    "bottom_volume": "底部放量",
    "one_yang_three_yin": "一阳夹三阴",
    "box_oscillation": "箱体震荡",
    "chan_theory": "缠论",
    "wave_theory": "波浪理论",
    "emotion_cycle": "情绪周期",
    "dragon_head": "龙头策略",
}


@dataclass(slots=True)
class StrategyResult:
    strategy_id: str
    display_name: str
    signal_status: str
    bias_score: int
    buy_zone: str
    stop_loss: str
    target_1: str
    trigger_condition: str
    risk_notes: str
    why_not_trade: str
    tier: str = "rule"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
