"""Unit tests for the OKX data source (no network)."""

from __future__ import annotations

import pytest

from pa_agent.data import okx_source
from pa_agent.data.base import DataSourceTransientError
from pa_agent.data.okx_source import (
    OKX_DEFAULT_SYMBOL,
    OkxApiError,
    OkxSource,
    is_derivative_symbol,
    normalize_okx_symbol,
    normalize_okx_timeframe,
    parse_okx_candles,
)

_HOUR_MS = 3_600_000
_TS0 = 1_700_000_000_000


def _row(
    ts: int,
    o=1.0,
    h=2.0,
    low=0.5,
    c=1.5,
    vol=10.0,
    vol_ccy=1.0,
    vol_quote=100.0,
    confirm: str = "1",
) -> list[str]:
    return [
        str(ts),
        str(o),
        str(h),
        str(low),
        str(c),
        str(vol),
        str(vol_ccy),
        str(vol_quote),
        confirm,
    ]


# ── 品种 / 周期 ───────────────────────────────────────────────────────────────


def test_normalize_symbol_shorthand_defaults_to_usdt_swap():
    assert normalize_okx_symbol("btc") == "BTC-USDT-SWAP"
    assert normalize_okx_symbol("BTCUSDT") == "BTC-USDT-SWAP"
    assert normalize_okx_symbol("  eth  ") == "ETH-USDT-SWAP"
    assert normalize_okx_symbol("1000PEPE") == "1000PEPE-USDT-SWAP"


def test_normalize_symbol_explicit_pair_is_spot():
    # 显式写出交易对 → 现货；加 -SWAP 才是永续
    assert normalize_okx_symbol("btc/usdt") == "BTC-USDT"
    assert normalize_okx_symbol("BTC_USDT") == "BTC-USDT"


def test_normalize_symbol_keeps_explicit_instrument_ids():
    assert normalize_okx_symbol("BTC-USDT") == "BTC-USDT"
    assert normalize_okx_symbol("eth-usdt-swap") == "ETH-USDT-SWAP"
    assert normalize_okx_symbol("BTC-USD-SWAP") == "BTC-USD-SWAP"


def test_normalize_symbol_rejects_non_okx_input():
    assert normalize_okx_symbol("") == ""
    assert normalize_okx_symbol("XAUUSDm") == ""
    assert normalize_okx_symbol("RB0 螺纹钢") == ""


def test_is_derivative_symbol():
    assert is_derivative_symbol("BTC-USDT-SWAP") is True
    assert is_derivative_symbol("BTC-USDT") is False
    assert is_derivative_symbol("") is False


def test_timeframe_mapping_uses_okx_bar_codes():
    assert normalize_okx_timeframe("15m") == "15m"
    assert normalize_okx_timeframe("1h") == "1H"
    assert normalize_okx_timeframe("4h") == "4H"
    assert normalize_okx_timeframe("1d") == "1D"
    assert normalize_okx_timeframe("1w") == "1W"
    assert normalize_okx_timeframe("7m") == ""


def test_supported_timeframes_include_gui_defaults():
    supported = OkxSource().supported_timeframes()
    for tf in ("1m", "5m", "15m", "1h", "4h", "1d"):
        assert tf in supported


# ── K 线解析 ──────────────────────────────────────────────────────────────────


def test_parse_candles_swap_volume_uses_volccy():
    bars = parse_okx_candles(
        [_row(_TS0, vol=127565.71, vol_ccy=1275.6571, vol_quote=108827068.0)],
        symbol="BTC-USDT-SWAP",
    )
    assert len(bars) == 1
    assert bars[0].volume == pytest.approx(1275.6571)  # 基础币（BTC）
    assert bars[0].amount == pytest.approx(108827068.0)  # 计价币（USDT）


def test_parse_candles_spot_volume_uses_vol():
    bars = parse_okx_candles(
        [_row(_TS0, vol=255.21308, vol_ccy=21795910.0, vol_quote=21795910.0)], symbol="BTC-USDT"
    )
    assert bars[0].volume == pytest.approx(255.21308)
    assert bars[0].amount == pytest.approx(21795910.0)


def test_parse_candles_flags_forming_bar_and_numbers_seq():
    rows = [
        _row(_TS0 + _HOUR_MS, confirm="0"),  # 正在走的那根
        _row(_TS0, confirm="1"),
        _row(_TS0 - _HOUR_MS, confirm="1"),
    ]
    bars = parse_okx_candles(rows, symbol="BTC-USDT-SWAP")
    assert [b.seq for b in bars] == [1, 2, 3]
    assert bars[0].closed is False
    assert bars[1].closed is True
    assert bars[0].ts_open > bars[1].ts_open > bars[2].ts_open


def test_parse_candles_sorts_newest_first_and_dedupes():
    rows = [_row(_TS0 - _HOUR_MS), _row(_TS0), _row(_TS0)]
    bars = parse_okx_candles(rows, symbol="BTC-USDT")
    assert [b.ts_open for b in bars] == [_TS0, _TS0 - _HOUR_MS]
    assert [b.seq for b in bars] == [1, 2]


def test_parse_candles_skips_malformed_rows():
    rows = [["bad"], _row(_TS0), ["1", "not-a-number", "1", "1", "1", "1", "1", "1", "1"]]
    bars = parse_okx_candles(rows, symbol="BTC-USDT")
    assert len(bars) == 1


# ── 快照 / 分页 / 缓存 ────────────────────────────────────────────────────────


