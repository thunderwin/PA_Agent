"""统一交易网关 —— 一个入口，两种交易所（OKX / 币安）。

为什么要这一层

界面和监控线程不该知道"用哪个交易所"，它们只需要一个满足
:class:`TradingGateway` 的对象：给它一个决策，它要么下单、要么给出拒绝理由。
于是：

- ``create_trader(settings)`` 按 ``settings.trading.venue`` 返回对应网关；
- 两个网关**共用同一套风控核心**（:func:`plan_order` 以损定量、
  :func:`evaluate_guard` 风控闸门、:class:`OrderPlan` / :class:`ExecutionResult`），
  所以"每笔最大亏损"这类承诺不会因为换交易所而变；
- 两个网关都**只认同一种品种写法**（本项目沿用的 ``BTC-USDT-SWAP``），
  币安那套 ``BTCUSDT`` 只在网关内部换算（见 :func:`to_venue_symbol`）。

历史说明：风控核心目前物理上还住在 ``okx_trader.py`` 里（那是它诞生的地方），
本模块通过再导出提供统一入口。等币安网关能在真实网络下跑通、有测试托底之后，
再把那部分代码搬进独立的 ``trading/core.py``；现在不动它，是为了不碰正在实盘
运行的下单路径。

网络 / 代理

两个交易所的出口要求可能不同（比如 OKX 的 API Key 白名单绑定了某个固定 IP，
而币安又按地区限制访问），所以代理是**按交易所分开配置**的：

1. ``settings.trading.okx_proxy`` / ``settings.trading.binance_proxy``
2. 环境变量 ``PA_OKX_PROXY`` / ``PA_BINANCE_PROXY``
3. 都没配 → 交给系统/环境里的 ``HTTPS_PROXY``（urllib 默认行为）
"""

from __future__ import annotations

import os
from typing import Any, Literal, Protocol, runtime_checkable

from pa_agent.trading.okx_trader import (
    ORDER_TAG,
    ExecutionResult,
    GuardResult,
    InstrumentSpec,
    OrderPlan,
    TradeRejected,
    evaluate_guard,
    format_plan_confirmation,
    is_executable_decision,
    plan_order,
)
from pa_agent.trading.okx_trader import OkxTradeError as TradeError

__all__ = [
    "ORDER_TAG",
    "DEFAULT_CREDENTIALS_PATHS",
    "PROXY_ENV_VARS",
    "VENUES",
    "VENUE_LABELS",
    "ExecutionResult",
    "GuardResult",
    "InstrumentSpec",
    "OrderPlan",
    "TradeError",
    "TradeRejected",
    "TradingGateway",
    "Venue",
    "create_trader",
    "credentials_path_for",
    "evaluate_guard",
    "format_plan_confirmation",
    "is_executable_decision",
    "normalize_venue",
    "plan_order",
    "resolve_proxy",
    "to_canonical_symbol",
    "to_venue_symbol",
    "venue_label",
]

#: 支持的交易所。加新交易所时：这里加一项 + 写一个 ``*Trader`` + 在
#: :func:`create_trader` 里加一条分支即可，其余代码不用动。
Venue = Literal["okx", "binance"]
VENUES: tuple[Venue, ...] = ("okx", "binance")
VENUE_LABELS: dict[str, str] = {"okx": "OKX", "binance": "币安"}

#: 凭据文件默认位置（都已被 .gitignore 排除）。
DEFAULT_CREDENTIALS_PATHS: dict[str, str] = {
    "okx": "config/okx_trading.json",
    "binance": "config/binance_trading.json",
}

#: 各交易所的代理环境变量名。
PROXY_ENV_VARS: dict[str, str] = {
    "okx": "PA_OKX_PROXY",
    "binance": "PA_BINANCE_PROXY",
}


def normalize_venue(raw: Any) -> Venue:
    """把配置里的交易所名规范化；认不出来就回落到 OKX（老配置的行为）。"""
    text = str(raw or "").strip().lower()
    aliases = {
        "okx": "okx",
        "ok": "okx",
        "okex": "okx",
        "binance": "binance",
        "bn": "binance",
        "binance_futures": "binance",
        "binanceusdm": "binance",
    }
    return aliases.get(text, "okx")  # type: ignore[return-value]


def venue_label(venue: Any) -> str:
    return VENUE_LABELS.get(normalize_venue(venue), "OKX")


def resolve_proxy(venue: Any, settings: Any = None) -> str | None:
    """该交易所该走哪个代理；都取不到时返回 ``None``（用系统默认）。"""
    name = normalize_venue(venue)
    trading = getattr(settings, "trading", None)
    configured = str(getattr(trading, f"{name}_proxy", "") or "").strip()
    if configured:
        return configured
    env_value = str(os.environ.get(PROXY_ENV_VARS[name], "") or "").strip()
    return env_value or None


