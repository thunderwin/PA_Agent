"""OKX 下单执行层 —— 本项目里唯一会动用真实资金的部分。

安全设计（改代码前先读完）
--------------------------

1. **默认全关**：``trading.enabled`` 默认 False，凭据缺失时直接拒单。
2. **默认模拟盘**：``trading.simulated=True`` → 请求头带 ``x-simulated-trading: 1``，
   只有显式改成 False 才会碰实盘。
3. **每笔先算亏损**：用 入场价/止损价 的距离反推下单量，使「止损被打到」的亏损
   ≤ ``trading.max_loss_per_trade_usd``（默认 10 USDT），算不出来就不下单。
4. **没有止损就不下单**：AI 输出必须有 ``stop_loss_price``，且必须在正确一侧。
5. **止损挂在交易所**：下单时通过 ``attachAlgoOrds`` 把止损/止盈一并托管到 OKX，
   不依赖本程序活着。
6. **当日亏损熔断**：读取账户账单统计当日已实现亏损，超过 ``daily_loss_cap_usd``
   当天不再下单。
7. **触发方式**：``trading.trigger_mode`` = ``manual``（默认，分析完只提示，人工点
   「执行下单」）/ ``auto``（新 K 线收盘、分析结束且满足条件时自动下单）。
8. **凭据不进 settings.json**：只从环境变量或 ``config/okx_trading.json``（已 gitignore）
   读取；本模块永远不打印密钥。

OKX 私有接口签名：``base64(hmac_sha256(secret, ts + method + requestPath + body))``，
``ts`` 用 ISO8601 毫秒（``2026-10-06T09:08:57.715Z``）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import os
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pa_agent.data.okx_source import OKX_BASE_URL, OkxSource, is_derivative_symbol

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_DEFAULT_TIMEOUT_S = 10.0

_ORDER_TYPE_LIMIT = "限价单"
_ORDER_TYPE_BREAKOUT = "突破单"
_ORDER_TYPE_MARKET = "市价单"
_SUPPORTED_ORDER_TYPES = (_ORDER_TYPE_LIMIT, _ORDER_TYPE_BREAKOUT, _ORDER_TYPE_MARKET)


def is_executable_decision(decision: Any) -> bool:
    """决策里是否有可执行订单（限价/突破/市价 + 方向 + 止损价）。"""
    if not isinstance(decision, dict):
        return False
    if str(decision.get("order_type") or "").strip() not in _SUPPORTED_ORDER_TYPES:
        return False
    if str(decision.get("order_direction") or "").strip() not in ("做多", "做空"):
        return False
    return _as_float(decision.get("stop_loss_price")) is not None


class OkxTradeError(Exception):
    """OKX 私有接口错误 / 网络错误。"""


class TradeRejected(Exception):
    """风控或参数校验不通过 —— 没有任何请求发出去。"""


# ── 凭据 ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OkxCredentials:
    """OKX API 凭据（只存在于内存里）。"""

    api_key: str
    secret_key: str
    passphrase: str
    simulated: bool = True

    @property
    def complete(self) -> bool:
        return bool(self.api_key and self.secret_key and self.passphrase)

    def mask(self) -> str:
        key = self.api_key
        shown = f"{key[:4]}…{key[-4:]}" if len(key) > 8 else "****"
        return f"{shown}{'（模拟盘）' if self.simulated else '（实盘）'}"

    @classmethod
    def from_env(cls, *, simulated: bool = True) -> OkxCredentials | None:
        api_key = os.environ.get("OKX_API_KEY", "").strip()
        secret_key = os.environ.get("OKX_SECRET_KEY", "").strip()
        passphrase = os.environ.get("OKX_PASSPHRASE", "").strip()
        if not (api_key and secret_key and passphrase):
            return None
        return cls(api_key, secret_key, passphrase, simulated=simulated)

    @classmethod
    def from_file(cls, path: str | Path, *, simulated: bool = True) -> OkxCredentials | None:
        """从凭据文件读取三件套。

        ``simulated`` 只认调用方（settings）传进来的值，文件里即使写了 ``simulated``
        也会被忽略 —— 模拟盘/实盘全局只有一个开关，避免两处配置打架。
        """
        p = Path(path)
        if not p.exists():
            return None
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("OKX 凭据文件读取失败: %s", exc)
            return None
        if not isinstance(raw, dict):
            return None
        api_key = str(raw.get("api_key", "")).strip()
        secret_key = str(raw.get("secret_key", "")).strip()
        passphrase = str(raw.get("passphrase", "")).strip()
        if not (api_key and secret_key and passphrase):
            return None
        return cls(api_key, secret_key, passphrase, simulated=simulated)

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        simulated: bool = True,
    ) -> OkxCredentials | None:
        """环境变量优先，其次凭据文件。"""
        return cls.from_env(simulated=simulated) or (
            cls.from_file(path, simulated=simulated) if path else None
        )


# ── 签名 ──────────────────────────────────────────────────────────────────────


def sign_request(
    secret_key: str, timestamp: str, method: str, request_path: str, body: str = ""
) -> str:
    """OKX v5 签名：``base64(hmac_sha256(secret, ts + method + path + body))``。"""
    message = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _iso_timestamp() -> str:
    """OKX 要求的 ISO8601 毫秒时间戳，例如 ``2026-10-06T09:08:57.715Z``。"""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _day_start_ms(tz_offset_hours: int = 8) -> int:
    """本地（默认 UTC+8）当天 00:00 的毫秒时间戳。"""
    now_s = time.time()
    offset_s = tz_offset_hours * 3600
    local = now_s + offset_s
    day_start_local = math.floor(local / 86_400) * 86_400
    return int((day_start_local - offset_s) * 1000)


def _fmt_num(value: float) -> str:
    """OKX 参数格式：避免科学计数法，去掉多余 0。"""
    text = f"{float(value):.10f}".rstrip("0").rstrip(".")
    return text or "0"


def _send_http(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> str:
    """发一个 HTTP 请求并返回响应文本（requests 优先，兜底标准库）。"""
    try:
        import requests  # type: ignore
    except ImportError:  # pragma: no cover
        requests = None  # type: ignore[assignment]

    if requests is not None:
        try:
            resp = requests.request(method, url, headers=headers, data=body, timeout=timeout)
        except Exception as exc:
            raise OkxTradeError(f"OKX 网络请求失败: {exc}") from exc
        if resp.status_code != 200:
            raise OkxTradeError(f"OKX HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.text

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except Exception as exc:
        raise OkxTradeError(f"OKX 网络请求失败: {exc}") from exc


# ── 私有 REST 客户端 ──────────────────────────────────────────────────────────


class OkxPrivateClient:
    """OKX 私有接口（账户 / 持仓 / 下单），带签名与模拟盘请求头。"""

    def __init__(
        self,
        credentials: OkxCredentials,
        *,
        base_url: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        now: Callable[[], str] | None = None,
    ) -> None:
        if not credentials.complete:
            raise OkxTradeError("OKX 凭据不完整（需要 api_key / secret_key / passphrase）")
        self._cred = credentials
        self._base_url = (base_url or OKX_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._now = now or _iso_timestamp

    @property
    def simulated(self) -> bool:
        return self._cred.simulated

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """调用私有接口并返回 ``data``；``code != 0`` 抛 :class:`OkxTradeError`。"""
        method = method.upper()
        body_text = json.dumps(body, separators=(",", ":")) if body else ""
        request_path = path
        if params:
            request_path = f"{path}?{urllib.parse.urlencode(params)}"
        url = f"{self._base_url}{request_path}"

        timestamp = self._now()
        sign = sign_request(self._cred.secret_key, timestamp, method, request_path, body_text)
        headers = {
            "User-Agent": _USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "OK-ACCESS-KEY": self._cred.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self._cred.passphrase,
        }
        if self._cred.simulated:
            headers["x-simulated-trading"] = "1"

        raw = _send_http(method, url, headers, body_text.encode("utf-8") or None, self._timeout)
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise OkxTradeError("OKX 返回内容不是 JSON") from exc
        if not isinstance(payload, dict):
            raise OkxTradeError("OKX 返回格式异常")
        code = str(payload.get("code", ""))
        if code != "0":
            detail = payload.get("data")
            raise OkxTradeError(
                f"OKX 接口错误 {code}: {payload.get('msg') or ''} {detail or ''}".strip()
            )
        return payload.get("data")

    # ── 账户 ──────────────────────────────────────────────────────────────────

    def balance(self, ccy: str = "USDT") -> list[dict[str, Any]]:
        data = self.request("GET", "/api/v5/account/balance", params={"ccy": ccy})
        return list(data or [])

    def equity_usd(self, ccy: str = "USDT") -> float:
        """账户权益（USDT 口径）；取不到时返回 0.0。"""
        try:
            rows = self.balance(ccy)
        except OkxTradeError as exc:
            logger.warning("读取 OKX 账户权益失败: %s", exc)
            return 0.0
        if not rows:
            return 0.0
        total = rows[0].get("totalEq")
        if total not in (None, ""):
            try:
                return float(total)
            except (TypeError, ValueError):
                pass
        for item in rows[0].get("details", []) or []:
            if str(item.get("ccy", "")).upper() == ccy.upper():
                for key in ("eq", "availEq", "cashBal", "availBal"):
                    value = _as_float(item.get(key))
                    if value is not None:
                        return value
        return 0.0

    def positions(
        self, inst_id: str | None = None, inst_type: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if inst_type:
            params["instType"] = inst_type
        if inst_id:
            params["instId"] = inst_id
        data = self.request("GET", "/api/v5/account/positions", params=params or None)
        return list(data or [])

    def open_position_count(self, inst_type: str | None = None) -> int:
        rows = self.positions(inst_type=inst_type)
        return sum(1 for r in rows if abs(_as_float(r.get("pos")) or 0.0) > 0)

    def realized_pnl_today_usd(self, tz_offset_hours: int = 8) -> float:
        """当日（默认 UTC+8）已实现盈亏 + 手续费，用于日亏熔断。"""
        start = _day_start_ms(tz_offset_hours)
        total = 0.0
        for bill_type in ("2", "3", "4"):  # 交易 / 资金费 / 强平
            try:
                data = self.request(
                    "GET",
                    "/api/v5/account/bills",
                    # 不限定 instType：现货与永续的当日盈亏都要算进熔断
                    params={"type": bill_type, "limit": "100"},
                )
            except OkxTradeError as exc:
                raise OkxTradeError(f"读取当日账单失败（无法确认当日亏损）: {exc}") from exc
            for row in data or []:
                try:
                    if int(row.get("ts") or 0) < start:
                        continue
                    total += float(row.get("pnl") or 0.0) + float(row.get("fee") or 0.0)
                except (TypeError, ValueError):
                    continue
        return total

    # ── 交易 ──────────────────────────────────────────────────────────────────

    def set_leverage(self, inst_id: str, leverage: int, mgn_mode: str = "cross") -> None:
        self.request(
            "POST",
            "/api/v5/account/set-leverage",
            body={"instId": inst_id, "lever": str(int(leverage)), "mgnMode": mgn_mode},
        )

    def place_order(
        self,
        *,
        inst_id: str,
        side: str,
        ord_type: str,
        size: float,
        price: float | None = None,
        trigger_px: float | None = None,
        stop_px: float | None = None,
        take_profit_px: float | None = None,
        td_mode: str = "cross",
        pos_side: str | None = "net",
    ) -> dict[str, Any]:
        """下单；``stop_px`` / ``take_profit_px`` 作为 ``attachAlgoOrds`` 托管给交易所。"""
        body: dict[str, Any] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": ord_type,
            "sz": _fmt_num(size),
        }
        if not is_derivative_symbol(inst_id):
            body["tgtCcy"] = "base_ccy"  # 现货：sz 按基础币数量
        elif pos_side:
            body["posSide"] = pos_side

        if ord_type == "limit":
            if price is None:
                raise TradeRejected("限价单缺少价格")
            body["px"] = _fmt_num(price)
        elif ord_type == "trigger":
            if trigger_px is None:
                raise TradeRejected("突破单缺少触发价")
            body["triggerPx"] = _fmt_num(trigger_px)
            body["orderPx"] = "-1"  # 触发后市价成交

        algo: dict[str, Any] = {}
        if stop_px is not None:
            algo["slTriggerPx"] = _fmt_num(stop_px)
            algo["slOrdPx"] = "-1"
        if take_profit_px is not None:
            algo["tpTriggerPx"] = _fmt_num(take_profit_px)
            algo["tpOrdPx"] = "-1"
        if algo:
            body["attachAlgoOrds"] = [algo]

        data = self.request("POST", "/api/v5/trade/order", body=body)
        row = data[0] if isinstance(data, list) and data else {}
        if str(row.get("sCode", "0")) != "0":
            raise OkxTradeError(f"下单被拒: {row.get('sMsg') or row}")
        logger.info(
            "OKX 下单成功 instId=%s side=%s sz=%s ordId=%s",
            inst_id,
            side,
            size,
            row.get("ordId"),
        )
        return dict(row)

    def cancel_order(self, inst_id: str, ord_id: str) -> dict[str, Any]:
        data = self.request(
            "POST", "/api/v5/trade/cancel-order", body={"instId": inst_id, "ordId": ord_id}
        )
        return dict(data[0]) if isinstance(data, list) and data else {}

    def close_position(
        self, inst_id: str, mgn_mode: str = "cross", pos_side: str | None = "net"
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"instId": inst_id, "mgnMode": mgn_mode, "autoCxl": True}
        if pos_side:
            body["posSide"] = pos_side
        data = self.request("POST", "/api/v5/trade/close-position", body=body)
        return dict(data[0]) if isinstance(data, list) and data else {}


# ── 订单规划（核心：每笔最大亏损）─────────────────────────────────────────────


@dataclass(frozen=True)
class InstrumentSpec:
    """下单需要的合约规格（来自 /public/instruments）。"""

    inst_id: str
    inst_type: str
    tick_sz: float
    lot_sz: float
    min_sz: float
    ct_val: float = 0.0
    ct_val_ccy: str = ""

    @property
    def derivative(self) -> bool:
        return self.inst_type.upper() in ("SWAP", "FUTURES")

    @classmethod
    def from_okx(cls, raw: dict[str, Any]) -> InstrumentSpec:
        return cls(
            inst_id=str(raw.get("instId", "")),
            inst_type=str(raw.get("instType", "")),
            tick_sz=_as_float(raw.get("tickSz")) or 0.0,
            lot_sz=_as_float(raw.get("lotSz")) or 0.0,
            min_sz=_as_float(raw.get("minSz")) or 0.0,
            ct_val=_as_float(raw.get("ctVal")) or 0.0,
            ct_val_ccy=str(raw.get("ctValCcy") or ""),
        )


@dataclass(frozen=True)
class OrderPlan:
    """一笔待执行订单：数量已经按「最大亏损」反推好。"""

    inst_id: str
    side: str  # buy / sell
    ord_type: str  # limit / market / trigger
    size: float  # 永续=张数，现货=基础币数量
    price: float | None
    stop_px: float
    take_profit_px: float | None
    risk_usd: float  # 止损被打到时的亏损（USDT）
    notional_usd: float
    leverage: int
    stop_distance: float = 0.0  # 入场价与止损价的距离（价格单位）
    base_qty: float = 0.0  # 折算成基础币的数量（现货即 size）
    notes: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        bits = [
            f"{self.inst_id} {self.side} {self.ord_type} sz={_fmt_num(self.size)}",
            f"止损亏损≈{self.risk_usd:.2f} USDT",
            f"名义={self.notional_usd:.2f} USDT",
        ]
        if self.price:
            bits.append(f"价={_fmt_num(self.price)}")
        bits.append(f"止损={_fmt_num(self.stop_px)}")
        if self.notes:
            bits.append("；".join(self.notes))
        return " · ".join(bits)


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


def _floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(round(value / step, 9)) * step


def _round_to_tick(value: float, tick: float) -> float:
    if tick <= 0:
        return value
    return round(round(value / tick) * tick, 10)


def plan_order(
    decision: dict[str, Any],
    spec: InstrumentSpec,
    *,
    equity_usd: float,
    max_loss_usd: float,
    leverage: int = 3,
    price: float | None = None,
) -> OrderPlan:
    """把阶段二决策翻译成具体订单，并保证止损亏损 ≤ ``max_loss_usd``。

    ``price`` 用于市价单（用最新价估算风险）；缺省时用决策里的 entry_price。
    """
    if not isinstance(decision, dict):
        raise TradeRejected("决策内容为空，无法下单")

    order_type = str(decision.get("order_type") or "").strip()
    if order_type not in _SUPPORTED_ORDER_TYPES:
        raise TradeRejected(f"当前决策为「{order_type or '未知'}」，不下单")

    direction = str(decision.get("order_direction") or "").strip()
    if direction not in ("做多", "做空"):
        raise TradeRejected(f"决策方向无效（{direction or '缺失'}）")

    entry = _as_float(decision.get("entry_price"))
    stop = _as_float(decision.get("stop_loss_price"))
    tp = _as_float(decision.get("take_profit_price"))
    if entry is None or entry <= 0:
        raise TradeRejected("决策缺少有效入场价，无法计算亏损")
    if stop is None or stop <= 0:
        raise TradeRejected("决策缺少止损价：本程序拒绝未带止损的方案")
    if equity_usd <= 0:
        raise TradeRejected("账户权益为 0（或读取失败），拒绝下单")

    long = direction == "做多"
    if long and stop >= entry:
        raise TradeRejected(f"做多但止损({stop})未低于入场({entry})，价格方向矛盾")
    if (not long) and stop <= entry:
        raise TradeRejected(f"做空但止损({stop})未高于入场({entry})，价格方向矛盾")

    risk_per_unit = abs(entry - stop)
    if risk_per_unit <= 0:
        raise TradeRejected("止损距离为 0")

    notes: list[str] = []
    side = "buy" if long else "sell"
    ord_type = {
        _ORDER_TYPE_LIMIT: "limit",
        _ORDER_TYPE_BREAKOUT: "trigger",
        _ORDER_TYPE_MARKET: "market",
    }[order_type]

    # 市价单：用现价估算风险
    if ord_type == "market" and price and price > 0:
        risk_per_unit = abs(price - stop)
        if risk_per_unit <= 0:
            raise TradeRejected("现价与止损价重合，无法评估风险")
        notes.append(f"按现价 {_fmt_num(price)} 估算")

    if spec.derivative:
        if spec.ct_val <= 0:
            raise TradeRejected(f"{spec.inst_id} 缺少 ctVal，无法换算每张风险")
        risk_per_unit_effective = risk_per_unit * spec.ct_val
        unit_notional = spec.ct_val * entry
    else:
        risk_per_unit_effective = risk_per_unit
        unit_notional = entry
    if risk_per_unit_effective <= 0 or unit_notional <= 0:
        raise TradeRejected("无法换算每单位风险")

    lot = spec.lot_sz or (1e-8 if not spec.derivative else 1.0)
    max_sz_by_budget = _floor_to_step(max_loss_usd / risk_per_unit_effective, lot)

    # 资金上限：永续按杠杆可用保证金，现货按可用资金
    multiplier = float(max(int(leverage), 1)) if spec.derivative else 1.0
    budget_equity = equity_usd * (1.0 if spec.derivative else 0.95)
    max_sz_by_equity = _floor_to_step(budget_equity * multiplier / unit_notional, lot)

    size = min(max_sz_by_budget, max_sz_by_equity)
    if max_sz_by_equity < max_sz_by_budget:
        notes.append("按账户权益收窄仓位")

    min_sz = spec.min_sz or lot
    if size < min_sz:
        raise TradeRejected(
            f"风险预算 {max_loss_usd:.2f} USDT 不够下最小单（最小 {_fmt_num(min_sz)}"
            f"{'张' if spec.derivative else '币'}，每单位风险 {risk_per_unit_effective:.4f} USDT）"
        )

    risk_usd = size * risk_per_unit_effective
    if risk_usd > max_loss_usd * 1.02:  # 容差：合约取整后允许 2%
        raise TradeRejected(f"止损亏损 {risk_usd:.2f} USDT 超出上限 {max_loss_usd:.2f} USDT")

    notional_usd = size * unit_notional
    tick = spec.tick_sz or 0.0
    if ord_type == "market":
        plan_price = _round_to_tick(price, tick) if price else None
    else:
        plan_price = _round_to_tick(entry, tick)

    return OrderPlan(
        inst_id=spec.inst_id,
        side=side,
        ord_type=ord_type,
        size=size,
        price=plan_price,
        stop_px=_round_to_tick(stop, tick),
        take_profit_px=_round_to_tick(tp, tick) if tp else None,
        risk_usd=risk_usd,
        notional_usd=notional_usd,
        leverage=max(int(leverage), 1),
        stop_distance=risk_per_unit,
        base_qty=(size * spec.ct_val) if spec.derivative else size,
        notes=tuple(notes),
    )


def format_plan_confirmation(
    plan: OrderPlan,
    *,
    symbol: str = "",
    timeframe: str = "",
    order_type_label: str = "",
    simulated: bool = True,
    equity_usd: float | None = None,
) -> str:
    """下单前确认框里的文字 —— 把「以损定量」的每个数字摊开给人核对。"""
    qty_unit = "张" if plan.base_qty != plan.size else "币"
    base_qty = f"{plan.base_qty:,.6f}".rstrip("0").rstrip(".")
    pct = (plan.stop_distance / plan.price * 100.0) if plan.price else 0.0
    margin = plan.notional_usd / max(plan.leverage, 1)

    lines = [
        f"【OKX {'模拟盘' if simulated else '实盘'} 下单确认】",
        f"品种 / 周期：{symbol or plan.inst_id} {timeframe}".rstrip(),
        f"方向 / 类型：{'做多' if plan.side == 'buy' else '做空'}"
        f" · {order_type_label or plan.ord_type}",
    ]
    if plan.price:
        lines.append("入场价：" + f"{plan.price:,.4f}".rstrip("0").rstrip("."))
    lines.append(
        "止损价："
        + f"{plan.stop_px:,.4f}".rstrip("0").rstrip(".")
        + f"（距离 {plan.stop_distance:,.2f} 点 / {pct:.2f}%）"
    )
    if plan.take_profit_px:
        lines.append("止盈价：" + f"{plan.take_profit_px:,.4f}".rstrip("0").rstrip("."))
    lines.append(
        "下单量："
        + f"{plan.size:,.4f}".rstrip("0").rstrip(".")
        + f" {qty_unit}"
        + (f"（{base_qty} 基础币）" if qty_unit == "张" else "")
    )
    lines.append(f"名义价值：{plan.notional_usd:,.0f} USDT")
    if plan.leverage > 1:
        lines.append(f"预计占用保证金：{margin:,.0f} USDT（杠杆 {plan.leverage}x）")
    if equity_usd:
        lines.append(f"账户权益：{equity_usd:,.0f} USDT")
    lines.append("── 以损定量 ──")
    lines.append(f"止损被打到：亏 {plan.risk_usd:,.2f} USDT")
    if plan.notes:
        lines.append("备注：" + "；".join(plan.notes))
    return "\n".join(lines)


# ── 风控闸门 ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GuardResult:
    """下单前的闸门结论；``blocked`` 非空即拒单。"""

    blocked: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.blocked

    def __bool__(self) -> bool:
        return self.ok


def evaluate_guard(
    *,
    enabled: bool,
    credentials_present: bool,
    trigger_mode: str,
    manual_confirm: bool,
    simulated: bool,
    live_acknowledged: bool,
    symbol: str,
    decision: dict[str, Any],
    min_confidence: int,
    max_open_positions: int,
    open_positions: int,
    daily_loss_cap_usd: float,
    realized_pnl_today_usd: float | None,
    allowed_symbols: Sequence[str] = (),
) -> GuardResult:
    """所有「能不能下单」的判断集中在这里，便于测试与审计。"""
    blocked: list[str] = []
    notes: list[str] = []

    if not enabled:
        blocked.append("下单开关未打开")
    if not credentials_present:
        blocked.append("未找到 OKX API 凭据")
    if trigger_mode == "auto" and not manual_confirm:
        notes.append("自动触发：满足条件时会直接下单")
    else:
        notes.append("手动触发：需要人工点「执行下单」")
    if simulated:
        notes.append("OKX 模拟盘")
    else:
        notes.append("⚠️ 实盘账户（真实资金）")
        if not live_acknowledged:
            blocked.append("当前是实盘但未完成风险确认（OKX 交易设置里的实盘风险确认）")

    if allowed_symbols:
        allowed = {str(s).upper() for s in allowed_symbols}
        if str(symbol).upper() not in allowed:
            blocked.append(f"{symbol} 不在允许下单的品种列表里")

    confidence = _as_float(decision.get("trade_confidence"))
    if confidence is None:
        blocked.append("决策缺少置信度 trade_confidence")
    elif confidence < min_confidence:
        blocked.append(f"置信度 {confidence:.0f} 低于门槛 {min_confidence}")

    order_type = str(decision.get("order_type") or "").strip()
    if order_type not in _SUPPORTED_ORDER_TYPES:
        blocked.append(f"决策为「{order_type or '未知'}」，没有可执行订单")
    if _as_float(decision.get("stop_loss_price")) is None:
        blocked.append("决策没有止损价")

    if open_positions >= max_open_positions:
        blocked.append(f"持仓数 {open_positions} 已达上限 {max_open_positions}")

    if realized_pnl_today_usd is None:
        blocked.append("无法确认当日已实现亏损（账单读取失败），按保守策略拒单")
    elif realized_pnl_today_usd <= -abs(daily_loss_cap_usd):
        blocked.append(
            f"当日已实现亏损 {abs(realized_pnl_today_usd):.2f} USDT 已达上限 "
            f"{daily_loss_cap_usd:.2f} USDT，今日停止下单"
        )

    return GuardResult(blocked=tuple(blocked), notes=tuple(notes))


# ── 执行器 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExecutionResult:
    """一次下单尝试的结果。"""

    sent: bool
    plan: OrderPlan | None = None
    ord_id: str = ""
    guard: GuardResult | None = None
    message: str = ""
    dry_run: bool = False


class OkxTrader:
    """把「决策」变成「订单」的执行器（默认 dry-run）。"""

    def __init__(
        self,
        trading_settings: Any,
        *,
        credentials: OkxCredentials | None = None,
        client: OkxPrivateClient | None = None,
        market_source: OkxSource | None = None,
    ) -> None:
        self._settings = trading_settings
        self._market = market_source or OkxSource()
        self._cred = credentials
        self._client = client

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        market_source: OkxSource | None = None,
    ) -> OkxTrader:
        """按 settings.trading 装配执行器（凭据从环境变量/凭据文件读取）。"""
        trading = getattr(settings, "trading", None)
        path = getattr(trading, "credentials_path", None) if trading is not None else None
        cred = OkxCredentials.load(path, simulated=bool(getattr(trading, "simulated", True)))
        return cls(trading, credentials=cred, market_source=market_source)

    # ── 状态 ──────────────────────────────────────────────────────────────────

    @property
    def credentials(self) -> OkxCredentials | None:
        return self._cred

    @property
    def client(self) -> OkxPrivateClient:
        if self._client is None:
            if self._cred is None:
                raise OkxTradeError("未配置 OKX API 凭据")
            self._client = OkxPrivateClient(self._cred)
        return self._client

    def status_text(self) -> str:
        s = self._settings
        if not getattr(s, "enabled", False):
            return "交易：关闭"
        if self._cred is None or not self._cred.complete:
            return "交易：已开启但缺少 API 凭据"
        mode = "自动" if getattr(s, "trigger_mode", "manual") == "auto" else "手动"
        venue = "模拟盘" if self._cred.simulated else "实盘"
        cap = float(getattr(s, "max_loss_per_trade_usd", 0.0))
        return f"交易：{venue} · {mode} · 每笔上限 {cap:.0f} USDT"

    # ── 主流程 ────────────────────────────────────────────────────────────────

    def spec_for(self, inst_id: str) -> InstrumentSpec:
        raw = self._market.instrument_info(inst_id)
        if not raw:
            raise TradeRejected(f"OKX 查不到合约规格：{inst_id}")
        return InstrumentSpec.from_okx(raw)

    def execute(
        self,
        decision: dict[str, Any],
        *,
        symbol: str,
        dry_run: bool = True,
        manual_confirm: bool = False,
        price: float | None = None,
    ) -> ExecutionResult:
        """闸门 → 规划 → 下单。``dry_run=True`` 时只返回计划，不发单。"""
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
                sent=False,
                guard=guard,
                dry_run=dry_run,
                message="；".join(guard.blocked),
            )

        spec = self.spec_for(symbol)
        plan = plan_order(
            decision,
            spec,
            equity_usd=self.client.equity_usd(),
            max_loss_usd=float(getattr(s, "max_loss_per_trade_usd", 10.0)),
            leverage=int(getattr(s, "leverage", 3)),
            price=price,
        )
        if dry_run:
            return ExecutionResult(
                sent=False,
                plan=plan,
                guard=guard,
                dry_run=True,
                message=f"[演练] {plan.summary}",
            )

        if spec.derivative:
            self.client.set_leverage(plan.inst_id, plan.leverage)
        row = self.client.place_order(
            inst_id=plan.inst_id,
            side=plan.side,
            ord_type=plan.ord_type,
            size=plan.size,
            price=plan.price if plan.ord_type == "limit" else None,
            trigger_px=plan.price if plan.ord_type == "trigger" else None,
            stop_px=plan.stop_px,
            take_profit_px=plan.take_profit_px,
            td_mode="cross" if spec.derivative else "cash",
        )
        return ExecutionResult(
            sent=True,
            plan=plan,
            ord_id=str(row.get("ordId", "")),
            guard=guard,
            message=plan.summary,
        )

    def close_all(self, inst_id: str) -> dict[str, Any]:
        return self.client.close_position(inst_id)

    # ── 内部 ──────────────────────────────────────────────────────────────────

    def _open_positions_safe(self) -> int:
        try:
            return self.client.open_position_count()
        except Exception as exc:
            logger.warning("读取持仓失败: %s", exc)
            return 0

    def _daily_pnl_safe(self) -> float | None:
        try:
            return self.client.realized_pnl_today_usd()
        except Exception as exc:
            logger.warning("读取当日盈亏失败: %s", exc)
            return None
