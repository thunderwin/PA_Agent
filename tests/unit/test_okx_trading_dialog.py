"""Unit tests for the OKX trading settings dialog (offscreen, no network)."""

from __future__ import annotations

import json
import stat
import sys

import pytest
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from pa_agent.config.settings import Settings
from pa_agent.gui import okx_trading_dialog as dialog_mod
from pa_agent.gui.okx_trading_dialog import OkxTradingDialog


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture
def settings_and_path(tmp_path):
    settings = Settings()
    cred_path = tmp_path / "okx_trading.json"
    settings.trading.credentials_path = str(cred_path)
    # 币安凭据也必须指向临时目录：否则测试会去读开发者本机真实的
    # config/binance_trading.json，输入框被真 key 填满，断言全部失真。
    settings.trading.binance_credentials_path = str(tmp_path / "binance_trading.json")
    return settings, cred_path


def _fill_credentials(dlg: OkxTradingDialog) -> None:
    dlg._api_key_edit.setText("ak-test")
    dlg._secret_edit.setText("sk-test")
    dlg._passphrase_edit.setText("pp-test")


def test_dialog_saves_paper_credentials_and_settings(qapp, settings_and_path, monkeypatch):
    settings, cred_path = settings_and_path
    saved: list[Settings] = []
    monkeypatch.setattr(dialog_mod, "save_settings", lambda s, p=None: saved.append(s))

    dlg = OkxTradingDialog(settings)
    _fill_credentials(dlg)
    dlg._enabled_check.setChecked(True)
    dlg._demo_radio.setChecked(True)
    dlg._max_loss_spin.setValue(12.5)
    dlg._daily_cap_spin.setValue(40.0)
    dlg._leverage_spin.setValue(5)
    dlg._min_conf_spin.setValue(70)
    dlg._symbols_edit.setText("BTC-USDT-SWAP, eth-usdt-swap")
    dlg._on_save()

    assert dlg.result() == int(QDialog.DialogCode.Accepted)
    payload = json.loads(cred_path.read_text(encoding="utf-8"))
    assert payload == {"api_key": "ak-test", "secret_key": "sk-test", "passphrase": "pp-test"}
    mode = stat.S_IMODE(cred_path.stat().st_mode)
    assert mode == 0o600

    t = settings.trading
    assert t.enabled is True and t.simulated is True
    assert t.max_loss_per_trade_usd == pytest.approx(12.5)
    assert t.daily_loss_cap_usd == pytest.approx(40.0)
    assert t.leverage == 5 and t.min_confidence == 70
    assert t.allowed_symbols == ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
    assert saved and saved[0] is settings


def test_dialog_requires_acknowledgement_for_live(qapp, settings_and_path, monkeypatch):
    settings, cred_path = settings_and_path
    monkeypatch.setattr(dialog_mod, "save_settings", lambda s, p=None: None)
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *a, **k: warnings.append(a[2] if len(a) > 2 else ""),
    )

    dlg = OkxTradingDialog(settings)
    _fill_credentials(dlg)
    dlg._enabled_check.setChecked(True)
    dlg._live_radio.setChecked(True)
    dlg._live_ack_check.setChecked(False)
    dlg._on_save()

    assert warnings and "必须先勾选" in warnings[0]
    assert not cred_path.exists()
    assert settings.trading.enabled is False


def test_dialog_live_requires_second_confirmation(qapp, settings_and_path, monkeypatch):
    settings, cred_path = settings_and_path
    monkeypatch.setattr(dialog_mod, "save_settings", lambda s, p=None: None)
    answers = iter([QMessageBox.StandardButton.No])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: next(answers))

    dlg = OkxTradingDialog(settings)
    _fill_credentials(dlg)
    dlg._enabled_check.setChecked(True)
    dlg._live_radio.setChecked(True)
    dlg._live_ack_check.setChecked(True)
    dlg._on_save()
    assert dlg.result() != int(QDialog.DialogCode.Accepted)  # 用户取消 → 不保存
    assert not cred_path.exists()

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dlg._on_save()
    assert dlg.result() == int(QDialog.DialogCode.Accepted)
    assert cred_path.exists()
    assert settings.trading.simulated is False
    assert settings.trading.live_ack is True


