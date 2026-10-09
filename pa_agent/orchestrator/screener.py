"""成交量突变选币 —— 给多品种监控喂"每小时轮换"的标的。

要解决的问题：固定盯一批品种，形态会越来越像（相关性高），机会也少。
这里每小时重算一次全市场，挑出**突然放量**的小币，换进监控列表。

判定口径（"昨天前天都是平常量，今天突然爆量，而且量本身不小"）：

- ``R6``：最近 6 根已收盘 1h K 线的 USDT 成交额之和（"现在有多热"）
- ``med`` / ``sigma``：过去 7 天所有 6 小时滚动窗口的中位数 / 1.4826×MAD
- ``burst = (R6 - med) / sigma``：突变强度。用中位数+MAD 而不是均值+标准差，
  因为信号本身就是"方差大"，用均值会把基准自己抬起来
- ``ratio = R6 / med``：放大几倍
- ``D0/D1/D2``：最近 / 前一个 / 再前一个 24 小时成交额（滚动）
- ``persist``：最近 6 根里超过基线 p90 的根数（一根尖峰＝刷量/插针，不算）

硬门槛：``D0`` 落在成交量区间内、``burst``/``ratio`` 够大、
``D0 ≥ day_ratio × max(D1, D2)``（今天确实爆了）、
``max(D1, D2) ≤ quiet_max × med_daily``（之前是平常量）、``R6`` 有绝对下限、
``persist`` 够多、点差够窄，且不是"有空档"的品种（股票类合约每天有整点不成交，
加密永续是 7×24）。

数据来源是 :class:`~pa_agent.data.base.DataSource`，所以币安和 OKX 都能用；
点差可选（数据源实现了 ``book_ticker`` 才检查）。

命令行自测：``uv run python -m pa_agent.orchestrator.screener``（默认币安）。
"""

from __future__ import annotations

import logging
import math
import statistics as st
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from pa_agent.data.base import DataSourceError

logger = logging.getLogger(__name__)

#: 大币与稳定币：按成交量区间过滤之外的兜底排除（可被 cfg.exclude 追加）。
_MAJOR_BASES: frozenset[str] = frozenset(
    {
        "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "TRX", "LTC", "LINK",
        "AVAX", "DOT", "MATIC", "SHIB", "UNI", "ATOM", "ETC", "FIL", "APT", "ARB",
        "OP", "SUI", "TON", "NEAR", "INJ", "TIA", "SEI", "BCH", "HBAR", "ICP",
    }
)
_STABLE_BASES: frozenset[str] = frozenset(
    {"USDC", "USDE", "USDT", "DAI", "FDUSD", "PYUSD", "TUSD", "USDD", "EUR", "EURI"}
)


@dataclass(frozen=True)
class ScreenConfig:
    """选币参数（默认值都是实盘校准过的，见 docs/动态选币说明.md）。"""

    window_hours: int = 6
    bars: int = 300
    min_volume_usd: float = 2_000_000.0
    max_volume_usd: float = 400_000_000.0
    min_burst: float = 6.0
    min_ratio: float = 3.0
    day_ratio: float = 2.5
    quiet_max: float = 2.0
    min_window_usd: float = 300_000.0
    min_persist: int = 3
    max_spread_bp: float = 20.0
    max_gap_ratio: float = 0.10
    max_correlation: float = 0.6
    exclude: tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, general: Any) -> "ScreenConfig":
        """按 ``settings.general`` 里的门槛字段构造（界面改配置不用改代码）。"""
        if general is None:
            return cls()
        return cls(
            min_volume_usd=float(
                getattr(general, "watch_dynamic_min_volume_usd", 2_000_000.0) or 2_000_000.0
            ),
            max_volume_usd=float(
                getattr(general, "watch_dynamic_max_volume_usd", 400_000_000.0)
                or 400_000_000.0
            ),
            min_burst=float(getattr(general, "watch_dynamic_min_burst", 6.0) or 6.0),
            exclude=tuple(
                str(s).strip().upper()
                for s in (getattr(general, "watch_dynamic_exclude", []) or [])
                if str(s).strip()
            ),
        )


