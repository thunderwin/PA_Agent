"""Unit tests for the OKX trading layer (no network, no real orders)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pa_agent.config.settings import TradingSettings
from pa_agent.trading import okx_trader as ot
from pa_agent.trading.okx_trader import (
    ExecutionResult,
    InstrumentSpec,
    OkxCredentials,
    OkxPrivateClient,
    OkxTradeError,
    OkxTrader,
    TradeRejected,
    evaluate_guard,
    format_plan_confirmation,
    is_executable_decision,
    plan_order,
    sign_request,
)

BTC_SWAP = InstrumentSpec(
    inst_id="BTC-USDT-SWAP",
    inst_type="SWAP",
    tick_sz=0.1,
    lot_sz=0.01,
    min_sz=0.01,
    ct_val=0.01,
    ct_val_ccy="BTC",
)
BTC_SPOT = InstrumentSpec(
    inst_id="BTC-USDT",
    inst_type="SPOT",
    tick_sz=0.1,
    lot_sz=0.00000001,
    min_sz=0.00001,
)


def _decision(**over) -> dict:
    base = {
        "order_type": "限价单",
        "order_direction": "做多",
        "entry_price": 85000.0,
        "stop_loss_price": 84900.0,  # 每单位风险 100 USDT
        "take_profit_price": 85200.0,
        "trade_confidence": 75,
    }
    base.update(over)
    return base


# ── 签名 ──────────────────────────────────────────────────────────────────────


def test_sign_request_matches_documented_inputs():
    """OKX 文档示例输入（secret + GET /api/v5/account/balance?ccy=BTC）的固定签名值。"""
    sig = sign_request(
        "22582BD0CFF14C41EDBF1AB98506286D",
        "2020-12-08T09:08:57.715Z",
        "GET",
        "/api/v5/account/balance?ccy=BTC",
    )
    assert sig == "HiZhvSfMtWJA3uUIVXV3a/bSXNPCWvYFXoGCVS8V4zY="


def test_sign_request_includes_body_and_method_case():
    body = '{"instId":"BTC-USDT-SWAP","sz":"1"}'
    assert sign_request("k", "t", "post", "/api/v5/trade/order", body) == sign_request(
        "k", "t", "POST", "/api/v5/trade/order", body
    )
    assert sign_request("k", "t", "POST", "/p", body) != sign_request("k", "t", "POST", "/p")


# ── 凭据 ──────────────────────────────────────────────────────────────────────


def test_credentials_from_env(monkeypatch):
    monkeypatch.setenv("OKX_API_KEY", "ak-0123456789")
    monkeypatch.setenv("OKX_SECRET_KEY", "sk")
    monkeypatch.setenv("OKX_PASSPHRASE", "pp")
    cred = OkxCredentials.from_env(simulated=False)
    assert cred is not None and cred.complete and cred.simulated is False
    assert "ak-0" in cred.mask() and "sk" not in cred.mask()


def test_credentials_from_env_incomplete_returns_none(monkeypatch):
    monkeypatch.setenv("OKX_API_KEY", "ak")
    monkeypatch.delenv("OKX_SECRET_KEY", raising=False)
    monkeypatch.delenv("OKX_PASSPHRASE", raising=False)
    assert OkxCredentials.from_env() is None


def test_credentials_from_file_and_priority(tmp_path, monkeypatch):
    path = tmp_path / "okx_trading.json"
    path.write_text(
        json.dumps(
            {
                "api_key": "file-ak",
                "secret_key": "file-sk",
                "passphrase": "file-pp",
                "simulated": True,
            }
        ),
        encoding="utf-8",
    )
    cred = OkxCredentials.load(path)
    assert cred is not None and cred.api_key == "file-ak"

    monkeypatch.setenv("OKX_API_KEY", "env-ak")
    monkeypatch.setenv("OKX_SECRET_KEY", "env-sk")
    monkeypatch.setenv("OKX_PASSPHRASE", "env-pp")
    assert OkxCredentials.load(path).api_key == "env-ak"  # 环境变量优先


def test_credentials_missing_file_or_broken_json(tmp_path):
    assert OkxCredentials.load(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert OkxCredentials.load(bad) is None


# ── 私有客户端 ────────────────────────────────────────────────────────────────


class _Capture:
    def __init__(self, payload=None):
        self.calls: list[dict] = []
        self.payload = payload if payload is not None else {"code": "0", "data": [{"ordId": "1"}]}

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return json.dumps(self.payload)


def _client(capture: _Capture, **kw) -> OkxPrivateClient:
    cred = kw.pop("cred", OkxCredentials("ak", "sk", "pp", simulated=True))
    return OkxPrivateClient(
        cred, base_url="https://okx.test", now=lambda: "2026-10-06T00:00:00.000Z", **kw
    )


def test_request_signs_path_and_sets_simulated_header(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(ot, "_send_http", cap)
    _client(cap).request("GET", "/api/v5/account/balance", params={"ccy": "USDT"})

    call = cap.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://okx.test/api/v5/account/balance?ccy=USDT"
    assert call["headers"]["OK-ACCESS-TIMESTAMP"] == "2026-10-06T00:00:00.000Z"
    assert call["headers"]["x-simulated-trading"] == "1"
    assert call["headers"]["OK-ACCESS-SIGN"] == sign_request(
        "sk", "2026-10-06T00:00:00.000Z", "GET", "/api/v5/account/balance?ccy=USDT"
    )
    assert "sk" not in json.dumps(call["headers"])


def test_live_account_has_no_simulated_header(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(ot, "_send_http", cap)
    _client(cap, cred=OkxCredentials("ak", "sk", "pp", simulated=False)).request(
        "GET", "/api/v5/account/balance"
    )
    assert "x-simulated-trading" not in cap.calls[0]["headers"]


def test_request_raises_on_okx_error_code(monkeypatch):
    cap = _Capture({"code": "51001", "msg": "Instrument ID does not exist", "data": []})
    monkeypatch.setattr(ot, "_send_http", cap)
    with pytest.raises(OkxTradeError) as exc:
        _client(cap).request("GET", "/api/v5/account/balance")
    assert "51001" in str(exc.value)


def test_place_order_body_includes_algo_stop_and_profit(monkeypatch):
    cap = _Capture({"code": "0", "data": [{"ordId": "42", "sCode": "0"}]})
    monkeypatch.setattr(ot, "_send_http", cap)
    row = _client(cap).place_order(
        inst_id="BTC-USDT-SWAP",
        side="buy",
        ord_type="limit",
        size=10,
        price=85000,
        stop_px=84900,
        take_profit_px=85200,
    )
    assert row["ordId"] == "42"
    body = json.loads(cap.calls[0]["body"].decode())
    assert body == {
        "instId": "BTC-USDT-SWAP",
        "tdMode": "cross",
        "side": "buy",
        "ordType": "limit",
        "sz": "10",
        "posSide": "net",
        "px": "85000",
        "attachAlgoOrds": [
            {"slTriggerPx": "84900", "slOrdPx": "-1", "tpTriggerPx": "85200", "tpOrdPx": "-1"}
        ],
    }


def test_place_order_breakout_uses_trigger_order(monkeypatch):
    cap = _Capture({"code": "0", "data": [{"ordId": "7", "sCode": "0"}]})
    monkeypatch.setattr(ot, "_send_http", cap)
    _client(cap).place_order(
        inst_id="BTC-USDT-SWAP",
        side="buy",
        ord_type="trigger",
        size=1,
        trigger_px=86000,
        stop_px=85900,
    )
    body = json.loads(cap.calls[0]["body"].decode())
    assert body["ordType"] == "trigger"
    assert body["triggerPx"] == "86000"
    assert body["orderPx"] == "-1"
    assert "px" not in body


def test_spot_order_uses_base_ccy_and_cash_mode(monkeypatch):
    cap = _Capture({"code": "0", "data": [{"ordId": "9", "sCode": "0"}]})
    monkeypatch.setattr(ot, "_send_http", cap)
    _client(cap).place_order(
        inst_id="BTC-USDT",
        side="buy",
        ord_type="market",
        size=0.0005,
        td_mode="cash",
    )
    body = json.loads(cap.calls[0]["body"].decode())
    assert body["tgtCcy"] == "base_ccy"
    assert body["tdMode"] == "cash"
    assert "posSide" not in body


def test_place_order_rejects_exchange_error(monkeypatch):
    cap = _Capture({"code": "0", "data": [{"sCode": "51008", "sMsg": "Insufficient balance"}]})
    monkeypatch.setattr(ot, "_send_http", cap)
    with pytest.raises(OkxTradeError):
        _client(cap).place_order(inst_id="BTC-USDT-SWAP", side="buy", ord_type="market", size=1)


def test_realized_pnl_today_sums_pnl_and_fee(monkeypatch):
    calls: list[dict] = []

    def fake(method, url, headers, body, timeout):
        calls.append({"url": url})
        if "type=2" in url:
            return json.dumps(
                {
                    "code": "0",
                    "data": [
                        {"ts": str(ot._day_start_ms() + 1000), "pnl": "-8.5", "fee": "-0.5"},
                        {"ts": "1", "pnl": "-1000", "fee": "0"},  # 昨天，忽略
                    ],
                }
            )
        if "type=3" in url:
            return json.dumps(
                {
                    "code": "0",
                    "data": [
                        {"ts": str(ot._day_start_ms() + 2000), "pnl": "0", "fee": "-1.0"},
                    ],
                }
            )
        return json.dumps({"code": "0", "data": []})

    monkeypatch.setattr(ot, "_send_http", fake)
    assert _client(None).realized_pnl_today_usd() == pytest.approx(-10.0)
    assert len(calls) == 3


def test_equity_usd_falls_back_to_details(monkeypatch):
    cap = _Capture(
        {
            "code": "0",
            "data": [
                {
                    "totalEq": "",
                    "details": [
                        {"ccy": "USDT", "eq": "42.5", "availEq": "40"},
                        {"ccy": "BTC", "eq": "0.1"},
                    ],
                }
            ],
        }
    )
    monkeypatch.setattr(ot, "_send_http", cap)
    assert _client(cap).equity_usd() == pytest.approx(42.5)


# ── 下单量：每笔最大亏损 ──────────────────────────────────────────────────────


def test_plan_order_sizes_swap_by_max_loss():
    plan = plan_order(_decision(), BTC_SWAP, equity_usd=5000, max_loss_usd=10, leverage=3)
    # 每张风险 = 100 USDT * ctVal 0.01 = 1 USDT → 10 张
    assert plan.size == pytest.approx(10)
    assert plan.risk_usd == pytest.approx(10.0)
    assert plan.side == "buy" and plan.ord_type == "limit"
    assert plan.price == pytest.approx(85000)
    assert plan.stop_px == pytest.approx(84900)
    assert plan.notional_usd == pytest.approx(10 * 0.01 * 85000)


def test_plan_order_short_direction():
    plan = plan_order(
        _decision(order_direction="做空", entry_price=85000, stop_loss_price=85100),
        BTC_SWAP,
        equity_usd=5000,
        max_loss_usd=10,
        leverage=3,
    )
    assert plan.side == "sell"
    assert plan.size == pytest.approx(10)


def test_plan_order_narrows_size_by_equity():
    plan = plan_order(_decision(), BTC_SWAP, equity_usd=50, max_loss_usd=10, leverage=3)
    # 保证金上限：50*3 = 150 USDT / (0.01*85000=850) = 0.176 张 → 取整 0.17
    assert plan.size == pytest.approx(0.17)
    assert plan.risk_usd < 10
    assert any("收窄" in n for n in plan.notes)


def test_plan_order_spot_sizes_by_risk():
    plan = plan_order(_decision(), BTC_SPOT, equity_usd=20000, max_loss_usd=10)
    assert plan.size == pytest.approx(0.1)  # 10 USDT / 100 USDT每单位
    assert plan.risk_usd == pytest.approx(10.0)


def test_plan_order_spot_capped_by_balance():
    plan = plan_order(_decision(), BTC_SPOT, equity_usd=1000, max_loss_usd=500)
    assert plan.notional_usd <= 1000 * 0.95


@pytest.mark.parametrize(
    "decision,spec,message",
    [
        (_decision(order_type="不下单"), BTC_SWAP, "不下单"),
        (_decision(stop_loss_price=None), BTC_SWAP, "止损"),
        (_decision(order_direction="做多", stop_loss_price=85100), BTC_SWAP, "方向矛盾"),
        (_decision(order_direction="做空", stop_loss_price=84900), BTC_SWAP, "方向矛盾"),
        (_decision(order_direction="观望"), BTC_SWAP, "方向"),
    ],
)
def test_plan_order_rejections(decision, spec, message):
    with pytest.raises(TradeRejected) as exc:
        plan_order(decision, spec, equity_usd=1000, max_loss_usd=10)
    assert message in str(exc.value)


def test_plan_order_rejects_when_budget_too_small_for_min_size():
    with pytest.raises(TradeRejected) as exc:
        plan_order(_decision(), BTC_SPOT, equity_usd=20000, max_loss_usd=0.0001)
    assert "最小单" in str(exc.value)


def test_plan_order_rejects_zero_equity():
    with pytest.raises(TradeRejected):
        plan_order(_decision(), BTC_SWAP, equity_usd=0.0, max_loss_usd=10)


def test_plan_order_market_uses_live_price_for_risk():
    plan = plan_order(
        _decision(order_type="市价单", stop_loss_price=84950),
        BTC_SWAP,
        equity_usd=5000,
        max_loss_usd=10,
        price=85000,
    )
    assert plan.ord_type == "market"
    # 每张风险 = 50 USDT × ctVal 0.01 = 0.5；资金上限把它压到 17.64 张
    assert plan.size == pytest.approx(17.64)
    assert plan.risk_usd == pytest.approx(8.82)
    assert plan.risk_usd <= 10.0
    assert plan.price == pytest.approx(85000)


def test_plan_order_breakout_maps_to_trigger():
    plan = plan_order(_decision(order_type="突破单"), BTC_SWAP, equity_usd=1000, max_loss_usd=10)
    assert plan.ord_type == "trigger"


# ── 风控闸门 ──────────────────────────────────────────────────────────────────


def _guard(**over):
    base = dict(
        enabled=True,
        credentials_present=True,
        trigger_mode="manual",
        manual_confirm=True,
        simulated=True,
        live_acknowledged=True,
        symbol="BTC-USDT-SWAP",
        decision=_decision(),
        min_confidence=60,
        max_open_positions=1,
        open_positions=0,
        daily_loss_cap_usd=30.0,
        realized_pnl_today_usd=0.0,
        allowed_symbols=(),
    )
    base.update(over)
    return evaluate_guard(**base)


def test_guard_passes_by_default():
    guard = _guard()
    assert guard.ok and guard.blocked == ()
    assert any("手动" in n for n in guard.notes)


@pytest.mark.parametrize(
    "over,expected",
    [
        ({"enabled": False}, "开关"),
        ({"credentials_present": False}, "凭据"),
        ({"decision": _decision(trade_confidence=10)}, "置信度"),
        ({"decision": _decision(order_type="不下单")}, "没有可执行订单"),
        ({"decision": _decision(stop_loss_price=None)}, "止损"),
        ({"open_positions": 1}, "上限"),
        ({"realized_pnl_today_usd": -30.0}, "停止下单"),
        ({"realized_pnl_today_usd": None}, "无法确认"),
        ({"allowed_symbols": ["ETH-USDT-SWAP"]}, "不在允许"),
    ],
)
def test_guard_blocks(over, expected):
    guard = _guard(**over)
    assert not guard.ok
    assert any(expected in reason for reason in guard.blocked), guard.blocked


def test_guard_notes_auto_and_live_account():
    guard = _guard(
        trigger_mode="auto", manual_confirm=False, simulated=False, live_acknowledged=True
    )
    assert any("自动触发" in n for n in guard.notes)
    assert any("实盘" in n for n in guard.notes)


def test_guard_blocks_live_without_acknowledgement():
    guard = _guard(simulated=False, live_acknowledged=False)
    assert not guard.ok
    assert any("风险确认" in r for r in guard.blocked)


def test_guard_allows_live_after_acknowledgement():
    assert _guard(simulated=False, live_acknowledged=True).ok is True


# ── 执行器 ────────────────────────────────────────────────────────────────────


class _FakeMarket:
    def __init__(self, raw=None):
        self.raw = raw or {
            "instId": "BTC-USDT-SWAP",
            "instType": "SWAP",
            "tickSz": "0.1",
            "lotSz": "0.01",
            "minSz": "0.01",
            "ctVal": "0.01",
            "ctValCcy": "BTC",
        }
        self.calls = 0

    def instrument_info(self, inst_id=None):
        self.calls += 1
        return dict(self.raw, instId=inst_id or self.raw["instId"])


class _FakeClient:
    def __init__(self, equity=5000.0, positions=0, pnl=0.0):
        self._equity, self._positions, self._pnl = equity, positions, pnl
        self.orders: list[dict] = []
        self.leverage: list[tuple] = []
        self.pending: list[dict] = []
        self.algo: list[dict] = []

    def equity_usd(self, ccy="USDT"):
        return self._equity

    def open_position_count(self, inst_type=None):
        return self._positions

    def pending_orders(self, inst_id=None, inst_type="SWAP"):
        return list(self.pending)

    def algo_pending(self, inst_id=None, ord_type="oco"):
        return list(self.algo)

    def realized_pnl_today_usd(self, tz_offset_hours=8):
        return self._pnl

    def set_leverage(self, inst_id, leverage, mgn_mode="cross"):
        self.leverage.append((inst_id, leverage, mgn_mode))

    def place_order(self, **kw):
        self.orders.append(kw)
        return {"ordId": "ORD-1", "sCode": "0"}


def _trader(**settings_over) -> tuple[OkxTrader, _FakeClient, _FakeMarket]:
    over = {"enabled": True, "simulated": True, "trigger_mode": "manual", **settings_over}
    trading = TradingSettings(**over)
    client = _FakeClient()
    market = _FakeMarket()
    trader = OkxTrader(
        trading,
        credentials=OkxCredentials("ak", "sk", "pp", simulated=True),
        client=client,
        market_source=market,
    )
    return trader, client, market


def test_execute_dry_run_does_not_send():
    trader, client, _market = _trader()
    result = trader.execute(_decision(), symbol="BTC-USDT-SWAP", dry_run=True)
    assert isinstance(result, ExecutionResult)
    assert result.sent is False and result.dry_run is True
    assert result.plan is not None and result.plan.risk_usd == pytest.approx(10.0)
    assert client.orders == []


def test_execute_sends_order_with_stop_attached():
    trader, client, _market = _trader()
    result = trader.execute(_decision(), symbol="BTC-USDT-SWAP", dry_run=False, manual_confirm=True)
    assert result.sent is True and result.ord_id == "ORD-1"
    order = client.orders[0]
    assert order["inst_id"] == "BTC-USDT-SWAP"
    assert order["side"] == "buy" and order["ord_type"] == "limit"
    assert order["stop_px"] == pytest.approx(84900)
    assert order["take_profit_px"] == pytest.approx(85200)
    assert client.leverage == [("BTC-USDT-SWAP", 3, "cross")]


def test_execute_blocked_when_disabled():
    trader, client, market = _trader(enabled=False)
    result = trader.execute(_decision(), symbol="BTC-USDT-SWAP", dry_run=False)
    assert result.sent is False
    assert "开关" in result.message
    assert client.orders == [] and market.calls == 0


def test_execute_skips_when_symbol_has_pending_order():
    """同一品种已有未成交挂单时不再重复建仓（否则风险按笔数翻倍）。"""
    trader, client, _market = _trader()
    client.pending = [{"ordId": "1", "instId": "BTC-USDT-SWAP"}]
    result = trader.execute(_decision(), symbol="BTC-USDT-SWAP", dry_run=False, manual_confirm=True)
    assert result.sent is False
    assert "未成交挂单" in result.message
    assert client.orders == []


def test_execute_fails_closed_when_pending_query_fails():
    trader, client, _market = _trader()

    def _boom(*a, **k):
        raise OkxTradeError("network down")

    client.pending_orders = _boom          # type: ignore[assignment]
    result = trader.execute(_decision(), symbol="BTC-USDT-SWAP", dry_run=False, manual_confirm=True)
    assert result.sent is False and "无法确认" in result.message


def test_execute_reports_stop_attached():
    """下单后应核对到止损单（OKX 里附带止损是 oco 类型）。"""
    trader, client, _market = _trader()
    client.algo = [{"instId": "BTC-USDT-SWAP", "slTriggerPx": "84900", "tpTriggerPx": "85200"}]
    result = trader.execute(_decision(), symbol="BTC-USDT-SWAP", dry_run=False, manual_confirm=True)
    assert result.sent is True
    assert "止损已挂" in result.message and "84900" in result.message


def test_execute_warns_when_no_stop_found():
    trader, client, _market = _trader()
    client.algo = []
    result = trader.execute(_decision(), symbol="BTC-USDT-SWAP", dry_run=False, manual_confirm=True)
    assert result.sent is True
    assert "未检测到止损单" in result.message


def test_execute_blocked_without_credentials():
    trading = TradingSettings(enabled=True)
    trader = OkxTrader(trading, credentials=None, client=_FakeClient(), market_source=_FakeMarket())
    result = trader.execute(_decision(), symbol="BTC-USDT-SWAP", dry_run=False)
    assert result.sent is False and "凭据" in result.message


def test_execute_blocked_when_daily_loss_reached():
    trader, client, _market = _trader(daily_loss_cap_usd=20.0)
    client._pnl = -25.0
    result = trader.execute(_decision(), symbol="BTC-USDT-SWAP", dry_run=False)
    assert result.sent is False and "停止下单" in result.message


def test_execute_blocks_unwhitelisted_symbol():
    trader, _client_unused, _market = _trader(allowed_symbols=["ETH-USDT-SWAP"])
    result = trader.execute(_decision(), symbol="BTC-USDT-SWAP", dry_run=False)
    assert result.sent is False and "不在允许" in result.message


def test_status_text_reports_mode_and_cap():
    trader, _, _ = _trader()
    assert "模拟盘" in trader.status_text()
    assert "手动" in trader.status_text()
    assert "10" in trader.status_text()

    trading = TradingSettings(enabled=False)
    assert OkxTrader(trading).status_text() == "交易：关闭"


# ── 是否可下单 / 确认文案 / 从 settings 装配 ──────────────────────────────────


@pytest.mark.parametrize(
    "decision,expected",
    [
        (_decision(), True),
        (_decision(order_type="突破单"), True),
        (_decision(order_type="不下单"), False),
        (_decision(order_direction="观望"), False),
        (_decision(stop_loss_price=None), False),
        ({}, False),
        (None, False),
    ],
)
def test_is_executable_decision(decision, expected):
    assert is_executable_decision(decision) is expected


def test_format_plan_confirmation_shows_risk_numbers():
    plan = plan_order(_decision(), BTC_SWAP, equity_usd=5000, max_loss_usd=10, leverage=3)
    text = format_plan_confirmation(
        plan,
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        order_type_label="限价单",
        simulated=False,
        equity_usd=5000,
    )
    assert "实盘" in text
    assert "BTC-USDT-SWAP 15m" in text
    assert "止损价" in text and "距离" in text
    assert "以损定量" in text
    assert "9.99" in text or "10.00" in text  # 止损亏损
    assert "杠杆 3x" in text
    assert "5,000" in text


def test_format_plan_confirmation_keeps_thousands_separators():
    """价格格式化不能把 1000.0 截成 '1,'（千分位与去尾零要同时正确）。"""
    plan = plan_order(
        _decision(entry_price=1000.0, stop_loss_price=999.0, take_profit_price=1002.0),
        BTC_SWAP,
        equity_usd=5000,
        max_loss_usd=10,
        leverage=3,
    )
    text = format_plan_confirmation(plan, symbol="X-USDT-SWAP", simulated=True)
    assert "入场价：1,000" in text
    assert "止损价：999" in text
    assert "止盈价：1,002" in text


def test_from_settings_reads_credentials_file(tmp_path):
    cred_path = tmp_path / "okx_trading.json"
    cred_path.write_text(
        json.dumps(
            {
                "api_key": "ak",
                "secret_key": "sk",
                "passphrase": "pp",
                "simulated": True,  # 文件里的值应被 settings 覆盖
            }
        ),
        encoding="utf-8",
    )
    trading = TradingSettings(enabled=True, simulated=False, credentials_path=str(cred_path))
    settings = SimpleNamespace(trading=trading)

    trader = OkxTrader.from_settings(settings, market_source=_FakeMarket())
    assert trader.credentials is not None
    assert trader.credentials.api_key == "ak"
    assert trader.credentials.simulated is False  # 以 settings 为准（实盘）
    assert "实盘" in trader.status_text()


def test_from_settings_without_credentials_file(tmp_path):
    trading = TradingSettings(enabled=True, credentials_path=str(tmp_path / "nope.json"))
    trader = OkxTrader.from_settings(SimpleNamespace(trading=trading), market_source=_FakeMarket())
    assert trader.credentials is None
    result = trader.execute(_decision(), symbol="BTC-USDT-SWAP", dry_run=False)
    assert result.sent is False and "凭据" in result.message


def test_live_execution_requires_acknowledgement(tmp_path):
    trading = TradingSettings(enabled=True, simulated=False, live_ack=False)
    trader = OkxTrader(
        trading,
        credentials=OkxCredentials("ak", "sk", "pp", simulated=False),
        client=_FakeClient(),
        market_source=_FakeMarket(),
    )
    blocked = trader.execute(_decision(), symbol="BTC-USDT-SWAP", dry_run=False)
    assert blocked.sent is False and "风险确认" in blocked.message

    trading.live_ack = True
    ok = trader.execute(_decision(), symbol="BTC-USDT-SWAP", dry_run=False, manual_confirm=True)
    assert ok.sent is True
