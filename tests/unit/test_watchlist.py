"""多品种监控：核心逻辑 + 面板 + 设置对话框（无网络）。"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication

from pa_agent.config.settings import Settings
from pa_agent.data.base import KlineBar
from pa_agent.gui import watchlist_dialog as dialog_mod
from pa_agent.gui.watchlist_dialog import WatchlistDialog
from pa_agent.gui.watchlist_panel import WatchlistPanel
from pa_agent.orchestrator.watchlist import (
    WatchResult,
    WatchTarget,
    analyze_target,
    newest_closed_ts,
    run_watchlist,
)

_TF_S = 15 * 60
_T0 = 1_700_000_000_000  # 一个整点附近的毫秒时间戳


def _bars(count: int = 130, *, with_forming: bool = True) -> list[KlineBar]:
    bars: list[KlineBar] = []
    step_ms = _TF_S * 1000
    start = _T0 + (count + 1) * step_ms  # 最旧的一根
    for i in range(count):
        ts = start - i * step_ms
        bars.append(
            KlineBar(
                seq=i + 1,
                ts_open=ts,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=10.0,
                closed=True,
            )
        )
    if with_forming:
        bars.insert(
            0,
            KlineBar(
                seq=0,
                ts_open=start + step_ms,
                open=100.5,
                high=100.8,
                low=100.2,
                close=100.6,
                volume=3.0,
                closed=False,
            ),
        )
    return bars


class _FakeSource:
    def __init__(self, bars: list[KlineBar] | Exception) -> None:
        self._bars = bars
        self.subscribed: list[tuple[str, str]] = []

    def subscribe(self, symbol: str, timeframe: str) -> None:
        self.subscribed.append((symbol, timeframe))

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        if isinstance(self._bars, Exception):
            raise self._bars
        return list(self._bars[:n])


class _FakeOrchestrator:
    def __init__(self, *, stage2: dict | None = None, exception: dict | None = None) -> None:
        self._stage2 = (
            stage2
            if stage2 is not None
            else {
                "decision": {
                    "order_type": "限价单",
                    "order_direction": "做多",
                    "entry_price": 100.5,
                    "stop_loss_price": 99.0,
                    "take_profit_price": 102.0,
                    "trade_confidence": 72,
                },
                "diagnosis_summary": {"cycle_position": "tight_channel"},
            }
        )
        self._exception = exception
        self.frames: list = []

    def submit(self, frame, cancel_token, on_event) -> SimpleNamespace:
        self.frames.append(frame)
        return SimpleNamespace(
            stage2_decision=self._stage2,
            stage1_diagnosis={"cycle_position": "tight_channel"},
            exception=self._exception,
        )


def _target(symbol: str = "BTC-USDT-SWAP") -> WatchTarget:
    return WatchTarget(symbol, "15m")


def _now_ms(bars: list[KlineBar]) -> int:
    """把「现在」设在未收盘 K 线刚开盘 1 分钟后，让它被判定为仍在形成。"""
    return int(bars[0].ts_open) + 60_000


# ── 核心逻辑 ──────────────────────────────────────────────────────────────────


def test_newest_closed_ts_skips_forming_bar():
    bars = _bars(with_forming=True)
    assert newest_closed_ts(
        bars, timeframe="15m", symbol="BTC-USDT-SWAP", now_ms=_now_ms(bars)
    ) == int(bars[1].ts_open)
    # 没有未收盘 K 线时，最新一根就是它自己
    closed_only = _bars(with_forming=False)
    assert newest_closed_ts(closed_only, timeframe="15m") == int(closed_only[0].ts_open)
    assert newest_closed_ts([]) is None


def test_analyze_target_parses_decision():
    bars = _bars()
    source = _FakeSource(bars)
    result = analyze_target(
        _target(),
        source=source,
        make_orchestrator=_FakeOrchestrator,
        now_ms=_now_ms(bars),
    )
    assert result.ok is True
    assert result.order_type == "限价单" and result.direction == "做多"
    assert result.confidence == 72
    assert result.entry == 100.5 and result.stop == 99.0
    assert result.has_order is True
    assert result.diagnosis == "tight_channel"
    assert result.price == 100.6  # 最新一根（含未收盘）的收盘价
    assert result.closed_ts == int(bars[1].ts_open)
    assert source.subscribed == [("BTC-USDT-SWAP", "15m")]


def test_analyze_target_skips_without_new_closed_bar():
    bars = _bars()
    source = _FakeSource(bars)
    orchestrator = _FakeOrchestrator()
    closed_ts = newest_closed_ts(
        bars, timeframe="15m", symbol="BTC-USDT-SWAP", now_ms=_now_ms(bars)
    )
    result = analyze_target(
        _target(),
        source=source,
        make_orchestrator=lambda: orchestrator,
        previous_closed_ts=closed_ts,
        now_ms=_now_ms(bars),
    )
    assert result.skipped is True and result.ok is False
    assert orchestrator.frames == []  # 没有调用大模型


def test_analyze_target_reports_fetch_failure():
    source = _FakeSource(RuntimeError("boom"))
    result = analyze_target(_target(), source=source, make_orchestrator=_FakeOrchestrator)
    assert result.ok is False and "取数失败" in result.error


def test_analyze_target_reports_orchestrator_exception_field():
    exc = {"type": "validation_error", "message": "stage2 校验失败"}
    result = analyze_target(
        _target(),
        source=_FakeSource(_bars()),
        make_orchestrator=lambda: _FakeOrchestrator(exception=exc),
    )
    assert result.ok is False and "校验失败" in result.error


def test_analyze_target_requires_decision():
    orch = _FakeOrchestrator(stage2={})
    result = analyze_target(_target(), source=_FakeSource(_bars()), make_orchestrator=lambda: orch)
    assert result.ok is False and "决策" in result.error


def test_analyze_target_no_order_decision():
    orch = _FakeOrchestrator(stage2={"decision": {"order_type": "不下单"}})
    result = analyze_target(_target(), source=_FakeSource(_bars()), make_orchestrator=lambda: orch)
    assert result.ok is True and result.has_order is False and result.order_type == "不下单"


def test_run_watchlist_tracks_state_and_reports_each_target():
    targets = [WatchTarget("BTC-USDT-SWAP", "15m"), WatchTarget("XAU-USDT-SWAP", "15m")]
    source = _FakeSource(_bars())
    results: list[WatchResult] = []
    state = run_watchlist(
        targets,
        source=source,
        make_orchestrator=_FakeOrchestrator,
        on_result=results.append,
    )
    assert [r.symbol for r in results] == ["BTC-USDT-SWAP", "XAU-USDT-SWAP"]
    assert set(state) == {("BTC-USDT-SWAP", "15m"), ("XAU-USDT-SWAP", "15m")}

    # 第二轮：K 线没变 → 全部跳过，不再调用模型
    orch2 = _FakeOrchestrator()
    results2: list[WatchResult] = []
    run_watchlist(
        targets,
        source=source,
        make_orchestrator=lambda: orch2,
        on_result=results2.append,
        previous_closed=state,
    )
    assert all(r.skipped for r in results2)
    assert orch2.frames == []


def test_run_watchlist_respects_cancel_token():
    from pa_agent.util.threading import CancelToken

    token = CancelToken()
    token.set()
    results: list[WatchResult] = []
    run_watchlist(
        [_target()],
        source=_FakeSource(_bars()),
        make_orchestrator=_FakeOrchestrator,
        cancel_token=token,
        on_result=results.append,
    )
    assert results == []


# ── 面板 / 对话框 ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def test_panel_renders_results(qapp):
    panel = WatchlistPanel()
    panel.set_targets([_target(), WatchTarget("XAU-USDT-SWAP", "15m")])
    assert panel.row_count() == 2
    assert panel.cell_text("BTC-USDT-SWAP", 0) == "BTC-USDT-SWAP"
    assert panel.cell_text("BTC-USDT-SWAP", 2) == "—"

    panel.update_result(
        WatchResult(
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            ts_ms=_T0,
            ok=True,
            price=86500.0,
            order_type="限价单",
            direction="做空",
            confidence=66,
        )
    )
    assert panel.cell_text("BTC-USDT-SWAP", 2) == "86,500.0"
    assert panel.cell_text("BTC-USDT-SWAP", 3) == "限价单"
    assert panel.cell_text("BTC-USDT-SWAP", 4) == "做空"
    assert panel.cell_text("BTC-USDT-SWAP", 5) == "66"

    panel.update_result(
        WatchResult(
            symbol="XAU-USDT-SWAP",
            timeframe="15m",
            ts_ms=_T0,
            error="取数失败：timeout",
        )
    )
    assert "取数失败" in panel.cell_text("XAU-USDT-SWAP", 7)


def test_panel_emits_symbol_on_double_click(qapp):
    panel = WatchlistPanel()
    panel.set_targets([_target()])
    seen: list[tuple[str, str]] = []
    panel.symbol_activated.connect(lambda s, t: seen.append((s, t)))
    index = panel._table.model().index(0, 0)
    panel._on_double_clicked(index)
    assert seen == [("BTC-USDT-SWAP", "15m")]


def test_watchlist_dialog_saves_settings(qapp, monkeypatch, tmp_path):
    settings = Settings()
    saved: list[Settings] = []
    monkeypatch.setattr(dialog_mod, "save_settings", lambda s, p=None: saved.append(s))

    dlg = WatchlistDialog(settings)
    dlg._enabled_check.setChecked(True)
    dlg._symbols_edit.setPlainText("btc-usdt-swap\nXAU-USDT-SWAP\n\nxag-usdt-swap\nBTC-USDT-SWAP")
    dlg._tf_combo.setCurrentIndex(dlg._tf_combo.findData("15m"))
    dlg._interval_spin.setValue(120)
    dlg._alert_check.setChecked(False)
    dlg._on_save()

    g = settings.general
    assert g.watch_enabled is True
    assert g.watch_symbols == ["BTC-USDT-SWAP", "XAU-USDT-SWAP", "XAG-USDT-SWAP"]
    assert g.watch_timeframe == "15m"
    assert g.watch_interval_s == 120
    assert g.watch_alert_on_signal is False
    assert saved
