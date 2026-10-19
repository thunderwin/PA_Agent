"""Unit tests for trade_metrics helpers."""
from __future__ import annotations

import pytest

from pa_agent.util.trade_metrics import (
    NARROW_STOP_ATR_MULTIPLE,
    adjust_decision_stop_for_tp1_rr_cap,
    atr_from_frame,
    compute_risk_reward,
    decision_stop_atr_multiple,
    format_estimated_win_rate,
    format_estimated_win_rate_reasoning,
    is_long_direction,
    max_risk_reward_ratio,
    min_risk_reward_ratio,
    stop_atr_multiple,
    widen_stop_for_tp1_rr_cap,
)


def test_is_long_direction():
    assert is_long_direction("做多") is True
    assert is_long_direction("做空") is False


def test_compute_risk_reward_short():
    rr = compute_risk_reward(4541, 4510, 4553, "做空")
    assert rr is not None
    assert rr["risk"] == 12
    assert rr["reward"] == 31


def test_rr_bounds_all_stances_share_one_floor() -> None:
    for stance in ("conservative", "balanced", "aggressive", "extreme_aggressive", None):
        assert min_risk_reward_ratio(stance) == 1.0
    # 2026-10-12：上限取消（None）——AI 定的止损不許被程序改写
    assert max_risk_reward_ratio() is None


def test_widen_stop_for_tp1_rr_cap_long():
    # entry=100, tp=110, stop=99 -> risk=1, reward=10, RR=10
    # 显式给 cap=1.0 才复现旧行为（程序默认不再撑止损）
    widened = widen_stop_for_tp1_rr_cap(100.0, 110.0, 99.0, "做多", tick=0.01, cap=1.0)
    assert widened is not None
    assert widened < 99.0
    rr = compute_risk_reward(100.0, 110.0, widened, "做多")
    assert rr is not None
    assert rr["ratio"] <= 1.0 + 1e-9


def test_widen_stop_for_tp1_rr_cap_short():
    widened = widen_stop_for_tp1_rr_cap(100.0, 90.0, 101.0, "做空", tick=0.01, cap=1.0)
    assert widened is not None
    assert widened > 101.0
    rr = compute_risk_reward(100.0, 90.0, widened, "做空")
    assert rr is not None
    assert rr["ratio"] <= 1.0 + 1e-9


def test_adjust_decision_stop_for_tp1_rr_cap_mutates_decision():
    decision = {
        "order_type": "限价单",
        "order_direction": "做多",
        "entry_price": 100.0,
        "take_profit_price": 110.0,
        "stop_loss_price": 99.0,
    }
    assert adjust_decision_stop_for_tp1_rr_cap(decision, tick=0.01, cap=1.0)
    rr = compute_risk_reward(
        decision["entry_price"],
        decision["take_profit_price"],
        decision["stop_loss_price"],
        decision["order_direction"],
    )
    assert rr is not None
    assert rr["ratio"] <= 1.0


def test_stop_is_never_widened_by_default() -> None:
    """2026-10-12 用户要求：AI 定的止损位置一个字都不能改。"""
    decision = {
        "order_type": "限价单",
        "order_direction": "做多",
        "entry_price": 0.00233,
        "take_profit_price": 0.002496,      # RR = 166/65 ≈ 2.55
        "stop_loss_price": 0.002265,
    }
    assert adjust_decision_stop_for_tp1_rr_cap(decision, tick=1e-6) is False
    assert decision["stop_loss_price"] == pytest.approx(0.002265)
    # 直接调底层也不会动：默认 cap=None
    assert widen_stop_for_tp1_rr_cap(
        0.00233, 0.002496, 0.002265, "做多", tick=1e-6
    ) == pytest.approx(0.002265)


def test_format_estimated_win_rate_from_model_field():
    decision = {
        "estimated_win_rate": 47,
        "estimated_win_rate_reasoning": "宽通道顺势，方程用 47%",
    }
    assert format_estimated_win_rate(decision) == "47%"
    assert "47" in format_estimated_win_rate_reasoning(decision)


# ── 止损宽度 = N×ATR（2026-10-12 加）─────────────────────────────────────────


def test_stop_atr_multiple_is_dimensionless():
    """同样 1.5×ATR：低价币和高价币应该算出同一个倍数。"""
    # RSR 实盘：入场 0.00233 / 止损 0.002164 / 30m ATR 0.000131 → 约 1.27×
    assert stop_atr_multiple(0.00233, 0.002164, 0.000131) == pytest.approx(1.267, rel=0.01)
    # BTC 假想：入场 85000 / 止损 84000 / ATR 1000 → 1.0×
    assert stop_atr_multiple(85000, 84000, 1000) == pytest.approx(1.0)


def test_stop_atr_multiple_handles_bad_input():
    assert stop_atr_multiple(None, 1, 1) is None
    assert stop_atr_multiple(100, None, 1) is None
    assert stop_atr_multiple(100, 90, 0) is None          # ATR 0 → 算不出
    assert stop_atr_multiple(100, 100, 5) is None         # 入场=止损 → 算不出


def test_atr_from_frame_reads_latest_atr():
    from types import SimpleNamespace

    frame = SimpleNamespace(indicators=SimpleNamespace(atr14=(12.0, 11.0, 10.0)))
    assert atr_from_frame(frame) == pytest.approx(12.0)
    assert atr_from_frame(SimpleNamespace(indicators=None)) is None
    assert atr_from_frame(SimpleNamespace(indicators=SimpleNamespace(atr14=()))) is None
    assert atr_from_frame(SimpleNamespace(indicators=SimpleNamespace(atr14=(0.0,)))) is None


def test_decision_stop_atr_multiple_only_for_executable_plans():
    from types import SimpleNamespace

    frame = SimpleNamespace(indicators=SimpleNamespace(atr14=(0.000131,)))
    order = {
        "order_type": "限价单",
        "entry_price": 0.00233,
        "stop_loss_price": 0.002164,
    }
    assert decision_stop_atr_multiple(order, frame) == pytest.approx(1.267, rel=0.01)
    assert decision_stop_atr_multiple({**order, "order_type": "不下单"}, frame) is None
    assert decision_stop_atr_multiple("not a dict", frame) is None


def test_narrow_stop_reference_line_is_documented_value():
    assert NARROW_STOP_ATR_MULTIPLE == pytest.approx(1.5)


# ── 市价「直接入场」合法性（2026-10-19 加）──────────────────────────────────


def test_market_entry_rejected_when_price_crossed_stop():
    from pa_agent.util.trade_metrics import market_entry_reject_reason

    # 做空：现价已经涨破止损 → 不能直接入场
    assert market_entry_reject_reason(4175.0, 4174.3, "做空") is not None
    # 做多：现价已经跌破止损 → 不能直接入场
    assert market_entry_reject_reason(78000.0, 78400.0, "做多") is not None


def test_market_entry_allowed_when_stop_still_valid():
    from pa_agent.util.trade_metrics import market_entry_reject_reason

    assert market_entry_reject_reason(4154.7, 4174.3, "做空") is None
    assert market_entry_reject_reason(80000.0, 78400.0, "做多") is None
    assert market_entry_reject_reason(0, 100, "做多") is not None