def credentials_path_for(settings: Any, venue: Any = None) -> str:
    """该交易所的凭据文件路径（没配就用默认位置）。"""
    trading = getattr(settings, "trading", None)
    name = normalize_venue(venue if venue is not None else getattr(trading, "venue", "okx"))
    configured = str(getattr(trading, f"{name}_credentials_path", "") or "").strip()
    if not configured and name == "okx":
        # 老字段名 ``trading.credentials_path``（OKX 专用时代留下的），继续认。
        configured = str(getattr(trading, "credentials_path", "") or "").strip()
    return configured or DEFAULT_CREDENTIALS_PATHS[name]


# ── 品种写法换算 ──────────────────────────────────────────────────────────────
#
# 全项目统一使用 OKX 风格的 ``BASE-USDT-SWAP`` 作为"规范写法"（历史原因：记录
# 文件、监控列表、图表缓存都按它命名）。币安的 ``BASEUSDT`` 只在这里出现。


def to_binance_symbol(canonical: str) -> str:
    """``BTC-USDT-SWAP`` → ``BTCUSDT``。

    只支持 USDT 本位永续：币安的币本位（``BTCUSD_PERP``）与现货不在本网关范围内，
    遇到就明确拒绝，而不是猜一个可能下错单的代码。
    """
    text = str(canonical or "").strip().upper()
    parts = [p for p in text.split("-") if p]
    if len(parts) == 3 and parts[1] == "USDT" and parts[2] == "SWAP":
        return f"{parts[0]}USDT"
    if len(parts) == 2 and parts[1] == "USDT":
        raise TradeRejected(
            f"币安网关目前只支持 USDT 本位永续：{canonical!r} 是现货写法"
            "（请写成 BASE-USDT-SWAP）"
        )
    raise TradeRejected(
        f"无法把 {canonical!r} 换算成币安合约代码（只支持 BASE-USDT-SWAP）"
    )


def from_binance_symbol(venue_symbol: str) -> str:
    """``BTCUSDT`` → ``BTC-USDT-SWAP``；认不出就拒绝。"""
    text = str(venue_symbol or "").strip().upper()
    if not text or "_" in text:
        raise TradeRejected(f"币安品种代码无法识别：{venue_symbol!r}（非 USDT 本位永续）")
    if not text.endswith("USDT") or len(text) <= 4:
        raise TradeRejected(f"币安品种代码无法识别：{venue_symbol!r}（只支持 *USDT 永续）")
    return f"{text[:-4]}-USDT-SWAP"


def to_venue_symbol(canonical: str, venue: Any = "okx") -> str:
    """规范写法 → 该交易所的下单代码。"""
    name = normalize_venue(venue)
    text = str(canonical or "").strip().upper()
    if name == "okx":
        return text
    return to_binance_symbol(text)


def to_canonical_symbol(venue_symbol: str, venue: Any = "okx") -> str:
    """该交易所的下单代码 → 规范写法。"""
    name = normalize_venue(venue)
    text = str(venue_symbol or "").strip().upper()
    if name == "okx":
        return text
    return from_binance_symbol(text)


# ── 统一接口 ──────────────────────────────────────────────────────────────────


@runtime_checkable
class TradingGateway(Protocol):
    """界面 / 监控线程看到的全部能力（OKX 与币安都满足）。"""

    #: 交易所名（"okx" / "binance"）
    venue: str

    def execute(
        self,
        decision: dict[str, Any],
        *,
        symbol: str,
        dry_run: bool = True,
        manual_confirm: bool = False,
        price: float | None = None,
    ) -> ExecutionResult:
        """闸门 → 规划 → 下单；``dry_run=True`` 只返回计划，不发单。"""

    def close_all(self, inst_id: str) -> dict[str, Any]:
        """市价平掉该品种全部持仓（并撤掉该品种的挂单）。"""

    def cancel_stale_entries(
        self, inst_id: str, timeframe: str, *, max_bars: int, now_ms: int | None = None
    ) -> list[dict[str, Any]]:
        """撤掉超过 ``max_bars`` 根 K 线未成交的**程序**入场挂单。"""

    def ensure_stops(self, inst_id: str | None = None) -> list[str]:
        """给"有持仓但没止损"的程序仓位补挂止损，返回处理说明。

        OKX 的止损随订单一起托管（``attachAlgoOrds``），这里只做核对；
        币安没有"附带止损"这回事，挂单成交后必须由程序补挂——这是那道保险。
        """

    def status_text(self) -> str:
        """状态栏那一行文字。"""

    @property
    def client(self) -> Any:
        """底层客户端；至少要提供 ``occupied_symbols()`` / ``positions()`` /
        ``pending_orders()`` / ``equity_usd()`` / ``open_position_count()`` /
        ``realized_pnl_today_usd()``，且**返回同一套字段名**。"""


def create_trader(settings: Any, *, market_source: Any = None) -> TradingGateway:
    """按 ``settings.trading.venue`` 装配网关（凭据从环境变量/凭据文件读取）。"""
    venue = normalize_venue(getattr(getattr(settings, "trading", None), "venue", "okx"))
    if venue == "binance":
        from pa_agent.trading.binance_trader import BinanceTrader

        return BinanceTrader.from_settings(settings, market_source=market_source)
    from pa_agent.trading.okx_trader import OkxTrader

    return OkxTrader.from_settings(settings, market_source=market_source)
