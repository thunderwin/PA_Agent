"""Pydantic settings models for PA Agent."""
from __future__ import annotations
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DecisionStance = Literal["conservative", "balanced", "aggressive", "extreme_aggressive"]
DataSourceKind = Literal[
    "mt5",
    "tradingview",
    "okx",
    "binance",
    "akshare",
    "eastmoney",
    "eastmoney_futures",
    "tushare",
]
NormalizationMode = Literal["strict", "lenient"]


class AIProviderSettings(BaseModel):
    """AI provider connection and behaviour settings."""
    model_config = ConfigDict(extra="ignore")

    model: str = "deepseek-v4-flash"
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    api_key_encrypted: str = ""
    thinking: bool = True
    reasoning_effort: Literal["low", "medium", "high", "max"] = "high"
    context_window: int = 2_000_000


class PromptSettings(BaseModel):
    """Prompt assembly tuning (accuracy-oriented defaults)."""
    model_config = ConfigDict(extra="ignore")

    #: When True, Stage 2 loads every strategy .txt (legacy/test behaviour).
    stage2_load_full_strategy_library: bool = False
    experience_max_entries: int = Field(default=0, ge=0, le=10)
    experience_max_chars_per_entry: int = Field(default=400, ge=100, le=4000)
    #: Inject pattern判定表 + 速查 brief into Stage 1 user prompt (reduces missed tags).
    stage1_inject_pattern_briefs: bool = True


class ValidationSettings(BaseModel):
    """Post-LLM validation behaviour."""
    model_config = ConfigDict(extra="ignore")

    normalization_mode: NormalizationMode = "lenient"
    #: Stage-1 cross-field checks (gate trace, bar_by_bar, pattern tags). Off by default.
    stage1_coherence_checks: bool = False
    #: Stage-2 trace / diagnosis cross-checks (not order safety). Off by default.
    stage2_coherence_checks: bool = False
    trace_semantic_checks: bool = False
    strict_bar_by_bar_features: bool = False
    #: Allow Stage 1 truncated JSON tail repair before failing syntax validation.
    disable_truncation_repair: bool = False
    #: Re-call API with structured feedback when validation fails (format errors).
    retry_enabled: bool = True
    retry_max: int = Field(default=3, ge=0, le=5)
    #: Max retries for category=c semantic errors (subset only).
    retry_max_semantic: int = Field(default=1, ge=0, le=3)
    retry_stage2: bool = True


