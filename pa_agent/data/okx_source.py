"""OKX（欧易）K 线数据源 —— 公开 REST v5，无需 API Key。

只使用公开行情接口，不含任何下单接口：

* ``GET /api/v5/market/candles``          最近 300 根 K 线
* ``GET /api/v5/market/history-candles``  更早的历史（分页，每页 100 根）
* ``GET /api/v5/public/instruments``      可交易合约列表（校验品种）
* ``GET /api/v5/market/tickers``          24h 成交额（按流动性排序）
* ``GET /api/v5/public/time``             交易所时间（「等待 K 线收盘」倒计时用）

品种写法沿用 OKX ``instId``::

    BTC-USDT-SWAP   比特币 USDT 永续合约（默认，本交易所流动性最好）
    ETH-USDT-SWAP   以太坊 USDT 永续合约
    BTC-USDT        比特币现货

简写（``BTC`` / ``BTCUSDT`` / ``btc/usdt``）按 **USDT 永续** 解析；
带 ``-`` 的输入按原样使用，``BTC-USDT`` 即现货。

字段映射（OKX 每行是 ``[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]``）::

    volume = 基础币成交量（永续取 volCcy，现货取 vol）
    amount = 计价币成交额（volCcyQuote，统一是 USDT 口径）
    closed = confirm == "1"（"0" 表示这根还在走）
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Any

from pa_agent.data.base import (
    DataSource,
    DataSourceError,
    DataSourceTransientError,
    KlineBar,
    normalize_kline_bar,
)

logger = logging.getLogger(__name__)

#: 可通过环境变量指向备用域名（部分地区 www.okx.com 不可达，可用 aws.okx.com）。
OKX_BASE_URL: str = os.environ.get("OKX_BASE_URL", "https://www.okx.com").rstrip("/")

#: 默认品种：USDT 永续（OKX 成交额榜首）。
OKX_DEFAULT_SYMBOL = "BTC-USDT-SWAP"

# 应用内周期 → OKX bar 代码（OKX 的 H/D/W/M 必须大写）
_TF_TO_BAR: dict[str, str] = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1H",
    "2h": "2H",
    "4h": "4H",
    "6h": "6H",
    "12h": "12H",
    "1d": "1D",
    "1w": "1W",
    "1M": "1M",
}

_DERIVATIVE_SUFFIXES = ("SWAP", "FUTURES")
_QUOTE_CCYS: tuple[str, ...] = ("USDT", "USDC", "USD", "EUR", "BTC", "ETH")

#: 裸币种里长度 > 6 的常见币（其余裸币种按 ≤6 位字母数字判断）。
_KNOWN_BASES: frozenset[str] = frozenset(
    {
        "1000BONK",
        "1000FLOKI",
        "1000PEPE",
        "1000RATS",
        "1000SATS",
        "1MBABYDOGE",
        "AVAX",
        "MATIC",
        "PEPE",
        "PAXG",
        "PENGU",
        "RENDER",
        "SHIB",
        "XAUT",
    }
)

#: 界面下拉框预置品种：先永续（主力流动性），后现货。
_PRESET_SYMBOLS: tuple[str, ...] = (
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "XRP-USDT-SWAP",
    "DOGE-USDT-SWAP",
    "BNB-USDT-SWAP",
    "SUI-USDT-SWAP",
    "BTC-USDT",
    "ETH-USDT",
    "SOL-USDT",
    "XRP-USDT",
    "DOGE-USDT",
)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_MAX_CANDLES_LIMIT = 300  # /market/candles 单次上限
_MAX_HISTORY_LIMIT = 100  # /market/history-candles 单次上限
_SNAPSHOT_CACHE_TTL_S = 1.0
_INSTRUMENTS_CACHE_TTL_S = 600.0
_TICKERS_CACHE_TTL_S = 60.0
_SERVER_TIME_CACHE_TTL_S = 30.0
_DEFAULT_TIMEOUT_S = 10.0


class OkxApiError(DataSourceError):
    """OKX 返回 ``code != "0"``（参数错误 / 品种不存在 / 被限频）。"""


# ── 品种与周期 ────────────────────────────────────────────────────────────────


def is_derivative_symbol(symbol: str) -> bool:
    """True for 永续/交割合约（``BTC-USDT-SWAP``），False for 现货（``BTC-USDT``）。"""
    parts = [p for p in str(symbol or "").upper().split("-") if p]
    return len(parts) >= 3 and parts[-1] in _DERIVATIVE_SUFFIXES


def normalize_okx_symbol(raw: str) -> str:
    """把用户输入整理成 OKX ``instId``；无法识别时返回空串。

    >>> normalize_okx_symbol("btc")
    'BTC-USDT-SWAP'
    >>> normalize_okx_symbol("BTCUSDT")
    'BTC-USDT-SWAP'
    >>> normalize_okx_symbol("btc/usdt")     # 显式交易对 → 现货
    'BTC-USDT'
    >>> normalize_okx_symbol("BTC-USDT")
    'BTC-USDT'
    >>> normalize_okx_symbol("ETH-USDT-SWAP")
    'ETH-USDT-SWAP'
    >>> normalize_okx_symbol("XAUUSDm")
    ''
    """
    s = str(raw or "").strip().upper()
    for sep in ("/", "_", " ", ":"):
        s = s.replace(sep, "-")
    s = "-".join(p for p in s.split("-") if p)
    if not s:
        return ""

    if "-" in s:
        parts = s.split("-")
        if not all(p.isascii() and p.isalnum() for p in parts):
            return ""
        if len(parts) == 2:
            return f"{parts[0]}-{parts[1]}"  # 现货
        if len(parts) == 3 and parts[2] in _DERIVATIVE_SUFFIXES:
            return f"{parts[0]}-{parts[1]}-{parts[2]}"  # 永续 / 交割
        return ""

    for quote in _QUOTE_CCYS:
        if s.endswith(quote) and len(s) > len(quote):
            base = s[: -len(quote)]
            if 1 < len(base) <= 10:
                return f"{base}-{quote}-SWAP"
    # 裸币种：BTC / ETH / DOGE（含 1000PEPE 这类长名）
    if s.isascii() and s.isalnum() and any(ch.isalpha() for ch in s):
        if s in _KNOWN_BASES:
            return f"{s}-USDT-SWAP"
        if 1 < len(s) <= 6:
            return f"{s}-USDT-SWAP"
    return ""


def normalize_okx_timeframe(timeframe: str) -> str:
    """应用周期 → OKX bar 代码；不支持时返回空串。"""
    return _TF_TO_BAR.get(str(timeframe or "").strip(), "")


# ── HTTP ──────────────────────────────────────────────────────────────────────


def _http_get_json(
    path: str, params: dict[str, Any] | None = None, *, timeout: float = _DEFAULT_TIMEOUT_S
) -> Any:
    """GET 一个 OKX 公开接口并返回 ``data`` 字段。

    网络/HTTP 问题抛 :class:`DataSourceTransientError`（可重试），
    业务错误码抛 :class:`OkxApiError`。
    """
    url = f"{OKX_BASE_URL}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}

    payload: Any
    try:
        import requests  # type: ignore
    except ImportError:  # pragma: no cover
        requests = None  # type: ignore[assignment]

    if requests is not None:
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
        except Exception as exc:
            raise DataSourceTransientError(f"OKX 网络请求失败: {exc}") from exc
        if resp.status_code != 200:
            raise DataSourceTransientError(f"OKX HTTP {resp.status_code}（{path}）")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise DataSourceTransientError("OKX 返回内容不是 JSON") from exc
    else:
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except Exception as exc:
            raise DataSourceTransientError(f"OKX 网络请求失败: {exc}") from exc
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise DataSourceTransientError("OKX 返回内容不是 JSON") from exc

    if not isinstance(payload, dict):
        raise DataSourceTransientError("OKX 返回格式异常")
    code = str(payload.get("code", ""))
    if code != "0":
        msg = payload.get("msg") or ""
        raise OkxApiError(f"OKX 接口错误 {code}: {msg}（{path}）")
    return payload.get("data")


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ── K 线解析 ──────────────────────────────────────────────────────────────────


def parse_okx_candles(rows: Iterable[Sequence[Any]], *, symbol: str = "") -> list[KlineBar]:
    """把 OKX K 线原始行解析成 newest-first 的 :class:`KlineBar` 列表。

    OKX 每行 ``[ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]``，
    返回顺序本身就是「新 → 旧」，这里再按 ``ts_open`` 排一次并重编号 seq。
    """
    derivative = is_derivative_symbol(symbol)
    bars: list[KlineBar] = []
    seen: set[int] = set()

    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 9:
            continue
        try:
            ts_open = int(float(row[0]))
            open_, high, low, close = (float(row[i]) for i in (1, 2, 3, 4))
        except (TypeError, ValueError):
            continue
        if ts_open <= 0 or ts_open in seen:
            continue
        seen.add(ts_open)

        vol = _to_float(row[5])
        vol_ccy = _to_float(row[6])
        vol_quote = _to_float(row[7])
        # 基础币成交量：永续的 vol 是张数，用 volCcy；现货 vol 已经是基础币。
        volume = vol_ccy if derivative else vol

        bars.append(
            normalize_kline_bar(
                KlineBar(
                    seq=0,
                    ts_open=ts_open,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    amount=vol_quote,
                    closed=str(row[8]).strip() == "1",
                )
            )
        )

    bars.sort(key=lambda b: b.ts_open, reverse=True)
    return [replace(bar, seq=i + 1) for i, bar in enumerate(bars)]


# ── DataSource 实现 ───────────────────────────────────────────────────────────


class OkxSource(DataSource):
    """OKX 公开行情数据源（轮询 REST，无 API Key）。

    适合 BTC/ETH 永续与现货；下单能力不在本模块（见 ``pa_agent/trading``）。
    """

    def __init__(self, *, base_url: str | None = None) -> None:
        self._base_url = (base_url or OKX_BASE_URL).rstrip("/")
        self._symbol: str = ""
        self._timeframe: str = ""
        self._connected: bool = False
        self._snap_cache_n: int = 0
        self._snap_cache_ts: float = 0.0
        self._snap_cache_bars: list[KlineBar] = []
        self._instruments: dict[str, list[dict[str, Any]]] = {}
        self._instruments_ts: dict[str, float] = {}
        self._tickers: dict[str, list[dict[str, Any]]] = {}
        self._tickers_ts: dict[str, float] = {}
        self._server_time_cache_ms: int = 0
        self._server_time_cache_ts: float = 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """公开行情无需鉴权：这里只做状态标记，第一次取数时才真正发请求。"""
        self._connected = True
        logger.info("OkxSource connected (public REST, base=%s)", self._base_url)

    def disconnect(self) -> None:
        self._connected = False
        self._snap_cache_bars = []
        self._snap_cache_n = 0
        logger.info("OkxSource disconnected")

    # ── Discovery ─────────────────────────────────────────────────────────────

    def list_symbols(self) -> list[str]:
        """预置品种（纯本地，绝不阻塞 UI；需要全量用 :meth:`fetch_liquid_symbols`）。"""
        return list(_PRESET_SYMBOLS)

    def supported_timeframes(self) -> list[str]:
        return list(_TF_TO_BAR.keys())

    def list_instruments(
        self, inst_type: str = "SWAP", *, refresh: bool = False
    ) -> list[dict[str, Any]]:
        """按类型拉取可交易合约（带 10 分钟缓存）。"""
        key = str(inst_type or "SWAP").upper()
        now = time.monotonic()
        if (
            not refresh
            and key in self._instruments
            and now - self._instruments_ts.get(key, 0.0) < _INSTRUMENTS_CACHE_TTL_S
        ):
            return list(self._instruments[key])

        data = _http_get_json("/api/v5/public/instruments", {"instType": key})
        rows = [r for r in (data or []) if isinstance(r, dict) and r.get("instId")]
        self._instruments[key] = rows
        self._instruments_ts[key] = now
        return list(rows)

    def fetch_liquid_symbols(
        self,
        *,
        limit: int = 30,
        inst_type: str = "SWAP",
        quote: str = "USDT",
        refresh: bool = False,
    ) -> list[str]:
        """按 24h 成交额（名义额）从高到低返回高流动性品种。

        需要网络，请勿在 UI 线程同步调用。
        """
        itype = str(inst_type or "SWAP").upper()
        quote_ccy = str(quote or "USDT").upper()
        now = time.monotonic()
        cached = self._tickers.get(itype, [])
        if not refresh and cached and now - self._tickers_ts.get(itype, 0.0) < _TICKERS_CACHE_TTL_S:
            tickers = cached
        else:
            data = _http_get_json("/api/v5/market/tickers", {"instType": itype})
            tickers = [r for r in (data or []) if isinstance(r, dict) and r.get("instId")]
            self._tickers[itype] = tickers
            self._tickers_ts[itype] = now

        # 现货 instId 形如 BTC-USDT；永续/交割形如 BTC-USDT-SWAP、BTC-USDT-260327。
        if itype == "SPOT":
            rows = [r for r in tickers if str(r.get("instId", "")).endswith(f"-{quote_ccy}")]
        else:
            rows = [r for r in tickers if f"-{quote_ccy}-" in str(r.get("instId", ""))]
        rows.sort(
            key=lambda r: _to_float(r.get("volCcy24h")) * _to_float(r.get("last")),
            reverse=True,
        )
        return [str(r["instId"]) for r in rows[: max(int(limit), 1)]]

    def instrument_info(self, inst_id: str | None = None) -> dict[str, Any] | None:
        """取某品种的合约规格（tickSz/lotSz/ctVal 等），供下单换算使用。"""
        target = str(inst_id or self._symbol or "").upper()
        if not target:
            return None
        inst_type = "SWAP" if is_derivative_symbol(target) else "SPOT"
        try:
            rows = self.list_instruments(inst_type)
        except DataSourceError as exc:
            logger.debug("instrument_info failed: %s", exc)
            return None
        for row in rows:
            if str(row.get("instId", "")).upper() == target:
                return row
        return None

    # ── Subscription ──────────────────────────────────────────────────────────

    def subscribe(self, symbol: str, timeframe: str) -> None:
        inst_id = normalize_okx_symbol(symbol)
        if not inst_id:
            raise ValueError(
                f"OKX 品种无效: {symbol!r}。示例：BTC-USDT-SWAP（永续）、ETH-USDT（现货）"
            )
        if normalize_okx_timeframe(timeframe) == "":
            raise ValueError(f"不支持的周期: {timeframe!r}。可用: {list(_TF_TO_BAR.keys())}")
        if inst_id != self._symbol or timeframe != self._timeframe:
            self._snap_cache_bars = []
            self._snap_cache_n = 0
        self._symbol = inst_id
        self._timeframe = timeframe
        logger.info("OkxSource subscribed: %s %s", inst_id, timeframe)

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._timeframe = ""
        self._snap_cache_bars = []
        self._snap_cache_n = 0
        logger.info("OkxSource unsubscribed")

    # ── Market data ───────────────────────────────────────────────────────────

    def server_time_ms(self) -> int | None:
        """交易所时间（30 秒缓存），用于「等待 K 线收盘」倒计时校准。"""
        now = time.monotonic()
        if (
            self._server_time_cache_ms
            and now - self._server_time_cache_ts < _SERVER_TIME_CACHE_TTL_S
        ):
            return self._server_time_cache_ms
        try:
            data = _http_get_json("/api/v5/public/time", timeout=5.0)
        except DataSourceError as exc:
            logger.debug("OKX server_time unavailable: %s", exc)
            return self._server_time_cache_ms or None
        if isinstance(data, list) and data and isinstance(data[0], dict):
            ts = _to_float(data[0].get("ts"))
            if ts > 0:
                self._server_time_cache_ms = int(ts)
                self._server_time_cache_ts = now
        return self._server_time_cache_ms or None

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        """返回 ``n`` 根最新 K 线（newest-first，含正在走的最后一根）。"""
        if not self._connected:
            raise DataSourceTransientError("OKX 数据源未连接")
        if not self._symbol or not self._timeframe:
            raise DataSourceTransientError("OKX 未订阅品种/周期")

        want = max(int(n), 1) + 5  # 冗余：形成中的 K 线 + 指标预热
        now = time.monotonic()
        if (
            self._snap_cache_bars
            and self._snap_cache_n >= want
            and now - self._snap_cache_ts < _SNAPSHOT_CACHE_TTL_S
        ):
            return list(self._snap_cache_bars[:want])

        bar_code = normalize_okx_timeframe(self._timeframe)
        try:
            rows = self._fetch_candle_rows(bar_code, min(want, _MAX_CANDLES_LIMIT))
            while rows and len(rows) < want:
                oldest = int(float(rows[-1][0]))
                page = self._fetch_candle_rows(
                    bar_code,
                    min(want - len(rows), _MAX_HISTORY_LIMIT),
                    after=oldest,
                    history=True,
                )
                if not page or int(float(page[-1][0])) >= oldest:
                    break
                rows.extend(page)
        except OkxApiError as exc:
            raise DataSourceTransientError(
                f"OKX 拉取失败（{self._symbol} {self._timeframe}）：{exc}"
            ) from exc

        bars = parse_okx_candles(rows, symbol=self._symbol)
        if not bars:
            raise DataSourceTransientError(
                f"OKX 未返回 K 线数据：{self._symbol} {self._timeframe}（请检查品种是否存在）"
            )

        bars = bars[:want]
        self._snap_cache_bars = bars
        self._snap_cache_n = len(bars)
        self._snap_cache_ts = time.monotonic()
        return list(bars)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _fetch_candle_rows(
        self,
        bar_code: str,
        limit: int,
        *,
        after: int | None = None,
        history: bool = False,
    ) -> list[list[Any]]:
        path = "/api/v5/market/history-candles" if history else "/api/v5/market/candles"
        params: dict[str, Any] = {
            "instId": self._symbol,
            "bar": bar_code,
            "limit": max(1, min(int(limit), _MAX_HISTORY_LIMIT if history else _MAX_CANDLES_LIMIT)),
        }
        if after is not None:
            params["after"] = int(after)
        data = _http_get_json(path, params)
        if not isinstance(data, list):
            return []
        return [row for row in data if isinstance(row, (list, tuple))]
