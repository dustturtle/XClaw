"""Deterministic trade-point rules used by strategy scanning."""

from __future__ import annotations

from typing import Any

import pandas as pd

from xclaw.investment.strategy_models import STRATEGY_DISPLAY_NAMES, StrategyResult


def build_context(df: pd.DataFrame, bias_threshold: float) -> dict[str, Any]:
    closes = df["收盘"].astype(float)
    opens = df["开盘"].astype(float)
    highs = df["最高"].astype(float)
    lows = df["最低"].astype(float)
    volumes = df["成交量"].astype(float)

    ma5 = closes.rolling(5, min_periods=1).mean()
    ma10 = closes.rolling(10, min_periods=1).mean()
    ma20 = closes.rolling(20, min_periods=1).mean()

    latest_close = float(closes.iloc[-1])
    latest_open = float(opens.iloc[-1])
    latest_high = float(highs.iloc[-1])
    latest_low = float(lows.iloc[-1])
    latest_volume = float(volumes.iloc[-1])
    avg5_volume = float(volumes.tail(5).mean())
    resistance_20 = float(highs.tail(20).max())
    support_20 = float(lows.tail(20).min())
    recent_low = float(lows.tail(5).min())
    recent_high = float(highs.tail(5).max())

    ma5_now = float(ma5.iloc[-1])
    ma10_now = float(ma10.iloc[-1])
    ma20_now = float(ma20.iloc[-1])

    ma5_prev = float(ma5.iloc[-2]) if len(ma5) > 1 else ma5_now
    ma10_prev = float(ma10.iloc[-2]) if len(ma10) > 1 else ma10_now
    ma20_prev = float(ma20.iloc[-2]) if len(ma20) > 1 else ma20_now

    bias_pct = ((latest_close - ma5_now) / ma5_now * 100) if ma5_now else 0.0
    volume_ratio = latest_volume / avg5_volume if avg5_volume else 1.0

    return {
        "df": df,
        "bias_threshold": bias_threshold,
        "latest_close": latest_close,
        "latest_open": latest_open,
        "latest_high": latest_high,
        "latest_low": latest_low,
        "latest_volume": latest_volume,
        "avg5_volume": avg5_volume,
        "volume_ratio": volume_ratio,
        "ma5": ma5_now,
        "ma10": ma10_now,
        "ma20": ma20_now,
        "ma5_prev": ma5_prev,
        "ma10_prev": ma10_prev,
        "ma20_prev": ma20_prev,
        "bias_pct": bias_pct,
        "resistance_20": resistance_20,
        "support_20": support_20,
        "recent_low": recent_low,
        "recent_high": recent_high,
        "drawdown_pct": ((resistance_20 - latest_close) / resistance_20 * 100) if resistance_20 else 0.0,
    }


def evaluate_rule_strategy(strategy_id: str, ctx: dict[str, Any]) -> StrategyResult:
    if strategy_id == "bull_trend":
        return _bull_trend(ctx)
    if strategy_id == "ma_golden_cross":
        return _ma_golden_cross(ctx)
    if strategy_id == "shrink_pullback":
        return _shrink_pullback(ctx)
    if strategy_id == "volume_breakout":
        return _volume_breakout(ctx)
    if strategy_id == "bottom_volume":
        return _bottom_volume(ctx)
    if strategy_id == "one_yang_three_yin":
        return _one_yang_three_yin(ctx)
    if strategy_id == "box_oscillation":
        return _box_oscillation(ctx)
    raise ValueError(f"unsupported rule strategy: {strategy_id}")


