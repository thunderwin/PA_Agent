"""浏览器里的分析引擎桥接层（Pyodide 内运行）。

它做的事情：
1. 用原样的 ``pa_agent`` 引擎（提示词组装 / 两阶段编排 / 校验）跑一轮分析；
2. 把「调大模型」这一步换成浏览器里的同步 XHR（Pyodide 没有 requests/openai）；
3. 把「下单量计算」复用 ``pa_agent.trading.okx_trader.plan_order``（同一套以损定量代码）。

密钥只在本函数参数里过一遍，不回传、不落盘。
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

_ENGINE_READY = False


def _prepare_engine() -> None:
    global _ENGINE_READY
    if _ENGINE_READY:
        return
    import sys

    for path in ("/repo", "/shims"):
        if path not in sys.path:
            sys.path.insert(0, path)
    _ENGINE_READY = True


# ── 模型客户端替身 ────────────────────────────────────────────────────────────

class _Usage:
    def __init__(self, raw: dict | None) -> None:
        raw = raw or {}
        self.prompt_tokens = int(raw.get("prompt_tokens") or 0)
        self.completion_tokens = int(raw.get("completion_tokens") or 0)
        self.total_tokens = int(raw.get("total_tokens") or 0)
        details = raw.get("prompt_tokens_details") or {}
        self.cached_prompt_tokens = int(
            details.get("cached_tokens") or raw.get("prompt_cache_hit_tokens") or 0
        )

    def as_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "cached_prompt_tokens": self.cached_prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


class _Reply:
    def __init__(self, content: str, reasoning: str, raw: dict) -> None:
        self.content = content
        self.reasoning_content = reasoning
        self.raw = raw
        self.usage = _Usage(raw.get("usage"))
        self.request_id = str(raw.get("id") or "")
        self.latency_ms = 0.0


class BrowserLLMClient:
    """与 DeepSeekClient 同接口（chat / stream_chat / update_provider）。"""

    def __init__(self, provider: Any, effort: str = "high") -> None:
        self._provider = provider
        self._effort = effort

    def update_provider(self, provider: Any) -> None:
        self._provider = provider

    # ── 真正发请求的地方 ──────────────────────────────────────────────────────

    def _post(self, messages: list[dict], *, thinking: bool | None) -> dict:
        from js import XMLHttpRequest  # type: ignore[import-not-found]

        base = (getattr(self._provider, "base_url", "") or "").rstrip("/")
        model = getattr(self._provider, "model", "") or ""
        api_key = getattr(self._provider, "api_key", "") or ""
        if not api_key:
            raise RuntimeError("未配置 AI API Key（在「设置」里填写）")
        url = base + "/chat/completions"

        body: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        # 仅在显式打开「深度思考」且模型看起来支持时附带 reasoning_effort
        if thinking:
            body["reasoning_effort"] = self._effort

        xhr = XMLHttpRequest.new()
        xhr.open("POST", url, False)          # 同步：在 Worker 里阻塞，不影响界面
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.setRequestHeader("Authorization", "Bearer " + api_key)
        xhr.send(json.dumps(body, ensure_ascii=False))

        if xhr.status != 200:
            detail = (xhr.responseText or "")[:400]
            raise RuntimeError(f"AI 接口返回 HTTP {xhr.status}：{detail}")
        try:
            return json.loads(xhr.responseText)
        except ValueError as exc:
            raise RuntimeError("AI 接口返回的不是 JSON") from exc

    def stream_chat(
        self,
        messages: list[dict],
        *,
        on_reasoning_token: Callable[[str], None] | None = None,
        on_content_token: Callable[[str], None] | None = None,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        cancel_token: Any = None,
        timeout_s: float = 600.0,
    ) -> _Reply:
        started = time.time()
        raw = self._post(messages, thinking=thinking)
        try:
            message = raw["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"AI 返回结构异常：{str(raw)[:300]}") from exc

        content = str(message.get("content") or "")
        reasoning = str(message.get("reasoning_content") or message.get("reasoning") or "")
        # 同步 XHR 拿不到增量，这里一次性把整段交给回调（编排器按「是否已流式」判断，不会重复）
        if reasoning and on_reasoning_token is not None:
            on_reasoning_token(reasoning)
        if content and on_content_token is not None:
            on_content_token(content)

        reply = _Reply(content, reasoning, raw)
        reply.latency_ms = (time.time() - started) * 1000.0
        return reply

    def chat(self, messages: list[dict], **kw: Any) -> _Reply:
        kw.pop("timeout_s", None)
        return self.stream_chat(messages, **kw)


# ── 记录写入 / 经验库替身 ─────────────────────────────────────────────────────

class _MemoryWriter:
    """浏览器里不落盘：把记录留在内存（供调试），接口与 PendingWriter 完全一致。

    引擎会调用 save_full / save_partial / append_followup；其余未知方法一律吞掉，
    避免因为桌面端新增了方法就让 Web 版崩掉。
    """

    def __init__(self) -> None:
        self.saved: list[dict] = []

    def save_full(self, record: Any) -> str:
        self._remember(record, "ok")
        return f"memory://{len(self.saved)}"

    def save(self, record: Any, reason: str = "ok") -> str:
        self._remember(record, reason)
        return f"memory://{len(self.saved)}"

    def save_partial(self, record: Any, reason: str) -> str:
        self._remember(record, reason)
        return f"memory://{len(self.saved)}"

    def append_followup(self, record_id: str, turn: Any) -> None:
        return None

    def _remember(self, record: Any, reason: str) -> None:
        try:
            self.saved.append(
                {
                    "reason": reason,
                    "ts": int(time.time() * 1000),
                    "symbol": getattr(getattr(record, "meta", None), "symbol", ""),
                }
            )
        except Exception:  # noqa: BLE001
            pass

    def __getattr__(self, name: str):
        """兜底：任何未实现的方法都当空操作，返回 None。"""
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: None


class _EmptyExperience:
    def load_for(self, *a: Any, **k: Any) -> list:
        return []

    def __getattr__(self, _name: str):
        return lambda *a, **k: []


# ── 对外接口 ──────────────────────────────────────────────────────────────────

def _build_settings(payload: dict):
    from pa_agent.config.settings import (
        AIProviderSettings,
        GeneralSettings,
        PromptSettings,
        Settings,
        ValidationSettings,
    )

    ai = payload.get("ai") or {}
    gen = payload.get("general") or {}
    return Settings(
        provider=AIProviderSettings(
            model=str(ai.get("model") or "deepseek-chat"),
            base_url=str(ai.get("baseUrl") or "https://api.deepseek.com"),
            api_key=str(ai.get("apiKey") or ""),
            thinking=bool(ai.get("thinking", False)),
            reasoning_effort=str(ai.get("effort") or "high"),
        ),
        general=GeneralSettings(
            decision_stance=str(gen.get("decisionStance") or "balanced"),
            analysis_bar_count=int(gen.get("barCount") or 100),
            enable_next_bar_prediction=bool(gen.get("nextBar") or False),
        ),
        prompt=PromptSettings(
            stage2_load_full_strategy_library=False,
            experience_max_entries=0,
            stage1_inject_pattern_briefs=True,
        ),
        validation=ValidationSettings(),
    )


def _bars_from_payload(rows: list[dict]):
    from pa_agent.data.base import KlineBar

    bars = []
    for i, row in enumerate(rows):
        bars.append(
            KlineBar(
                seq=i + 1,
                ts_open=float(row["ts"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume") or 0.0),
                amount=float(row.get("amount") or 0.0),
                closed=bool(row.get("closed", True)),
            )
        )
    return bars


def run_analysis(payload_json: str, on_progress: Callable[[str, str], None] | None = None) -> str:
    """跑一轮两阶段分析，返回 JSON 字符串。"""
    _prepare_engine()
    payload = json.loads(payload_json)
    emit = on_progress or (lambda _stage, _text: None)

    from pa_agent.ai.prompt_assembler import PromptAssembler
    from pa_agent.ai.json_validator import JsonValidator
    from pa_agent.ai.router import route_strategy_files
    from pa_agent.config.paths import PROMPT_DIR
    from pa_agent.data.snapshot import take_snapshot_from_bars
    from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
    from pa_agent.util.threading import CancelToken

    settings = _build_settings(payload)
    symbol = str(payload.get("symbol") or "")
    timeframe = str(payload.get("timeframe") or "")
    bar_count = int((payload.get("general") or {}).get("barCount") or 100)
    now_ms = int(payload.get("nowMs") or time.time() * 1000)

    bars = _bars_from_payload(payload.get("bars") or [])
    frame = take_snapshot_from_bars(bars, bar_count, symbol, timeframe, now_ms=now_ms)

    client = BrowserLLMClient(settings.provider, effort=settings.provider.reasoning_effort)
    exp = _EmptyExperience()
    assembler = PromptAssembler(
        prompt_dir=PROMPT_DIR, experience_reader=exp, prompt_settings=settings.prompt
    )
    orchestrator = TwoStageOrchestrator(
        client=client,
        assembler=assembler,
        router=route_strategy_files,
        validator=JsonValidator(settings),
        pending_writer=_MemoryWriter(),
        exp_reader=exp,
        settings=settings,
    )

    def _on_event(event: Any) -> None:
        name = getattr(event, "name", None) or str(event).split(".")[-1]
        emit("event", name)

    record = orchestrator.submit(
        frame,
        CancelToken(),
        _on_event,
        on_stage1_reasoning=lambda t: emit("stage1_reasoning", t),
        on_stage1_content=lambda t: emit("stage1_content", t),
        on_stage2_reasoning=lambda t: emit("stage2_reasoning", t),
        on_stage2_content=lambda t: emit("stage2_content", t),
        on_stage2_files=lambda files: emit("stage2_files", json.dumps(files, ensure_ascii=False)),
    )

    out = {
        "symbol": symbol,
        "timeframe": timeframe,
        "close": float(bars[0].close) if bars else None,
        "stage1": record.stage1_diagnosis or {},
        "stage2": record.stage2_decision or {},
        "exception": record.exception or None,
        "usage": record.usage_total or {},
        "strategy_files": list(record.strategy_files_used or []),
    }
    return json.dumps(out, ensure_ascii=False)


def plan_trade(payload_json: str) -> str:
    """按「以损定量」算下单量（复用桌面端同一套代码，不联网）。"""
    _prepare_engine()
    payload = json.loads(payload_json)
    from pa_agent.trading.okx_trader import (
        InstrumentSpec,
        TradeRejected,
        plan_order,
    )

    spec = InstrumentSpec.from_okx(payload["instrument"])
    try:
        plan = plan_order(
            payload["decision"],
            spec,
            equity_usd=float(payload["equityUsd"]),
            max_loss_usd=float(payload["maxLossUsd"]),
            leverage=int(payload.get("leverage") or 1),
            price=payload.get("price"),
        )
    except TradeRejected as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    return json.dumps(
        {
            "ok": True,
            "plan": {
                "instId": plan.inst_id,
                "side": plan.side,
                "ordType": plan.ord_type,
                "size": plan.size,
                "price": plan.price,
                "stopPx": plan.stop_px,
                "takeProfitPx": plan.take_profit_px,
                "riskUsd": plan.risk_usd,
                "notionalUsd": plan.notional_usd,
                "baseQty": plan.base_qty,
                "stopDistance": plan.stop_distance,
                "leverage": plan.leverage,
                "notes": list(plan.notes),
            },
        },
        ensure_ascii=False,
    )


def engine_selftest() -> str:
    """给前端做健康检查：确认引擎能 import、策略文本能读到。"""
    _prepare_engine()
    info: dict[str, Any] = {}
    try:
        from pa_agent.config.paths import PROMPT_DIR
        from pa_agent.ai.router import route_strategy_files

        files = sorted(p.name for p in PROMPT_DIR.glob("*.txt"))
        info["prompt_files"] = len(files)
        info["sample"] = files[:3]
        info["routed"] = route_strategy_files(
            {
                "cycle_position": "trading_range",
                "direction": "bullish",
                "gate_result": "proceed",
                "detected_patterns": [],
                "gate_trace": [],
            }
        )
        info["ok"] = True
    except Exception as exc:  # noqa: BLE001
        info = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return json.dumps(info, ensure_ascii=False)
