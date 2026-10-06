"""多品种后台监控：对一组品种分别取 K 线并跑两阶段分析。

设计要点
--------

* 每个品种用**独立的数据源订阅**（轮到自己时 ``subscribe``），不会干扰主窗口图表。
* 只有「最新一根已收盘 K 线」发生变化时才真正调用大模型分析 —— 每个品种每分钟
  一次免费 REST 探活，新 K 线收盘才产生 token 消耗。
* 结果是一个扁平的 :class:`WatchResult`，方便直接渲染成一张表。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field as _field
from typing import Any

logger = logging.getLogger(__name__)

#: 可以拿去下单的决策类型
EXECUTABLE_ORDER_TYPES: tuple[str, ...] = ("限价单", "突破单", "市价单")


@dataclass(frozen=True)
class WatchTarget:
    """一个被监控的品种/周期。"""

    symbol: str
    timeframe: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.symbol, self.timeframe)


@dataclass
class WatchResult:
    """一个品种一轮监控的结果。"""

    symbol: str
    timeframe: str
    ts_ms: int
    ok: bool = False
    skipped: bool = False  # 没有新 K 线收盘，本轮跳过分析
    price: float | None = None
    closed_ts: int | None = None  # 最新已收盘 K 线的时间戳（用于判断是否有新 K 线）
    order_type: str = ""
    direction: str = ""
    confidence: int | None = None
    entry: float | None = None
    stop: float | None = None
    take_profit: float | None = None
    diagnosis: str = ""  # 周期位置 / 诊断摘要
    error: str = ""
    #: 阶段二内层决策原文（自动下单要用，与主流程同一份结构）
    decision: dict = _field(default_factory=dict)

    @property
    def has_order(self) -> bool:
        return self.order_type in EXECUTABLE_ORDER_TYPES


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def newest_closed_ts(
    bars: Sequence[Any], *, now_ms: int | None = None, timeframe: str = "", symbol: str = ""
) -> int | None:
    """返回最新一根**已收盘** K 线的 ts_open（没有则 None）。"""
    if not bars:
        return None
    from pa_agent.data.bar_close_wait import has_forming_bar_at_head

    forming = False
    try:
        forming = has_forming_bar_at_head(
            list(bars), timeframe or None, symbol=symbol or None, now_ms=now_ms
        )
    except Exception:
        forming = False
    bar = bars[1] if forming and len(bars) > 1 else bars[0]
    ts = getattr(bar, "ts_open", None)
    return int(ts) if ts else None


def analyze_target(
    target: WatchTarget,
    *,
    source: Any,
    make_orchestrator: Callable[[], Any],
    bar_count: int = 100,
    cancel_token: Any = None,
    on_event: Callable[[Any], None] | None = None,
    previous_closed_ts: int | None = None,
    now_ms: int | None = None,
) -> WatchResult:
    """取一轮数据；有新 K 线收盘才跑分析，否则返回 ``skipped=True``。"""
    from pa_agent.data.snapshot import INDICATOR_WARMUP_BARS, take_snapshot_from_bars

    result = WatchResult(
        symbol=target.symbol, timeframe=target.timeframe, ts_ms=int(time.time() * 1000)
    )
    if cancel_token is not None and cancel_token.is_set():
        result.error = "cancelled"
        return result

    try:
        source.subscribe(target.symbol, target.timeframe)
        bars = source.latest_snapshot(max(int(bar_count), 2) + INDICATOR_WARMUP_BARS + 5)
    except Exception as exc:
        result.error = f"取数失败：{exc}"
        logger.debug("watchlist fetch failed %s: %s", target.symbol, exc)
        return result

    closed_ts = newest_closed_ts(
        bars, now_ms=now_ms, timeframe=target.timeframe, symbol=target.symbol
    )
    result.closed_ts = closed_ts
    if bars:
        result.price = _as_float(getattr(bars[0], "close", None))

    if previous_closed_ts is not None and closed_ts is not None and closed_ts == previous_closed_ts:
        result.skipped = True
        return result

    try:
        frame = take_snapshot_from_bars(
            list(bars), int(bar_count), target.symbol, target.timeframe, now_ms=now_ms
        )
    except Exception as exc:
        result.error = f"数据不足：{exc}"
        return result

    orchestrator = make_orchestrator()
    if orchestrator is None:
        result.error = "编排器未就绪（检查 AI 模型设置）"
        return result

    from pa_agent.util.threading import CancelToken

    token = cancel_token or CancelToken()
    try:
        record = orchestrator.submit(frame, token, on_event or (lambda _e: None))
    except Exception as exc:
        result.error = f"分析失败：{exc}"
        logger.warning("watchlist analysis failed %s: %s", target.symbol, exc)
        return result

    exc_info = getattr(record, "exception", None)
    if exc_info:
        result.error = f"{exc_info.get('type') or 'error'}: {exc_info.get('message') or ''}".strip()
        return result

    stage2 = getattr(record, "stage2_decision", None) or {}
    inner = stage2.get("decision") if isinstance(stage2, dict) else None
    if not isinstance(inner, dict):
        result.error = "未获得决策内容"
        return result

    result.order_type = str(inner.get("order_type") or "")
    result.decision = dict(inner)
    result.direction = str(inner.get("order_direction") or "")
    result.confidence = _as_float(inner.get("trade_confidence"))
    result.entry = _as_float(inner.get("entry_price"))
    result.stop = _as_float(inner.get("stop_loss_price"))
    result.take_profit = _as_float(inner.get("take_profit_price"))
    diag = stage2.get("diagnosis_summary") if isinstance(stage2, dict) else None
    if isinstance(diag, dict):
        result.diagnosis = str(
            diag.get("cycle_position") or diag.get("market_phase") or diag.get("summary") or ""
        )
    elif getattr(record, "stage1_diagnosis", None):
        s1 = record.stage1_diagnosis or {}
        result.diagnosis = str(s1.get("cycle_position") or "")
    result.ok = True
    if result.price is None:
        result.price = result.entry
    return result


def run_watchlist(
    targets: Iterable[WatchTarget],
    *,
    source: Any,
    make_orchestrator: Callable[[], Any],
    bar_count: int = 100,
    cancel_token: Any = None,
    on_result: Callable[[WatchResult], None] | None = None,
    previous_closed: dict[tuple[str, str], int] | None = None,
    gap_s: float = 0.0,
) -> dict[tuple[str, str], int]:
    """按顺序把 *targets* 跑一遍；返回各品种最新已收盘 K 线时间戳。

    ``previous_closed`` 传上一轮的结果即可实现「只在新 K 线收盘时分析」。
    """
    state: dict[tuple[str, str], int] = dict(previous_closed or {})
    for target in targets:
        if cancel_token is not None and cancel_token.is_set():
            break
        result = analyze_target(
            target,
            source=source,
            make_orchestrator=make_orchestrator,
            bar_count=bar_count,
            cancel_token=cancel_token,
            previous_closed_ts=state.get(target.key),
        )
        if result.closed_ts is not None:
            state[target.key] = result.closed_ts
        if on_result is not None:
            try:
                on_result(result)
            except Exception as exc:
                logger.debug("watchlist on_result failed: %s", exc)
        if gap_s > 0:
            time.sleep(gap_s)
    return state
