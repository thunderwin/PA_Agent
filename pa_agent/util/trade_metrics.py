"""Risk/reward and estimated win-rate helpers for trading decisions."""
from __future__ import annotations

from typing import Any

#: "偏窄止损"的参考线（只需 ATR 倍数）：低于它的止损大概率落在该币的正常噪音里。
#: 2026-10-12 起只用于日志提示，**尚未**用来拦单 —— 当天 7 笔成交里两笔赢家
#: （GRASS 0.36×、RVN 0.79×）反而比输家（1.05~1.5×）更窄，样本不足以定门槛。
NARROW_STOP_ATR_MULTIPLE: float = 1.5


def is_long_direction(direction: object) -> bool | None:
    """Return True for long, False for short, None if unknown."""
    text = str(direction or "").strip().lower()
    if not text:
        return None
    if "多" in text or text in ("long", "buy", "bull"):
        return True
    if "空" in text or text in ("short", "sell", "bear"):
        return False
    return None


def compute_risk_reward(
    entry: object,
    take_profit: object,
    stop_loss: object,
    direction: object,
) -> dict[str, float | str] | None:
    """Compute risk/reward distances and reward:risk ratio (盈亏比).

    Returns None when prices are invalid or risk is zero.
    """
    try:
        e = float(entry)
        tp = float(take_profit)
        sl = float(stop_loss)
    except (TypeError, ValueError):
        return None

    long = is_long_direction(direction)
    if long is True:
        risk = e - sl
        reward = tp - e
    elif long is False:
        risk = sl - e
        reward = e - tp
    else:
        if tp > e and sl < e:
            risk = e - sl
            reward = tp - e
        elif tp < e and sl > e:
            risk = sl - e
            reward = e - tp
        else:
            return None

    if risk <= 0 or reward <= 0:
        return None

    ratio = reward / risk
    return {
        "risk": risk,
        "reward": reward,
        "ratio": ratio,
        "ratio_text": f"{ratio:.2f} : 1",
    }


def format_estimated_win_rate(decision: dict[str, Any]) -> str | None:
    """Format model-provided estimated_win_rate (0–100) for display."""
    value = decision.get("estimated_win_rate")
    if value is None or value == "":
        return None
    try:
        pct = max(0, min(100, int(float(str(value).strip()))))
    except (ValueError, TypeError):
        return None
    return f"{pct}%"


def format_estimated_win_rate_reasoning(decision: dict[str, Any]) -> str:
    return str(decision.get("estimated_win_rate_reasoning", "") or "").strip()


# Lower cap: reward must be at least equal to risk (1:1) for any stance.
MIN_RISK_REWARD_RATIO = 1.0
# Upper cap on TP1 reward:risk. ``None`` = 不设上限。
#
# 2026-10-12 改成 None（用户要求）：**AI 决定的止损位置绝不能被程序改写**。
# 以前这里是 1.0，效果是"盈亏比高于 1:1 就把止损往外撑到 1:1"——RSR 那笔的
# 结构止损 0.002265（2.79%）就是这样被撑成 0.002164（7.12%）的，而且静默发生
# （只写 debug 日志）。止盈由 trading.risk_reward_ratio 统一覆盖（当前 1:3），
# 与 AI 自己写的 TP1 无关，所以这个上限既没必要也有害。
MAX_TP1_RISK_REWARD_RATIO: float | None = None


def min_risk_reward_ratio(decision_stance: str | None = None) -> float:
    """Minimum reward:risk ratio required to place an order (same for all stances)."""
    _ = decision_stance  # kept for call-site compatibility
    return MIN_RISK_REWARD_RATIO


def max_risk_reward_ratio() -> float | None:
    """Maximum TP1 reward:risk after program stop adjustment."""
    return MAX_TP1_RISK_REWARD_RATIO