@dataclass
class Candidate:
    """一个候选品种及其全部指标（便于打印/落盘复盘）。"""

    symbol: str
    burst: float
    ratio: float
    day_ratio: float
    quiet: float
    volume_window_usd: float
    volume_24h_usd: float
    baseline_6h_usd: float
    persist: int
    spread_bp: float
    price: float
    score: float = 0.0
    #: 未通过的硬门槛（空 = 合格）
    reasons: tuple[str, ...] = ()
    #: 最近 72 根 1h 对数收益（算相关性用）
    returns: tuple[float, ...] = field(default=(), repr=False)

    @property
    def ok(self) -> bool:
        return not self.reasons


def _base_of(symbol: str) -> str:
    return str(symbol or "").strip().upper().split("-")[0]


def _rolling_sums(values: list[float], window: int) -> list[float]:
    return [sum(values[i - window:i]) for i in range(window, len(values) + 1)]


def _median(values: Iterable[float], default: float = 0.0) -> float:
    seq = list(values)
    return st.median(seq) if seq else default


def _percentile(values: list[float], frac: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(frac * (len(ordered) - 1)))))
    return ordered[idx]


def _log_returns(prices: list[float]) -> tuple[float, ...]:
    out: list[float] = []
    for prev, cur in zip(prices, prices[1:], strict=False):
        out.append(math.log(cur / prev) if prev > 0 and cur > 0 else 0.0)
    return tuple(out)


def evaluate_bars(
    bars: list[Any], symbol: str, cfg: ScreenConfig, *, spread_bp: float = 0.0
) -> Candidate | None:
    """把 K 线算成候选指标。``bars`` 需 newest-first（含未收盘那根，会被忽略）。

    返回 ``None`` 表示数据不足或该品种根本不适合（例如有空档的股票类合约）。
    """
    closed = [b for b in bars if getattr(b, "closed", True)]
    closed.reverse()                       # 旧 → 新
    window = max(int(cfg.window_hours), 1)
    if len(closed) < 24 * 7 + window:
        return None

    volumes: list[float] = []
    prices: list[float] = []
    zeros = 0
    for bar in closed:
        amount = float(getattr(bar, "amount", 0.0) or 0.0)
        if amount <= 0:
            zeros += 1
        volumes.append(amount)
        prices.append(float(getattr(bar, "close", 0.0) or 0.0))

    if zeros / len(volumes) > cfg.max_gap_ratio:
        return None                        # 有空档 → 不是 7×24 的币

    now_bars = volumes[-window:]
    current = sum(now_bars)
    if current <= 0:
        return None

    windows = _rolling_sums(volumes[:-window], window)
    if len(windows) < 24 * 5:
        return None
    baseline = _median(windows)
    mad = _median([abs(w - baseline) for w in windows])
    sigma = max(1.4826 * mad, baseline * 0.05, 1.0)
    burst = (current - baseline) / sigma
    ratio = current / max(baseline, 1.0)

    d0 = sum(volumes[-24:])
    d1 = sum(volumes[-48:-24])
    d2 = sum(volumes[-72:-48])
    daily = _rolling_sums(volumes, 24)[:-1] or [d0]
    med_daily = _median(daily, d0)

    baseline_hourly = [v for v in volumes[-24 * 4:-window] if v > 0]
    p90 = _percentile(baseline_hourly, 0.9)
    persist = sum(1 for v in now_bars if v > p90)

    price = prices[-1] if prices else 0.0
    score = burst + 3.0 * math.log10(max(ratio, 1.0)) + 2.0 * math.log10(max(d0 / 1e6, 1.0))

    reasons: list[str] = []
    if not (cfg.min_volume_usd <= d0 <= cfg.max_volume_usd):
        reasons.append(
            f"24h 成交额 {d0/1e6:.1f}M 不在 {cfg.min_volume_usd/1e6:.0f}M~"
            f"{cfg.max_volume_usd/1e6:.0f}M 区间"
        )
    if burst < cfg.min_burst:
        reasons.append(f"突变强度 {burst:.1f} < {cfg.min_burst}")
    if ratio < cfg.min_ratio:
        reasons.append(f"放大 {ratio:.1f}x < {cfg.min_ratio}x")
    if d0 < cfg.day_ratio * max(d1, d2):
        reasons.append(f"今天/昨天只有 {d0/max(d1,1):.1f}x")
    if max(d1, d2) > cfg.quiet_max * med_daily:
        reasons.append("之前就已在放量")
    if current < cfg.min_window_usd:
        reasons.append(f"最近 {window}h 只有 {current/1e4:.0f} 万 USDT")
    if persist < cfg.min_persist:
        reasons.append(f"最近 {window} 根只 {persist} 根放量 < {cfg.min_persist}")
    if spread_bp > 0 and cfg.max_spread_bp > 0 and spread_bp > cfg.max_spread_bp:
        reasons.append(f"点差 {spread_bp:.1f}bp > {cfg.max_spread_bp}bp")

    return Candidate(
        symbol=symbol,
        burst=burst,
        ratio=ratio,
        day_ratio=d0 / max(d1, d2, 1.0),
        quiet=max(d1, d2) / max(med_daily, 1.0),
        volume_window_usd=current,
        volume_24h_usd=d0,
        baseline_6h_usd=baseline,
        persist=persist,
        spread_bp=spread_bp,
        price=price,
        score=score,
        reasons=tuple(reasons),
        returns=_log_returns(prices[-73:]),
    )


