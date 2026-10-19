"""Telegram 交易提醒用的 K 线图（2026-10-19 加）。"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")

from pa_agent.notify.chart_image import ema, render_kline_png


def test_ema_warms_up_with_nan_then_converges():
    closes = [float(i) for i in range(1, 31)]
    values = ema(closes, 20)
    assert len(values) == len(closes)
    assert all(v != v for v in values[:19])            # 前 19 根 NaN
    assert values[19] == pytest.approx(sum(closes[:20]) / 20)
    assert values[-1] > values[19]                     # 上涨序列 EMA 上倾


def test_render_kline_png_writes_file(qtbot, tmp_path):
    bars = [
        SimpleNamespace(
            ts_open=1_700_000_000_000 + i * 1_800_000,
            open=100 + i, high=103 + i, low=98 + i, close=101 + i,
            volume=1.0, closed=True,
        )
        for i in range(100)
    ]
    path = str(tmp_path / "chart.png")
    ok = render_kline_png(
        bars, path, title="BTC-USDT-SWAP 30m",
        levels=[(110, "入场", "#f5c542"), (95, "止损", "#ef5350")],
    )
    assert ok is True
    assert os.path.getsize(path) > 5_000


def test_render_skips_when_too_few_bars(tmp_path):
    assert render_kline_png([], str(tmp_path / "x.png")) is False