def _make_source(monkeypatch, calls: list[tuple[str, dict]]) -> OkxSource:
    def fake_get(path, params=None, *, timeout=10.0):
        calls.append((path, dict(params or {})))
        if path == "/api/v5/market/candles":
            limit = int(params["limit"])
            return [_row(_TS0 - i * _HOUR_MS) for i in range(limit)]
        if path == "/api/v5/market/history-candles":
            after = int(params["after"])
            limit = int(params["limit"])
            return [_row(after - (i + 1) * _HOUR_MS) for i in range(limit)]
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(okx_source, "_http_get_json", fake_get)
    src = OkxSource()
    src.connect()
    src.subscribe("BTC-USDT-SWAP", "1h")
    return src


def test_latest_snapshot_paginates_beyond_single_request(monkeypatch):
    calls: list[tuple[str, dict]] = []
    src = _make_source(monkeypatch, calls)

    bars = src.latest_snapshot(400)

    assert len(bars) >= 405
    assert calls[0][0] == "/api/v5/market/candles"
    assert calls[0][1]["bar"] == "1H"
    assert calls[0][1]["limit"] == 300
    assert calls[1][0] == "/api/v5/market/history-candles"
    assert calls[1][1]["after"] == bars[299].ts_open
    ts = [b.ts_open for b in bars]
    assert ts == sorted(ts, reverse=True)
    assert len(set(ts)) == len(ts)


def test_latest_snapshot_caches_within_ttl(monkeypatch):
    calls: list[tuple[str, dict]] = []
    src = _make_source(monkeypatch, calls)

    first = src.latest_snapshot(100)
    second = src.latest_snapshot(100)

    assert len(calls) == 1
    assert [b.ts_open for b in second] == [b.ts_open for b in first]


def test_resubscribe_clears_cache(monkeypatch):
    calls: list[tuple[str, dict]] = []
    src = _make_source(monkeypatch, calls)

    src.latest_snapshot(100)
    src.subscribe("ETH-USDT-SWAP", "1h")
    bars = src.latest_snapshot(100)

    assert len(calls) == 2
    assert calls[1][1]["instId"] == "ETH-USDT-SWAP"
    assert bars


def test_subscribe_rejects_bad_symbol_and_timeframe():
    src = OkxSource()
    src.connect()
    with pytest.raises(ValueError):
        src.subscribe("XAUUSDm", "1h")
    with pytest.raises(ValueError):
        src.subscribe("BTC-USDT-SWAP", "7m")


def test_latest_snapshot_requires_connection_and_subscription():
    src = OkxSource()
    with pytest.raises(DataSourceTransientError):
        src.latest_snapshot(10)
    src.connect()
    with pytest.raises(DataSourceTransientError):
        src.latest_snapshot(10)


def test_api_error_is_wrapped(monkeypatch):
    def fake_get(path, params=None, *, timeout=10.0):
        raise OkxApiError("OKX 接口错误 51001: Instrument ID does not exist")

    monkeypatch.setattr(okx_source, "_http_get_json", fake_get)
    src = OkxSource()
    src.connect()
    src.subscribe("NOPE-USDT-SWAP", "1h")
    with pytest.raises(DataSourceTransientError) as excinfo:
        src.latest_snapshot(10)
    assert "NOPE-USDT-SWAP" in str(excinfo.value)


def test_empty_payload_raises_transient(monkeypatch):
    monkeypatch.setattr(okx_source, "_http_get_json", lambda *a, **k: [])
    src = OkxSource()
    src.connect()
    src.subscribe("BTC-USDT-SWAP", "1h")
    with pytest.raises(DataSourceTransientError):
        src.latest_snapshot(10)


# ── 品种发现 / 交易时间 ───────────────────────────────────────────────────────


def test_list_symbols_includes_default_and_is_offline():
    src = OkxSource()
    symbols = src.list_symbols()
    assert OKX_DEFAULT_SYMBOL in symbols
    assert "ETH-USDT-SWAP" in symbols


def test_fetch_liquid_symbols_sorts_by_quote_notional(monkeypatch):
    def fake_get(path, params=None, *, timeout=10.0):
        assert path == "/api/v5/market/tickers"
        return [
            {"instId": "SMALL-USDT-SWAP", "volCcy24h": "1000", "last": "1"},
            {"instId": "BTC-USDT-SWAP", "volCcy24h": "100", "last": "85000"},
            {"instId": "BTC-USD-SWAP", "volCcy24h": "999999", "last": "85000"},
            {"instId": "BTC-USDT", "volCcy24h": "500", "last": "85000"},
        ]

    monkeypatch.setattr(okx_source, "_http_get_json", fake_get)
    src = OkxSource()
    liquid = src.fetch_liquid_symbols(limit=5)
    assert liquid[:2] == ["BTC-USDT-SWAP", "SMALL-USDT-SWAP"]


def test_server_time_ms_caches(monkeypatch):
    calls: list[str] = []

    def fake_get(path, params=None, *, timeout=10.0):
        calls.append(path)
        return [{"ts": "1700000000000"}]

    monkeypatch.setattr(okx_source, "_http_get_json", fake_get)
    src = OkxSource()
    assert src.server_time_ms() == 1_700_000_000_000
    assert src.server_time_ms() == 1_700_000_000_000
    assert calls == ["/api/v5/public/time"]


def test_server_time_failure_is_non_fatal(monkeypatch):
    def fake_get(path, params=None, *, timeout=10.0):
        raise DataSourceTransientError("offline")

    monkeypatch.setattr(okx_source, "_http_get_json", fake_get)
    assert OkxSource().server_time_ms() is None
