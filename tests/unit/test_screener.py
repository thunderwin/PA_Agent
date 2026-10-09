"""选币器单测：用构造出来的 K 线验证每个门槛都在干活（不联网）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pa_agent.orchestrator.screener import (
    Candidate,
    ScreenConfig,
    evaluate_bars,
    pick_symbols,
    rank_candidates,
    universe,
)

BAR_HOURS = 300


def _bars(volumes: list[float], *, price: float = 100.0, zeros: int = 0) -> list[SimpleNamespace]:
    """按"旧的在前"的顺序给量，返回 newest-first 的 K 线（对齐真实数据源的顺序）。"""
    values = [0.0] * zeros + list(volumes)
    bars = [
        SimpleNamespace(amount=v, close=price, closed=True)
        for v in values
    ]
    bars.reverse()
    return bars


def _series(baseline: float, window_volume: float, *, base_bars: int = BAR_HOURS - 6) -> list[float]:
    """前面全是 baseline，最后 6 根换成 window_volume。"""
    return [baseline] * base_bars + [window_volume] * 6


CFG = ScreenConfig()


# ── 基础门槛 ──────────────────────────────────────────────────────────────────


def test_flat_then_burst_is_a_candidate():
    """平常量 + 最后 6 根放量 8 倍 → 应该入选。"""
    bars = _bars(_series(100_000.0, 800_000.0))
    cand = evaluate_bars(bars, "ABC-USDT-SWAP", CFG)
    assert cand is not None and cand.ok, cand.reasons
    assert cand.ratio == pytest.approx(8.0, rel=0.05)
    assert cand.burst > CFG.min_burst
    assert cand.persist == 6
    assert cand.volume_24h_usd == pytest.approx(6.6e6, rel=0.02)


def test_burst_must_be_recent():
    """放量发生在 30 小时前（当前 6 根已回落）→ 不该入选。"""
    volumes = [100_000.0] * 24 + [900_000.0] * 6 + [100_000.0] * (BAR_HOURS - 30)
    cand = evaluate_bars(_bars(volumes), "OLD-USDT-SWAP", CFG)
    assert cand is not None and not cand.ok
    assert any("放大" in r or "突变" in r for r in cand.reasons)


def test_single_spike_bar_is_rejected():
    """一根尖峰（刷量/插针）不算：最后 6 根只有 1 根放量。"""
    volumes = [100_000.0] * (BAR_HOURS - 6) + [100_000.0] * 5 + [9_000_000.0]
    cand = evaluate_bars(_bars(volumes), "SPIKE-USDT-SWAP", CFG)
    assert cand is not None and not cand.ok
    assert any("根放量" in r for r in cand.reasons)


def test_already_hot_symbol_is_rejected():
    """已经连续放量好几天 → "之前就已在放量"，不是新机会。"""
    volumes = [100_000.0] * 120 + [900_000.0] * (BAR_HOURS - 120)
    cand = evaluate_bars(_bars(volumes), "HOT-USDT-SWAP", CFG)
    assert cand is not None and not cand.ok
    assert any("已在放量" in r or "放大" in r or "突变" in r for r in cand.reasons)


def test_tiny_absolute_volume_is_rejected():
    """放大倍数很漂亮但绝对量太小（1000 USDT/小时）→ 不该碰。"""
    bars = _bars(_series(1_000.0, 8_000.0))
    cand = evaluate_bars(bars, "TINY-USDT-SWAP", CFG)
    assert cand is not None and not cand.ok
    assert any("24h 成交额" in r for r in cand.reasons)


def test_gappy_symbol_returns_none():
    """有空档的 K 线（股票类合约）直接返回 None，不参与排名。"""
    assert evaluate_bars(_bars(_series(100_000.0, 800_000.0), zeros=60), "STOCK", CFG) is None


def test_not_enough_history_returns_none():
    assert evaluate_bars(_bars([100_000.0] * 100), "NEW-USDT-SWAP", CFG) is None


def test_wide_spread_is_rejected():
    cand = evaluate_bars(_bars(_series(100_000.0, 800_000.0)), "WIDE-USDT-SWAP", CFG,
                         spread_bp=45.0)
    assert cand is not None and not cand.ok
    assert any("点差" in r for r in cand.reasons)


def test_unclosed_bar_is_ignored():
    """正在走的那根不参与计算（否则量还没走完，判断会失真）。"""
    bars = _bars(_series(100_000.0, 800_000.0))
    bars[0].closed = False
    cand = evaluate_bars(bars, "LIVE-USDT-SWAP", CFG)
    assert cand is not None
    assert cand.persist >= 3


# ── 候选池与排序 ──────────────────────────────────────────────────────────────


class _FakeSource:
    """够用的假数据源：给一批品种各自一套 K 线。"""

    def __init__(self, data: dict[str, list[SimpleNamespace]], *, volumes=None,
                 spreads=None, info=None) -> None:
        self._data = data
        self._volumes = volumes or {}
        self._spreads = spreads or {}
        self._info = info or []
        self.subscribed: list[str] = []

    def subscribe(self, symbol, timeframe):
        self.subscribed.append(symbol)

    def latest_snapshot(self, n):
        return list(self._data[self.subscribed[-1]])

    def volumes_24h(self):
        return dict(self._volumes)

    def book_tickers(self):
        return dict(self._spreads)

    def exchange_info(self):
        return list(self._info)


def test_universe_drops_majors_and_stables():
    info = [
        {"baseAsset": "BTC", "quoteAsset": "USDT"},
        {"baseAsset": "USDC", "quoteAsset": "USDT"},
        {"baseAsset": "MERL", "quoteAsset": "USDT"},
        {"baseAsset": "SOL", "quoteAsset": "USDC"},
    ]
    src = _FakeSource({}, info=info)
    assert universe(src, CFG) == ["MERL-USDT-SWAP"]


def test_universe_respects_exclude():
    info = [{"baseAsset": "MERL", "quoteAsset": "USDT"},
            {"baseAsset": "AEVO", "quoteAsset": "USDT"}]
    src = _FakeSource({}, info=info)
    cfg = ScreenConfig(exclude=("MERL-USDT-SWAP",))
    assert universe(src, cfg) == ["AEVO-USDT-SWAP"]


def test_rank_prefilters_by_24h_volume():
    """粗筛：24h 成交额不够的不去拉 K 线（省一次请求）。"""
    good = _series(100_000.0, 800_000.0)
    data = {
        "BIG-USDT-SWAP": _bars(good),
        "SMALL-USDT-SWAP": _bars(good),
    }
    src = _FakeSource(data, volumes={"BIG-USDT-SWAP": 5e6, "SMALL-USDT-SWAP": 100_000.0})
    ranked = rank_candidates(src, CFG, symbols=list(data))
    assert [c.symbol for c in ranked] == ["BIG-USDT-SWAP"]


def test_rank_sorts_by_score():
    data = {
        "A-USDT-SWAP": _bars(_series(100_000.0, 400_000.0)),   # 放大 4 倍
        "B-USDT-SWAP": _bars(_series(100_000.0, 900_000.0)),   # 放大 9 倍
    }
    src = _FakeSource(data)
    ranked = rank_candidates(src, CFG, symbols=list(data), prefilter=False)
    assert [c.symbol for c in ranked] == ["B-USDT-SWAP", "A-USDT-SWAP"]


def _cand(symbol: str, score: float, returns: tuple[float, ...]) -> Candidate:
    return Candidate(
        symbol=symbol, burst=score, ratio=score, day_ratio=score, quiet=1.0,
        volume_window_usd=1e6, volume_24h_usd=1e7, baseline_6h_usd=1e5, persist=6,
        spread_bp=1.0, price=1.0, score=score, reasons=(), returns=returns,
    )


def test_pick_symbols_dedupes_correlated_picks():
    """两个走势高度同步的标的只留一个，顺延下一个不相关的。"""
    same = tuple(0.01 * ((-1) ** i) for i in range(72))
    other = tuple(0.01 * ((-1) ** (i // 2)) for i in range(72))
    ranked = [
        _cand("X-USDT-SWAP", 100.0, same),
        _cand("Y-USDT-SWAP", 90.0, same),          # 与 X 完全相关 → 跳过
        _cand("Z-USDT-SWAP", 80.0, other),
    ]
    picked = pick_symbols(_FakeSource({}), CFG, count=2, candidates=ranked)
    assert [c.symbol for c in picked] == ["X-USDT-SWAP", "Z-USDT-SWAP"]


def test_pick_symbols_skips_failing_candidates():
    bad = _cand("BAD-USDT-SWAP", 999.0, ())
    bad.reasons = ("放大不足",)
    good = _cand("GOOD-USDT-SWAP", 1.0, ())
    picked = pick_symbols(_FakeSource({}), CFG, count=2, candidates=[bad, good])
    assert [c.symbol for c in picked] == ["GOOD-USDT-SWAP"]


def test_pick_symbols_can_disable_correlation_check():
    same = tuple(0.01 * ((-1) ** i) for i in range(72))
    ranked = [_cand("X-USDT-SWAP", 2.0, same), _cand("Y-USDT-SWAP", 1.0, same)]
    cfg = ScreenConfig(max_correlation=0.0)
    picked = pick_symbols(_FakeSource({}), cfg, count=2, candidates=ranked)
    assert [c.symbol for c in picked] == ["X-USDT-SWAP", "Y-USDT-SWAP"]


# ── 与配置对接 ────────────────────────────────────────────────────────────────


def test_config_from_settings_reads_general_fields():
    from pa_agent.config.settings import GeneralSettings

    general = GeneralSettings(
        watch_dynamic_min_volume_usd=5e6,
        watch_dynamic_max_volume_usd=1e8,
        watch_dynamic_min_burst=9.0,
        watch_dynamic_exclude=["btc-usdt-swap", " MERL-USDT-SWAP "],
    )
    cfg = ScreenConfig.from_settings(general)
    assert cfg.min_volume_usd == 5e6
    assert cfg.max_volume_usd == 1e8
    assert cfg.min_burst == 9.0
    assert cfg.exclude == ("BTC-USDT-SWAP", "MERL-USDT-SWAP")   # 统一大写并去空格


def test_config_from_settings_defaults():
    cfg = ScreenConfig.from_settings(None)
    assert cfg == ScreenConfig()


def test_dynamic_defaults_are_off():
    """默认不开启动态选币：升级后行为不能变。"""
    from pa_agent.config.settings import GeneralSettings

    general = GeneralSettings()
    assert general.watch_dynamic_enabled is False
    assert general.watch_dynamic_count == 2
    assert general.watch_dynamic_keep_static is False
