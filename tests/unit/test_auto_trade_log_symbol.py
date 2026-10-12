"""自动下单日志必须带品种名。

2026-10-12 复盘时发现：日志里只有「自动下单未执行：置信度 45 低于门槛 50」，
认不出是哪个品种，只能靠翻 ``records/pending/`` 目录反推。

修法：``trade_finished`` 信号从 ``(结果)`` 改成 ``(结果, 品种)``，
``_on_trade_finished`` 把品种写进日志。本测试锁住这个行为。
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PyQt6")


@pytest.fixture
def window(qtbot):
    """最小可用的主窗口（AI / 记录 / 经验都换成 mock）。"""
    from pa_agent.app_context import AppContext
    from pa_agent.gui.main_window import MainWindow

    ctx = AppContext()
    ctx.client = MagicMock()
    ctx.assembler = MagicMock()
    ctx.pending_writer = MagicMock()
    ctx.exp_reader = MagicMock()
    ctx.exp_reader.read_top5.return_value = []

    win = MainWindow(ctx)
    qtbot.addWidget(win)
    return win


def test_trade_finished_signal_carries_symbol(window):
    """信号必须是两个参数，否则后台线程 emit 时会直接报错。"""
    raw = window.trade_finished.signal
    signature = raw.decode() if isinstance(raw, bytes) else str(raw)
    assert signature.count(",") == 1, signature


def test_auto_trade_skip_log_includes_symbol(window, caplog):
    from pa_agent.trading.okx_trader import ExecutionResult

    window._last_trade_interactive = False
    result = ExecutionResult(sent=False, message="置信度 45 低于门槛 50")

    with caplog.at_level(logging.WARNING, logger="pa_agent.gui.main_window"):
        window.trade_finished.emit(result, "PLUME-USDT-SWAP")

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "自动下单未执行" in logged
    assert "PLUME-USDT-SWAP" in logged, logged
    assert "置信度 45 低于门槛 50" in logged, logged


def test_auto_trade_success_log_includes_symbol(window, caplog):
    from pa_agent.trading.okx_trader import ExecutionResult

    window._last_trade_interactive = False
    result = ExecutionResult(sent=True, ord_id="123", message="buy limit sz=1")

    with caplog.at_level(logging.WARNING, logger="pa_agent.gui.main_window"):
        window.trade_finished.emit(result, "IO-USDT-SWAP")

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "IO-USDT-SWAP" in logged, logged
    assert "123" in logged, logged
