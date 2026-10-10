"""币安 USDT 本位永续下单执行层（与 OKX 网关同一套风控核心）。

与 OKX 的**根本差别**（改代码前先读）

1. **没有"附带止损"这回事**。OKX 可以用 ``attachAlgoOrds`` 把止损跟入场单绑在
   一起、成交后自动生效；币安只能下**独立的条件单**。而且条件单**必须走 Algo
   Order API**（``/fapi/v1/algoOrder``，字段是 ``triggerPrice`` 而不是 ``stopPrice``）
   —— 2026-10-09 实测：老接口 ``/fapi/v1/order`` 会直接回
   ``-4120 Order type not supported for this endpoint``。所以这里的流程是：
   入场 → 看成交状态 → **成交了就补挂止损**。挂单还没成交时，把"打算挂在哪"记进
   ``records/trading_stop_intents.json``，由 :meth:`BinanceTrader.ensure_stops`
   每轮核对补挂。
   另外注意：币安要求**已有持仓**才能挂 ``closePosition`` 条件单（否则 ``-4509``），
   这也是"必须成交后补挂"的硬约束。
   ⚠️ 由此产生一个**真实风险**：从成交到止损挂上之间有一个窗口（正常 ≤1 分钟；
   程序若正好崩在这个窗口里，仓位就是裸的）。这是币安的机制限制，不是实现取舍。
2. **签名不同**：``hex(hmac_sha256(secret, query_string))``，签名串跟在 query 后面；
   请求头只有 ``X-MBX-APIKEY``（没有 passphrase）。
3. **参数放在 query string 里**，POST/DELETE 也是，不是 JSON body。
4. **时间戳要求严**：与服务端时间差超过 ``recvWindow`` 直接报 -1021，
   所以这里会先同步一次服务端时间（:meth:`BinancePrivateClient.sync_time`）。
5. **只支持单向持仓模式**（one-way）。账户若是双向持仓（hedge），
   ``execute`` 会明确拒单，而不是下错方向。
6. **品种写法**：对外仍用 ``BTC-USDT-SWAP``，只在网关内部换算成 ``BTCUSDT``。

安全设计与 OKX 网关一致（见 ``gateway.py``）：默认全关、默认测试网、每笔先算亏损、
没有止损不下单、只撤自己下的单（``newClientOrderId`` 以 ``PAAGENT`` 开头）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pa_agent.config.paths import RECORDS_PENDING_DIR
from pa_agent.trading.gateway import (
    ORDER_TAG,
    ExecutionResult,
    InstrumentSpec,
    OrderPlan,
    TradeError,
    TradeRejected,
    evaluate_guard,
    from_binance_symbol,
    plan_order,
    resolve_proxy,
    to_binance_symbol,
)

logger = logging.getLogger(__name__)

#: 别名，便于阅读（两个网关抛的是同一个异常类型）。
BinanceTradeError = TradeError

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_DEFAULT_TIMEOUT_S = 10.0
_RECV_WINDOW_MS = 5000

#: 实盘 / 测试网（模拟盘）。
BINANCE_FAPI_URL = os.environ.get("PA_BINANCE_FAPI_URL", "https://fapi.binance.com").rstrip("/")
BINANCE_TESTNET_URL = os.environ.get(
    "PA_BINANCE_TESTNET_URL", "https://testnet.binancefuture.com"
).rstrip("/")

#: 止损/止盈挂单的意图（成交后要补挂的那份记录）。
_STOP_INTENT_PATH = RECORDS_PENDING_DIR.parent / "trading_stop_intents.json"

#: 币安认定为"止损/止盈"的订单类型。
_STOP_TYPES = (
    "STOP",
    "STOP_MARKET",
    "TAKE_PROFIT",
    "TAKE_PROFIT_MARKET",
    "TRAILING_STOP_MARKET",
)


def sign_query(secret_key: str, query: str) -> str:
    """币安签名：``hex(hmac_sha256(secret, query_string))``。

    官方说的 ``totalParams`` 就是"参与签名的完整 query string"（含 timestamp 与所有业务参数）。
    """
    return hmac.new(
        secret_key.encode("utf-8"), str(query).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _day_start_ms(tz_offset_hours: int = 8) -> int:
    """本地（默认 UTC+8）当天 00:00 的毫秒时间戳。"""
    now_s = time.time()
    offset_s = tz_offset_hours * 3600
    day_start_local = math.floor((now_s + offset_s) / 86_400) * 86_400
    return int((day_start_local - offset_s) * 1000)


def _decimals(step: float) -> int:
    """把步长（0.001）翻译成小数位数（3）。"""
    if step <= 0:
        return 8
    text = f"{step:.12f}".rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


def _fmt_step(value: float, step: float) -> str:
    """按步长精度格式化（币安对多余小数位会直接报 -1111）。"""
    if step <= 0:
        return f"{value:.8f}".rstrip("0").rstrip(".") or "0"
    digits = _decimals(step)
    floor_value = math.floor(round(value / step, 6)) * step
    return f"{floor_value:.{digits}f}"


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _canonical_or_blank(symbol: Any) -> str:
    """把币安代码换成规范写法；**空值返回空串**而不是报错。

    撤销/改单这类接口的返回体里经常没有 ``symbol`` 字段，解析时不该因此抛异常
    （否则一次"撤单成功但解析失败"就会被当成撤单失败，掩盖真实状态）。
    """
    text = str(symbol or "").strip().upper()
    if not text:
        return ""
    try:
        return from_binance_symbol(text)
    except TradeRejected:
        return text


def _send_http(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
    proxy: str | None = None,
) -> tuple[int, str]:
    """发 HTTP 请求，返回 ``(status_code, text)``；网络故障抛 :class:`BinanceTradeError`。"""
    try:
        import requests  # type: ignore
    except ImportError:  # pragma: no cover
        requests = None  # type: ignore[assignment]

    if requests is not None:
        try:
            proxies = {"http": proxy, "https": proxy} if proxy else None
            resp = requests.request(
                method, url, headers=headers, data=body, timeout=timeout, proxies=proxies
            )
        except Exception as exc:  # noqa: BLE001
            raise BinanceTradeError(f"币安网络请求失败: {exc}") from exc
        return resp.status_code, resp.text

    if proxy:  # pragma: no cover - 兜底路径
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    else:
        opener = urllib.request.build_opener()
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as resp:  # type: ignore[attr-defined]
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:  # pragma: no cover - 兜底路径
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        raise BinanceTradeError(f"币安网络请求失败: {exc}") from exc


# ── 凭据 ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BinanceCredentials:
    """币安 API 凭据（只存在于内存里；币安没有 passphrase）。"""

    api_key: str
    secret_key: str
    simulated: bool = True

    @property
    def complete(self) -> bool:
        return bool(self.api_key and self.secret_key)

    def mask(self) -> str:
        key = self.api_key
        shown = f"{key[:4]}…{key[-4:]}" if len(key) > 8 else "****"
        return f"{shown}{'（测试网）' if self.simulated else '（实盘）'}"

    @classmethod
    def from_env(cls, *, simulated: bool = True) -> BinanceCredentials | None:
        api_key = os.environ.get("BINANCE_API_KEY", "").strip()
        secret_key = os.environ.get("BINANCE_SECRET_KEY", "").strip()
        if not (api_key and secret_key):
            return None
        return cls(api_key, secret_key, simulated=simulated)

    @classmethod
    def from_file(
        cls, path: str | Path, *, simulated: bool = True
    ) -> BinanceCredentials | None:
        p = Path(path)
        if not p.exists():
            return None
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("币安凭据文件不可读（%s）：%s", p, exc)
            return None
        if not isinstance(raw, dict):
            return None
        return cls(
            api_key=str(raw.get("api_key", "")).strip(),
            secret_key=str(raw.get("secret_key", "")).strip(),
            simulated=simulated,
        )

    @classmethod
    def load(
        cls, path: str | Path | None, *, simulated: bool = True
    ) -> BinanceCredentials | None:
        """环境变量优先于文件（与 OKX 网关一致）。"""
        from_env = cls.from_env(simulated=simulated)
        if from_env is not None:
            return from_env
        if not path:
            return None
        return cls.from_file(path, simulated=simulated)


# ── 私有 REST 客户端 ──────────────────────────────────────────────────────────


class BinancePrivateClient:
    """币安 USDⓈ-M 合约私有接口。

    所有读接口都**翻译成 OKX 那套字段名**（``instId`` / ``pos`` / ``sz`` / ``cTime`` /
    ``tag`` …），这样上层「跳过已持仓品种」「撤过期挂单」这些逻辑两个交易所共用一份。
    """

    def __init__(
        self,
        credentials: BinanceCredentials,
        *,
        base_url: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        now_ms: Callable[[], int] | None = None,
        proxy: str | None = None,
    ) -> None:
        if not credentials.complete:
            raise BinanceTradeError("币安凭据不完整（需要 api_key / secret_key）")
        self._cred = credentials
        default_base = BINANCE_TESTNET_URL if credentials.simulated else BINANCE_FAPI_URL
        self._base_url = (base_url or default_base).rstrip("/")
        self._timeout = timeout
        self._proxy = proxy
        self._clock_offset_ms = 0
        self._synced = False
        self._local_now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._instruments: dict[str, dict[str, Any]] = {}

    # ── 基础设施 ──────────────────────────────────────────────────────────────

    @property
    def simulated(self) -> bool:
        return self._cred.simulated

    @property
    def base_url(self) -> str:
        return self._base_url

    def _now_ms(self) -> int:
        return int(self._local_now_ms()) + int(self._clock_offset_ms)

    def sync_time(self) -> int:
        """同步服务端时间，返回本机相对服务端的偏移（毫秒）。"""
        data = self.request("GET", "/fapi/v1/time", signed=False)
        server = int((data or {}).get("serverTime") or 0)
        if server > 0:
            self._clock_offset_ms = server - int(self._local_now_ms())
        self._synced = True
        return self._clock_offset_ms

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        signed: bool = True,
    ) -> Any:
        """调用币安接口；HTTP 非 2xx 或返回 error code 时抛 :class:`BinanceTradeError`。"""
        method = method.upper()
        query_items: dict[str, Any] = {
            k: v for k, v in (params or {}).items() if v is not None
        }
        if signed:
            if not self._synced:
                try:
                    self.sync_time()
                except BinanceTradeError as exc:   # 同步失败不致命，继续用本机时间
                    logger.warning("币安时间同步失败，改用本机时间：%s", exc)
            query_items["timestamp"] = self._now_ms()
            query_items["recvWindow"] = _RECV_WINDOW_MS

        query = urllib.parse.urlencode(query_items)
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
            "X-MBX-APIKEY": self._cred.api_key,
        }
        if signed:
            query = f"{query}&signature={sign_query(self._cred.secret_key, query)}"
        url = f"{self._base_url}{path}"
        if query:
            url = f"{url}?{query}"

        status, text = _send_http(method, url, headers, None, self._timeout, self._proxy)
        payload = self._parse(status, text)
        if isinstance(payload, dict) and int(payload.get("code") or 0) == -1021:
            logger.warning("币安时间戳过期，重新对时后重试一次：%s", path)
            self.sync_time()
            return self.request(method, path, params=params, signed=signed)
        self._raise_for_error(payload)
        return payload

    def _parse(self, status: int, text: str) -> Any:
        try:
            payload = json.loads(text) if text else {}
        except ValueError as exc:
            raise BinanceTradeError(
                f"币安返回内容不是 JSON（HTTP {status}）：{text[:160]}"
            ) from exc
        if status != 200 and not (
            isinstance(payload, dict) and payload.get("code") is not None
        ):
            raise BinanceTradeError(f"币安 HTTP {status}: {text[:200]}")
        return payload

    def _raise_for_error(self, payload: Any) -> None:
        # 注意：``code`` 既可能是数字（-1121）也可能是字符串；成功时多数接口不带 code，
        # 但 ``DELETE /fapi/v1/allOpenOrders`` 成功时返回的是 ``{"code":200,...}`` —— 200 也算成功。
        if not isinstance(payload, dict):
            return
        code = payload.get("code")
        if code in (None, 0, 200, "0", "200"):
            return
        raise BinanceTradeError(f"币安接口错误 {code}: {payload.get('msg') or ''}")

    # ── 账户 ──────────────────────────────────────────────────────────────────

    def balance(self) -> list[dict[str, Any]]:
        data = self.request("GET", "/fapi/v2/balance")
        return list(data or [])

    def equity_usd(self, ccy: str = "USDT") -> float:
        """账户权益（USDT）。

        必须用 ``/fapi/v2/account``：``/fapi/v2/balance`` 里**没有** ``totalMarginBalance``，
        只按那份数据取会拿到 ``availableBalance``（= 权益 − 已占用保证金），
        一开仓就变小，会连带把「按权益收窄仓位」算错。
        """
        try:
            acct = self.request("GET", "/fapi/v2/account")
        except BinanceTradeError as exc:
            logger.warning("读取币安账户权益失败: %s", exc)
            return 0.0
        for key in ("totalMarginBalance", "totalWalletBalance"):
            value = _as_float((acct or {}).get(key))
            if value is not None:
                return value
        return 0.0

    def dual_side_position(self) -> bool:
        """账户是否处于双向持仓模式（本网关不支持）。"""
        data = self.request("GET", "/fapi/v1/positionSide/dual")
        return bool((data or {}).get("dualSidePosition"))

    def positions(
        self, inst_id: str | None = None, inst_type: str | None = None
    ) -> list[dict[str, Any]]:
        """持仓列表（只返回真的有仓位的；字段名对齐 OKX）。"""
        params: dict[str, Any] = {}
        if inst_id:
            params["symbol"] = to_binance_symbol(inst_id)
        rows = self.request("GET", "/fapi/v2/positionRisk", params=params or None)
        out: list[dict[str, Any]] = []
        for row in rows or []:
            amount = _as_float(row.get("positionAmt")) or 0.0
            if abs(amount) <= 0:
                continue
            out.append(
                {
                    "instId": from_binance_symbol(str(row.get("symbol") or "")),
                    "pos": amount,
                    "avgPx": _as_float(row.get("entryPrice")) or 0.0,
                    "upl": _as_float(row.get("unRealizedProfit")) or 0.0,
                    "lever": _as_float(row.get("leverage")) or 0.0,
                    "raw": row,
                }
            )
        return out

    def open_position_count(self, inst_type: str | None = None) -> int:
        return len(self.positions())

    def pending_orders(
        self, inst_id: str | None = None, inst_type: str | None = None
    ) -> list[dict[str, Any]]:
        """未成交挂单（字段名对齐 OKX：``sz`` / ``px`` / ``cTime`` / ``tag``）。"""
        params: dict[str, Any] = {}
        if inst_id:
            params["symbol"] = to_binance_symbol(inst_id)
        rows = self.request("GET", "/fapi/v1/openOrders", params=params or None)
        return [self._normalize_order(row) for row in rows or []]

    def algo_pending(
        self, inst_id: str | None = None, ord_type: str = "oco"
    ) -> list[dict[str, Any]]:
        """未触发的止损/止盈单（币安的 **Algo Order API**）。

        返回项带上 ``slTriggerPx``，让上层共用 OKX 那套"有没有止损"的判断。
        """
        params: dict[str, Any] = {}
        if inst_id:
            params["symbol"] = to_binance_symbol(inst_id)
        rows = self.request("GET", "/fapi/v1/openAlgoOrders", params=params or None)
        if not isinstance(rows, list):        # 接口异常时别把 dict 当列表迭代
            return []
        return [self._normalize_algo_order(row) for row in rows if isinstance(row, dict)]

    def occupied_symbols(self, inst_type: str | None = None) -> set[str]:
        """已有持仓**或**有未成交挂单的品种集合（与 OKX 网关同名字段）。"""
        out = {str(r.get("instId") or "").strip().upper() for r in self.positions()}
        out |= {str(r.get("instId") or "").strip().upper() for r in self.pending_orders()}
        out.discard("")
        return out

    def realized_pnl_today_usd(self, tz_offset_hours: int = 8) -> float:
        """当日已实现盈亏 + 手续费 + 资金费（用于日亏熔断）。"""
        start = _day_start_ms(tz_offset_hours)
        total = 0.0
        for income_type in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
            rows = self.request(
                "GET",
                "/fapi/v1/income",
                params={"incomeType": income_type, "startTime": start, "limit": 1000},
            )
            for row in rows or []:
                value = _as_float(row.get("income"))
                if value is not None:
                    total += value
        return total

    # ── 订单 ──────────────────────────────────────────────────────────────────

    def _normalize_order(self, row: dict[str, Any]) -> dict[str, Any]:
        """把币安订单字段翻译成 OKX 那套名字。"""
        client_id = str(row.get("clientOrderId") or "")
        return {
            "instId": _canonical_or_blank(row.get("symbol")),
            "ordId": str(row.get("orderId") or ""),
            "clOrdId": client_id,
            "tag": ORDER_TAG if client_id.startswith(ORDER_TAG) else "",
            "side": str(row.get("side") or "").lower(),
            "sz": _as_float(row.get("origQty")) or 0.0,
            "px": _as_float(row.get("price")) or 0.0,
            "cTime": int(row.get("time") or 0),
            "type": str(row.get("type") or ""),
            "stopPx": _as_float(row.get("stopPrice")) or 0.0,
            "status": str(row.get("status") or ""),
            "executedQty": _as_float(row.get("executedQty")) or 0.0,
            "raw": row,
        }

    def _normalize_algo_order(self, row: dict[str, Any]) -> dict[str, Any]:
        """把 **Algo Order** 字段翻译成同一套名字（止损/止盈走这个接口）。

        实测（2026-10-09）：Algo 单的字段是 ``algoId`` / ``clientAlgoId`` /
        ``triggerPrice``（不是 ``stopPrice``）/ ``bookTime`` / ``algoStatus``。
        """
        client_id = str(row.get("clientAlgoId") or row.get("clientOrderId") or "")
        trigger = _as_float(row.get("triggerPrice")) or _as_float(row.get("stopPrice")) or 0.0
        return {
            "instId": _canonical_or_blank(row.get("symbol")),
            "ordId": str(row.get("algoId") or row.get("orderId") or ""),
            "clOrdId": client_id,
            "tag": ORDER_TAG if client_id.startswith(ORDER_TAG) else "",
            "side": str(row.get("side") or "").lower(),
            "sz": _as_float(row.get("quantity")) or _as_float(row.get("origQty")) or 0.0,
            "px": trigger,
            "cTime": int(row.get("bookTime") or row.get("time") or 0),
            "type": str(row.get("orderType") or row.get("type") or ""),
            "stopPx": trigger,
            "slTriggerPx": trigger or "",
            "status": str(row.get("algoStatus") or row.get("status") or ""),
            "closePosition": str(row.get("closePosition") or ""),
            "raw": row,
        }

    def order_status(self, inst_id: str, ord_id: str) -> dict[str, Any]:
        row = self.request(
            "GET",
            "/fapi/v1/order",
            params={"symbol": to_binance_symbol(inst_id), "orderId": ord_id},
        )
        return self._normalize_order(row or {})

    def set_leverage(self, inst_id: str, leverage: int, mgn_mode: str = "cross") -> None:
        """设置全仓 + 杠杆（已经是全仓时币安报 -4046，忽略即可）。"""
        symbol = to_binance_symbol(inst_id)
        margin_type = "ISOLATED" if str(mgn_mode).lower() == "isolated" else "CROSSED"
        try:
            self.request(
                "POST",
                "/fapi/v1/marginType",
                params={"symbol": symbol, "marginType": margin_type},
            )
        except BinanceTradeError as exc:
            if "-4046" not in str(exc):        # -4046 = No need to change margin type
                logger.warning("设置 %s 保证金模式失败（继续）：%s", inst_id, exc)
        self.request(
            "POST", "/fapi/v1/leverage", params={"symbol": symbol, "leverage": int(leverage)}
        )

    def place_entry(
        self,
        *,
        inst_id: str,
        side: str,
        ord_type: str,
        size: float,
        price: float | None = None,
        trigger_px: float | None = None,
        tag: str = ORDER_TAG,
        step: float = 0.0,
        tick: float = 0.0,
    ) -> dict[str, Any]:
        """下入场单：限价 LIMIT / 市价 MARKET / 突破 STOP_MARKET。"""
        symbol = to_binance_symbol(inst_id)
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": str(side).upper(),
            "quantity": _fmt_step(size, step),
            "newClientOrderId": self._client_id("E", tag),
        }
        kind = str(ord_type).lower()
        if kind == "limit":
            if price is None:
                raise TradeRejected("限价单缺少价格")
            params["type"] = "LIMIT"
            params["timeInForce"] = "GTC"
            params["price"] = _fmt_step(price, tick)
        elif kind == "market":
            params["type"] = "MARKET"
        elif kind == "trigger":
            if trigger_px is None:
                raise TradeRejected("突破单缺少触发价")
            params["type"] = "STOP_MARKET"
            params["stopPrice"] = _fmt_step(trigger_px, tick)
            params["workingType"] = "MARK_PRICE"
        else:
            raise TradeRejected(f"币安网关不支持的订单类型：{ord_type}")
        row = self.request("POST", "/fapi/v1/order", params=params)
        logger.info("币安下单成功 symbol=%s side=%s sz=%s", symbol, side, size)
        return self._normalize_order(row or {})

    def place_stop(
        self,
        *,
        inst_id: str,
        side: str,
        stop_px: float,
        size: float = 0.0,
        tick: float = 0.0,
        step: float = 0.0,
        tag: str = ORDER_TAG,
        kind: str = "stop",
    ) -> dict[str, Any]:
        """挂止损/止盈 —— 必须走 **Algo Order API**（2026-10-09 实测）。

        币安已把条件单从 ``/fapi/v1/order`` 迁走：老写法直接报
        ``-4120 Order type not supported for this endpoint. Please use the Algo Order API``。
        新接口 ``POST /fapi/v1/algoOrder`` 要求的字段是：``algoType`` / ``symbol`` /
        ``side`` / ``type`` / **``triggerPrice``**（不是 ``stopPrice``）。

        优先 ``closePosition=true``（整仓保护，仓位翻倍也一次平掉）；失败退回
        ``reduceOnly`` + 数量。**两者都要求已有持仓**，否则币安回 ``-4509``。

        ``side`` 是**平仓方向**：多头仓位的止损是 SELL，空头是 BUY。
        """
        symbol = to_binance_symbol(inst_id)
        order_type = "TAKE_PROFIT_MARKET" if kind == "take_profit" else "STOP_MARKET"
        base: dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": str(side).upper(),
            "type": order_type,
            "triggerPrice": _fmt_step(stop_px, tick),
            "workingType": "MARK_PRICE",
            "clientAlgoId": self._client_id("S" if kind == "stop" else "T", tag),
        }
        attempts: list[dict[str, Any]] = [{**base, "closePosition": "true"}]
        if size > 0:
            attempts.append({**base, "quantity": _fmt_step(size, step), "reduceOnly": "true"})
        last: Exception | None = None
        for params in attempts:
            try:
                row = self.request("POST", "/fapi/v1/algoOrder", params=params)
                logger.info(
                    "币安挂 %s 成功 %s @%s", order_type, symbol, params.get("triggerPrice")
                )
                return self._normalize_algo_order(row or {})
            except BinanceTradeError as exc:
                last = exc
                logger.warning("币安挂 %s 失败，换一种方式重试：%s", order_type, exc)
        raise last or BinanceTradeError(f"币安挂 {order_type} 失败")

    def cancel_order(self, inst_id: str, ord_id: str) -> dict[str, Any]:
        """撤**普通**挂单（限价入场单走这里）。"""
        row = self.request(
            "DELETE",
            "/fapi/v1/order",
            params={"symbol": to_binance_symbol(inst_id), "orderId": ord_id},
        )
        return self._normalize_order(row or {})

    def cancel_algo_order(self, inst_id: str, algo_id: str) -> dict[str, Any]:
        """撤**条件单**（止损/止盈走这里）。"""
        row = self.request(
            "DELETE",
            "/fapi/v1/algoOrder",
            params={"algoId": algo_id},
        )
        return self._normalize_algo_order(row or {})

    def cancel_all_orders(self, inst_id: str) -> list[dict[str, Any]]:
        """撤掉该品种**所有**程序会的挂单：普通挂单 + Algo 条件单。

        两处都要撤：平仓后若把止损伤留在市场上，等下次再开同品种的仓，
        那张旧止损会立刻把新仓位打掉——这是很隐蔽的坑。
        """
        out: list[dict[str, Any]] = []
        for row in self.algo_pending(inst_id=inst_id):
            algo_id = str(row.get("ordId") or "")
            if not algo_id:
                continue
            try:
                out.append(self.cancel_algo_order(inst_id, algo_id))
            except Exception as exc:  # noqa: BLE001 - 撤单尽力而为，不能挡住平仓
                logger.warning("撤条件单失败 %s %s: %s", inst_id, algo_id, exc)
        try:
            self.request(
                "DELETE",
                "/fapi/v1/allOpenOrders",
                params={"symbol": to_binance_symbol(inst_id)},
            )
        except Exception as exc:  # noqa: BLE001 - 同上
            logger.warning("撤普通挂单失败 %s: %s", inst_id, exc)
        return out

    def close_position(
        self, inst_id: str, mgn_mode: str = "cross", pos_side: str | None = None
    ) -> dict[str, Any]:
        """市价平掉该品种全部持仓（并撤掉该品种所有挂单）。"""
        rows = self.positions(inst_id=inst_id)
        if not rows:
            return {}
        amount = _as_float(rows[0].get("pos")) or 0.0
        if abs(amount) <= 0:
            return {}
        side = "SELL" if amount > 0 else "BUY"
        self.cancel_all_orders(inst_id)
        params: dict[str, Any] = {
            "symbol": to_binance_symbol(inst_id),
            "side": side,
            "type": "MARKET",
            "quantity": self._fmt_symbol_qty(inst_id, abs(amount)),
            "reduceOnly": "true",
            "newClientOrderId": self._client_id("C", ORDER_TAG),
        }
        row = self.request("POST", "/fapi/v1/order", params=params)
        return self._normalize_order(row or {})

    # ── 工具 ──────────────────────────────────────────────────────────────────

    def _client_id(self, kind: str, tag: str) -> str:
        """``PAAGENT-E-1791510000123``：既标记"程序单"，又保证唯一。"""
        stamp = int(time.time() * 1000) % 10**13
        return f"{tag}-{kind}-{stamp}"[:36]

    def _fmt_symbol_qty(self, inst_id: str, qty: float) -> str:
        spec = binance_filters(self.exchange_info(), to_binance_symbol(inst_id))
        return _fmt_step(qty, spec.get("step", 0.0))

    def exchange_info(self) -> list[dict[str, Any]]:
        """合约规格（进程内缓存）。"""
        if self._instruments:
            return list(self._instruments.values())
        rows = self.request("GET", "/fapi/v1/exchangeInfo", signed=False)
        for row in (rows or {}).get("symbols") or []:
            self._instruments[str(row.get("symbol") or "")] = row
        return list(self._instruments.values())


def binance_filters(
    instruments: list[dict[str, Any]], symbol: str
) -> dict[str, float]:
    """取该品种的 tick/step/minQty/最小名义额。"""
    out = {"tick": 0.0, "step": 0.0, "min_qty": 0.0, "min_notional": 0.0}
    row = next((r for r in instruments if str(r.get("symbol")) == symbol), None)
    if row is None:
        return out
    for f in row.get("filters") or []:
        kind = f.get("filterType")
        if kind == "PRICE_FILTER":
            out["tick"] = _as_float(f.get("tickSize")) or 0.0
        elif kind == "LOT_SIZE":
            out["step"] = _as_float(f.get("stepSize")) or 0.0
            out["min_qty"] = _as_float(f.get("minQty")) or 0.0
        elif kind == "MIN_NOTIONAL":
            out["min_notional"] = _as_float(f.get("notional")) or 0.0
    return out


# ── 止损意图（成交后补挂的那份记录）───────────────────────────────────────────


def _load_stop_intents(path: Path | None = None) -> dict[str, dict[str, Any]]:
    p = path or _STOP_INTENT_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_stop_intents(data: dict[str, dict[str, Any]], path: Path | None = None) -> None:
    p = path or _STOP_INTENT_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        logger.warning("写入止损意图失败（%s）：%s", p, exc)


# ── 执行器 ────────────────────────────────────────────────────────────────────


class BinanceTrader:
    """把决策变成币安订单（接口与 ``OkxTrader`` 一致，可互换）。"""

    venue = "binance"

    def __init__(
        self,
        trading_settings: Any,
        *,
        credentials: BinanceCredentials | None = None,
        client: BinancePrivateClient | None = None,
        settings: Any = None,
    ) -> None:
        self._settings = trading_settings
        self._full_settings = settings
        self._cred = credentials
        self._client = client

    @classmethod
    def from_settings(cls, settings: Any, *, market_source: Any = None) -> BinanceTrader:
        from pa_agent.trading.gateway import credentials_path_for

        trading = getattr(settings, "trading", None)
        cred = BinanceCredentials.load(
            credentials_path_for(settings, "binance"),
            simulated=bool(getattr(trading, "simulated", True)),
        )
        return cls(trading, credentials=cred, settings=settings)

    # ── 状态 ──────────────────────────────────────────────────────────────────

    @property
    def credentials(self) -> BinanceCredentials | None:
        return self._cred

    @property
    def client(self) -> BinancePrivateClient:
        if self._client is None:
            if self._cred is None:
                raise BinanceTradeError("未配置币安 API 凭据")
            self._client = BinancePrivateClient(
                self._cred, proxy=resolve_proxy("binance", self._full_settings)
            )
        return self._client

    def status_text(self) -> str:
        s = self._settings
        if not getattr(s, "enabled", False):
            return "交易：关闭"
        if self._cred is None or not self._cred.complete:
            return "交易：已开启但缺少币安 API 凭据"
        mode = "自动" if getattr(s, "trigger_mode", "manual") == "auto" else "手动"
        venue = "测试网" if self._cred.simulated else "实盘"
        cap = float(getattr(s, "max_loss_per_trade_usd", 0.0))
        return f"交易：币安{venue} · {mode} · 每笔上限 {cap:.0f} USDT"

    # ── 合约规格 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _canonical(inst_id: str) -> str:
        """入参可能是规范写法（``BTC-USDT-SWAP``）也可能是币安代码（``BTCUSDT``）。"""
        text = str(inst_id or "").strip().upper()
        return text if "-" in text else from_binance_symbol(text)

    def spec_for(self, inst_id: str) -> InstrumentSpec:
        """取 tick/step/minQty 组成统一规格。

        币安的下单量单位就是**标的币数量**，没有 ctVal 概念 —— 等价于 ``ct_val=1``，
        于是共用的 ``plan_order`` 能直接算出正确张数（这里"张"就是币数）。

        返回的 ``inst_id`` 一律是**规范写法**（``BTC-USDT-SWAP``），这样计划、
        止损意图、日志在 OKX / 币安两边长得一样，上层不用关心交易所。
        """
        canonical = self._canonical(inst_id)
        symbol = to_binance_symbol(canonical)
        rows = self.client.exchange_info()
        row = next((r for r in rows if str(r.get("symbol")) == symbol), None)
        if row is None:
            raise TradeRejected(f"币安查不到合约规格：{inst_id}（{symbol}）")
        filters = binance_filters(rows, symbol)
        return InstrumentSpec(
            inst_id=canonical,
            inst_type="SWAP",
            tick_sz=filters["tick"],
            lot_sz=filters["step"],
            min_sz=filters["min_qty"],
            ct_val=1.0,
            ct_val_ccy=str(row.get("baseAsset") or ""),
        )

    def min_notional(self, inst_id: str) -> float:
        """该品种的最小名义额（多数币安合约是 5 USDT）；读不到按 0 处理。"""
        return binance_filters(
            self.client.exchange_info(), to_binance_symbol(self._canonical(inst_id))
        )["min_notional"]

    # ── 主流程 ────────────────────────────────────────────────────────────────

    def execute(
        self,
        decision: dict[str, Any],
        *,
        symbol: str,
        dry_run: bool = True,
        manual_confirm: bool = False,
        price: float | None = None,
    ) -> ExecutionResult:
        """闸门 → 规划 → 下单 → 补挂止损。``dry_run=True`` 时只返回计划。"""
        s = self._settings
        cred = self._cred

        guard = evaluate_guard(
            enabled=bool(getattr(s, "enabled", False)),
            credentials_present=bool(cred and cred.complete),
            trigger_mode=str(getattr(s, "trigger_mode", "manual")),
            manual_confirm=manual_confirm,
            simulated=bool(getattr(cred, "simulated", True)),
            live_acknowledged=bool(getattr(s, "live_ack", False)),
            symbol=symbol,
            decision=decision,
            min_confidence=int(getattr(s, "min_confidence", 60)),
            max_open_positions=int(getattr(s, "max_open_positions", 1)),
            open_positions=self._open_positions_safe(),
            daily_loss_cap_usd=float(getattr(s, "daily_loss_cap_usd", 30.0)),
            realized_pnl_today_usd=self._daily_pnl_safe(),
            allowed_symbols=getattr(s, "allowed_symbols", []) or (),
        )
        if not guard.ok:
            return ExecutionResult(
                sent=False, guard=guard, dry_run=dry_run, message="；".join(guard.blocked)
            )

        if self._hedge_mode_blocked():
            return ExecutionResult(
                sent=False,
                guard=guard,
                dry_run=dry_run,
                message="币安账户处于双向持仓模式，本网关只支持单向持仓，已拒单",
            )

        pending = self._pending_orders_for(symbol)
        if pending is None:
            return ExecutionResult(
                sent=False,
                guard=guard,
                dry_run=dry_run,
                message=f"无法确认 {symbol} 的挂单状态（接口异常），按保守策略跳过",
            )
        if pending > 0:
            return ExecutionResult(
                sent=False,
                guard=guard,
                dry_run=dry_run,
                message=f"{symbol} 已有 {pending} 张未成交挂单，跳过（避免重复建仓）",
            )
        if self._has_open_position_for(symbol):
            return ExecutionResult(
                sent=False,
                guard=guard,
                dry_run=dry_run,
                message=f"{symbol} 已有持仓，跳过（不加仓：加仓会让单笔风险翻倍）",
            )

        spec = self.spec_for(symbol)
        plan = plan_order(
            decision,
            spec,
            equity_usd=self.client.equity_usd(),
            max_loss_usd=float(getattr(s, "max_loss_per_trade_usd", 10.0)),
            leverage=int(getattr(s, "leverage", 3)),
            price=price,
            max_notional_usd=float(getattr(s, "max_notional_usd", 0.0) or 0.0),
        )
        floor = self.min_notional(symbol)
        if floor and plan.notional_usd < floor:
            return ExecutionResult(
                sent=False,
                guard=guard,
                dry_run=dry_run,
                message=(
                    f"币安要求 {symbol} 最小名义额 {floor:.0f} USDT，"
                    f"当前只有 {plan.notional_usd:.2f} USDT（风险预算太小），不下单"
                ),
            )
        if dry_run:
            return ExecutionResult(
                sent=False, plan=plan, guard=guard, dry_run=True, message=f"[演练] {plan.summary}"
            )

        self.client.set_leverage(plan.inst_id, plan.leverage)
        row = self.client.place_entry(
            inst_id=plan.inst_id,
            side=plan.side,
            ord_type=plan.ord_type,
            size=plan.size,
            price=plan.price if plan.ord_type == "limit" else None,
            trigger_px=plan.price if plan.ord_type == "trigger" else None,
            step=spec.lot_sz,
            tick=spec.tick_sz,
        )
        note = self._place_protection(plan, row, spec)
        return ExecutionResult(
            sent=True,
            plan=plan,
            ord_id=str(row.get("ordId", "")),
            guard=guard,
            message=plan.summary + note,
        )

    def _place_protection(
        self, plan: OrderPlan, entry_row: dict[str, Any], spec: InstrumentSpec
    ) -> str:
        """成交了就补挂止损；没成交就记下意图，交给 :meth:`ensure_stops`。"""
        order_id = str(entry_row.get("ordId") or "")
        filled = (_as_float(entry_row.get("executedQty")) or 0.0) > 0 or str(
            entry_row.get("status") or ""
        ).upper() == "FILLED"
        if not filled and order_id:
            try:
                latest = self.client.order_status(plan.inst_id, order_id)
                filled = (_as_float(latest.get("executedQty")) or 0.0) > 0 or str(
                    latest.get("status") or ""
                ).upper() == "FILLED"
            except BinanceTradeError as exc:
                logger.warning("读取币安订单状态失败：%s", exc)

        self._record_intent(plan, filled)
        if not filled:
            return (
                f"｜挂单未成交（币安不支持附带止损）：成交后由程序补挂止损 {plan.stop_px}"
                "（已记入意图文件，每轮监控会核对）"
            )

        note = self._place_stop_for(plan, spec)
        if bool(getattr(self._settings, "attach_take_profit", False)) and plan.take_profit_px:
            try:
                self.client.place_stop(
                    inst_id=plan.inst_id,
                    side="sell" if plan.side == "buy" else "buy",
                    stop_px=plan.take_profit_px,
                    size=plan.size,
                    tick=spec.tick_sz,
                    step=spec.lot_sz,
                    kind="take_profit",
                )
                note += f"｜止盈已挂（触发价 {plan.take_profit_px}）"
            except BinanceTradeError as exc:
                note += f"｜⚠️ 止盈挂单失败：{exc}"
                logger.warning("币安挂止盈失败 %s：%s", plan.inst_id, exc)
        return note

    def _place_stop_for(self, plan: OrderPlan, spec: InstrumentSpec) -> str:
        try:
            self.client.place_stop(
                inst_id=plan.inst_id,
                side="sell" if plan.side == "buy" else "buy",
                stop_px=plan.stop_px,
                size=plan.size,
                tick=spec.tick_sz,
                step=spec.lot_sz,
                kind="stop",
            )
        except BinanceTradeError as exc:
            logger.warning(
                "⚠️ %s 已成交但止损挂单失败，请立即到币安手动补挂：%s", plan.inst_id, exc
            )
            return f"｜⚠️ 止损挂单失败（{exc}），请立即到币安手动补挂"
        return f"｜止损已挂（触发价 {plan.stop_px}）"

    # ── 止损核对 / 补挂 ───────────────────────────────────────────────────────

    def ensure_stops(self, inst_id: str | None = None) -> list[str]:
        """核对**交易所上的真实持仓**，缺止损就补挂。

        2026-10-10 修（实盘踩过）：原来只扫"意图文件里的品种"，一旦意图记录丢失、
        或该品种从监控列表里轮换出去，就再也没人管它——US/ZK/BTC 三笔限价单成交后
        就是这么裸奔的。现在改成**以交易所持仓为准**：

        1. 有持仓、有意图记录 → 按 记录里的止损价 补挂；
        2. 有持仓、**没有意图记录** → 按「每笔最多亏 ``max_loss_per_trade_usd``」
           从持仓与开仓均价反推止损价，直接补挂（不再只报警）。
        """
        notes: list[str] = []
        intents = _load_stop_intents()
        if inst_id:
            symbols = [inst_id]
        else:
            try:
                symbols = [str(p.get("instId") or "") for p in self.client.positions()]
            except BinanceTradeError as exc:
                return [f"读取持仓失败（无法核对止损）：{exc}"]
        for symbol in symbols:
            if not symbol:
                continue
            try:
                rows = self.client.positions(inst_id=symbol)
            except BinanceTradeError as exc:
                notes.append(f"{symbol} 持仓查询失败：{exc}")
                continue
            if not rows:
                if symbol in intents:          # 仓位已经没了 → 清掉旧的意图
                    intents.pop(symbol, None)
                    _save_stop_intents(intents)
                continue
            amount = _as_float(rows[0].get("pos")) or 0.0
            if abs(amount) <= 0:
                continue
            try:
                if self._has_stop_order(symbol):
                    continue
            except BinanceTradeError as exc:
                notes.append(f"{symbol} 止损失败核对：{exc}")
                continue
            stop_px = _as_float((intents.get(symbol) or {}).get("stop_px"))
            if not stop_px:
                stop_px = self._safety_stop_px(
                    _as_float(rows[0].get("avgPx")) or 0.0, amount
                )
                if stop_px is None:
                    notes.append(
                        f"⚠️ {symbol} 有持仓但没有止损，也算不出安全距离"
                        "（可能仓位过小），请手动处理"
                    )
                    logger.warning("⚠️ %s 有持仓但无止损且算不出安全距离，请手动补挂", symbol)
                    continue
                logger.warning(
                    "⚠️ %s 有持仓但无止损意图记录，按「每笔最多亏 %.2f USDT」自动补挂 %.8g",
                    symbol,
                    float(getattr(self._settings, "max_loss_per_trade_usd", 0.0)),
                    stop_px,
                )
                intents[symbol] = {
                    "stop_px": stop_px, "size": abs(amount),
                    "side": "buy" if amount > 0 else "sell", "filled": True,
                    "ts": int(time.time() * 1000), "note": "程序兜底补挂",
                }
                _save_stop_intents(intents)
            side = "sell" if amount > 0 else "buy"
            try:
                spec = self.spec_for(symbol)
                self.client.place_stop(
                    inst_id=symbol,
                    side=side,
                    stop_px=stop_px,
                    size=abs(amount),
                    tick=spec.tick_sz,
                    step=spec.lot_sz,
                )
                notes.append(f"{symbol} 已补挂止损（触发价 {stop_px}）")
                logger.warning("补挂止损成功：%s 触发价 %s", symbol, stop_px)
            except (BinanceTradeError, TradeRejected) as exc:
                notes.append(f"⚠️ {symbol} 补挂止损失败：{exc}")
                logger.warning("补挂止损失败 %s：%s", symbol, exc)
        return notes

    def _safety_stop_px(self, entry_px: float, amount: float) -> float | None:
        """按「每笔最多亏 N USDT」从持仓反推一个保命止损价；算不出来返回 None。

        币安 ``positionAmt`` 就是标的币数量，所以：亏损 = |数量| × 价格距离。
        """
        cap = float(getattr(self._settings, "max_loss_per_trade_usd", 0.0) or 0.0)
        if cap <= 0 or entry_px <= 0 or abs(amount) <= 0:
            return None
        distance = cap / abs(amount)
        stop = entry_px - distance if amount > 0 else entry_px + distance
        return stop if stop > 0 else None

    def _has_stop_order(self, symbol: str) -> bool:
        """该品种是否已有止损单（查 Algo 条件单，也兼容普通挂单里的止损）。"""
        for row in self.client.algo_pending(inst_id=symbol):
            if str(row.get("type", "")).upper() in ("STOP_MARKET", "STOP"):
                return True
        for row in self.client.pending_orders(inst_id=symbol):
            if str(row.get("type", "")).upper() in ("STOP_MARKET", "STOP"):
                return True
        return False

    def _record_intent(self, plan: OrderPlan, filled: bool) -> None:
        intents = _load_stop_intents()
        intents[plan.inst_id] = {
            "stop_px": plan.stop_px,
            "size": plan.size,
            "side": plan.side,
            "filled": bool(filled),
            "ts": int(time.time() * 1000),
        }
        _save_stop_intents(intents)

    # ── 平仓 / 撤单 ───────────────────────────────────────────────────────────

    def close_all(self, inst_id: str) -> dict[str, Any]:
        row = self.client.close_position(inst_id)
        intents = _load_stop_intents()
        if inst_id in intents:
            intents.pop(inst_id, None)
            _save_stop_intents(intents)
        return row

    def cancel_stale_entries(
        self, inst_id: str, timeframe: str, *, max_bars: int, now_ms: int | None = None
    ) -> list[dict[str, Any]]:
        """撤掉超过 ``max_bars`` 根 K 线未成交的**程序**入场挂单（不碰手动单）。"""
        if int(max_bars or 0) <= 0:
            return []
        from pa_agent.data.bar_close_wait import timeframe_to_seconds

        bar_seconds = timeframe_to_seconds(timeframe)
        if not bar_seconds:
            return []
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        max_age_ms = int(max_bars) * bar_seconds * 1000

        try:
            rows = self.client.pending_orders(inst_id=inst_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("查询 %s 挂单失败，跳过过期清理: %s", inst_id, exc)
            return []

        cancelled: list[dict[str, Any]] = []
        for row in rows:
            if str(row.get("tag") or "") != ORDER_TAG:
                continue                       # 不是程序下的单 → 绝不碰
            if str(row.get("type", "")).upper() in _STOP_TYPES:
                continue                       # 止损/止盈单不按"入场挂单过期"处理
            try:
                created = int(row.get("cTime") or 0)
            except (TypeError, ValueError):
                continue
            if created <= 0 or now - created < max_age_ms:
                continue
            try:
                self.client.cancel_order(inst_id, str(row.get("ordId")))
            except Exception as exc:  # noqa: BLE001
                logger.warning("撤销过期挂单失败 %s %s: %s", inst_id, row.get("ordId"), exc)
                continue
            cancelled.append(row)
            logger.warning(
                "撤掉过期入场挂单：%s %s sz=%s 价=%s（挂了 %.0f 分钟，超过 %d 根 %s K 线）",
                inst_id, row.get("side"), row.get("sz"), row.get("px"),
                (now - created) / 60_000.0, max_bars, timeframe,
            )
        return cancelled

    # ── 内部 ──────────────────────────────────────────────────────────────────

    def _hedge_mode_blocked(self) -> bool:
        try:
            return bool(self.client.dual_side_position())
        except BinanceTradeError as exc:
            logger.warning("读取币安持仓模式失败（按单向处理）：%s", exc)
            return False

    def _open_positions_safe(self) -> int:
        try:
            return self.client.open_position_count()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取持仓失败: %s", exc)
            return 0

    def _pending_orders_for(self, symbol: str) -> int | None:
        """该品种未成交挂单数；查询失败返回 None（调用方按保守策略处理）。"""
        try:
            return len(self.client.pending_orders(inst_id=symbol))
        except Exception as exc:  # noqa: BLE001
            logger.warning("查询 %s 未成交挂单失败: %s", symbol, exc)
            return None

    def _has_open_position_for(self, symbol: str) -> bool:
        """该品种是否已有持仓；查询失败时按「有」处理（保守，拒绝加仓）。"""
        try:
            for row in self.client.positions(inst_id=symbol):
                if abs(_as_float(row.get("pos")) or 0.0) > 0:
                    return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("查询 %s 持仓失败（按有持仓处理）: %s", symbol, exc)
            return True
        return False

    def _daily_pnl_safe(self) -> float | None:
        try:
            return self.client.realized_pnl_today_usd()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取当日盈亏失败: %s", exc)
            return None
