"""多品种监控面板：一张表看完所有被监控品种的最新结论。"""

from __future__ import annotations

import time
from collections.abc import Iterable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pa_agent.orchestrator.watchlist import WatchResult, WatchTarget

_COLUMNS = ("品种", "周期", "最新价", "决策", "方向", "置信度", "更新时间", "状态")

_COLOR_LONG = QColor("#3fb950")
_COLOR_SHORT = QColor("#f85149")
_COLOR_MUTED = QColor("#8b949e")
_COLOR_ERROR = QColor("#ff7b72")


def _fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1000:
        return f"{value:,.1f}"
    if value >= 1:
        return f"{value:,.3f}"
    return f"{value:.6f}".rstrip("0").rstrip(".")


class WatchlistPanel(QWidget):
    """多品种结果表；双击一行切换主图表到该品种。"""

    #: (symbol, timeframe)
    symbol_activated = pyqtSignal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: dict[str, int] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        self._hint = QLabel(
            "多品种监控：后台按各品种自己的 K 线收盘节奏跑分析。"
            "双击一行可把主图表切到该品种。菜单「多品种监控」里增删品种。"
        )
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("color: #8b949e; font-size: 11px;")
        root.addWidget(self._hint)

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(list(_COLUMNS))
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self._table.doubleClicked.connect(self._on_double_clicked)
        root.addWidget(self._table, 1)

    # ── 数据 ──────────────────────────────────────────────────────────────────

    def set_targets(self, targets: Iterable[WatchTarget]) -> None:
        """重建表格（保留已存在的行数据不作保留，简单起见清空重排）。"""
        self._table.setRowCount(0)
        self._rows.clear()
        for target in targets:
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._rows[target.symbol] = row
            self._set(row, 0, target.symbol)
            self._set(row, 1, target.timeframe)
            for col in range(2, len(_COLUMNS)):
                self._set(row, col, "—")
            self._set(row, len(_COLUMNS) - 1, "等待")

    def update_result(self, result: WatchResult) -> None:
        """写入某一品种的最新结果。"""
        row = self._rows.get(result.symbol)
        if row is None:
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._rows[result.symbol] = row
            self._set(row, 0, result.symbol)
            self._set(row, 1, result.timeframe)

        self._set(row, 2, _fmt_price(result.price))
        self._set(row, 3, result.order_type or "—")
        self._set(row, 4, result.direction or "—")
        conf = f"{result.confidence:.0f}" if isinstance(result.confidence, (int, float)) else "—"
        self._set(row, 5, conf)
        self._set(row, 6, time.strftime("%H:%M:%S", time.localtime(result.ts_ms / 1000)))

        if result.error:
            self._set(row, 7, result.error[:40], color=_COLOR_ERROR)
        elif result.skipped:
            self._set(row, 7, "无新K线", color=_COLOR_MUTED)
        else:
            self._set(row, 7, "已更新", color=_COLOR_MUTED)

        # 有可执行方案时给方向染色，方便一眼扫到
        for col in (3, 4, 5):
            item = self._table.item(row, col)
            if item is None:
                continue
            if result.has_order and result.direction == "做多":
                item.setForeground(_COLOR_LONG)
            elif result.has_order and result.direction == "做空":
                item.setForeground(_COLOR_SHORT)
            else:
                item.setForeground(_COLOR_MUTED)

    def set_status_text(self, text: str) -> None:
        self._hint.setText(text)

    def row_count(self) -> int:
        return self._table.rowCount()

    def cell_text(self, symbol: str, column: int) -> str:
        row = self._rows.get(symbol)
        if row is None:
            return ""
        item = self._table.item(row, column)
        return item.text() if item is not None else ""

    # ── 内部 ──────────────────────────────────────────────────────────────────

    def _set(self, row: int, col: int, text: str, *, color: QColor | None = None) -> None:
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if color is not None:
            item.setForeground(color)
        self._table.setItem(row, col, item)

    def _on_double_clicked(self, index) -> None:
        symbol_item = self._table.item(index.row(), 0)
        tf_item = self._table.item(index.row(), 1)
        if symbol_item is None:
            return
        self.symbol_activated.emit(
            symbol_item.text(), tf_item.text() if tf_item is not None else ""
        )