class GeneralSettings(BaseModel):
    """UI and data-feed general settings."""
    model_config = ConfigDict(extra="ignore")

    analysis_bar_count: int = Field(default=100, ge=2, le=5000)
    refresh_interval_ms: int = 1000
    context_warning_threshold_pct: float = 99_999_999.0
    last_data_source: DataSourceKind = "mt5"
    #: A-share K-line adjust for East Money / Baostock (qfq=前复权)
    kline_adjust: Literal["qfq", "hfq", "none"] = "qfq"
    #: TradingView 交易所；空字符串 =（自动）依次探测预设列表
    last_tradingview_exchange: str = ""
    last_symbol: str = "XAUUSDm"
    last_timeframe: str = "15m"
    decision_flow_auto_play: bool = True
    decision_flow_play_seconds: int = 50
    #: 阶段二给出限价/突破/市价单时：警报音、弹窗，并自动切到「决策」页（跳过决策树可视化演示）
    alert_on_order_opportunity: bool = True
    incremental_max_new_bars: int = Field(default=10, ge=0, le=500)
    #: 阶段二交易倾向：balanced=默认；conservative/aggressive 逐级调整下单意愿
    decision_stance: DecisionStance = "balanced"
    #: 决策树可视化：在「整图适配」基础上的缩放百分比（100=与适配一致；可任意放大，仅下限 10%）
    decision_flow_default_zoom_pct: int = Field(default=600, ge=10)
    #: 「实时」页思考过程/撰写回答框与追问输入框的等宽字体字号（pt）
    stream_pane_font_pt: int = Field(default=11, ge=8, le=28)
    #: K 线图上 #序号 标签的字号（pt）
    chart_seq_label_font_pt: int = Field(default=11, ge=6, le=24)
    #: 两阶段分析结束后是否自动恢复 K 线图表实时刷新
    auto_resume_chart_after_analysis: bool = False
    #: 持续跟踪分析：有新K线收盘时自动触发新一轮分析
    keep_analysis: bool = False
    #: 启动程序后自动开始拉取 K 线（无人值守监控用；默认关闭）
    auto_start_capture: bool = False
    #: 启动程序后自动勾选「持续跟踪分析」（无人值守监控用；默认关闭）
    auto_keep_analysis: bool = False
    #: 多品种监控：后台对 watch_symbols 里每个品种各自跑分析（不影响主图表看当前品种）
    watch_enabled: bool = False
    #: 被监控的品种列表（如 BTC-USDT-SWAP / ETH-USDT-SWAP）
    watch_symbols: list[str] = Field(default_factory=list)
    #: 监控周期；空字符串 = 跟随主窗口当前周期
    watch_timeframe: str = ""
    #: 探活间隔（秒）：每次检查各品种有没有新 K 线收盘（只有收盘才真正调模型）
    watch_interval_s: int = Field(default=60, ge=10, le=3600)
    #: 多品种并发分析线程数（1 = 串行）。并发时每个品种用独立数据源实例。
    watch_concurrency: int = Field(default=3, ge=1, le=20)
    #: 已有持仓或已有挂单的品种跳过分析（它们的结论无法执行，白花 token）
    watch_skip_occupied: bool = True
    #: 动态选币：每小时按"成交量突变"重选品种，替换监控列表（见 orchestrator/screener.py）
    watch_dynamic_enabled: bool = False
    #: 每轮选几个（用户要的是 2 个）
    watch_dynamic_count: int = Field(default=2, ge=1, le=20)
    #: 多久重选一次（分钟）
    watch_dynamic_refresh_min: int = Field(default=30, ge=5, le=1440)
    #: True = 保留静态列表并**追加**选出来的（默认 False = 整个换掉）
    watch_dynamic_keep_static: bool = False
    #: 选币门槛：24h 成交额区间（USDT）
    watch_dynamic_min_volume_usd: float = Field(default=8_000_000.0, gt=0)
    watch_dynamic_max_volume_usd: float = Field(default=400_000_000.0, gt=0)
    #: 选币门槛：最近 6 小时成交额下限（USDT）
    watch_dynamic_min_window_usd: float = Field(default=1_000_000.0, gt=0)
    #: 选币门槛：点差上限（bp）
    watch_dynamic_max_spread_bp: float = Field(default=15.0, gt=0)
    #: 选币门槛：盘口前 5 档较小一侧的名义额下限（USDT）——防"有量但盘口薄"
    watch_dynamic_min_depth_usd: float = Field(default=20_000.0, gt=0)
    #: 选币门槛：突变强度（稳健 z 分数）
    watch_dynamic_min_burst: float = Field(default=6.0, gt=0)
    #: 选币时额外排除的品种（规范写法）
    watch_dynamic_exclude: list[str] = Field(default_factory=list)
    #: 监控到可下单方案时播放提示音并提示
    watch_alert_on_signal: bool = True
    #: 重试后取消持续跟踪分析：校验失败触发重试后自动关闭 keep_analysis
    cancel_keep_analysis_on_retry: bool = False
    #: 交易决策置信度门槛：仅当 trade_confidence >= 此值时，才视为有下单机会（弹窗警报并提供决策详情）
    decision_confidence_threshold: int = Field(default=40, ge=0, le=100)
    #: 开启下根K线预期功能；关闭时不向模型请求该预测，节省 token
    enable_next_bar_prediction: bool = False
    #: 同一结构位 entry 相差≤3跳时，禁止反向新方案的冷却 K 线根数（已收盘）
    structure_flip_cooldown_bars: int = Field(default=3, ge=1, le=50)

    @field_validator("last_data_source", mode="before")
    @classmethod
    def _coerce_legacy_data_source(cls, v: object) -> object:
        if v == "yfinance":
            return "eastmoney"
        if v in ("adata", "a_share"):
            return "akshare"
        if v == "eastmoney":
            return "eastmoney"
        if v == "tushare":
            return "tushare"
        return v

    @field_validator("decision_flow_default_zoom_pct", mode="before")
    @classmethod
    def _coerce_zoom_pct(cls, v: object) -> object:
        if v is None:
            return 50
        return v