def evaluate_framework_strategy(strategy_id: str, ctx: dict[str, Any]) -> StrategyResult:
    latest_close = ctx["latest_close"]
    ma10 = ctx["ma10"]
    ma20 = ctx["ma20"]
    bias_pct = ctx["bias_pct"]
    trend_up = ctx["ma5"] >= ma10 >= ma20

    if strategy_id == "chan_theory":
        status = "near_trigger" if trend_up else "watch"
        trigger = "等待二买/三买确认，优先看回踩不破 MA10" if trend_up else "当前更像中枢震荡，暂无明确买点"
    elif strategy_id == "wave_theory":
        status = "near_trigger" if trend_up and bias_pct < ctx["bias_threshold"] else "watch"
        trigger = "疑似推进浪延续，等待回踩 0.382~0.5 区域企稳" if trend_up else "浪型不清晰，避免主观归浪"
    elif strategy_id == "emotion_cycle":
        status = "risk_off" if bias_pct > ctx["bias_threshold"] * 1.5 else "watch"
        trigger = "情绪升温但未极热，关注量能变化" if status != "risk_off" else "情绪与乖离同时偏热，先别追"
    elif strategy_id == "dragon_head":
        status = "watch"
        trigger = "缺少板块共振与龙头确认数据，先按观察处理"
    else:
        raise ValueError(f"unsupported framework strategy: {strategy_id}")

    return StrategyResult(
        strategy_id=strategy_id,
        display_name=STRATEGY_DISPLAY_NAMES[strategy_id],
        signal_status=status,
        bias_score=_base_bias_score(ctx, status),
        buy_zone=f"{min(ma10, latest_close):.2f}-{max(ma10, latest_close):.2f}",
        stop_loss=f"{ma20 * 0.99:.2f}",
        target_1=f"{ctx['recent_high']:.2f}",
        trigger_condition=trigger,
        risk_notes="框架型策略，当前输出为条件式参考位，不是唯一精确点位。",
        why_not_trade="" if status != "watch" else "当前信息不足以给出确定性入场点。",
        tier="framework",
    )


def is_valuable_strategy(result: dict[str, object]) -> bool:
    return str(result.get("signal_status", "")) in {"triggered", "near_trigger", "risk_off"}


def _base_bias_score(ctx: dict[str, Any], status: str) -> int:
    base = 50
    if ctx["ma5"] >= ctx["ma10"] >= ctx["ma20"]:
        base += 15
    if ctx["bias_pct"] <= ctx["bias_threshold"]:
        base += 8
    if status == "triggered":
        base += 12
    elif status == "near_trigger":
        base += 6
    elif status == "risk_off":
        base -= 12
    return max(5, min(95, int(base)))


def _bull_trend(ctx: dict[str, Any]) -> StrategyResult:
    trend_up = ctx["ma5"] >= ctx["ma10"] >= ctx["ma20"] and ctx["ma20"] >= ctx["ma20_prev"]
    near_ma5 = abs(ctx["latest_close"] - ctx["ma5"]) / ctx["ma5"] <= 0.025 if ctx["ma5"] else False
    low_bias = ctx["bias_pct"] <= ctx["bias_threshold"]
    status = "triggered" if trend_up and near_ma5 and low_bias else "near_trigger" if trend_up else "watch"
    why_not_trade = "" if status != "watch" else "均线多头结构还不够完整，先等待趋势更清晰。"
    return StrategyResult(
        strategy_id="bull_trend",
        display_name=STRATEGY_DISPLAY_NAMES["bull_trend"],
        signal_status=status,
        bias_score=_base_bias_score(ctx, status),
        buy_zone=f"{ctx['ma5'] * 0.995:.2f}-{ctx['ma10'] * 1.005:.2f}",
        stop_loss=f"{ctx['ma20'] * 0.99:.2f}",
        target_1=f"{ctx['recent_high'] * 1.03:.2f}",
        trigger_condition="MA5>=MA10>=MA20 且优先回踩均线附近再考虑介入。",
        risk_notes="若价格明显跌破 MA20，多头延续假设失效。",
        why_not_trade=why_not_trade,
    )


