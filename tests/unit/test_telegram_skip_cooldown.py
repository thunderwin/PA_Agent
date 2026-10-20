"""「跳过」后的同品种同方向提醒冷却（2026-10-20 加）。

背景：用户明确跳过黄金做空后，下一小时又收到几乎相同的提醒。
"""

from __future__ import annotations

from pa_agent.notify.telegram_approval import skip_cooldown_active


def test_same_direction_within_cooldown_is_suppressed():
    prev = {"direction": "做空", "entry": 4153.9, "ts": 1000.0}
    assert skip_cooldown_active(prev, "做空", now=1000 + 60 * 60, cooldown_min=120) is True


def test_cooldown_expires():
    prev = {"direction": "做空", "ts": 1000.0}
    assert skip_cooldown_active(prev, "做空", now=1000 + 121 * 60, cooldown_min=120) is False


def test_direction_flip_notifies_again():
    prev = {"direction": "做空", "ts": 1000.0}
    assert skip_cooldown_active(prev, "做多", now=1000 + 60, cooldown_min=120) is False


def test_no_history_or_disabled_cooldown():
    assert skip_cooldown_active(None, "做空", now=1000, cooldown_min=120) is False
    assert skip_cooldown_active({"direction": "做空", "ts": 1000}, "做空", now=1001, cooldown_min=0) is False