class TradingSettings(BaseModel):
    """真实下单设置（默认全关；凭据不写在这里）。

    支持两个交易所，由 ``venue`` 选择：

    - ``okx``：凭据来自环境变量（``OKX_API_KEY`` / ``OKX_SECRET_KEY`` /
      ``OKX_PASSPHRASE``）或 ``okx_credentials_path``（默认沿用老字段
      ``credentials_path`` = ``config/okx_trading.json``）。
    - ``binance``：凭据来自 ``BINANCE_API_KEY`` / ``BINANCE_SECRET_KEY``
      或 ``binance_credentials_path``。

    凭据永远不进 settings.json，避免跟着配置到处跑。
    """

    model_config = ConfigDict(extra="ignore")

    #: 用哪个交易所下单：okx / binance（认不出的值一律回落到 okx）。
    venue: str = "okx"
    #: 总开关。False 时任何下单请求都会被拒绝。
    enabled: bool = False
    #: True = OKX 模拟盘（请求头 x-simulated-trading: 1）；False = 实盘。
    simulated: bool = True
    #: 实盘风险确认：simulated=False 时必须为 True，否则拒绝下单（防止误切实盘）。
    live_ack: bool = False
    #: manual = 分析完只提示，等你点「执行下单」；auto = 新 K 线收盘后自动下单。
    trigger_mode: Literal["manual", "auto"] = "manual"
    #: 每笔最大亏损（USDT）。用 入场价与止损价的距离 反推下单量，超出即拒单。
    max_loss_per_trade_usd: float = Field(default=10.0, gt=0, le=10_000)
    #: 单笔名义额上限（USDT）；0 = 不限制。触顶时自动缩量——实际风险只会更小，不会更大。
    max_notional_usd: float = Field(default=0.0, ge=0.0, le=1_000_000)
    #: 是否把 AI 算出的止盈一并挂到交易所。
    #: False（默认）= 只挂止损，止盈由人工了结；True = 恢复"止损+止盈一起托管"。
    attach_take_profit: bool = False
    #: 入场挂单的有效期（K 线根数）：超过这么多根仍未成交就自动撤单，0 = 不撤。
    #: 只撤程序自己下的单（带 tag=PAAGENT），绝不碰手动挂单。
    pending_entry_expiry_bars: int = Field(default=8, ge=0, le=200)
    #: 当日（UTC+8）累计已实现亏损上限，超过后当天不再下单。
    daily_loss_cap_usd: float = Field(default=30.0, gt=0, le=100_000)
    #: 同时持有的最大仓位数。
    max_open_positions: int = Field(default=1, ge=1, le=20)
    #: 永续杠杆（仅永续；现货忽略）。
    leverage: int = Field(default=3, ge=1, le=50)
    #: 低于该置信度不下单（AI 的 trade_confidence，0-100）。
    min_confidence: int = Field(default=60, ge=0, le=100)
    #: 允许下单的品种白名单；空列表 = 只允许当前订阅的品种。
    allowed_symbols: list[str] = Field(default_factory=list)
    #: OKX 凭据文件（JSON，内含 api_key/secret_key/passphrase），务必不要提交到 Git。
    credentials_path: str = "config/okx_trading.json"
    #: OKX 凭据文件的显式配置（留空则沿用上面的 ``credentials_path``）。
    okx_credentials_path: str = ""
    #: 币安凭据文件（JSON，内含 api_key/secret_key）。
    binance_credentials_path: str = "config/binance_trading.json"
    #: 各交易所的代理（留空则用环境变量 ``PA_OKX_PROXY`` / ``PA_BINANCE_PROXY``，
    #: 再留空就用系统代理）。两家出口要求可能不同，所以分开配。
    okx_proxy: str = ""
    binance_proxy: str = ""


_FEISHU_CONFIG_KEYS = (
    "enabled",
    "webhook_url",
    "secret",
    "app_id",
    "app_secret",
    "notify_on_order_only",
)


class FeishuSettings(BaseModel):
    """Feishu bot notification settings (persisted in settings.json)."""
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    webhook_url: str = ""
    secret: str = ""
    app_id: str = ""
    app_secret: str = ""
    #: True = only push when there is an order opportunity.
    notify_on_order_only: bool = True