def _ma_golden_cross(ctx: dict[str, Any]) -> StrategyResult:
    crossed_5_10 = ctx["ma5_prev"] <= ctx["ma10_prev"] and ctx["ma5"] > ctx["ma10"]
    crossed_10_20 = ctx["ma10_prev"] <= ctx["ma20_prev"] and ctx["ma10"] > ctx["ma20"]
    status = "triggered" if (crossed_5_10 or crossed_10_20) and ctx["bias_pct"] <= ctx["bias_threshold"] else "near_trigger" if (ctx["ma5"] > ctx["ma10"] and ctx["volume_ratio"] >= 1.1) else "watch"
    return StrategyResult(
        strategy_id="ma_golden_cross",
        display_name=STRATEGY_DISPLAY_NAMES["ma_golden_cross"],
        signal_status=status,
        bias_score=_base_bias_score(ctx, status),
        buy_zone=f"{min(ctx['ma5'], ctx['ma10']):.2f}-{max(ctx['ma5'], ctx['ma10']):.2f}",
        stop_loss=f"{ctx['ma20'] * 0.99:.2f}",
        target_1=f"{ctx['recent_high'] * 1.02:.2f}",
        trigger_condition="关注 MA5 上穿 MA10，或 MA10 上穿 MA20，且量能同步放大。",
        risk_notes="金叉后若快速跌回均线下方，容易演变成假信号。",
        why_not_trade="" if status != "watch" else "当前没有看到足够清晰的近端金叉信号。",
    )


def _shrink_pullback(ctx: dict[str, Any]) -> StrategyResult:
    trend_up = ctx["ma5"] >= ctx["ma10"] >= ctx["ma20"]
    near_support = min(abs(ctx["latest_close"] - ctx["ma5"]) / ctx["ma5"], abs(ctx["latest_close"] - ctx["ma10"]) / ctx["ma10"]) <= 0.02
    shrink = ctx["volume_ratio"] <= 0.8
    status = "triggered" if trend_up and near_support and shrink else "near_trigger" if trend_up and near_support else "watch"
    return StrategyResult(
        strategy_id="shrink_pullback",
        display_name=STRATEGY_DISPLAY_NAMES["shrink_pullback"],
        signal_status=status,
        bias_score=_base_bias_score(ctx, status),
        buy_zone=f"{ctx['ma10']:.2f}-{ctx['ma5']:.2f}",
        stop_loss=f"{ctx['ma20'] * 0.99:.2f}",
        target_1=f"{ctx['recent_high']:.2f}",
        trigger_condition="多头结构里，缩量回踩 MA5/MA10 并守住支撑。",
        risk_notes="回踩若放量跌穿 MA10，延续假设明显转弱。",
        why_not_trade="" if status != "watch" else "还没等到像样的回踩企稳节奏。",
    )


def _volume_breakout(ctx: dict[str, Any]) -> StrategyResult:
    breakout = ctx["latest_close"] >= ctx["resistance_20"] * 0.995 and ctx["volume_ratio"] >= 1.8
    status = "triggered" if breakout and ctx["bias_pct"] <= ctx["bias_threshold"] else "near_trigger" if ctx["latest_close"] >= ctx["resistance_20"] * 0.99 else "watch"
    return StrategyResult(
        strategy_id="volume_breakout",
        display_name=STRATEGY_DISPLAY_NAMES["volume_breakout"],
        signal_status=status,
        bias_score=_base_bias_score(ctx, status),
        buy_zone=f"{ctx['resistance_20'] * 0.995:.2f}-{ctx['resistance_20'] * 1.01:.2f}",
        stop_loss=f"{ctx['resistance_20'] * 0.97:.2f}",
        target_1=f"{(ctx['resistance_20'] + (ctx['resistance_20'] - ctx['support_20'])):.2f}",
        trigger_condition="放量站上近 20 日阻力位，优先看有效突破而不是盘中假穿。",
        risk_notes="若突破后快速跌回箱体内，容易变成诱多。",
        why_not_trade="" if status != "watch" else "当前还没有看到足够明确的放量突破。",
    )


