"""币安 USDⓈ-M 合约公开行情数据源（轮询 REST，不需要 API Key）。

与 :mod:`pa_agent.data.okx_source` 是**同一个接口**（都实现 ``DataSource``），
所以界面、图表、两阶段分析、多品种监控都不用改：换数据源 = 换这一层。

品种写法沿用本项目的规范形式 ``BTC-USDT-SWAP``（币安的 ``BTCUSDT`` 只在内部换算），
这样监控列表、记录文件、图表缓存两个交易所可以共用一份。

网络：默认走系统代理；要单独指定就用环境变量 ``PA_BINANCE_PROXY``
（或 ``settings.trading.binance_proxy``，由上层注入）。
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from dataclasses import replace
from typing import Any, Iterable, Sequence

from pa_agent.data.base import (
    DataSource,
    DataSourceError,
    DataSourceTransientError,
    KlineBar,
    normalize_kline_bar,
)

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_DEFAULT_TIMEOUT_S = 12.0

#: 币安合约接口。部分地区会返回 451（Unavailable For Legal Reasons），
#: 那就需要一条币安接受的网络出口（见 ``PA_BINANCE_PROXY``）。
BINANCE_FAPI_URL: str = os.environ.get(
    "PA_BINANCE_FAPI_URL", "https://fapi.binance.com"
).rstrip("/")

#: 单次 klines 请求最多 1500 根。
_MAX_KLINES_LIMIT = 1500
#: 默认品种（与 OKX 用同一套规范写法）。
BINANCE_DEFAULT_SYMBOL = "BTC-USDT-SWAP"
_SNAPSHOT_CACHE_TTL_S = 1.5
_INSTRUMENTS_CACHE_TTL_S = 600.0
_TICKERS_CACHE_TTL_S = 60.0

#: 应用内周期 → 币安 interval 代码。
_TF_TO_INTERVAL: dict[str, str] = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "12h": "12h",
    "1d": "1d",
    "3d": "3d",
    "1w": "1w",
    "1M": "1M",
}

#: 界面下拉框预置品种（与 OKX 数据源保持同一套写法）。
_PRESET_SYMBOLS: tuple[str, ...] = (
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "XRP-USDT-SWAP",
    "DOGE-USDT-SWAP",
    "BNB-USDT-SWAP",
    "1000PEPE-USDT-SWAP",
    "SUI-USDT-SWAP",
)


class BinanceApiError(DataSourceError):
    """币安业务错误码（品种不存在等）。"""


def normalize_binance_symbol(raw: str) -> str:
    """把用户输入整理成规范写法 ``BASE-USDT-SWAP``；认不出返回空串。

    >>> normalize_binance_symbol("btcusdt")
    'BTC-USDT-SWAP'
    >>> normalize_binance_symbol("1000PEPEUSDT")
    '1000PEPE-USDT-SWAP'
    >>> normalize_binance_symbol("BTC-USDT-SWAP")
    'BTC-USDT-SWAP'
    >>> normalize_binance_symbol("BTCUSD_PERP")
    ''
    """
    text = str(raw or "").strip().upper()
    for sep in ("/", "_", " ", ":"):
        text = text.replace(sep, "-")
    parts = [p for p in text.split("-") if p]
    if not parts:
        return ""
    if len(parts) >= 2 and parts[-1] in ("SWAP", "PERP"):
        if parts[-2] == "USDT" and len(parts) >= 3:
            return f"{parts[-3]}-USDT-SWAP"
        return ""
    if len(parts) == 2 and parts[1] == "USDT":
        return f"{parts[0]}-USDT-SWAP"
    joined = "".join(parts)
    if joined.endswith("USDT") and len(joined) > 4:
        return f"{joined[:-4]}-USDT-SWAP"
    return ""


def normalize_binance_timeframe(timeframe: str) -> str:
    """应用周期 → 币安 interval 代码；不支持时返回空串。"""
    return _TF_TO_INTERVAL.get(str(timeframe or "").strip(), "")


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _proxy() -> str | None:
    return str(os.environ.get("PA_BINANCE_PROXY", "") or "").strip() or None


def _http_get_json(
    path: str, params: dict[str, Any] | None = None, *, timeout: float = _DEFAULT_TIMEOUT_S
) -> Any:
    """GET 一个币安公开接口（直接返回 JSON，不拆包）。"""
    url = f"{BINANCE_FAPI_URL}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}

    text = ""
    status = 0
    try:
        import requests  # type: ignore
    except ImportError:  # pragma: no cover
        requests = None  # type: ignore[assignment]

    proxy = _proxy()
    try:
        if requests is not None:
            proxies = {"http": proxy, "https": proxy} if proxy else None
            resp = requests.get(url, headers=headers, timeout=timeout, proxies=proxies)
            status, text = resp.status_code, resp.text
        else:  # pragma: no cover - 兜底路径
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy})
                )
            else:
                opener = urllib.request.build_opener()
            req = urllib.request.Request(url, headers=headers)
            with opener.open(req, timeout=timeout) as resp:  # type: ignore[attr-defined]
                status, text = resp.status, resp.read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise DataSourceTransientError(f"币安接口连接失败：{exc}") from exc

    if status == 451:
        raise DataSourceTransientError(
            "币安接口返回 451（该出口 IP 被地区限制）。需要给币安配一条可用的网络出口："
            "设置环境变量 PA_BINANCE_PROXY，或在 settings.trading.binance_proxy 里填代理。"
        )
    if status != 200:
        raise DataSourceTransientError(f"币安接口 HTTP {status}：{text[:160]}")

    try:
        payload = json.loads(text) if text else {}
    except ValueError as exc:
        raise DataSourceTransientError("币安返回内容不是 JSON") from exc
    if isinstance(payload, dict) and payload.get("code") not in (None, 0):
        raise BinanceApiError(
            f"币安接口错误 {payload.get('code')}: {payload.get('msg') or ''}".strip()
        )
    return payload


def parse_binance_klines(rows: Iterable[Sequence[Any]], *, symbol: str = "") -> list[KlineBar]:
    """把币安 K 线行解析成 newest-first 的 :class:`KlineBar` 列表。

    币安每行::

        [openTime, open, high, low, close, volume, closeTime,
         quoteAssetVolume, trades, takerBuyBase, takerBuyQuote, ignore]

    ``volume`` 是**标的币数量**（不是张数），``quoteAssetVolume`` 是计价币成交额。
    最后一根是"正在走"的那根：用 ``closeTime`` 跟当前时间比来判断是否已收盘。
    """
    now_ms = int(time.time() * 1000)
    bars: list[KlineBar] = []
    seen: set[int] = set()
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 9:
            continue
        try:
            ts_open = int(float(row[0]))
            close_time = int(float(row[6]))
            open_, high, low, close = (float(row[i]) for i in (1, 2, 3, 4))
        except (TypeError, ValueError):
            continue
        if ts_open <= 0 or ts_open in seen:
            continue
        seen.add(ts_open)
        bars.append(
            normalize_kline_bar(
                KlineBar(
                    seq=0,
                    ts_open=ts_open,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=_to_float(row[5]),
                    amount=_to_float(row[7]),
                    closed=close_time < now_ms,
                )
            )
        )
    bars.sort(key=lambda b: b.ts_open, reverse=True)
    return [replace(bar, seq=i + 1) for i, bar in enumerate(bars)]


class BinanceSource(DataSource):
    """币安 USDⓈ-M 永续行情源（公开 REST，无密钥）。"""

    def __init__(self, *, base_url: str | None = None) -> None:
        self._base_url = (base_url or BINANCE_FAPI_URL).rstrip("/")
        self._symbol: str = ""
        self._timeframe: str = ""
        self._connected: bool = False
        self._snap_cache_n: int = 0
        self._snap_cache_ts: float = 0.0
        self._snap_cache_bars: list[KlineBar] = []
        self._instruments: list[dict[str, Any]] = []
        self._instruments_ts: float = 0.0
        self._tickers: list[dict[str, Any]] = []
        self._tickers_ts: float = 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """公开行情无需鉴权；这里探一次连通性，连不上就直接报错（多半是网络出口）。"""
        self._connected = True
        try:
            _http_get_json("/fapi/v1/ping", timeout=8.0)
        except DataSourceError:
            self._connected = False
            raise
        logger.info("BinanceSource connected (public REST, base=%s)", self._base_url)

    def disconnect(self) -> None:
        self._connected = False
        self._snap_cache_bars = []
        self._snap_cache_n = 0
        logger.info("BinanceSource disconnected")

    # ── Discovery ─────────────────────────────────────────────────────────────

    def list_symbols(self) -> list[str]:
        return list(_PRESET_SYMBOLS)

    def supported_timeframes(self) -> list[str]:
        return list(_TF_TO_INTERVAL.keys())

    def exchange_info(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """合约规格（默认缓存 10 分钟）。"""
        now = time.monotonic()
        if (
            not refresh
            and self._instruments
            and now - self._instruments_ts < _INSTRUMENTS_CACHE_TTL_S
        ):
            return list(self._instruments)
        payload = _http_get_json("/fapi/v1/exchangeInfo")
        rows = [
            r
            for r in (payload or {}).get("symbols") or []
            if str(r.get("contractType", "")).upper() == "PERPETUAL"
            and str(r.get("quoteAsset", "")).upper() == "USDT"
            and str(r.get("status", "")).upper() == "TRADING"
        ]
        self._instruments = rows
        self._instruments_ts = now
        return list(rows)

    def fetch_liquid_symbols(
        self, *, limit: int = 30, inst_type: str = "SWAP", quote: str = "USDT", refresh: bool = False
    ) -> list[str]:
        """按 24h 成交额（USDT 名义额）从高到低返回可交易品种。"""
        now = time.monotonic()
        if not refresh and self._tickers and now - self._tickers_ts < _TICKERS_CACHE_TTL_S:
            rows = self._tickers
        else:
            rows = list(_http_get_json("/fapi/v1/ticker/24hr") or [])
            self._tickers = rows
            self._tickers_ts = now
        tradable = {
            str(r.get("symbol")) for r in self.exchange_info()
        }
        rows = [r for r in rows if str(r.get("symbol")) in tradable]
        rows.sort(key=lambda r: _to_float(r.get("quoteVolume")), reverse=True)
        out: list[str] = []
        for row in rows[: max(int(limit), 1)]:
            symbol = str(row.get("symbol") or "")
            if symbol.endswith("USDT") and len(symbol) > 4:
                out.append(f"{symbol[:-4]}-USDT-SWAP")
        return out

    def instrument_info(self, inst_id: str | None = None) -> dict[str, Any] | None:
        """取合约规格，字段名对齐 OKX（tickSz/lotSz/minSz/ctVal）。"""
        target = normalize_binance_symbol(inst_id or self._symbol)
        if not target:
            return None
        symbol = f"{target.split('-')[0]}USDT"
        try:
            rows = self.exchange_info()
        except DataSourceError as exc:
            logger.debug("binance instrument_info failed: %s", exc)
            return None
        row = next((r for r in rows if str(r.get("symbol")) == symbol), None)
        if row is None:
            return None
        tick = lot = min_sz = 0.0
        for f in row.get("filters") or []:
            kind = f.get("filterType")
            if kind == "PRICE_FILTER":
                tick = _to_float(f.get("tickSize"))
            elif kind == "LOT_SIZE":
                lot = _to_float(f.get("stepSize"))
                min_sz = _to_float(f.get("minQty"))
        return {
            "instId": target,
            "instType": "SWAP",
            "tickSz": tick,
            "lotSz": lot,
            "minSz": min_sz,
            "ctVal": 1.0,             # 币安按标的币数量下单，等价于 ctVal=1
            "ctValCcy": str(row.get("baseAsset") or ""),
        }

    def book_ticker(self, inst_id: str) -> tuple[float, float]:
        """该品种的 ``(买一, 卖一)``；选币用点差过滤时需要（可选能力）。"""
        canonical = normalize_binance_symbol(inst_id)
        if not canonical:
            raise DataSourceError(f"币安品种无效: {inst_id!r}")
        payload = _http_get_json(
            "/fapi/v1/ticker/bookTicker", {"symbol": f"{canonical.split('-')[0]}USDT"}
        )
        return _to_float((payload or {}).get("bidPrice")), _to_float((payload or {}).get("askPrice"))

    def book_tickers(self) -> dict[str, float]:
        """全市场点差（规范写法 → bp），一次请求拿全 —— 选币用这个，别逐币查。"""
        rows = _http_get_json("/fapi/v1/ticker/bookTicker") or []
        out: dict[str, float] = {}
        for row in rows:
            symbol = str((row or {}).get("symbol") or "")
            if not symbol.endswith("USDT") or len(symbol) <= 4:
                continue
            bid = _to_float(row.get("bidPrice"))
            ask = _to_float(row.get("askPrice"))
            if bid > 0 and ask > 0 and ask >= bid:
                out[f"{symbol[:-4]}-USDT-SWAP"] = (ask - bid) / ((ask + bid) / 2.0) * 1e4
        return out

    def volumes_24h(self) -> dict[str, float]:
        """全市场 24h USDT 名义成交额（规范写法 → 金额），一次请求拿全。

        选币时先用它做一遍粗筛，能把"评估 497 个币"压到只评估有量的那些，
        全量扫描从 ~3 分钟降到 1 分钟出头。
        """
        rows = _http_get_json("/fapi/v1/ticker/24hr") or []
        out: dict[str, float] = {}
        for row in rows:
            symbol = str((row or {}).get("symbol") or "")
            if symbol.endswith("USDT") and len(symbol) > 4:
                out[f"{symbol[:-4]}-USDT-SWAP"] = _to_float(row.get("quoteVolume"))
        return out

    # ── Subscription ──────────────────────────────────────────────────────────

    def subscribe(self, symbol: str, timeframe: str) -> None:
        canonical = normalize_binance_symbol(symbol)
        if not canonical:
            raise ValueError(
                f"币安品种无效: {symbol!r}。示例：BTC-USDT-SWAP（USDT 本位永续）"
            )
        if normalize_binance_timeframe(timeframe) == "":
            raise ValueError(
                f"不支持的周期: {timeframe!r}。可用: {list(_TF_TO_INTERVAL.keys())}"
            )
        if canonical != self._symbol or timeframe != self._timeframe:
            self._snap_cache_bars = []
            self._snap_cache_n = 0
        self._symbol = canonical
        self._timeframe = timeframe
        logger.info("BinanceSource subscribed: %s %s", canonical, timeframe)

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._timeframe = ""
        self._snap_cache_bars = []
        self._snap_cache_n = 0
        logger.info("BinanceSource unsubscribed")

    # ── Market data ───────────────────────────────────────────────────────────

    def server_time_ms(self) -> int | None:
        """交易所时间，用于「等待 K 线收盘」倒计时校准。"""
        try:
            payload = _http_get_json("/fapi/v1/time", timeout=5.0)
        except DataSourceError as exc:
            logger.debug("binance server_time unavailable: %s", exc)
            return None
        value = int((payload or {}).get("serverTime") or 0)
        return value or None

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        """返回 ``n`` 根最新 K 线（newest-first，含正在走的最后一根）。"""
        if not self._connected:
            raise DataSourceTransientError("币安数据源未连接")
        if not self._symbol or not self._timeframe:
            raise DataSourceTransientError("币安未订阅品种/周期")

        want = max(int(n), 1) + 5      # 冗余：形成中的 K 线 + 指标预热
        now = time.monotonic()
        if (
            self._snap_cache_bars
            and self._snap_cache_n >= want
            and now - self._snap_cache_ts < _SNAPSHOT_CACHE_TTL_S
        ):
            return list(self._snap_cache_bars[:want])

        interval = normalize_binance_timeframe(self._timeframe)
        symbol = f"{self._symbol.split('-')[0]}USDT"
        try:
            rows = _http_get_json(
                "/fapi/v1/klines",
                {"symbol": symbol, "interval": interval, "limit": min(want, _MAX_KLINES_LIMIT)},
            )
        except BinanceApiError as exc:
            raise DataSourceTransientError(
                f"币安拉取失败（{self._symbol} {self._timeframe}）：{exc}"
            ) from exc

        bars = parse_binance_klines(rows or [], symbol=self._symbol)
        if not bars:
            raise DataSourceTransientError(
                f"币安未返回 K 线数据：{self._symbol} {self._timeframe}（请检查品种是否存在）"
            )
        bars = bars[:want]
        self._snap_cache_bars = bars
        self._snap_cache_n = len(bars)
        self._snap_cache_ts = now
        return list(bars)