def _correlation(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    n = min(len(a), len(b))
    if n < 10:
        return 0.0
    xs, ys = a[-n:], b[-n:]
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=False))
    vx = sum((x - mx) ** 2 for x in xs) ** 0.5
    vy = sum((y - my) ** 2 for y in ys) ** 0.5
    if vx <= 0 or vy <= 0:
        return 0.0
    return cov / (vx * vy)


def _spread_bp(source: Any, symbol: str) -> float:
    """数据源实现 ``book_ticker`` 时才有点差，否则返回 0（= 不检查）。"""
    hook = getattr(source, "book_ticker", None)
    if hook is None:
        return 0.0
    try:
        bid, ask = hook(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.debug("取 %s 盘口失败: %s", symbol, exc)
        return 0.0
    if bid <= 0 or ask <= 0 or ask < bid:
        return 0.0
    return (ask - bid) / ((ask + bid) / 2.0) * 1e4


def universe(source: Any, cfg: ScreenConfig) -> list[str]:
    """候选池：币安走 exchangeInfo，OKX 走成交额榜（都只取 USDT 永续）。"""
    symbols: list[str] = []
    info = getattr(source, "exchange_info", None)
    if callable(info):
        try:
            symbols = [
                f"{str(row.get('baseAsset') or '').upper()}-USDT-SWAP"
                for row in info()
                if str(row.get("quoteAsset", "")).upper() == "USDT"
            ]
        except DataSourceError as exc:
            logger.warning("拉取合约列表失败: %s", exc)
    if not symbols:
        liquid = getattr(source, "fetch_liquid_symbols", None)
        if callable(liquid):
            try:
                symbols = list(liquid(limit=200))
            except DataSourceError as exc:
                logger.warning("拉取高流动性品种失败: %s", exc)
    skip = {str(s).strip().upper() for s in cfg.exclude}
    seen: set[str] = set()
    out: list[str] = []
    for symbol in symbols:
        base = _base_of(symbol)
        if not base or base in _MAJOR_BASES or base in _STABLE_BASES:
            continue
        if symbol in seen or symbol in skip:
            continue
        seen.add(symbol)
        out.append(symbol)
    return out


def screen_symbol(
    source: Any,
    symbol: str,
    cfg: ScreenConfig,
    *,
    sleep_s: float = 0.0,
    spread_bp: float | None = None,
) -> Candidate | None:
    """评估单个品种（内部会 subscribe + 取数）。"""
    try:
        source.subscribe(symbol, "1h")
        bars = source.latest_snapshot(cfg.bars)
    except DataSourceError as exc:
        logger.debug("取 %s 数据失败: %s", symbol, exc)
        return None
    finally:
        if sleep_s > 0:
            time.sleep(sleep_s)
    spread = _spread_bp(source, symbol) if spread_bp is None else spread_bp
    return evaluate_bars(bars, symbol, cfg, spread_bp=spread)


def rank_candidates(
    source: Any,
    cfg: ScreenConfig | None = None,
    *,
    symbols: Iterable[str] | None = None,
    exclude: Iterable[str] = (),
    limit: int | None = None,
    prefilter: bool = True,
    on_progress: Any = None,
) -> list[Candidate]:
    """把候选池全部评估一遍，返回**按分数排序**的候选（含未达标的，方便复盘）。"""
    cfg = cfg or ScreenConfig()
    skip = {str(s).strip().upper() for s in exclude}
    pool = [
        s for s in (symbols if symbols is not None else universe(source, cfg))
        if str(s).strip().upper() not in skip
    ]
    # 粗筛：24h 成交额已经不够的品种不可能通过门槛，先剔除省掉一次 K 线请求。
    volumes = None
    if prefilter:
        hook = getattr(source, "volumes_24h", None)
        if callable(hook):
            try:
                volumes = hook()
            except DataSourceError as exc:
                logger.warning("拉取 24h 成交额失败，跳过粗筛: %s", exc)
    if volumes:
        floor = cfg.min_volume_usd * 0.8      # 留点余量：官方 24h 与滚动 24h 略有差
        before = len(pool)
        pool = [s for s in pool if volumes.get(s, 0.0) >= floor]
        logger.info("选币粗筛：%d → %d 个品种", before, len(pool))

    # 点差也一次性拿全（逐币查盘口会让扫描慢一倍）
    spread_table: dict[str, float] | None = None
    if cfg.max_spread_bp > 0:
        hook = getattr(source, "book_tickers", None)
        if callable(hook):
            try:
                spread_table = hook()
            except DataSourceError as exc:
                logger.warning("拉取全市场盘口失败，跳过点差过滤: %s", exc)
    if limit:
        pool = pool[: max(int(limit), 1)]

    out: list[Candidate] = []
    for idx, symbol in enumerate(pool, 1):
        spread = None if spread_table is None else spread_table.get(symbol, 0.0)
        cand = screen_symbol(source, symbol, cfg, sleep_s=0.05, spread_bp=spread)
        if cand is not None:
            out.append(cand)
        if callable(on_progress):
            on_progress(idx, len(pool), symbol)
    out.sort(key=lambda c: (-c.score, c.symbol))
    return out


def pick_symbols(
    source: Any,
    cfg: ScreenConfig | None = None,
    *,
    count: int = 2,
    exclude: Iterable[str] = (),
    candidates: list[Candidate] | None = None,
) -> list[Candidate]:
    """挑出 ``count`` 个达标标的：按分数取，并做**相关性去重**。"""
    cfg = cfg or ScreenConfig()
    ranked = (
        candidates
        if candidates is not None
        else rank_candidates(source, cfg, exclude=exclude)
    )
    picked: list[Candidate] = []
    for cand in ranked:
        if not cand.ok:
            continue
        if cfg.max_correlation > 0 and any(
            abs(_correlation(cand.returns, prev.returns)) >= cfg.max_correlation
            for prev in picked
        ):
            logger.info("跳过 %s：与已选标的相关系数过高", cand.symbol)
            continue
        picked.append(cand)
        if len(picked) >= max(int(count), 1):
            break
    return picked


def _cli() -> int:  # pragma: no cover - 手动跑用
    import argparse

    from pa_agent.data.factory import create_data_source

    parser = argparse.ArgumentParser(description="成交量突变选币（只读公开行情）")
    parser.add_argument("--venue", default="binance", choices=("binance", "okx"))
    parser.add_argument("--count", type=int, default=2)
    parser.add_argument("--limit", type=int, default=0, help="只评估前 N 个（调试用）")
    args = parser.parse_args()

    source = create_data_source(args.venue)
    source.connect()
    cfg = ScreenConfig()
    print(f"候选池：{len(universe(source, cfg))} 个（{args.venue}）")
    ranked = rank_candidates(
        source, cfg, limit=args.limit or None,
        on_progress=lambda i, n, s: print(f"  ...{i}/{n} {s}      ", end="\r"),
    )
    print()
    hits = [c for c in ranked if c.ok]
    print(f"达标 {len(hits)} 个 / 评估 {len(ranked)} 个\n")
    print(f"{'品种':<20}{'burst':>7}{'放大':>7}{'今天/昨天':>10}{'6h(万)':>9}"
          f"{'24h(百万)':>11}{'点差bp':>8}{'持续':>5}")
    for c in hits[:15]:
        print(f"{c.symbol:<20}{c.burst:>7.1f}{c.ratio:>7.1f}{c.day_ratio:>10.1f}"
              f"{c.volume_window_usd/1e4:>9.0f}{c.volume_24h_usd/1e6:>11.1f}"
              f"{c.spread_bp:>8.1f}{c.persist:>5}")
    chosen = pick_symbols(source, cfg, count=args.count, candidates=ranked)
    print("\n>>> 本轮要盯的：", [c.symbol for c in chosen] or "（无）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
