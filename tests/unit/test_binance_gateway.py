"""币安网关单测（不联网、不下真单）。

覆盖四层：
1. 签名 / 精度格式化 —— 用币安官方文档的示例做固定向量
2. 品种写法换算 —— 规范写法 ↔ ``BTCUSDT``
3. HTTP 层 —— 拦截 ``_send_http``，检查 query 里的签名与字段翻译
4. 执行流程 —— 以损定量、成交后补挂止损、过期撤单、止损体检
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pa_agent.config.settings import TradingSettings
from pa_agent.data import factory as data_factory
from pa_agent.data.binance_source import (
    BinanceSource,
    normalize_binance_symbol,
    normalize_binance_timeframe,
    parse_binance_klines,
)
from pa_agent.trading import binance_trader as bt
from pa_agent.trading.binance_trader import (
    BinanceCredentials,
    BinancePrivateClient,
    BinanceTradeError,
    BinanceTrader,
    _decimals,
    _fmt_step,
    sign_query,
)
from pa_agent.trading.gateway import (
    create_trader,
    credentials_path_for,
    from_binance_symbol,
    normalize_venue,
    resolve_proxy,
    to_binance_symbol,
    to_canonical_symbol,
    to_venue_symbol,
    venue_label,
)

SECRET = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"


# ── 1. 签名与精度 ─────────────────────────────────────────────────────────────


def test_sign_query_matches_binance_documented_example():
    """币安官方文档的签名示例（secret + 限价单参数）→ 固定结果。"""
    query = (
        "symbol=BTCUSDT&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1"
        "&price=0.1&recvWindow=5000&timestamp=1591702613943"
    )
    assert sign_query(SECRET, query) == (
        "30f67158e3891f82fdc25717203b201261874f5e769676e16599d4fd0df60392"
    )


def test_sign_query_is_hex_hmac_sha256():
    assert len(sign_query("k", "a=1")) == 64
    assert sign_query("k", "a=1") != sign_query("k", "a=2")


def test_fmt_step_respects_instrument_precision():
    assert _decimals(0.001) == 3
    assert _decimals(1.0) == 0
    assert _fmt_step(1.23456, 0.001) == "1.234"     # 向下取整到步长
    assert _fmt_step(5.0, 0.001) == "5.000"
    assert _fmt_step(0.123456789, 0.00001) == "0.12345"


# ── 2. 品种写法 ───────────────────────────────────────────────────────────────


def test_canonical_to_binance_symbol():
    assert to_binance_symbol("BTC-USDT-SWAP") == "BTCUSDT"
    assert to_binance_symbol("1000PEPE-USDT-SWAP") == "1000PEPEUSDT"
    assert to_venue_symbol("MERL-USDT-SWAP", "binance") == "MERLUSDT"
    assert to_venue_symbol("MERL-USDT-SWAP", "okx") == "MERL-USDT-SWAP"


def test_binance_to_canonical_symbol():
    assert from_binance_symbol("BTCUSDT") == "BTC-USDT-SWAP"
    assert to_canonical_symbol("1000PEPEUSDT", "binance") == "1000PEPE-USDT-SWAP"


@pytest.mark.parametrize("bad", ["BTC-USD-SWAP", "BTC-USDC-SWAP", "XAUUSDm", ""])
def test_unsupported_symbol_rejected(bad):
    """只支持 USDT 本位永续；其他写法明确报错，不猜代码。"""
    from pa_agent.trading.gateway import TradeRejected

    with pytest.raises(TradeRejected):
        to_binance_symbol(bad)


def test_data_source_symbol_normalizer():
    assert normalize_binance_symbol("btcusdt") == "BTC-USDT-SWAP"
    assert normalize_binance_symbol("BTC/USDT") == "BTC-USDT-SWAP"
    assert normalize_binance_symbol("1000pepeusdt") == "1000PEPE-USDT-SWAP"
    assert normalize_binance_symbol("BTC-USDT-SWAP") == "BTC-USDT-SWAP"
    assert normalize_binance_symbol("BTCUSD_PERP") == ""     # 币本位不支持
    assert normalize_binance_symbol("") == ""


def test_timeframe_mapping():
    assert normalize_binance_timeframe("1h") == "1h"
    assert normalize_binance_timeframe("4h") == "4h"
    assert normalize_binance_timeframe("2m") == ""
    assert "1h" in BinanceSource().supported_timeframes()


def test_venue_helpers():
    assert normalize_venue("Binance") == "binance"
    assert normalize_venue("") == "okx"
    assert normalize_venue("不认识") == "okx"
    assert venue_label("binance") == "币安"
    path = credentials_path_for(SimpleNamespace(trading=TradingSettings()), "binance")
    assert path.endswith("binance_trading.json")


def test_credentials_path_keeps_legacy_okx_field():
    """老配置只写了 credentials_path（OKX 专用时代的字段），不能被忽略。"""
    trading = TradingSettings(credentials_path="config/my_okx.json")
    settings = SimpleNamespace(trading=trading)
    assert credentials_path_for(settings, "okx") == "config/my_okx.json"
    assert credentials_path_for(settings) == "config/my_okx.json"


def test_resolve_proxy_prefers_settings_then_env(monkeypatch):
    settings = SimpleNamespace(trading=TradingSettings(binance_proxy="http://127.0.0.1:9"))
    assert resolve_proxy("binance", settings) == "http://127.0.0.1:9"
    monkeypatch.setenv("PA_BINANCE_PROXY", "socks5h://127.0.0.1:8")
    empty = SimpleNamespace(trading=TradingSettings())
    assert resolve_proxy("binance", empty) == "socks5h://127.0.0.1:8"
    monkeypatch.delenv("PA_BINANCE_PROXY", raising=False)
    assert resolve_proxy("binance", empty) is None


# ── 3. HTTP 层（拦截 _send_http）───────────────────────────────────────────────


class _Recorder:
    """记录请求，并把预置响应按顺序吐回去。"""

    def __init__(self, responses: list[tuple[int, str]] | None = None) -> None:
        self.calls: list[dict] = []
        self.responses = list(responses or [])

    def __call__(self, method, url, headers, body, timeout, proxy=None):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "proxy": proxy}
        )
        if self.responses:
            return self.responses.pop(0)
        return 200, "{}"


def _client(monkeypatch, responses=None, **kw) -> tuple[BinancePrivateClient, _Recorder]:
    rec = _Recorder(responses)
    monkeypatch.setattr(bt, "_send_http", rec)
    cred = BinanceCredentials("KEY123456789", "SECRET", simulated=True)
    client = BinancePrivateClient(cred, base_url="https://example.invalid", **kw)
    return client, rec


def test_request_signs_query_with_timestamp_and_recvwindow(monkeypatch):
    client, rec = _client(
        monkeypatch,
        [
            (200, '{"serverTime": 1700000000000}'),      # sync_time
            (200, '{"dualSidePosition": false}'),        # 业务请求
        ],
    )
    client.dual_side_position()

    signed_call = rec.calls[-1]
    query = signed_call["url"].split("?", 1)[1]
    assert "timestamp=" in query and "recvWindow=5000" in query
    unsigned, signature = query.split("&signature=", 1)
    assert signature == sign_query("SECRET", unsigned)
    assert signed_call["headers"]["X-MBX-APIKEY"] == "KEY123456789"
    assert signed_call["method"] == "GET"


def test_testnet_base_url_is_used_for_simulated():
    cred = BinanceCredentials("k" * 12, "s", simulated=True)
    assert "testnet" in BinancePrivateClient(cred).base_url
    live = BinanceCredentials("k" * 12, "s", simulated=False)
    assert "fapi.binance.com" in BinancePrivateClient(live).base_url


def test_proxy_is_passed_to_http(monkeypatch):
    client, rec = _client(monkeypatch, [(200, '{"serverTime":1}'), (200, "[]")])
    client._proxy = "http://127.0.0.1:10809"
    client.pending_orders()
    assert rec.calls[-1]["proxy"] == "http://127.0.0.1:10809"


def test_error_payload_raises_with_code(monkeypatch):
    client, _ = _client(monkeypatch, [(200, '{"code":-1121,"msg":"Invalid symbol."}')])
    client._synced = True
    with pytest.raises(BinanceTradeError) as exc:
        client.pending_orders()
    assert "-1121" in str(exc.value) and "Invalid symbol" in str(exc.value)


def test_timestamp_error_resyncs_and_retries(monkeypatch):
    """-1021（时间戳过期）→ 重新对时后重试一次，第二次成功。"""
    client, rec = _client(
        monkeypatch,
        [
            (200, '{"code":-1021,"msg":"Timestamp outside of the recvWindow."}'),
            (200, '{"serverTime":1700000000000}'),       # 重试前重新对时
            (200, '{"dualSidePosition": false}'),
        ],
    )
    client._synced = True
    assert client.dual_side_position() is False
    assert len(rec.calls) == 3


def test_positions_and_orders_are_translated_to_okx_field_names(monkeypatch):
    positions = json.dumps(
        [
            {"symbol": "BTCUSDT", "positionAmt": "-0.64", "entryPrice": "83170",
             "unRealizedProfit": "8.39", "leverage": "10"},
            {"symbol": "ETHUSDT", "positionAmt": "0", "entryPrice": "0"},
        ]
    )
    orders = json.dumps(
        [
            {"symbol": "AAPLUSDT", "orderId": 3993098523218432000,
             "clientOrderId": "PAAGENT-E-1", "side": "BUY", "origQty": "5.86",
             "price": "340.78", "time": 1700000000000, "type": "LIMIT",
             "stopPrice": "0", "status": "NEW", "executedQty": "0"},
        ]
    )
    client, _ = _client(
        monkeypatch, [(200, '{"serverTime":1}'), (200, positions), (200, orders)]
    )
    rows = client.positions()
    assert len(rows) == 1                                 # 零仓位被过滤掉
    assert rows[0]["instId"] == "BTC-USDT-SWAP"
    assert rows[0]["pos"] == -0.64 and rows[0]["upl"] == 8.39

    pending = client.pending_orders()
    assert pending[0]["instId"] == "AAPL-USDT-SWAP"
    assert pending[0]["sz"] == 5.86 and pending[0]["px"] == 340.78
    assert pending[0]["cTime"] == 1700000000000
    assert pending[0]["tag"] == "PAAGENT"                 # 认得出是程序单
    assert str(pending[0]["ordId"]) == "3993098523218432000"


def test_manual_order_has_no_tag(monkeypatch):
    orders = json.dumps(
        [{"symbol": "BTCUSDT", "orderId": 1, "clientOrderId": "web_abc", "side": "SELL",
          "origQty": "1", "price": "1", "time": 1, "type": "LIMIT", "stopPrice": "0",
          "status": "NEW", "executedQty": "0"}]
    )
    client, _ = _client(monkeypatch, [(200, '{"serverTime":1}'), (200, orders)])
    assert client.pending_orders()[0]["tag"] == ""


def test_occupied_symbols_unions_positions_and_orders(monkeypatch):
    positions = json.dumps(
        [{"symbol": "BTCUSDT", "positionAmt": "0.5", "entryPrice": "1"}]
    )
    orders = json.dumps(
        [{"symbol": "XAUUSDT", "orderId": 1, "clientOrderId": "PAAGENT-E-1", "side": "BUY",
          "origQty": "1", "price": "1", "time": 1, "type": "LIMIT", "stopPrice": "0",
          "status": "NEW", "executedQty": "0"}]
    )
    client, _ = _client(
        monkeypatch, [(200, '{"serverTime":1}'), (200, positions), (200, orders)]
    )
    assert client.occupied_symbols() == {"BTC-USDT-SWAP", "XAU-USDT-SWAP"}


def test_realized_pnl_today_sums_three_income_types(monkeypatch):
    client, _ = _client(
        monkeypatch,
        [
            (200, '{"serverTime":1}'),
            (200, '[{"income":"-2.8"}]'),
            (200, '[{"income":"-1.4"}]'),
            (200, '[{"income":"0.2"}]'),
        ],
    )
    assert client.realized_pnl_today_usd() == pytest.approx(-4.0)


def test_stop_order_uses_close_position_then_falls_back(monkeypatch):
    """closePosition 失败（比如没仓位，币安回 -4509）→ 自动退回 reduceOnly + 数量。

    两者都必须走 **Algo Order API**：老接口会回 -4120。
    """
    ok = (
        '{"algoId":7,"clientAlgoId":"PAAGENT-S-1","symbol":"BTCUSDT","side":"SELL",'
        '"orderType":"STOP_MARKET","triggerPrice":"80000","closePosition":"true",'
        '"bookTime":1,"algoStatus":"NEW"}'
    )
    err = '{"code":-4509,"msg":"Time in Force (TIF) GTE can only be used with open positions."}'
    client, rec = _client(monkeypatch, [(200, '{"serverTime":1}'), (200, err), (200, ok)])
    row = client.place_stop(
        inst_id="BTC-USDT-SWAP", side="sell", stop_px=80000.0, size=0.5, tick=0.1, step=0.001
    )
    assert row["ordId"] == "7"
    first, second = rec.calls[-2]["url"], rec.calls[-1]["url"]
    assert "/fapi/v1/algoOrder" in first and "/fapi/v1/order" not in first
    assert "algoType=CONDITIONAL" in first
    assert "triggerPrice=80000" in first          # 字段名是 triggerPrice，不是 stopPrice
    assert "closePosition=true" in first
    assert "reduceOnly=true" in second and "quantity=0.500" in second


def test_algo_order_fields_are_translated(monkeypatch):
    """Algo 单的字段名（algoId/triggerPrice/clientAlgoId/bookTime）翻译成统一字段。"""
    rows = json.dumps([
        {"algoId": 123456, "clientAlgoId": "PAAGENT-S-9", "symbol": "SOLUSDT",
         "side": "SELL", "orderType": "STOP_MARKET", "triggerPrice": "108.72",
         "quantity": "0", "closePosition": "true", "bookTime": 1700000000000,
         "algoStatus": "NEW"},
        {"algoId": 999, "clientAlgoId": "manual-x", "symbol": "SOLUSDT", "side": "BUY",
         "orderType": "TAKE_PROFIT_MARKET", "triggerPrice": "120", "bookTime": 1,
         "algoStatus": "NEW"},
    ])
    client, _ = _client(monkeypatch, [(200, '{"serverTime":1}'), (200, rows)])
    out = client.algo_pending(inst_id="SOL-USDT-SWAP")
    assert out[0]["instId"] == "SOL-USDT-SWAP"
    assert out[0]["ordId"] == "123456"
    assert out[0]["stopPx"] == 108.72 and out[0]["slTriggerPx"] == 108.72
    assert out[0]["tag"] == "PAAGENT" and out[0]["type"] == "STOP_MARKET"
    assert out[0]["cTime"] == 1700000000000
    assert out[1]["tag"] == ""                     # 手动单不带标记


def test_cancel_all_orders_cancels_algo_orders_too(monkeypatch):
    """平仓时必须把 Algo 止损伤一起撤掉，否则会打掉下一次开的同品种仓位。"""
    algo = json.dumps([{"algoId": 5, "clientAlgoId": "PAAGENT-S-1", "symbol": "SOLUSDT",
                        "side": "SELL", "orderType": "STOP_MARKET", "triggerPrice": "1",
                        "bookTime": 1, "algoStatus": "NEW"}])
    client, rec = _client(
        monkeypatch,
        [(200, '{"serverTime":1}'),                    # sync_time
         (200, algo),                                  # 列条件单
         (200, '{"algoId":5,"clientAlgoId":"PAAGENT-S-1","symbol":"SOLUSDT"}'),  # 撤条件单
         (200, '{"code":200,"msg":"The operation of cancel all open order is done."}')],
    )
    client.cancel_all_orders("SOL-USDT-SWAP")
    urls = [c["url"] for c in rec.calls]
    assert any("DELETE" == c["method"] and "/fapi/v1/algoOrder" in c["url"] for c in rec.calls)
    assert any("/fapi/v1/allOpenOrders" in u for u in urls)


def test_code_200_is_treated_as_success(monkeypatch):
    """DELETE allOpenOrders 成功时返回 {"code":200,...}，不能当成错误。"""
    client, _ = _client(
        monkeypatch,
        [(200, '{"serverTime":1}'),
         (200, "[]"),                                  # 没有条件单
         (200, '{"code":200,"msg":"The operation of cancel all open order is done."}')],
    )
    client.cancel_all_orders("SOL-USDT-SWAP")       # 不应该抛异常


def test_equity_uses_account_endpoint(monkeypatch):
    """/fapi/v2/balance 没有 totalMarginBalance，必须用 /fapi/v2/account。"""
    acct = json.dumps({"totalMarginBalance": "1913.13", "totalWalletBalance": "1900",
                       "availableBalance": "1500"})
    client, rec = _client(monkeypatch, [(200, '{"serverTime":1}'), (200, acct)])
    assert client.equity_usd() == pytest.approx(1913.13)
    assert "/fapi/v2/account" in rec.calls[-1]["url"]


# ── 4. 执行流程 ───────────────────────────────────────────────────────────────


class _FakeClient:
    """替身客户端：只实现 BinanceTrader 用到的那几个方法。"""

    def __init__(
        self, *, equity=2000.0, positions=None, pending=None, pnl=0.0,
        dual=False, entry_status="NEW", entry_filled=0.0, algo=None,
    ) -> None:
        self._equity = equity
        self._positions = list(positions or [])
        self._pending = list(pending or [])
        self._algo = list(algo or [])
        self._pnl = pnl
        self._dual = dual
        self.entry_status = entry_status
        self.entry_filled = entry_filled
        self.placed_entries: list[dict] = []
        self.placed_stops: list[dict] = []
        self.leverage: list[tuple] = []
        self.cancelled: list[tuple] = []

    def equity_usd(self, ccy="USDT"):
        return self._equity

    def open_position_count(self, inst_type=None):
        return len(self._positions)

    def positions(self, inst_id=None, inst_type=None):
        return list(self._positions)

    def pending_orders(self, inst_id=None, inst_type=None):
        return list(self._pending)

    def algo_pending(self, inst_id=None, ord_type="oco"):
        return list(self._algo)

    def realized_pnl_today_usd(self, tz_offset_hours=8):
        return self._pnl

    def dual_side_position(self):
        return self._dual

    def exchange_info(self):
        return [
            {"symbol": "BTCUSDT", "baseAsset": "BTC", "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ]},
            {"symbol": "XYZUSDT", "baseAsset": "XYZ", "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ]},
        ]

    def set_leverage(self, inst_id, leverage, mgn_mode="cross"):
        self.leverage.append((inst_id, leverage, mgn_mode))

    def place_entry(self, **kw):
        self.placed_entries.append(kw)
        return {"ordId": "E1", "instId": "BTC-USDT-SWAP", "status": self.entry_status,
                "executedQty": self.entry_filled}

    def place_stop(self, **kw):
        self.placed_stops.append(kw)
        return {"ordId": "S1"}

    def order_status(self, inst_id, ord_id):
        return {"ordId": ord_id, "status": self.entry_status,
                "executedQty": self.entry_filled}

    def cancel_order(self, inst_id, ord_id):
        self.cancelled.append((inst_id, ord_id))
        return {"ordId": ord_id}


DECISION = {
    "order_type": "限价单",
    "order_direction": "做多",
    "entry_price": 85000.0,
    "stop_loss_price": 84000.0,        # 每单位风险 1000 USDT
    "take_profit_price": 86000.0,
    "trade_confidence": 75,
}


def _trader(monkeypatch, tmp_path, client: _FakeClient, **over) -> BinanceTrader:
    monkeypatch.setattr(bt, "_STOP_INTENT_PATH", tmp_path / "intents.json")
    base: dict = dict(
        enabled=True, simulated=True, trigger_mode="manual", venue="binance",
        max_loss_per_trade_usd=2.0, max_notional_usd=2000.0, leverage=10,
        min_confidence=50, max_open_positions=8,
    )
    base.update(over)
    trading = TradingSettings(**base)
    cred = BinanceCredentials("KEY123456789", "SECRET", simulated=True)
    return BinanceTrader(trading, credentials=cred, client=client)


def test_dry_run_sizes_by_stop_distance(monkeypatch, tmp_path):
    """以损定量：入场 85000 / 止损 84000 → 每张风险 1000，预算 2 USDT → 0.002 张。"""
    client = _FakeClient()
    trader = _trader(monkeypatch, tmp_path, client)
    res = trader.execute(DECISION, symbol="BTC-USDT-SWAP", dry_run=True)
    assert res.sent is False and res.dry_run is True
    assert res.plan is not None
    assert res.plan.size == pytest.approx(0.002)
    assert res.plan.risk_usd <= 2.0
    assert client.placed_entries == []                # 演练不发单


def test_notional_cap_shrinks_position(monkeypatch, tmp_path):
    """名义额上限 2000 USDT 在 85000 时只允许 0.023 张，风险随之变小。"""
    client = _FakeClient()
    trader = _trader(monkeypatch, tmp_path, client, max_loss_per_trade_usd=500.0)
    res = trader.execute(DECISION, symbol="BTC-USDT-SWAP", dry_run=True)
    assert res.plan is not None
    assert res.plan.notional_usd <= 2000.0
    assert res.plan.size == pytest.approx(0.023)


def test_disabled_blocks(monkeypatch, tmp_path):
    trader = _trader(monkeypatch, tmp_path, _FakeClient(), enabled=False)
    res = trader.execute(DECISION, symbol="BTC-USDT-SWAP", dry_run=True)
    assert res.sent is False and "下单开关未打开" in res.message


def test_low_confidence_blocks(monkeypatch, tmp_path):
    trader = _trader(monkeypatch, tmp_path, _FakeClient())
    res = trader.execute(
        {**DECISION, "trade_confidence": 20}, symbol="BTC-USDT-SWAP", dry_run=True
    )
    assert res.sent is False and "置信度" in res.message


def test_missing_stop_blocks(monkeypatch, tmp_path):
    trader = _trader(monkeypatch, tmp_path, _FakeClient())
    decision = {k: v for k, v in DECISION.items() if k != "stop_loss_price"}
    res = trader.execute(decision, symbol="BTC-USDT-SWAP", dry_run=True)
    assert res.sent is False and "止损价" in res.message


def test_hedge_mode_blocks(monkeypatch, tmp_path):
    client = _FakeClient(dual=True)
    trader = _trader(monkeypatch, tmp_path, client)
    res = trader.execute(DECISION, symbol="BTC-USDT-SWAP", dry_run=False)
    assert res.sent is False and "双向持仓" in res.message
    assert client.placed_entries == []


def test_existing_position_blocks(monkeypatch, tmp_path):
    client = _FakeClient(positions=[{"instId": "BTC-USDT-SWAP", "pos": 0.5}])
    trader = _trader(monkeypatch, tmp_path, client)
    res = trader.execute(DECISION, symbol="BTC-USDT-SWAP", dry_run=False)
    assert res.sent is False and "已有持仓" in res.message


def test_existing_pending_blocks(monkeypatch, tmp_path):
    client = _FakeClient(
        pending=[{"instId": "BTC-USDT-SWAP", "ordId": "1", "tag": "PAAGENT"}]
    )
    trader = _trader(monkeypatch, tmp_path, client)
    res = trader.execute(DECISION, symbol="BTC-USDT-SWAP", dry_run=False)
    assert res.sent is False and "未成交挂单" in res.message


def test_above_position_cap_blocks(monkeypatch, tmp_path):
    client = _FakeClient(
        positions=[{"instId": f"C{i}-USDT-SWAP", "pos": 1} for i in range(8)]
    )
    trader = _trader(monkeypatch, tmp_path, client)
    res = trader.execute(DECISION, symbol="BTC-USDT-SWAP", dry_run=False)
    assert res.sent is False and "上限" in res.message


def test_daily_loss_cap_blocks(monkeypatch, tmp_path):
    trader = _trader(monkeypatch, tmp_path, _FakeClient(pnl=-30.0))
    res = trader.execute(DECISION, symbol="BTC-USDT-SWAP", dry_run=False)
    assert res.sent is False and "当日已实现亏损" in res.message


def test_order_below_min_notional_is_rejected(monkeypatch, tmp_path):
    """4 USDT 的币、止损距离 2（风险 2/币）、预算 2 USDT → 1 币 = 4 USDT < 币安的 5 USDT 下限。"""
    client = _FakeClient()
    trader = _trader(monkeypatch, tmp_path, client)
    decision = {
        "order_type": "限价单", "order_direction": "做多", "entry_price": 4.0,
        "stop_loss_price": 2.0, "trade_confidence": 75,
    }
    res = trader.execute(decision, symbol="XYZ-USDT-SWAP", dry_run=False)
    assert res.sent is False and "最小名义额" in res.message
    assert client.placed_entries == []


def test_filled_entry_places_stop_immediately(monkeypatch, tmp_path):
    client = _FakeClient(entry_status="FILLED", entry_filled=0.002)
    trader = _trader(monkeypatch, tmp_path, client)
    res = trader.execute(DECISION, symbol="BTC-USDT-SWAP", dry_run=False)
    assert res.sent is True
    assert client.leverage and client.leverage[0][1] == 10
    assert len(client.placed_stops) == 1
    stop = client.placed_stops[0]
    assert stop["side"] == "sell"                     # 多单 → 止损是卖出
    assert stop["stop_px"] == 84000.0
    assert "止损已挂" in res.message


def test_unfilled_entry_defers_stop_and_records_intent(monkeypatch, tmp_path):
    client = _FakeClient(entry_status="NEW", entry_filled=0.0)
    trader = _trader(monkeypatch, tmp_path, client)
    res = trader.execute(DECISION, symbol="BTC-USDT-SWAP", dry_run=False)
    assert res.sent is True
    assert client.placed_stops == []                  # 还没成交，不挂止损
    saved = json.loads((tmp_path / "intents.json").read_text(encoding="utf-8"))
    assert saved["BTC-USDT-SWAP"]["stop_px"] == 84000.0
    assert saved["BTC-USDT-SWAP"]["filled"] is False
    assert "补挂" in res.message


def test_take_profit_only_placed_when_enabled(monkeypatch, tmp_path):
    client = _FakeClient(entry_status="FILLED", entry_filled=0.002)
    trader = _trader(monkeypatch, tmp_path, client)   # 默认 attach_take_profit=False
    trader.execute(DECISION, symbol="BTC-USDT-SWAP", dry_run=False)
    assert len(client.placed_stops) == 1              # 只有止损

    client2 = _FakeClient(entry_status="FILLED", entry_filled=0.002)
    trader2 = _trader(monkeypatch, tmp_path, client2, attach_take_profit=True)
    trader2.execute(DECISION, symbol="BTC-USDT-SWAP", dry_run=False)
    assert [s["kind"] for s in client2.placed_stops] == ["stop", "take_profit"]


# ── 止损体检 ──────────────────────────────────────────────────────────────────


def test_ensure_stops_places_missing_stop_from_intent(monkeypatch, tmp_path):
    (tmp_path / "intents.json").write_text(
        json.dumps({"BTC-USDT-SWAP": {"stop_px": 84000.0, "size": 0.002, "side": "buy"}}),
        encoding="utf-8",
    )
    client = _FakeClient(positions=[{"instId": "BTC-USDT-SWAP", "pos": 0.002}])
    trader = _trader(monkeypatch, tmp_path, client)
    notes = trader.ensure_stops("BTC-USDT-SWAP")
    assert client.placed_stops and client.placed_stops[0]["stop_px"] == 84000.0
    assert any("已补挂止损" in n for n in notes)


def test_ensure_stops_skips_when_stop_exists(monkeypatch, tmp_path):
    (tmp_path / "intents.json").write_text(
        json.dumps({"BTC-USDT-SWAP": {"stop_px": 84000.0}}), encoding="utf-8"
    )
    client = _FakeClient(
        positions=[{"instId": "BTC-USDT-SWAP", "pos": 0.002}],
        algo=[{"instId": "BTC-USDT-SWAP", "type": "STOP_MARKET", "tag": "PAAGENT",
               "ordId": "5"}],
    )
    trader = _trader(monkeypatch, tmp_path, client)
    assert trader.ensure_stops("BTC-USDT-SWAP") == []
    assert client.placed_stops == []


def test_ensure_stops_warns_without_intent(monkeypatch, tmp_path):
    client = _FakeClient(positions=[{"instId": "BTC-USDT-SWAP", "pos": 0.002}])
    trader = _trader(monkeypatch, tmp_path, client)
    notes = trader.ensure_stops("BTC-USDT-SWAP")
    assert any("没有止损" in n for n in notes)
    assert client.placed_stops == []


def test_ensure_stops_forgets_closed_position(monkeypatch, tmp_path):
    (tmp_path / "intents.json").write_text(
        json.dumps({"BTC-USDT-SWAP": {"stop_px": 84000.0}}), encoding="utf-8"
    )
    trader = _trader(monkeypatch, tmp_path, _FakeClient(positions=[]))
    trader.ensure_stops("BTC-USDT-SWAP")
    saved = json.loads((tmp_path / "intents.json").read_text(encoding="utf-8"))
    assert saved == {}


# ── 过期撤单 ──────────────────────────────────────────────────────────────────


def test_cancel_stale_only_touches_tagged_entries(monkeypatch, tmp_path):
    old = 1_000_000_000_000
    client = _FakeClient(pending=[
        {"instId": "BTC-USDT-SWAP", "ordId": "1", "tag": "PAAGENT", "type": "LIMIT",
         "cTime": old, "side": "buy", "sz": 1, "px": 1},
        {"instId": "BTC-USDT-SWAP", "ordId": "2", "tag": "", "type": "LIMIT",
         "cTime": old, "side": "buy", "sz": 1, "px": 1},        # 手动单 → 不碰
        {"instId": "BTC-USDT-SWAP", "ordId": "3", "tag": "PAAGENT", "type": "STOP_MARKET",
         "cTime": old, "side": "sell", "sz": 1, "px": 0},       # 止损单 → 不按入场单撤
    ])
    trader = _trader(monkeypatch, tmp_path, client)
    done = trader.cancel_stale_entries("BTC-USDT-SWAP", "1h", max_bars=2)
    assert [d["ordId"] for d in done] == ["1"]
    assert client.cancelled == [("BTC-USDT-SWAP", "1")]


def test_cancel_stale_keeps_fresh_orders(monkeypatch, tmp_path):
    import time as _t

    client = _FakeClient(pending=[{
        "instId": "BTC-USDT-SWAP", "ordId": "1", "tag": "PAAGENT", "type": "LIMIT",
        "cTime": int(_t.time() * 1000), "side": "buy", "sz": 1, "px": 1,
    }])
    trader = _trader(monkeypatch, tmp_path, client)
    assert trader.cancel_stale_entries("BTC-USDT-SWAP", "1h", max_bars=2) == []


def test_cancel_stale_noop_when_disabled(monkeypatch, tmp_path):
    trader = _trader(monkeypatch, tmp_path, _FakeClient())
    assert trader.cancel_stale_entries("BTC-USDT-SWAP", "1h", max_bars=0) == []


# ── 网关入口 / 数据源 ----------------------------------------------------------


def test_create_trader_picks_venue():
    okx = create_trader(SimpleNamespace(trading=TradingSettings(venue="okx")))
    assert okx.__class__.__name__ == "OkxTrader"
    bn = create_trader(SimpleNamespace(trading=TradingSettings(venue="binance")))
    assert isinstance(bn, BinanceTrader)
    assert bn.status_text().startswith("交易：关闭")     # enabled=False


def test_status_text_mentions_venue():
    trading = TradingSettings(enabled=True, venue="binance")
    trader = BinanceTrader(
        trading, credentials=BinanceCredentials("k" * 12, "s", simulated=False)
    )
    assert "币安" in trader.status_text() and "实盘" in trader.status_text()


def test_data_source_factory_knows_binance():
    src = data_factory.create_data_source("binance")
    assert isinstance(src, BinanceSource)
    assert data_factory.data_source_label("binance") == "币安"
    assert data_factory.default_symbol_for_kind("binance") == "BTC-USDT-SWAP"


# ── K 线解析 ──────────────────────────────────────────────────────────────────


def test_parse_binance_klines_newest_first_and_closed_flag():
    import time as _t

    now_ms = int(_t.time() * 1000)
    rows = [
        [1699999200000, "1", "2", "0.5", "1.5", "10", now_ms + 3_600_000, "15", 3],
        [1699995600000, "1", "2", "0.5", "1.2", "20", now_ms - 1000, "24", 4],
    ]
    bars = parse_binance_klines(rows, symbol="BTC-USDT-SWAP")
    assert [b.ts_open for b in bars] == [1699999200000, 1699995600000]   # 新 → 旧
    assert [b.seq for b in bars] == [1, 2]
    assert bars[0].volume == 10.0        # 标的币数量
    assert bars[0].amount == 15.0        # USDT 成交额
    assert bars[0].closed is False       # closeTime 在未来 → 还在走
    assert bars[1].closed is True


def test_subscribe_rejects_bad_symbol_and_timeframe():
    src = BinanceSource()
    with pytest.raises(ValueError):
        src.subscribe("BTCUSD_PERP", "1h")
    with pytest.raises(ValueError):
        src.subscribe("BTC-USDT-SWAP", "2m")
    src.subscribe("btcusdt", "1h")
    assert src._symbol == "BTC-USDT-SWAP" and src._timeframe == "1h"
