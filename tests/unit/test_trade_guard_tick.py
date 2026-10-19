"""交易看守定时器：补挂止损体检 + 过期挂单清理，必须扫全账户。

背景（2026-10-12）：IO 的限价单 13:44 挂出，按配置 4 根 30m（2 小时）该作废，
但 IO 在 14:13 被动态换榜轮出监控列表，之后没有任何分析结果回来，
``_expire_stale_entries()`` 就再也没被触发 —— 那张单一直挂到 16:16 才成交，
白亏 2.07 USDT。修法：每 2 分钟定时全量扫交易所侧的 PAAGENT 挂单，
不依赖监控列表（和 ``ensure_stops`` 同一套路）。
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PyQt6")


@pytest.fixture
def window(qtbot):
    from pa_agent.app_context import AppContext
    from pa_agent.config.settings import Settings
    from pa_agent.gui.main_window import MainWindow

    ctx = AppContext()
    ctx.settings = Settings()          # 用默认值，不读本机 config/settings.json
    ctx.client = MagicMock()
    ctx.assembler = MagicMock()
    ctx.pending_writer = MagicMock()
    ctx.exp_reader = MagicMock()
    ctx.exp_reader.read_top5.return_value = []

    win = MainWindow(ctx)
    qtbot.addWidget(win)
    return win


class _FakeTrader:
    def __init__(self) -> None:
        self.sweeps: list[list] = []
        self.stop_checks = 0

    def cancel_stale_entries(self, inst_id, timeframe, *, max_bars, now_ms=None):
        self.sweeps.append([inst_id, timeframe, max_bars])
        return []

    def ensure_stops(self, inst_id=None):
        self.stop_checks += 1
        return []


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_guard_timer_is_running(window):
    assert window._trade_guard_timer.interval() == 120_000
    assert window._trade_guard_timer.isActive() is True


def test_guard_tick_sweeps_whole_account(window, monkeypatch):
    """inst_id 必须是 None（全账户），否则轮出监控列表的挂单又没人清了。"""
    fake = _FakeTrader()
    monkeypatch.setattr("pa_agent.trading.gateway.create_trader", lambda settings: fake)

    window._ctx.settings.trading.enabled = True
    window._last_entry_expire_check = 0.0
    window._last_stop_check = 0.0

    window._on_trade_guard_tick()

    assert _wait_for(lambda: bool(fake.sweeps)), "过期挂单清理没有被触发"
    assert _wait_for(lambda: fake.stop_checks > 0), "止损体检没有被触发"
    assert fake.sweeps[0][0] is None
    assert int(fake.sweeps[0][2]) == int(window._ctx.settings.trading.pending_entry_expiry_bars)


def test_expire_is_throttled(window, monkeypatch):
    fake = _FakeTrader()
    monkeypatch.setattr("pa_agent.trading.gateway.create_trader", lambda settings: fake)

    window._ctx.settings.trading.enabled = True
    window._last_entry_expire_check = 0.0
    window._expire_stale_entries()
    assert _wait_for(lambda: len(fake.sweeps) == 1)

    window._expire_stale_entries()          # 2 分钟内第二次 → 直接返回
    time.sleep(0.2)
    assert len(fake.sweeps) == 1


def test_watchlist_watchdog_restarts_dead_worker(window, monkeypatch):
    """2026-10-18 事故：监控线程启动时探活失败就永久退出，空了 20 小时。
    看门狗必须把它拉起来。"""
    calls: list[int] = []
    monkeypatch.setattr(window, "_restart_watchlist_worker", lambda: calls.append(1))
    window._ctx.settings.general.watch_enabled = True
    window._watchlist_worker = None

    window._ensure_watchlist_worker_running()
    assert calls == [1]


def test_watchlist_watchdog_leaves_running_worker_alone(window, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(window, "_restart_watchlist_worker", lambda: calls.append(1))
    window._ctx.settings.general.watch_enabled = True

    class _Running:
        def isRunning(self):
            return True

    window._watchlist_worker = _Running()
    window._ensure_watchlist_worker_running()
    assert calls == []


def test_watchlist_watchdog_noop_when_disabled(window, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(window, "_restart_watchlist_worker", lambda: calls.append(1))
    window._ctx.settings.general.watch_enabled = False
    window._watchlist_worker = None

    window._ensure_watchlist_worker_running()
    assert calls == []
