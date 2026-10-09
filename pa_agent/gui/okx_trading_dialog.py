"""交易设置对话框：选交易所（OKX / 币安）、填 API 凭据、设以损定量参数。

凭据按交易所分别写到 ``config/okx_trading.json`` / ``config/binance_trading.json``
（都已 gitignore，权限 600）；其余参数写到 ``config/settings.json`` 的 ``trading`` 段。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.config.settings import Settings, save_settings
from pa_agent.trading.okx_trader import OkxCredentials, OkxPrivateClient, OkxTradeError
from pa_agent.trading.binance_trader import (
    BinanceCredentials,
    BinancePrivateClient,
    BinanceTradeError,
)
from pa_agent.trading.gateway import credentials_path_for, normalize_venue, venue_label
from pa_agent.trading.gateway import resolve_proxy

logger = logging.getLogger(__name__)


class OkxTradingDialog(QDialog):
    """交易设置（OKX / 币安）。保存后返回 QDialog.DialogCode.Accepted。"""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("交易设置（OKX / 币安）")
        self.setMinimumWidth(600)
        self._setup_ui()
        self._load_values()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        root.addWidget(self._status_label)

        # ── 凭据 ──────────────────────────────────────────────────────────────
        cred_group = QGroupBox("API 凭据（只勾「交易」权限，绝不要开提现/划转）")
        self._cred_group = cred_group
        cred_form = QFormLayout(cred_group)
        cred_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._venue_combo = QComboBox()
        self._venue_combo.addItem("OKX — USDT 永续 / 现货", "okx")
        self._venue_combo.addItem("币安 — USDT 本位永续", "binance")
        self._venue_combo.currentIndexChanged.connect(self._on_venue_changed)
        cred_form.addRow("交易所:", self._venue_combo)

        self._cred_hint_label = QLabel("")
        self._cred_hint_label.setWordWrap(True)
        self._cred_hint_label.setStyleSheet("color: #8b949e; font-size: 11px;")
        cred_form.addRow("", self._cred_hint_label)

        self._api_key_edit = QLineEdit()
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_edit.setPlaceholderText("API Key")
        cred_form.addRow("API Key:", self._api_key_edit)

        self._secret_edit = QLineEdit()
        self._secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._secret_edit.setPlaceholderText("Secret Key")
        cred_form.addRow("Secret Key:", self._secret_edit)

        self._passphrase_label = QLabel("Passphrase:")
        self._passphrase_edit = QLineEdit()
        self._passphrase_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._passphrase_edit.setPlaceholderText("创建 API Key 时设置的 Passphrase")
        cred_form.addRow(self._passphrase_label, self._passphrase_edit)

        self._cred_path_label = QLabel("")
        self._cred_path_label.setStyleSheet("color: #8b949e; font-size: 11px;")
        cred_form.addRow("凭据文件:", self._cred_path_label)

        test_row = QHBoxLayout()
        self._test_btn = QPushButton("测试连接（读余额/持仓）")
        self._test_btn.clicked.connect(self._on_test_connection)
        test_row.addWidget(self._test_btn)
        test_row.addStretch()
        cred_form.addRow("", self._wrap(test_row))
        root.addWidget(cred_group)

        # ── 账户与开关 ────────────────────────────────────────────────────────
        mode_group = QGroupBox("账户与开关")
        mode_form = QFormLayout(mode_group)
        mode_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._enabled_check = QCheckBox("允许下单（总开关）")
        mode_form.addRow("", self._enabled_check)

        mode_row = QHBoxLayout()
        self._live_radio = QRadioButton("实盘（真实资金）")
        self._demo_radio = QRadioButton("模拟盘")
        self._live_radio.toggled.connect(self._sync_live_ack_enabled)
        mode_row.addWidget(self._live_radio)
        mode_row.addWidget(self._demo_radio)
        mode_row.addStretch()
        mode_form.addRow("账户:", self._wrap(mode_row))

        self._live_ack_check = QCheckBox(
            "我已知晓：实盘下单是真实资金，亏损不可撤销，本程序按「以损定量」计算仓位"
        )
        self._live_ack_check.setStyleSheet("color: #ff7b72;")
        mode_form.addRow("", self._live_ack_check)

        self._trigger_combo = QComboBox()
        self._trigger_combo.addItem("手动确认（分析完等你点「执行下单」）", "manual")
        self._trigger_combo.addItem("自动触发（新 K 线收盘后自动下单）", "auto")
        mode_form.addRow("触发方式:", self._trigger_combo)

        root.addWidget(mode_group)

        # ── 以损定量 ──────────────────────────────────────────────────────────
        risk_group = QGroupBox("以损定量（仓位 = 每笔最大亏损 ÷ 止损距离）")
        risk_form = QFormLayout(risk_group)
        risk_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._max_loss_spin = QDoubleSpinBox()
        self._max_loss_spin.setRange(0.5, 10_000.0)
        self._max_loss_spin.setDecimals(2)
        self._max_loss_spin.setSuffix(" USDT")
        risk_form.addRow("每笔最大亏损:", self._max_loss_spin)

        self._daily_cap_spin = QDoubleSpinBox()
        self._daily_cap_spin.setRange(1.0, 100_000.0)
        self._daily_cap_spin.setDecimals(2)
        self._daily_cap_spin.setSuffix(" USDT")
        self._daily_cap_spin.setToolTip("当日已实现亏损达到该值后，当天不再下单")
        risk_form.addRow("当日亏损上限:", self._daily_cap_spin)

        self._max_positions_spin = QSpinBox()
        self._max_positions_spin.setRange(1, 20)
        risk_form.addRow("最大同时持仓:", self._max_positions_spin)

        self._leverage_spin = QSpinBox()
        self._leverage_spin.setRange(1, 50)
        self._leverage_spin.setSuffix("x")
        risk_form.addRow("永续杠杆:", self._leverage_spin)

        self._min_conf_spin = QSpinBox()
        self._min_conf_spin.setRange(0, 100)
        risk_form.addRow("最低置信度:", self._min_conf_spin)

        self._symbols_edit = QLineEdit()
        self._symbols_edit.setPlaceholderText("留空 = 只允许当前订阅品种；多个用逗号分隔")
        risk_form.addRow("允许下单品种:", self._symbols_edit)

        root.addWidget(risk_group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _wrap(self, layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    # ── 数据 ──────────────────────────────────────────────────────────────────

    def _venue(self) -> str:
        return normalize_venue(self._venue_combo.currentData())

    def _credentials_path(self) -> Path:
        return Path(credentials_path_for(self._settings, self._venue()))

    def _on_venue_changed(self) -> None:
        """切换交易所：改提示文字、隐藏币安用不到的 Passphrase、重读凭据文件。"""
        venue = self._venue()
        is_okx = venue == "okx"
        self._passphrase_label.setVisible(is_okx)
        self._passphrase_edit.setVisible(is_okx)
        if is_okx:
            self._cred_hint_label.setText(
                "OKX → API 管理 → 创建 V5 API Key：勾「交易」，绑定本机出口 IP；"
                "需要 API Key / Secret Key / Passphrase 三项。"
            )
        else:
            self._cred_hint_label.setText(
                "币安 → API 管理 → 创建 API Key：勾「启用合约」，不要勾提现；"
                "只要 API Key / Secret Key 两项。合约接口按地区限制，出口 IP 需可用。"
            )
        self._reload_credentials()
        self._refresh_status()

    def _reload_credentials(self) -> None:
        """把当前交易所凭据文件里的内容填进输入框（读不到就清空）。"""
        t = self._settings.trading
        path = self._credentials_path()
        self._cred_path_label.setText(str(path))
        self._api_key_edit.clear()
        self._secret_edit.clear()
        self._passphrase_edit.clear()
        if self._venue() == "okx":
            cred = OkxCredentials.load(path, simulated=bool(t.simulated))
            if cred is not None:
                self._api_key_edit.setText(cred.api_key)
                self._secret_edit.setText(cred.secret_key)
                self._passphrase_edit.setText(cred.passphrase)
        else:
            cred_bn = BinanceCredentials.load(path, simulated=bool(t.simulated))
            if cred_bn is not None:
                self._api_key_edit.setText(cred_bn.api_key)
                self._secret_edit.setText(cred_bn.secret_key)

    def _load_values(self) -> None:
        t = self._settings.trading
        idx = self._venue_combo.findData(normalize_venue(getattr(t, "venue", "okx")))
        if idx >= 0:
            self._venue_combo.setCurrentIndex(idx)
        self._on_venue_changed()

        self._enabled_check.setChecked(bool(t.enabled))
        self._live_radio.setChecked(not bool(t.simulated))
        self._demo_radio.setChecked(bool(t.simulated))
        self._live_ack_check.setChecked(bool(t.live_ack))
        idx = self._trigger_combo.findData(str(t.trigger_mode))
        if idx >= 0:
            self._trigger_combo.setCurrentIndex(idx)
        self._max_loss_spin.setValue(float(t.max_loss_per_trade_usd))
        self._daily_cap_spin.setValue(float(t.daily_loss_cap_usd))
        self._max_positions_spin.setValue(int(t.max_open_positions))
        self._leverage_spin.setValue(int(t.leverage))
        self._min_conf_spin.setValue(int(t.min_confidence))
        self._symbols_edit.setText(", ".join(getattr(t, "allowed_symbols", []) or []))
        self._sync_live_ack_enabled()
        self._refresh_status()

    def _sync_live_ack_enabled(self) -> None:
        live = self._live_radio.isChecked()
        self._live_ack_check.setEnabled(live)
        if not live:
            return
        self._live_ack_check.setToolTip("实盘必须勾选才能保存/下单")

    def _refresh_status(self) -> None:
        t = self._settings.trading
        path = self._credentials_path()
        venue = self._venue()
        cred = (
            OkxCredentials.load(path, simulated=bool(t.simulated))
            if venue == "okx"
            else BinanceCredentials.load(path, simulated=bool(t.simulated))
        )
        if not t.enabled:
            text = "当前状态：下单开关关闭（分析照常，不会下单）"
        elif cred is None:
            text = "当前状态：已开启，但还没读到 API 凭据"
        else:
            text = (
                f"当前状态：{venue_label(venue)} · "
                f"{'实盘' if not cred.simulated else '模拟盘/测试网'} · 凭据 {cred.mask()}"
            )
        self._status_label.setText(text)

    def _collect_credentials(self) -> OkxCredentials:
        return OkxCredentials(
            self._api_key_edit.text().strip(),
            self._secret_edit.text().strip(),
            self._passphrase_edit.text().strip(),
            simulated=self._demo_radio.isChecked(),
        )

    def _collect_binance_credentials(self) -> BinanceCredentials:
        return BinanceCredentials(
            self._api_key_edit.text().strip(),
            self._secret_edit.text().strip(),
            simulated=self._demo_radio.isChecked(),
        )

    def _credentials_complete(self) -> bool:
        cred = (
            self._collect_credentials()
            if self._venue() == "okx"
            else self._collect_binance_credentials()
        )
        return cred.complete

    def _missing_fields_text(self) -> str:
        return (
            "请先填写 API Key / Secret Key / Passphrase。"
            if self._venue() == "okx"
            else "请先填写币安的 API Key / Secret Key。"
        )

    # ── 动作 ──────────────────────────────────────────────────────────────────

    def _on_test_connection(self) -> None:
        venue = self._venue()
        if venue == "okx":
            cred = self._collect_credentials()
            if not cred.complete:
                QMessageBox.warning(self, "缺少凭据", self._missing_fields_text())
                return
            try:
                client: Any = OkxPrivateClient(cred, timeout=8.0)
                equity = client.equity_usd()
                positions = client.open_position_count()
            except OkxTradeError as exc:
                QMessageBox.critical(self, "连接失败", str(exc))
                return
            mode_text = "模拟盘" if cred.simulated else "实盘"
        else:
            cred_bn = self._collect_binance_credentials()
            if not cred_bn.complete:
                QMessageBox.warning(self, "缺少凭据", self._missing_fields_text())
                return
            try:
                client_bn = BinancePrivateClient(
                    cred_bn,
                    timeout=8.0,
                    proxy=resolve_proxy("binance", self._settings),
                )
                equity = client_bn.equity_usd()
                positions = client_bn.open_position_count()
            except BinanceTradeError as exc:
                QMessageBox.critical(self, "连接失败", str(exc))
                return
            mode_text = "测试网" if cred_bn.simulated else "实盘"
        QMessageBox.information(
            self,
            "连接成功",
            f"账户权益：{equity:,.2f} USDT\n当前持仓数：{positions}\n"
            f"交易所：{venue_label(venue)}\n模式：{mode_text}",
        )

    def _on_save(self) -> None:
        live = self._live_radio.isChecked()
        if live and not self._live_ack_check.isChecked():
            QMessageBox.warning(
                self,
                "需要风险确认",
                "选择实盘必须先勾选「我已知晓：实盘下单是真实资金…」，否则无法保存。",
            )
            return

        cred = self._collect_credentials()
        venue = self._venue()
        cred_bn = self._collect_binance_credentials()
        complete = cred.complete if venue == "okx" else cred_bn.complete
        if self._enabled_check.isChecked() and not complete:
            QMessageBox.warning(
                self,
                "缺少凭据",
                f"要开启下单，必须填写完整的{venue_label(venue)}凭据。\n{self._missing_fields_text()}",
            )
            return
        if live and complete:
            confirm = QMessageBox.question(
                self,
                "确认切换实盘",
                f"即将保存为【{venue_label(venue)} 实盘】配置，程序会向真实账户发送订单。\n\n"
                "每笔最大亏损会严格按设置值反推仓位。确定继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return

        if complete and not self._write_credentials(cred, cred_bn):
            return

        t = self._settings.trading
        t.venue = venue
        t.enabled = self._enabled_check.isChecked()
        t.simulated = not live
        t.live_ack = self._live_ack_check.isChecked() if live else False
        t.trigger_mode = str(self._trigger_combo.currentData())
        t.max_loss_per_trade_usd = float(self._max_loss_spin.value())
        t.daily_loss_cap_usd = float(self._daily_cap_spin.value())
        t.max_open_positions = int(self._max_positions_spin.value())
        t.leverage = int(self._leverage_spin.value())
        t.min_confidence = int(self._min_conf_spin.value())
        t.allowed_symbols = [
            s.strip().upper() for s in self._symbols_edit.text().split(",") if s.strip()
        ]
        try:
            save_settings(self._settings, SETTINGS_JSON_PATH)
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self.accept()

    def _write_credentials(
        self, cred: OkxCredentials, cred_bn: BinanceCredentials
    ) -> bool:
        """按当前交易所把凭据写进各自的文件（权限 600）。"""
        path = self._credentials_path()
        # 覆盖前先备份一份：手滑、或者被自动化脚本写坏时还能捞回来（.bak 已 gitignore）
        if path.exists():
            try:
                shutil.copy2(path, path.with_name(path.name + ".bak"))
            except OSError as exc:
                logger.warning("凭据备份失败（%s）：%s", path, exc)
        if self._venue() == "okx":
            payload = {
                "api_key": cred.api_key,
                "secret_key": cred.secret_key,
                "passphrase": cred.passphrase,
            }
        else:
            payload = {"api_key": cred_bn.api_key, "secret_key": cred_bn.secret_key}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.chmod(path, 0o600)
        except OSError as exc:
            QMessageBox.critical(self, "凭据写入失败", f"{path}\n{exc}")
            return False
        return True