def widen_stop_for_tp1_rr_cap(
    entry: float,
    take_profit: float,
    stop_loss: float,
    direction: object,
    *,
    tick: float | None = None,
    cap: float | None = None,
) -> float | None:
    """Move stop outward so TP1 RR <= cap (entry/TP unchanged).

    Long: lower stop; short: raise stop. Returns None if geometry cannot be fixed.

    ``cap`` 缺省取 :data:`MAX_TP1_RISK_REWARD_RATIO`；**当前它是 None，
    也就是"不撑止损"，函数直接把原止损原样返回**（2026-10-12 用户要求：
    AI 定的止损不可被程序改写）。传 ``cap=1.0`` 之类可以复现旧行为。
    """
    import math

    limit = MAX_TP1_RISK_REWARD_RATIO if cap is None else float(cap)
    if limit is None or limit <= 0:
        return stop_loss                      # 不设上限 → 止损原样不动

    rr = compute_risk_reward(entry, take_profit, stop_loss, direction)
    if rr is None:
        return None
    ratio = float(rr["ratio"])
    if ratio <= limit + 1e-9:
        return stop_loss

    reward = float(rr["reward"])
    min_risk = reward / limit
    long = is_long_direction(direction)
    t = tick if tick and tick > 0 else 0.0
    min_rr = MIN_RISK_REWARD_RATIO

    def _ratio_for_stop(candidate: float) -> float | None:
        probe = compute_risk_reward(entry, take_profit, candidate, direction)
        if probe is None:
            return None
        return float(probe["ratio"])

    def _pick_tick_aligned_stop(ideal_stop: float, *, round_down: bool) -> float | None:
        if not t:
            return ideal_stop
        scaled = ideal_stop / t
        aligned = math.floor(scaled + 1e-12) * t if round_down else math.ceil(scaled - 1e-12) * t
        return aligned

    if long is True:
        ideal_stop = entry - min_risk
        if ideal_stop >= entry:
            return None
        candidates: list[float] = [ideal_stop]
        if t:
            candidates.extend(
                s
                for s in (
                    _pick_tick_aligned_stop(ideal_stop, round_down=True),
                    _pick_tick_aligned_stop(ideal_stop, round_down=False),
                )
                if s is not None
            )
        best: float | None = None
        best_ratio: float | None = None
        for candidate in candidates:
            if candidate >= entry:
                continue
            cand_ratio = _ratio_for_stop(candidate)
            if cand_ratio is None:
                continue
            if cand_ratio < min_rr - 1e-9 or cand_ratio > limit + 1e-9:
                continue
            if best_ratio is None or cand_ratio > best_ratio:
                best = candidate
                best_ratio = cand_ratio
        return best
    if long is False:
        ideal_stop = entry + min_risk
        if ideal_stop <= entry:
            return None
        candidates = [ideal_stop]
        if t:
            candidates.extend(
                s
                for s in (
                    _pick_tick_aligned_stop(ideal_stop, round_down=False),
                    _pick_tick_aligned_stop(ideal_stop, round_down=True),
                )
                if s is not None
            )
        best = None
        best_ratio = None
        for candidate in candidates:
            if candidate <= entry:
                continue
            cand_ratio = _ratio_for_stop(candidate)
            if cand_ratio is None:
                continue
            if cand_ratio < min_rr - 1e-9 or cand_ratio > limit + 1e-9:
                continue
            if best_ratio is None or cand_ratio > best_ratio:
                best = candidate
                best_ratio = cand_ratio
        return best
    return None


def adjust_decision_stop_for_tp1_rr_cap(
    decision: dict[str, Any],
    *,
    kline_frame: Any = None,
    tick: float | None = None,
    cap: float | None = None,
) -> bool:
    """Widen stop_loss_price in-place when TP1 RR exceeds the program cap.

    **程序已不再调用它**（2026-10-12 用户要求：AI 定的止损不可被改写；
    :data:`MAX_TP1_RISK_REWARD_RATIO` 现在是 None，所以即使调用也不会改动）。
    保留函数是为了让旧测试/外部调用显式传 ``cap=`` 时仍能复现旧行为。
    """
    if decision.get("order_type") not in ("限价单", "突破单", "市价单"):
        return False
    try:
        entry = float(decision["entry_price"])
        tp = float(decision["take_profit_price"])
        sl = float(decision["stop_loss_price"])
    except (TypeError, ValueError, KeyError):
        return False

    if tick is None and kline_frame is not None:
        from pa_agent.util.price_tick import infer_price_tick_from_frame

        tick = infer_price_tick_from_frame(kline_frame)

    new_sl = widen_stop_for_tp1_rr_cap(
        entry, tp, sl, decision.get("order_direction"), tick=tick, cap=cap
    )
    if new_sl is None or abs(new_sl - sl) < 1e-12:
        return False
    decision["stop_loss_price"] = new_sl
    return True


def passes_trader_equation(
    win_rate_pct: float,
    risk: float,
    reward: float,
) -> bool:
    """Brooks equation: win_rate × reward > (1 - win_rate) × risk."""
    if risk <= 0 or reward <= 0:
        return False
    p = max(0.0, min(100.0, float(win_rate_pct))) / 100.0
    return p * reward > (1.0 - p) * risk


