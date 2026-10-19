"""把最近 N 根 K 线画成 PNG（给 Telegram 交易提醒用）。

不依赖 matplotlib（这个环境没装），直接用 Qt 的 QImage + QPainter 离屏绘制：
蜡烛图 + EMA20 + 成交价刻度 + 可选的入场/止损/止盈水平线。
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Sequence

logger = logging.getLogger(__name__)

_UP = "#26a69a"
_DOWN = "#ef5350"
_EMA = "#f5c542"
_GRID = "#2a2e39"
_TEXT = "#b2b5be"
_BG = "#131722"


def _bar_get(bar: Any, key: str) -> float:
    if isinstance(bar, dict):
        return float(bar.get(key) or 0.0)
    return float(getattr(bar, key, 0.0) or 0.0)


def _ts(bar: Any) -> int:
    if isinstance(bar, dict):
        return int(bar.get("ts_open") or 0)
    return int(getattr(bar, "ts_open", 0) or 0)


def ema(closes: Sequence[float], period: int = 20) -> list[float]:
    """EMA（用前 period 根的 SMA 做种子）。closes 为旧→新。"""
    if not closes:
        return []
    if len(closes) < period:
        return [float("nan")] * len(closes)
    k = 2.0 / (period + 1.0)
    out: list[float] = [float("nan")] * (period - 1)
    seed = sum(closes[:period]) / period
    out.append(seed)
    prev = seed
    for price in closes[period:]:
        prev = price * k + prev * (1 - k)
        out.append(prev)
    return out


def render_kline_png(
    bars: Sequence[Any],
    path: str,
    *,
    title: str = "",
    ema_period: int = 20,
    width: int = 1280,
    height: int = 620,
    levels: Sequence[tuple[float, str, str]] = (),
) -> bool:
    """画图并保存 PNG。``bars`` 新旧顺序都行（按 ts_open 自动排序）。返回是否成功。"""
    try:
        from PyQt6.QtCore import QPointF, QRectF, Qt
        from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen
    except Exception as exc:  # noqa: BLE001
        logger.warning("画图失败（PyQt6 不可用）：%s", exc)
        return False

    clean = [b for b in bars if _bar_get(b, "close") > 0]
    if len(clean) < 5:
        logger.warning("K 线不足，跳过画图（%d 根）", len(clean))
        return False
    clean = sorted(clean, key=_ts)
    if len(clean) > 100:                    # 只画最近 100 根
        clean = clean[-100:]
    closes = [_bar_get(b, "close") for b in clean]
    ema_vals = ema(closes, ema_period)
    highs = [_bar_get(b, "high") for b in clean]
    lows = [_bar_get(b, "low") for b in clean]

    lo = min([x for x in lows if x > 0] or closes)
    hi = max([x for x in highs if x > 0] or closes)
    for price, _label, _color in levels:
        if price and price > 0:
            lo, hi = min(lo, price), max(hi, price)
    pad = (hi - lo) * 0.06 or max(hi * 0.01, 1e-8)
    lo, hi = lo - pad, hi + pad

    left, right, top, bottom = 14, 96, 42, 34
    plot_w = max(width - left - right, 10)
    plot_h = max(height - top - bottom, 10)
    step = plot_w / len(clean)
    body_w = max(step * 0.62, 1.2)

    def y_of(price: float) -> float:
        return top + (hi - price) / (hi - lo) * plot_h

    def x_of(i: int) -> float:
        return left + (i + 0.5) * step

    img = QImage(width, height, QImage.Format.Format_RGB32)
    img.fill(QColor(_BG))
    painter = QPainter(img)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(_TEXT), 1))
        painter.setFont(QFont("Helvetica", 11))
        if title:
            painter.setFont(QFont("Helvetica", 13, QFont.Weight.Bold))
            painter.drawText(QRectF(left, 8, width - left - right, 26), Qt.AlignmentFlag.AlignLeft, title)

        # 网格 + 价格刻度（5 档）
        painter.setFont(QFont("Helvetica", 10))
        for i in range(1, 5):
            price = lo + (hi - lo) * i / 5
            y = y_of(price)
            painter.setPen(QPen(QColor(_GRID), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(left, y), QPointF(width - right, y))
            painter.setPen(QPen(QColor(_TEXT), 1))
            painter.drawText(QRectF(width - right + 6, y - 9, right - 8, 18),
                             Qt.AlignmentFlag.AlignLeft, f"{price:.6g}")

        # 蜡烛
        for i, bar in enumerate(clean):
            o, h, l, c = (_bar_get(bar, "open"), _bar_get(bar, "high"),
                          _bar_get(bar, "low"), _bar_get(bar, "close"))
            color = QColor(_UP if c >= o else _DOWN)
            x = x_of(i)
            painter.setPen(QPen(color, 1))
            painter.drawLine(QPointF(x, y_of(h)), QPointF(x, y_of(l)))
            top_y, bot_y = y_of(max(o, c)), y_of(min(o, c))
            rect = QRectF(x - body_w / 2, top_y, body_w, max(bot_y - top_y, 1.0))
            painter.fillRect(rect, color)

        # EMA20
        painter.setPen(QPen(QColor(_EMA), 1.8))
        prev: QPointF | None = None
        for i, value in enumerate(ema_vals):
            if value != value:          # NaN
                prev = None
                continue
            point = QPointF(x_of(i), y_of(value))
            if prev is not None:
                painter.drawLine(prev, point)
            prev = point

        # 入场/止损/止盈线
        painter.setFont(QFont("Helvetica", 10, QFont.Weight.Bold))
        for price, label, color in levels:
            if not price or price <= 0:
                continue
            y = y_of(price)
            painter.setPen(QPen(QColor(color), 1.4, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(left, y), QPointF(width - right, y))
            painter.drawText(QRectF(left + 6, y - 17, 260, 16),
                             Qt.AlignmentFlag.AlignLeft, f"{label} {price:.6g}")

        # 时间刻度（最旧 / 中间 / 最新）
        painter.setPen(QPen(QColor(_TEXT), 1))
        painter.setFont(QFont("Helvetica", 9))
        for idx in (0, len(clean) // 2, len(clean) - 1):
            ts = _ts(clean[idx])
            if ts <= 0:
                continue
            label = datetime.datetime.fromtimestamp(ts / 1000).strftime("%m-%d %H:%M")
            x = x_of(idx)
            label_x = min(max(x - 60, 2), width - 124)
            painter.drawText(QRectF(label_x, height - bottom + 8, 120, 18),
                             Qt.AlignmentFlag.AlignCenter, label)

        # 图例（右上，避开底部时间轴）
        painter.setFont(QFont("Helvetica", 10))
        painter.setPen(QPen(QColor(_EMA), 1.8))
        legend_x = width - right - 130
        painter.drawLine(QPointF(legend_x, 22), QPointF(legend_x + 24, 22))
        painter.setPen(QPen(QColor(_TEXT), 1))
        painter.drawText(QRectF(legend_x + 30, 12, 100, 18),
                         Qt.AlignmentFlag.AlignLeft, f"EMA{ema_period}")
    finally:
        painter.end()
    ok = img.save(path, "PNG")
    if not ok:
        logger.warning("图表保存失败：%s", path)
    return ok