class TushareSettings(BaseModel):
    """Tushare Pro data source settings (persisted in ignored settings.json)."""
    model_config = ConfigDict(extra="ignore")

    token: str = ""


class PushPlusSettings(BaseModel):
    """PushPlus notification settings (settings.json only; no GUI)."""
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    token: str = ""


class Settings(BaseModel):
    """Root settings object persisted to config/settings.json."""
    model_config = ConfigDict(extra="ignore")

    provider: AIProviderSettings = Field(default_factory=AIProviderSettings)
    general: GeneralSettings = Field(default_factory=GeneralSettings)
    prompt: PromptSettings = Field(default_factory=PromptSettings)
    validation: ValidationSettings = Field(default_factory=ValidationSettings)
    feishu: FeishuSettings = Field(default_factory=FeishuSettings)
    pushplus: PushPlusSettings = Field(default_factory=PushPlusSettings)
    tushare: TushareSettings = Field(default_factory=TushareSettings)
    trading: TradingSettings = Field(default_factory=TradingSettings)


def provider_api_key_configured(settings: Settings | None) -> bool:
    """Return True when a non-empty API key is loaded in memory."""
    if settings is None:
        return False
    return bool((settings.provider.api_key or "").strip())


# ── Persistence ───────────────────────────────────────────────────────────────
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _migrate_legacy_feishu_json(raw: dict, settings_path: Path) -> bool:
    """Merge legacy config/feishu.json into settings.feishu when needed."""
    legacy_path = settings_path.parent / "feishu.json"
    if not legacy_path.exists():
        return False

    feishu = raw.setdefault("feishu", {})
    if (feishu.get("webhook_url") or "").strip():
        return False

    try:
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("legacy feishu.json unreadable (%s); skipping migration", exc)
        return False

    migrated = False
    for key in _FEISHU_CONFIG_KEYS:
        if key not in legacy:
            continue
        value = legacy.get(key)
        if value in (None, ""):
            continue
        if feishu.get(key) in (None, ""):
            feishu[key] = value
            migrated = True
    if migrated:
        logger.info("Migrated Feishu config from %s into settings.json", legacy_path)
    return migrated


def load_settings(path: Path | None = None) -> "Settings":
    """Load settings from *path* (default: SETTINGS_JSON_PATH).

    Returns default Settings and writes them to disk if the file is absent.
    """
    from pa_agent.config.paths import SETTINGS_JSON_PATH

    path = path or SETTINGS_JSON_PATH

    if not path.exists():
        defaults = Settings()
        save_settings(defaults, path)
        return defaults

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("settings.json unreadable (%s); using defaults", exc)
        return Settings()

    # Migrate legacy field names
    general = raw.get("general", {})
    if "cost_warning_threshold_pct" in general and "context_warning_threshold_pct" not in general:
        general["context_warning_threshold_pct"] = general.pop("cost_warning_threshold_pct")
    general.pop("last_htf_text", None)
    from pa_agent.data.market_defaults import migrate_general_gold_defaults

    migrate_general_gold_defaults(general)
    if "default_bar_count" in general and "analysis_bar_count" not in general:
        general["analysis_bar_count"] = general.pop("default_bar_count")
    raw["general"] = general
    provider = raw.get("provider", {})
    provider.pop("pricing", None)
    raw["provider"] = provider

    # Migrate legacy encrypted key: drop it, api_key already in provider dict
    raw.setdefault("provider", {}).setdefault("api_key", "")

    migrated_feishu = _migrate_legacy_feishu_json(raw, path)
    settings = Settings.model_validate(raw)
    dirty = migrated_feishu
    if settings.pushplus.enabled and not settings.pushplus.token.strip():
        if not (os.environ.get("PUSHPLUS_TOKEN") or "").strip():
            settings.pushplus.enabled = False
            logger.info(
                "PushPlus enabled but token empty — auto-disabled "
                "(Feishu notifications unaffected)"
            )
            dirty = True
    if dirty:
        save_settings(settings, path)
    return settings


def save_settings(settings: "Settings", path: Path | None = None) -> None:
    """Persist settings to *path* (default: SETTINGS_JSON_PATH)."""
    from pa_agent.config.paths import SETTINGS_JSON_PATH

    path = path or SETTINGS_JSON_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    data = settings.model_dump()

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
