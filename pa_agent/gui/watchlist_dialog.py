"""多品种监控设置：维护监控列表与节奏。"""

from __future__ import annotations

import logging

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.config.settings import Settings, save_settings

logger = logging.getLogger(__name__)

_TIMEFRAMES = ("", "1m", "5m", "15m", "30m", "1h", "4h", "1d")


class WatchlistDialog(QDialog):
    """编辑「多品种监控」配置。"""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("多品种监控")
        self.setMinimumWidth(560)
        self._setup_ui()
        self._load_values()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)

        self._enabled_check = QCheckBox("启用多品种监控")
        root.addWidget(self._enabled_check)

        group = QGroupBox("监控品种（每行一个，OKX 用 BTC-USDT-SWAP 这种写法）")
        form = QFormLayout(group)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._symbols_edit = QPlainTextEdit()
        self._symbols_edit.setPlaceholderText("BTC-USDT-SWAP\nETH-USDT-SWAP\nSOL-USDT-SWAP")
        self._symbols_edit.setMinimumHeight(120)
        form.addRow("品种列表:", self._symbols_edit)

        self._tf_combo = QComboBox()
        for tf in _TIMEFRAMES:
            self._tf_combo.addItem("跟随主窗口当前周期" if tf == "" else tf, tf)
        form.addRow("监控周期:", self._tf_combo)

        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(10, 3600)
        self._interval_spin.setSuffix(" 秒")
        self._interval_spin.setToolTip(
            "每隔多久检查一次各品种有没有新 K 线收盘；只有收盘才会真正调用大模型分析"
        )
        form.addRow("探活间隔:", self._interval_spin)

        self._alert_check = QCheckBox("监控到可下单方案时提示（状态栏 + 提示音）")
        form.addRow("", self._alert_check)

        root.addWidget(group)

        note = QLabel(
            "说明：多品种监控使用独立的数据源订阅，不影响主图表；"
            "每个品种只在出现新的已收盘 K 线时才产生一次 AI 分析（消耗 token）。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #8b949e; font-size: 11px;")
        root.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _load_values(self) -> None:
        g = self._settings.general
        self._enabled_check.setChecked(bool(getattr(g, "watch_enabled", False)))
        self._symbols_edit.setPlainText("\n".join(getattr(g, "watch_symbols", []) or []))
        tf = str(getattr(g, "watch_timeframe", "") or "")
        idx = self._tf_combo.findData(tf)
        self._tf_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._interval_spin.setValue(int(getattr(g, "watch_interval_s", 60) or 60))
        self._alert_check.setChecked(bool(getattr(g, "watch_alert_on_signal", True)))

    def _on_save(self) -> None:
        g = self._settings.general
        symbols = [
            line.strip().upper()
            for line in self._symbols_edit.toPlainText().splitlines()
            if line.strip()
        ]
        # 去重保序
        seen: set[str] = set()
        unique = [s for s in symbols if not (s in seen or seen.add(s))]
        g.watch_symbols = unique
        g.watch_timeframe = str(self._tf_combo.currentData() or "")
        g.watch_interval_s = int(self._interval_spin.value())
        g.watch_alert_on_signal = bool(self._alert_check.isChecked())
        g.watch_enabled = bool(self._enabled_check.isChecked())
        try:
            save_settings(self._settings, SETTINGS_JSON_PATH)
        except Exception as exc:
            logger.warning("保存多品种监控设置失败: %s", exc)
        self.accept()