def _bottom_volume(ctx: dict[str, Any]) -> StrategyResult:
    oversold = ctx["drawdown_pct"] >= 12
    strong_reversal_bar = ctx["latest_close"] > ctx["latest_open"] and ctx["volume_ratio"] >= 2.5
    status = "triggered" if oversold and strong_reversal_bar else "near_trigger" if oversold else "watch"
    return StrategyResult(
        strategy_id="bottom_volume",
        display_name=STRATEGY_DISPLAY_NAMES["bottom_volume"],
        signal_status=status,
        bias_score=_base_bias_score(ctx, status),
        buy_zone=f"{ctx['latest_low']:.2f}-{ctx['latest_close']:.2f}",
        stop_loss=f"{ctx['recent_low'] * 0.99:.2f}",
        target_1=f"{ctx['ma20']:.2f}",
        trigger_condition="长时间回撤后放量阳线企稳，优先轻仓试错。",
        risk_notes="这是反转型策略，容错率低于顺势策略。",
        why_not_trade="" if status != "watch" else "还没有形成足够像样的底部放量企稳信号。",
    )


def _one_yang_three_yin(ctx: dict[str, Any]) -> StrategyResult:
    df = ctx["df"].tail(5).reset_index(drop=True)
    pattern = False
    if len(df) == 5:
        day1 = df.iloc[0]
        day2_4 = df.iloc[1:4]
        day5 = df.iloc[4]
        pattern = (
            float(day1["收盘"]) > float(day1["开盘"])
            and all(float(r["收盘"]) <= float(day1["收盘"]) for _, r in day2_4.iterrows())
            and all(float(r["最低"]) >= float(day1["开盘"]) for _, r in day2_4.iterrows())
            and float(day5["收盘"]) >= float(day1["收盘"])
        )
    status = "triggered" if pattern else "watch"
    first_open = float(df.iloc[0]["开盘"]) if len(df) == 5 else ctx["recent_low"]
    return StrategyResult(
        strategy_id="one_yang_three_yin",
        display_name=STRATEGY_DISPLAY_NAMES["one_yang_three_yin"],
        signal_status=status,
        bias_score=_base_bias_score(ctx, status),
        buy_zone=f"{ctx['latest_close'] * 0.995:.2f}-{ctx['latest_close'] * 1.005:.2f}",
        stop_loss=f"{first_open * 0.99:.2f}",
        target_1=f"{ctx['recent_high'] * 1.02:.2f}",
        trigger_condition="观察最近 5 根 K 线是否形成一阳夹三阴后的再度转强。",
        risk_notes="形态若跌破首根阳线开盘价，信号大概率失效。",
        why_not_trade="" if status != "watch" else "最近 5 个交易日没有形成完整的一阳夹三阴形态。",
    )


def _box_oscillation(ctx: dict[str, Any]) -> StrategyResult:
    box_top = ctx["resistance_20"]
    box_bottom = ctx["support_20"]
    box_width_pct = ((box_top - box_bottom) / box_bottom * 100) if box_bottom else 0.0
    distance_to_bottom = ((ctx["latest_close"] - box_bottom) / box_bottom * 100) if box_bottom else 0.0
    distance_to_top = ((box_top - ctx["latest_close"]) / box_top * 100) if box_top else 0.0
    if box_width_pct < 5:
        status = "watch"
        why_not_trade = "箱体空间太小，波段性价比不足。"
    elif distance_to_bottom <= 5:
        status = "triggered"
        why_not_trade = ""
    elif distance_to_top <= 5:
        status = "risk_off"
        why_not_trade = ""
    else:
        status = "watch"
        why_not_trade = "当前位于箱体中部，追价和抄底都不划算。"
    return StrategyResult(
        strategy_id="box_oscillation",
        display_name=STRATEGY_DISPLAY_NAMES["box_oscillation"],
        signal_status=status,
        bias_score=_base_bias_score(ctx, status),
        buy_zone=f"{box_bottom:.2f}-{box_bottom * 1.03:.2f}",
        stop_loss=f"{box_bottom * 0.97:.2f}",
        target_1=f"{box_top:.2f}",
        trigger_condition="箱底附近买、箱顶附近减，突破后再切换到趋势逻辑。",
        risk_notes="若连续收盘有效突破箱体，需要重新定义交易框架。",
        why_not_trade=why_not_trade,
    )