def _parse_win_rate(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return max(0.0, min(100.0, float(str(value).strip())))
    except (TypeError, ValueError):
        return None


def _latest_closed_bar(kline_frame: Any) -> Any | None:
    """Return K1 (newest closed bar) from a snapshot frame."""
    bars = getattr(kline_frame, "bars", None) if kline_frame is not None else None
    if not bars:
        return None
    for bar in bars:
        if int(getattr(bar, "seq", 0) or 0) == 1 and bool(getattr(bar, "closed", True)):
            return bar
    for bar in bars:
        if bool(getattr(bar, "closed", True)):
            return bar
    return None


def validate_limit_order_k1_freshness(
    decision: dict[str, Any],
    kline_frame: Any,
    *,
    bar_analysis: dict[str, Any] | None = None,
) -> list[str]:
    """Reject stale limit orders that K1 has already traded through."""
    if decision.get("order_type") != "限价单":
        return []

    try:
        entry = float(decision.get("entry_price"))
        sl = float(decision.get("stop_loss_price"))
    except (TypeError, ValueError):
        return []

    bar = _latest_closed_bar(kline_frame)
    if bar is None:
        return []

    from pa_agent.util.price_tick import infer_price_tick_from_frame

    tick = infer_price_tick_from_frame(kline_frame) or 0.0
    k_high = float(bar.high)
    k_low = float(bar.low)
    k_close = float(bar.close)
    long = is_long_direction(decision.get("order_direction"))

    pending_planned = False
    if isinstance(bar_analysis, dict):
        entry_bar = bar_analysis.get("entry_bar")
        if isinstance(entry_bar, dict):
            freshness = str(entry_bar.get("freshness", "") or "").strip().lower()
            strength = str(entry_bar.get("strength", "") or "").strip().lower()
            pending_planned = (
                freshness == "pending"
                or strength == "not_triggered"
                or entry_bar.get("bar") is None
            )

    errors: list[str] = []
    if long is True:
        if pending_planned:
            # Planned buy limit: entry must stay below market close (waiting for dip).
            if k_close < entry - tick:
                errors.append(
                    f"limit long (planned): K1 close {k_close:.6g} is below entry {entry:.6g}; "
                    "reprice entry or 不下单"
                )
        else:
            if k_low <= entry + tick:
                errors.append(
                    f"limit long: K1 low {k_low:.6g} already touched/below entry {entry:.6g}; "
                    "pending buy limit is stale — use 市价单, reprice, or 不下单"
                )
            if k_close < entry - tick:
                errors.append(
                    f"limit long: K1 close {k_close:.6g} is below entry {entry:.6g}; "
                    "do not keep a buy limit above market without repricing"
                )
        if k_low <= sl + tick:
            errors.append(
                f"limit long: K1 low {k_low:.6g} already at/below stop {sl:.6g}; "
                "plan invalid — order_type=不下单"
            )
    elif long is False:
        if pending_planned:
            if k_close > entry + tick:
                errors.append(
                    f"limit short (planned): K1 close {k_close:.6g} is above entry {entry:.6g}; "
                    "reprice entry or 不下单"
                )
        else:
            if k_high >= entry - tick:
                errors.append(
                    f"limit short: K1 high {k_high:.6g} already reached/exceeded entry {entry:.6g}; "
                    "pending sell limit is stale — use 市价单, reprice, or 不下单"
                )
            if k_close > entry + tick:
                errors.append(
                    f"limit short: K1 close {k_close:.6g} is above entry {entry:.6g}; "
                    "do not keep a sell limit below market without repricing"
                )
        if k_high >= sl - tick:
            errors.append(
                f"limit short: K1 high {k_high:.6g} already at/above stop {sl:.6g}; "
                "plan invalid — order_type=不下单"
            )

    return errors


def validate_take_profit_2_geometry(
    decision: dict[str, Any],
) -> list[str]:
    """Ensure TP2 is beyond TP1 in the profit direction (no RR cap on TP2)."""
    entry = decision.get("entry_price")
    tp1 = decision.get("take_profit_price")
    tp2 = decision.get("take_profit_price_2")
    sl = decision.get("stop_loss_price")
    direction = decision.get("order_direction")

    try:
        e = float(entry)
        t1 = float(tp1)
        t2 = float(tp2)
        s = float(sl)
    except (TypeError, ValueError):
        return ["decision.take_profit_price_2: required finite number when placing an order"]

    long = is_long_direction(direction)
    if long is True:
        if not (s < e < t1 < t2):
            return [
                "decision.take_profit_price_2: long plan requires "
                "stop < entry < take_profit_price < take_profit_price_2"
            ]
    elif long is False:
        if not (t2 < t1 < e < s):
            return [
                "decision.take_profit_price_2: short plan requires "
                "take_profit_price_2 < take_profit_price < entry < stop"
            ]
    else:
        if t1 > e and t2 <= t1:
            return [
                "decision.take_profit_price_2: must be above take_profit_price for long geometry"
            ]
        if t1 < e and t2 >= t1:
            return [
                "decision.take_profit_price_2: must be below take_profit_price for short geometry"
            ]

    return []


def validate_order_trade_metrics(
    decision: dict[str, Any],
    *,
    decision_stance: str | None = None,
    kline_frame: Any = None,
    bar_analysis: dict[str, Any] | None = None,
    apply_rr_cap_adjustment: bool = True,
) -> list[str]:
    """Validate entry/TP/SL geometry, RR floor, and trader equation for live orders."""
    order_type = decision.get("order_type")
    if order_type not in ("限价单", "突破单", "市价单"):
        return []

    if apply_rr_cap_adjustment:
        adjust_decision_stop_for_tp1_rr_cap(decision, kline_frame=kline_frame)

    entry = decision.get("entry_price")
    tp = decision.get("take_profit_price")
    sl = decision.get("stop_loss_price")
    direction = decision.get("order_direction")
    rr = compute_risk_reward(entry, tp, sl, direction)
    if rr is None:
        return [
            "decision prices: entry/stop/target must form a valid long (sl<entry<tp) "
            "or short (tp<entry<sl) trade with positive risk and reward"
        ]

    errors: list[str] = []
    ratio = float(rr["ratio"])
    risk = float(rr["risk"])
    reward = float(rr["reward"])
    min_rr = min_risk_reward_ratio(decision_stance)

    if ratio < min_rr:
        errors.append(
            f"decision prices: risk_reward {rr['ratio_text']} is below minimum "
            f"{min_rr:.2f}:1 for this stance; adjust take_profit/stop_loss or set "
            "order_type=不下单 with 10.3=否"
        )

    max_rr = max_risk_reward_ratio()
    if max_rr is not None and ratio > max_rr + 1e-9:
        errors.append(
            f"decision prices: risk_reward {rr['ratio_text']} exceeds maximum "
            f"{max_rr:.2f}:1 for TP1 after stop adjustment; set order_type=不下单 "
            "with 10.3=否"
        )

    win_rate = _parse_win_rate(decision.get("estimated_win_rate"))
    if win_rate is None:
        errors.append(
            "decision.estimated_win_rate: required integer 0–100 when placing an order"
        )
    elif not passes_trader_equation(win_rate, risk, reward):
        ev = win_rate / 100.0 * reward - (1.0 - win_rate / 100.0) * risk
        errors.append(
            f"decision prices: trader equation fails at {win_rate:.0f}% win rate "
            f"(risk={risk:.4g}, reward={reward:.4g}, expectancy≈{ev:.4g}); "
            "10.3 must be 否 and order_type=不下单 unless prices are fixed"
        )

    if kline_frame is not None:
        errors.extend(
            validate_limit_order_k1_freshness(
                decision, kline_frame, bar_analysis=bar_analysis
            )
        )

    errors.extend(validate_take_profit_2_geometry(decision))

    return errors


def atr_from_frame(kline_frame: Any) -> float | None:
    """取 K 线帧里最新的 ATR14；取不到（没算指标 / 值为空）时返回 None。"""
    indicators = getattr(kline_frame, "indicators", None)
    atr = getattr(indicators, "atr14", None)
    if not atr:
        return None
    try:
        value = float(atr[0])
    except (TypeError, ValueError, IndexError):
        return None
    return value if value > 0 else None


def stop_atr_multiple(entry: object, stop_loss: object, atr: object) -> float | None:
    """止损距离 ÷ ATR —— 用**这个币自己的波动**衡量止损够不够宽。

    为什么不用"止损距离 ÷ 价格"（固定百分比）：每种币的正常波动本来就不一样，
    BTC 的 2% 和 RSR 的 2% 完全不是一回事（2026-10-12 用户指出）。
    ATR 倍数是无量纲的：1.5×ATR 对任何币都表示"离正常噪音 1.5 倍远"。

    返回 None 表示算不出（缺值 / ATR ≤ 0 / 止损与入场相同）。
    """
    try:
        e = float(entry)
        sl = float(stop_loss)
        a = float(atr)
    except (TypeError, ValueError):
        return None
    if a <= 0 or e <= 0:
        return None
    distance = abs(e - sl)
    if distance <= 0:
        return None
    return distance / a


def decision_stop_atr_multiple(
    decision: Any, kline_frame: Any
) -> float | None:
    """从决策 + K 线帧算"止损距离 = N×ATR"；算不出返回 None。"""
    if not isinstance(decision, dict):
        return None
    if decision.get("order_type") not in ("限价单", "突破单", "市价单"):
        return None
    atr = atr_from_frame(kline_frame)
    if atr is None:
        return None
    return stop_atr_multiple(
        decision.get("entry_price"), decision.get("stop_loss_price"), atr
    )