def test_dialog_loads_existing_credentials(qapp, settings_and_path):
    settings, cred_path = settings_and_path
    cred_path.write_text(
        json.dumps({"api_key": "ak-old", "secret_key": "sk-old", "passphrase": "pp-old"}),
        encoding="utf-8",
    )
    settings.trading.simulated = False
    settings.trading.live_ack = True
    settings.trading.max_loss_per_trade_usd = 7.5

    dlg = OkxTradingDialog(settings)
    assert dlg._api_key_edit.text() == "ak-old"
    assert dlg._secret_edit.text() == "sk-old"
    assert dlg._passphrase_edit.text() == "pp-old"
    assert dlg._live_radio.isChecked() is True
    assert dlg._live_ack_check.isChecked() is True
    assert dlg._max_loss_spin.value() == pytest.approx(7.5)


def test_dialog_requires_credentials_when_enabled(qapp, settings_and_path, monkeypatch):
    settings, _ = settings_and_path
    monkeypatch.setattr(dialog_mod, "save_settings", lambda s, p=None: None)
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *a, **k: warnings.append(a[2] if len(a) > 2 else ""),
    )

    dlg = OkxTradingDialog(settings)
    dlg._enabled_check.setChecked(True)
    dlg._on_save()

    assert warnings and "API Key" in warnings[0]
    assert settings.trading.enabled is False


# ── 交易所切换（OKX / 币安）──────────────────────────────────────────────────


def test_dialog_switches_venue_and_hides_passphrase(qapp, settings_and_path):
    settings, _ = settings_and_path
    dlg = OkxTradingDialog(settings)
    assert dlg._venue() == "okx"
    assert dlg._passphrase_edit.isVisible() or not dlg.isVisible()   # 未显示时不可判定
    assert "okx_trading.json" in dlg._cred_path_label.text()

    idx = dlg._venue_combo.findData("binance")
    dlg._venue_combo.setCurrentIndex(idx)
    assert dlg._venue() == "binance"
    assert dlg._passphrase_edit.isHidden()
    assert "binance_trading.json" in dlg._cred_path_label.text()
    assert "币安" in dlg._cred_hint_label.text()

    dlg._venue_combo.setCurrentIndex(dlg._venue_combo.findData("okx"))
    assert not dlg._passphrase_edit.isHidden()


def test_dialog_saves_binance_credentials(qapp, settings_and_path, monkeypatch, tmp_path):
    settings, _ = settings_and_path
    settings.trading.binance_credentials_path = str(tmp_path / "binance_trading.json")
    monkeypatch.setattr(dialog_mod, "save_settings", lambda s, p=None: None)

    dlg = OkxTradingDialog(settings)
    dlg._venue_combo.setCurrentIndex(dlg._venue_combo.findData("binance"))
    dlg._api_key_edit.setText("bn-key")
    dlg._secret_edit.setText("bn-secret")
    dlg._enabled_check.setChecked(True)
    dlg._demo_radio.setChecked(True)
    dlg._on_save()

    assert dlg.result() == int(QDialog.DialogCode.Accepted)
    payload = json.loads((tmp_path / "binance_trading.json").read_text(encoding="utf-8"))
    assert payload == {"api_key": "bn-key", "secret_key": "bn-secret"}   # 没有 passphrase
    assert settings.trading.venue == "binance"


def test_dialog_binance_requires_two_fields(qapp, settings_and_path, monkeypatch):
    settings, _ = settings_and_path
    monkeypatch.setattr(dialog_mod, "save_settings", lambda s, p=None: None)
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *a, **k: warnings.append(a[2] if len(a) > 2 else "")
    )
    dlg = OkxTradingDialog(settings)
    dlg._venue_combo.setCurrentIndex(dlg._venue_combo.findData("binance"))
    dlg._api_key_edit.setText("bn-key")          # 只填了 key
    dlg._enabled_check.setChecked(True)
    dlg._on_save()
    assert warnings and "币安" in warnings[0]
    assert settings.trading.enabled is False
