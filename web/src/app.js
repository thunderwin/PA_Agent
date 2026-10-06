/* PA Agent Web —— 纯静态工具：密钥只存本机浏览器，直接调 OKX / 你自己配的 AI 接口。 */

const OKX_BASE = "https://www.okx.com";
const BAR_CODE = { "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "4h": "4H", "1d": "1D" };
const WARMUP = 55;
const ORDER_TYPES = ["限价单", "突破单", "市价单"];
const $ = (id) => document.getElementById(id);

const DEFAULTS = {
  ai: { baseUrl: "https://api.deepseek.com", model: "deepseek-chat", apiKey: "", thinking: false, effort: "high" },
  okx: { apiKey: "", secretKey: "", passphrase: "", simulated: true },
  risk: { maxLossUsd: 10, leverage: 10, minConfidence: 60 },
  auto: { enabled: false, maxOpenPositions: 4, dailyLossCapUsd: 30, liveAck: false },
  watchTimeframe: "15m",
  watch: ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "XAU-USDT-SWAP", "XAG-USDT-SWAP"],
  watchAuto: true,
  general: { decisionStance: "balanced", barCount: 100, nextBar: false },
};

const state = {
  settings: loadSettings(),
  engine: null,
  bars: [],
  decision: null,       // 内层 decision
  stage1: null,
  decisions: new Map(), // symbol -> 最近一次完整分析结果（用于快速切换时即时展示）
  watch: new Map(),     // symbol -> {price, order, dir, conf, ts, status, closedTs}
  autoLog: loadAutoLog(),
  account: { equity: null, positions: null, pnlToday: null, ts: 0 },
  busy: false,
};

// ── 设置（只存本机浏览器） ────────────────────────────────────────────────────
function loadSettings() {
  try {
    const raw = localStorage.getItem("paAgentWeb");
    return raw ? deepMerge(structuredClone(DEFAULTS), JSON.parse(raw)) : structuredClone(DEFAULTS);
  } catch {
    return structuredClone(DEFAULTS);
  }
}
function saveSettings() {
  localStorage.setItem("paAgentWeb", JSON.stringify(state.settings));
}
function deepMerge(base, patch) {
  for (const [k, v] of Object.entries(patch || {})) {
    if (v && typeof v === "object" && !Array.isArray(v)) base[k] = deepMerge(base[k] || {}, v);
    else base[k] = v;
  }
  return base;
}

// ── 工具 ─────────────────────────────────────────────────────────────────────
function setStatus(text, isError = false) {
  const el = $("status");
  el.textContent = text;
  el.style.color = isError ? "var(--short)" : "";
}
function normalizeSymbol(input) {
  const s = String(input || "").trim().toUpperCase().replace(/[\s/_]+/g, "-");
  if (!s) return "";
  if (s.includes("-")) return s;
  for (const q of ["USDT", "USDC", "USD", "BTC", "ETH"]) {
    if (s.endsWith(q) && s.length > q.length) return `${s.slice(0, -q.length)}-${q}-SWAP`;
  }
  return s.length <= 6 ? `${s}-USDT-SWAP` : s;
}
function fmtPrice(v) {
  if (v == null || Number.isNaN(v)) return "—";
  if (v >= 1000) return v.toLocaleString("en-US", { maximumFractionDigits: 1 });
  if (v >= 1) return v.toFixed(3);
  return v.toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
}
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// ── 引擎（Pyodide 工作线程） ─────────────────────────────────────────────────
class Engine {
  constructor() {
    this.worker = new Worker("worker.js");
    this.seq = 0;
    this.pending = new Map();
    this.progress = () => {};
    this.ready = new Promise((resolve, reject) => {
      this._resolveReady = resolve;
      this._rejectReady = reject;
    });
    this.worker.onmessage = (ev) => {
      const m = ev.data;
      if (m.type === "boot-progress") {
        $("engine-badge").textContent = m.text;
        return;
      }
      if (m.type === "ready") {
        $("engine-badge").textContent = m.info.ok
          ? `引擎就绪（策略文件 ${m.info.prompt_files} 个）`
          : `引擎异常：${m.info.error}`;
        $("engine-badge").classList.toggle("ok", !!m.info.ok);
        $("btn-analyze").disabled = !m.info.ok;
        if (!m.info.ok) setStatus(`引擎自检失败：${m.info.error}`, true);
        this._resolveReady(m.info);
        return;
      }
      if (m.type === "boot-error") {
        $("engine-badge").textContent = "引擎加载失败（点此重试）";
        $("engine-badge").style.cursor = "pointer";
        $("engine-badge").onclick = () => {
          $("engine-badge").textContent = "引擎加载中…";
          $("btn-analyze").disabled = true;
          this.worker.postMessage({ type: "boot" });
        };
        setStatus(`引擎加载失败：${m.message}`, true);
        this._rejectReady(new Error(m.message));
        return;
      }
      if (m.type === "progress") {
        this.progress(m.stage, m.text);
        return;
      }
      const entry = this.pending.get(m.id);
      if (!entry) return;
      this.pending.delete(m.id);
      if (m.type === "error") entry.reject(new Error(m.message));
      else entry.resolve(m.data);
    };
  }
  _send(type, payload, onProgress) {
    const id = ++this.seq;
    if (onProgress) this.progress = onProgress;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.worker.postMessage({ type, id, payload });
    });
  }
  analyze(payload, onProgress) { return this._send("analyze", payload, onProgress); }
  plan(payload) { return this._send("plan", payload); }
}

// ── OKX 行情 ─────────────────────────────────────────────────────────────────
async function okxPublic(path, params = {}) {
  const url = new URL(OKX_BASE + path);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  const res = await fetch(url);
  if (!res.ok) throw new Error(`OKX HTTP ${res.status}`);
  const json = await res.json();
  if (String(json.code) !== "0") throw new Error(`OKX 错误 ${json.code}: ${json.msg || ""}`);
  return json.data || [];
}

async function fetchCandles(symbol, timeframe, need) {
  const bar = BAR_CODE[timeframe] || timeframe;
  const want = Math.max(need, 60);
  let rows = await okxPublic("/api/v5/market/candles", { instId: symbol, bar, limit: Math.min(want, 300) });
  while (rows.length < want && rows.length) {
    const oldest = rows[rows.length - 1][0];
    const page = await okxPublic("/api/v5/market/history-candles", {
      instId: symbol, bar, limit: Math.min(want - rows.length, 100), after: oldest,
    });
    if (!page.length || Number(page[page.length - 1][0]) >= Number(oldest)) break;
    rows = rows.concat(page);
  }
  return rows.map((r) => ({
    ts: Number(r[0]), open: +r[1], high: +r[2], low: +r[3], close: +r[4],
    volume: +r[5], amount: +r[7], closed: String(r[8]) === "1",
  }));
}

async function fetchInstrument(instId) {
  const instType = instId.split("-").length >= 3 ? "SWAP" : "SPOT";
  const rows = await okxPublic("/api/v5/public/instruments", { instType, instId });
  if (!rows.length) throw new Error(`查不到合约规格：${instId}`);
  return rows[0];
}

// ── OKX 私有接口（浏览器内签名，密钥不出本机） ────────────────────────────────
async function hmacBase64(secret, message) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  return btoa(String.fromCharCode(...new Uint8Array(mac)));
}

async function okxPrivate(method, path, { params, body } = {}) {
  const { apiKey, secretKey, passphrase, simulated } = state.settings.okx;
  if (!apiKey || !secretKey || !passphrase) throw new Error("还没填 OKX API 凭据（右侧「设置」里填）");
  const query = params ? "?" + new URLSearchParams(params).toString() : "";
  const requestPath = path + query;
  const bodyText = body ? JSON.stringify(body) : "";
  const ts = new Date().toISOString();
  const sign = await hmacBase64(secretKey, ts + method + requestPath + bodyText);
  const headers = {
    "Content-Type": "application/json",
    "OK-ACCESS-KEY": apiKey,
    "OK-ACCESS-SIGN": sign,
    "OK-ACCESS-TIMESTAMP": ts,
    "OK-ACCESS-PASSPHRASE": passphrase,
  };
  if (simulated) headers["x-simulated-trading"] = "1";
  const res = await fetch(OKX_BASE + requestPath, {
    method, headers, body: bodyText || undefined,
  });
  const json = await res.json().catch(() => ({}));
  if (String(json.code) !== "0") throw new Error(`OKX ${json.code}: ${json.msg || JSON.stringify(json.data || {})}`);
  return json.data || [];
}

async function okxEquityUsd() {
  const rows = await okxPrivate("GET", "/api/v5/account/balance", { params: { ccy: "USDT" } });
  if (!rows.length) return 0;
  const total = rows[0].totalEq;
  return total ? Number(total) : Number(rows[0]?.details?.[0]?.eq || 0);
}

async function okxPositions() {
  return okxPrivate("GET", "/api/v5/account/positions", { params: { instType: "SWAP" } });
}

/* 当日（UTC+8）已实现盈亏 + 手续费，用于自动交易的日亏熔断。 */
async function okxRealizedPnlToday() {
  const offsetMs = 8 * 3600 * 1000;
  const now = Date.now();
  const dayStart = Math.floor((now + offsetMs) / 86400000) * 86400000 - offsetMs;
  let total = 0;
  for (const type of ["2", "3", "4"]) {
    const rows = await okxPrivate("GET", "/api/v5/account/bills", { params: { type, limit: "100" } });
    for (const row of rows) {
      if (Number(row.ts || 0) < dayStart) continue;
      total += Number(row.pnl || 0) + Number(row.fee || 0);
    }
  }
  return total;
}

// ── 自动交易日志 / 面板 ───────────────────────────────────────────────────────
function loadAutoLog() {
  try {
    const raw = localStorage.getItem("paAgentAutoLog");
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}
function pushAutoLog(text, cls = "") {
  state.autoLog.unshift({ t: Date.now(), text, cls });
  state.autoLog = state.autoLog.slice(0, 200);
  try { localStorage.setItem("paAgentAutoLog", JSON.stringify(state.autoLog)); } catch { /* ignore */ }
  renderAutoPanel();
}
function notify(text) {
  setStatus(text);
  try {
    if (window.Notification && Notification.permission === "granted") {
      new Notification("PA Agent", { body: text });
    }
  } catch { /* ignore */ }
}

function renderAutoPanel() {
  const s = state.settings;
  $("auto-state").textContent = s.auto.enabled ? `开启（${s.okx.simulated ? "模拟盘" : "实盘"}）` : "关闭";
  $("auto-state").className = s.auto.enabled ? (s.okx.simulated ? "" : "short") : "";
  $("auto-tf").textContent = s.watchTimeframe;
  $("auto-symbols").textContent = s.watch.length ? s.watch.join(" / ") : "（未设置）";
  const pnl = state.account.pnlToday;
  $("auto-pnl").textContent = pnl == null ? "—" : `${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)} USDT`;
  $("auto-pnl").className = pnl != null && pnl < 0 ? "short" : "";
  $("auto-pos").textContent = state.account.positions == null ? "—" : String(state.account.positions);
  $("auto-risk").textContent = `${s.risk.maxLossUsd} USDT · ${s.risk.leverage}x · 门槛 ${s.risk.minConfidence}`;
  $("btn-auto-toggle").textContent = s.auto.enabled ? "停止自动交易" : "开始自动交易";
  $("btn-auto-toggle").className = s.auto.enabled ? "danger" : "primary";

  const box = $("auto-log");
  box.innerHTML = "";
  if (!state.autoLog.length) {
    box.append(el("div", "skip", "（暂无记录）"));
  }
  for (const row of state.autoLog.slice(0, 80)) {
    const div = el("div", row.cls || "");
    div.append(el("span", "time", new Date(row.t).toLocaleString("zh-CN", { hour12: false })));
    div.append(document.createTextNode(row.text));
    box.append(div);
  }
}

async function refreshAccount() {
  if (!state.settings.okx.apiKey) return;
  try {
    const [equity, positions, pnl] = await Promise.all([
      okxEquityUsd(), okxPositions(), okxRealizedPnlToday(),
    ]);
    state.account = {
      equity,
      positions: positions.filter((p) => Math.abs(Number(p.pos || 0)) > 0).length,
      pnlToday: pnl,
      ts: Date.now(),
    };
  } catch (err) {
    pushAutoLog(`读取账户失败：${cleanMsg(err)}`, "err");
  }
  renderAutoPanel();
}
function cleanMsg(err) {
  return String((err && err.message) || err).split("\n").pop().trim();
}

// ── 图表 ─────────────────────────────────────────────────────────────────────
let chart, candleSeries, emaLines = [];

function initChart() {
  const box = $("chart");
  chart = LightweightCharts.createChart(box, {
    layout: { background: { color: "transparent" }, textColor: "#8b949e" },
    grid: { vertLines: { color: "#161b22" }, horzLines: { color: "#161b22" } },
    rightPriceScale: { borderColor: "#21262d" },
    timeScale: { borderColor: "#21262d", timeVisible: true, secondsVisible: false },
    crosshair: { mode: 0 },
  });
  candleSeries = chart.addCandlestickSeries({
    upColor: "#3fb950", downColor: "#f85149", borderVisible: false,
    wickUpColor: "#3fb950", wickDownColor: "#f85149",
  });
  const ro = new ResizeObserver(() => chart.applyOptions({ width: box.clientWidth, height: box.clientHeight }));
  ro.observe(box);
  chart.applyOptions({ width: box.clientWidth, height: box.clientHeight });
}

function renderChart(bars) {
  if (!chart || !bars.length) return;
  const asc = [...bars].reverse();
  candleSeries.setData(asc.map((b) => ({
    time: Math.floor(b.ts / 1000), open: b.open, high: b.high, low: b.low, close: b.close,
  })));
  // EMA20
  const k = 2 / 21;
  let ema = null;
  const emaData = asc.map((b) => {
    ema = ema == null ? b.close : b.close * k + ema * (1 - k);
    return { time: Math.floor(b.ts / 1000), value: ema };
  });
  if (!emaLines.length) {
    emaLines = [chart.addLineSeries({ color: "#d29922", lineWidth: 1, priceLineVisible: false, lastValueVisible: false })];
  }
  emaLines[0].setData(emaData.slice(19));
  chart.timeScale().fitContent();
}

function clearDecisionLines() {
  if (!candleSeries) return;
  if (candleSeries._paLines) {
    for (const line of candleSeries._paLines) candleSeries.removePriceLine(line);
  }
  candleSeries._paLines = [];
}

function drawDecisionLines(decision) {
  if (!candleSeries || !decision) return;
  clearDecisionLines();
  const add = (price, color, title) => {
    if (!price) return;
    const line = candleSeries.createPriceLine({ price: Number(price), color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title });
    candleSeries._paLines.push(line);
  };
  add(decision.entry_price, "#2f81f7", "入场");
  add(decision.stop_loss_price, "#f85149", "止损");
  add(decision.take_profit_price, "#3fb950", "止盈");
}

// ── 分析 ─────────────────────────────────────────────────────────────────────
function currentSymbol() { return normalizeSymbol($("symbol").value); }
function currentTimeframe() { return $("timeframe").value; }

function analysisPayload(bars, symbol, timeframe, price) {
  const s = state.settings;
  return {
    symbol, timeframe, bars, nowMs: Date.now(),
    ai: { baseUrl: s.ai.baseUrl, model: s.ai.model, apiKey: s.ai.apiKey, thinking: s.ai.thinking, effort: s.ai.effort },
    general: { ...s.general, barCount: Math.min(s.general.barCount, Math.max(bars.length - WARMUP, 20)) },
    price,
  };
}

async function runAnalysis(symbol, timeframe, bars, { silent = false } = {}) {
  const stream = $("stream");
  const onProgress = (stage, text) => {
    if (stage === "event") {
      stream.textContent += `\n[阶段] ${text}\n`;
      return;
    }
    if (stage === "stage2_files") { stream.textContent += `\n[策略文件] ${text}\n`; return; }
    if (stage.endsWith("reasoning") || stage.endsWith("content")) {
      stream.textContent += `\n[${stage}]\n${text}\n`;
    }
  };
  const out = await state.engine.analyze(analysisPayload(bars, symbol, timeframe), onProgress);
  return out;
}

function renderDecision(out) {
  const stage2 = out.stage2 || {};
  const d = stage2.decision || {};
  if (out.symbol) state.decisions.set(out.symbol, out);
  state.decision = d;
  state.stage1 = out.stage1 || {};
  $("decision-empty").classList.toggle("hidden", true);
  $("decision-body").classList.remove("hidden");
  $("d-order").textContent = d.order_type || "—";
  $("d-dir").textContent = d.order_direction || "—";
  $("d-prices").textContent = [d.entry_price, d.stop_loss_price, d.take_profit_price]
    .map((v) => (v == null ? "—" : fmtPrice(Number(v)))).join(" / ");
  $("d-conf").textContent = d.trade_confidence != null ? Math.round(d.trade_confidence) : "—";
  $("d-cycle").textContent = (out.stage1 && (out.stage1.cycle_position || out.stage1.market_phase)) || "—";
  $("d-reason").textContent = d.reasoning || "—";
  $("d-stage1").textContent = JSON.stringify(out.stage1 || {}, null, 2);
  drawDecisionLines(d);
  const executable = ORDER_TYPES.includes(String(d.order_type || "").trim());
  $("btn-trade").disabled = !executable;
  $("d-plan").textContent = executable ? "点「执行下单」计算" : "当前无可执行方案";
  renderQuickSymbols();
}

function resetDecisionPanel() {
  state.decision = null;
  state.stage1 = null;
  $("decision-empty").classList.remove("hidden");
  $("decision-body").classList.add("hidden");
  $("btn-trade").disabled = true;
  clearDecisionLines();
}

/* 顶部快捷品种：来自监控列表，点一下切换；圆点表示最近一次分析的方案方向。 */
function renderQuickSymbols() {
  const box = $("quick-symbols");
  if (!box) return;
  const current = currentSymbol();
  const symbols = [...state.settings.watch];
  if (current && !symbols.includes(current)) symbols.push(current);
  box.innerHTML = "";
  if (!symbols.length) {
    const hint = el("span", "muted small", "（点「＋监控」把当前品种加进来）");
    box.append(hint);
    return;
  }
  for (const sym of symbols) {
    const row = state.watch.get(sym) || {};
    const out = state.decisions.get(sym);
    const d = out?.stage2?.decision || {};
    let dotCls = "";
    if (d.order_type && ORDER_TYPES.includes(String(d.order_type).trim())) {
      dotCls = d.order_direction === "做多" ? "long" : d.order_direction === "做空" ? "short" : "none";
    } else if (d.order_type) {
      dotCls = "none";
    }
    const chip = el("span", "chip" + (sym === current ? " active" : ""));
    const dot = el("span", "dot " + dotCls);
    const conf = d.trade_confidence != null ? ` ${Math.round(d.trade_confidence)}` : "";
    chip.append(dot, document.createTextNode(sym.replace(/-USDT-SWAP$/, "").replace(/-USDT$/, "")));
    if (conf) chip.append(el("span", "conf", conf));
    chip.title = `${sym}${row.price ? ` · 最新价 ${fmtPrice(row.price)}` : ""}` +
      (d.order_type ? ` · 最近决策 ${d.order_type}${d.order_direction ? " " + d.order_direction : ""}` : " · 暂无分析");
    chip.onclick = () => switchToSymbol(sym);
    box.append(chip);
  }
}

/* 切品种：先把缓存的结果立刻显示出来，再拉最新 K 线（不自动烧 token）。 */
async function switchToSymbol(symbol, { analyze = false } = {}) {
  if (!symbol || state.busy) return;
  $("symbol").value = symbol;
  const cached = state.decisions.get(symbol);
  if (cached) renderDecision(cached);
  else resetDecisionPanel();
  renderQuickSymbols();
  if (analyze) return onAnalyze();
  try {
    setStatus(`正在加载 ${symbol} …`);
    const bars = await fetchCandles(symbol, currentTimeframe(), state.settings.general.barCount + WARMUP + 5);
    state.bars = bars;
    renderChart(bars);
    if (cached) drawDecisionLines(cached.stage2?.decision);
    const what = cached?.stage2?.decision?.order_type || "尚无分析";
    setStatus(cached ? `${symbol} 已切换（显示上次分析结果：${what}）` : `${symbol} 已切换（还没分析过，点「提交分析」）`);
  } catch (err) {
    setStatus(`取数失败：${err.message}`, true);
  }
}

function toggleWatchCurrent() {
  const sym = currentSymbol();
  if (!sym) return;
  const list = state.settings.watch;
  const idx = list.indexOf(sym);
  if (idx >= 0) list.splice(idx, 1);
  else { list.push(sym); state.watch.set(sym, state.watch.get(sym) || { status: "等待" }); }
  saveSettings();
  renderQuickSymbols();
  renderWatch();
  setStatus(idx >= 0 ? `已把 ${sym} 移出监控列表` : `已把 ${sym} 加入监控列表`);
}

async function onAnalyze() {
  if (state.busy) return;
  const symbol = currentSymbol();
  const timeframe = currentTimeframe();
  if (!symbol) return;
  state.busy = true;
  $("btn-analyze").disabled = true;
  const started = Date.now();
  let timer = null;
  setStatus("正在取 K 线…");
  try {
    const need = state.settings.general.barCount + WARMUP + 5;
    const bars = await fetchCandles(symbol, timeframe, need);
    state.bars = bars;
    renderChart(bars);
    setStatus("分析中（引擎已在浏览器内运行）…");
    timer = setInterval(() => {
      const sec = Math.round((Date.now() - started) / 1000);
      setStatus(`分析中…已用 ${sec} 秒（两阶段模型推理通常 30–120 秒）`);
    }, 1000);
    const out = await runAnalysis(symbol, timeframe, bars);
    if (out.exception) throw new Error(`${out.exception.type}: ${out.exception.message || ""}`);
    renderDecision(out);
    const used = Math.round((Date.now() - started) / 1000);
    setStatus(`分析完成（${used} 秒）：${out.stage2?.decision?.order_type || "—"}`);
  } catch (err) {
    setStatus(`分析失败：${err.message}`, true);
  } finally {
    if (timer) clearInterval(timer);
    state.busy = false;
    $("btn-analyze").disabled = false;
  }
}

// ── 下单（浏览器内签名 + 以损定量） ───────────────────────────────────────────
function orderBody(symbol, plan) {
  const body = {
    instId: symbol,
    tdMode: symbol.split("-").length >= 3 ? "cross" : "cash",
    side: plan.side,
    ordType: plan.ordType,
    sz: String(plan.size),
  };
  if (body.tdMode === "cross") body.posSide = "net";
  else body.tgtCcy = "base_ccy";
  if (plan.ordType === "limit") body.px = String(plan.price);
  if (plan.ordType === "trigger") { body.triggerPx = String(plan.price); body.orderPx = "-1"; }
  const algo = {};
  if (plan.stopPx) { algo.slTriggerPx = String(plan.stopPx); algo.slOrdPx = "-1"; }
  if (plan.takeProfitPx) { algo.tpTriggerPx = String(plan.takeProfitPx); algo.tpOrdPx = "-1"; }
  if (Object.keys(algo).length) body.attachAlgoOrds = [algo];
  return body;
}

async function planForSymbol(symbol, decision) {
  const s = state.settings;
  const [instrument, equity] = await Promise.all([fetchInstrument(symbol), okxEquityUsd()]);
  const planOut = await state.engine.plan({
    decision, instrument, equityUsd: equity,
    maxLossUsd: Number(s.risk.maxLossUsd), leverage: Number(s.risk.leverage),
    price: state.bars[0]?.close,
  });
  return { planOut, equity };
}

async function sendOrder(symbol, plan) {
  const body = orderBody(symbol, plan);
  if (body.tdMode === "cross") {
    await okxPrivate("POST", "/api/v5/account/set-leverage", {
      body: { instId: symbol, lever: String(plan.leverage), mgnMode: "cross" },
    }).catch(() => {});
  }
  const rows = await okxPrivate("POST", "/api/v5/trade/order", { body });
  const row = rows[0] || {};
  if (String(row.sCode ?? "0") !== "0") throw new Error(row.sMsg || JSON.stringify(row));
  return row;
}

function planSummary(symbol, timeframe, p, equity) {
  return [
    `【OKX ${state.settings.okx.simulated ? "模拟盘" : "实盘"} 下单确认】`,
    `${symbol} ${timeframe}`,
    `方向/类型：${p.side === "buy" ? "做多" : "做空"} · ${p.ordType === "limit" ? "限价单" : p.ordType === "trigger" ? "突破单" : "市价单"}`,
    p.price ? `价格：${fmtPrice(p.price)}` : "",
    `止损：${fmtPrice(p.stopPx)}（距离 ${fmtPrice(p.stopDistance)}）`,
    p.takeProfitPx ? `止盈：${fmtPrice(p.takeProfitPx)}` : "",
    `下单量：${p.size}（${p.baseQty} 基础币）`,
    `名义价值：${Math.round(p.notionalUsd).toLocaleString()} USDT`,
    `预计保证金：${Math.round(p.notionalUsd / Math.max(p.leverage, 1)).toLocaleString()} USDT（${p.leverage}x）`,
    equity ? `账户权益：${Math.round(equity).toLocaleString()} USDT` : "",
    "── 以损定量 ──",
    `止损被打到：亏 ${p.riskUsd.toFixed(2)} USDT`,
  ].filter(Boolean).join("\n");
}

async function onTrade() {
  const d = state.decision;
  if (!d) return;
  const symbol = currentSymbol();
  const s = state.settings;
  if (!s.okx.apiKey) { setStatus("先在「设置」里填 OKX API 凭据", true); return; }
  const conf = Number(d.trade_confidence ?? 0);
  if (conf < Number(s.risk.minConfidence)) {
    setStatus(`置信度 ${Math.round(conf)} 低于门槛 ${s.risk.minConfidence}，已拦下`, true);
    return;
  }
  try {
    setStatus("正在计算下单量…");
    const { planOut, equity } = await planForSymbol(symbol, d);
    if (!planOut.ok) {
      setStatus(`无法下单：${planOut.error}`, true);
      alert(`无法下单：\n${planOut.error}`);
      return;
    }
    const p = planOut.plan;
    $("d-plan").textContent = `${p.size} 张 · 止损亏 ${p.riskUsd.toFixed(2)} USDT`;
    if (!confirm(planSummary(symbol, currentTimeframe(), p, equity) + "\n\n确认下单吗？")) {
      setStatus("已取消下单");
      return;
    }
    if (!s.okx.simulated && !confirm("⚠️ 这是实盘：真实资金，确认后不可撤销。继续吗？")) {
      setStatus("已取消下单");
      return;
    }
    const row = await sendOrder(symbol, p);
    setStatus(`下单成功，订单号 ${row.ordId}`);
    pushAutoLog(`手动下单 ${symbol} ${p.side} ${p.size} 张 · 止损亏 ${p.riskUsd.toFixed(2)} USDT · ${row.ordId}`, "ok");
    alert(`下单成功（${s.okx.simulated ? "模拟盘" : "实盘"}）\n订单号：${row.ordId}`);
  } catch (err) {
    setStatus(`下单失败：${cleanMsg(err)}`, true);
    alert(`下单失败：\n${cleanMsg(err)}`);
  }
}

/* 自动交易：K 线收盘、分析完成后的落地判断与下单（无人值守）。 */
async function maybeAutoTrade(symbol, timeframe, out) {
  const s = state.settings;
  if (!s.auto.enabled) return;
  const d = out?.stage2?.decision || {};
  const conf = Number(d.trade_confidence ?? 0);
  const tag = `${symbol}`;

  if (!s.okx.apiKey || !s.okx.secretKey || !s.okx.passphrase) {
    pushAutoLog(`${tag} 跳过：未配置 OKX 凭据`, "err");
    return;
  }
  if (!ORDER_TYPES.includes(String(d.order_type || "").trim())) {
    pushAutoLog(`${tag} 跳过：决策「${d.order_type || "—"}」没有可执行订单`, "skip");
    return;
  }
  if (conf < Number(s.risk.minConfidence)) {
    pushAutoLog(`${tag} 跳过：置信度 ${Math.round(conf)} < 门槛 ${s.risk.minConfidence}`, "skip");
    return;
  }

  try {
    await refreshAccount();
    const positions = await okxPositions();
    const open = positions.filter((p) => Math.abs(Number(p.pos || 0)) > 0);
    if (open.some((p) => p.instId === symbol)) {
      pushAutoLog(`${tag} 跳过：该品种已有持仓`, "skip");
      return;
    }
    if (open.length >= Number(s.auto.maxOpenPositions)) {
      pushAutoLog(`${tag} 跳过：持仓数 ${open.length} 已达上限 ${s.auto.maxOpenPositions}`, "skip");
      return;
    }
    const pnl = state.account.pnlToday ?? 0;
    if (pnl <= -Math.abs(Number(s.auto.dailyLossCapUsd))) {
      pushAutoLog(`${tag} 跳过：当日已实现亏损 ${Math.abs(pnl).toFixed(2)} 已达上限，今日停止下单`, "err");
      return;
    }

    if (!s.okx.simulated && !s.auto.liveAck) {
      pushAutoLog(`${tag} 跳过：实盘自动下单未确认风险`, "err");
      return;
    }

    const { planOut, equity } = await planForSymbol(symbol, d);
    if (!planOut.ok) {
      pushAutoLog(`${tag} 跳过：无法计算仓位（${planOut.error}）`, "err");
      return;
    }
    const p = planOut.plan;
    const row = await sendOrder(symbol, p);
    const line = `✅ 自动下单 ${tag} ${timeframe} · ${p.side === "buy" ? "做多" : "做空"} ${p.size} 张 @ ${fmtPrice(p.price)}` +
      ` · 止损 ${fmtPrice(p.stopPx)}（亏 ${p.riskUsd.toFixed(2)} USDT） · 名义 ${Math.round(p.notionalUsd)} · 权益 ${Math.round(equity)} · ${row.ordId}`;
    pushAutoLog(line, "ok");
    notify(`已自动下单 ${symbol}（止损亏 ${p.riskUsd.toFixed(2)} USDT）`);
    state.watch.set(symbol, { ...(state.watch.get(symbol) || {}), status: `已下单 ${p.size} 张` });
    renderWatch();
  } catch (err) {
    pushAutoLog(`${tag} 自动下单失败：${cleanMsg(err)}`, "err");
    notify(`自动下单失败：${cleanMsg(err)}`);
  }
}

// ── 多品种监控 ───────────────────────────────────────────────────────────────
function renderWatch() {
  const tbody = $("watch-table").querySelector("tbody");
  tbody.innerHTML = "";
  for (const [symbol, row] of state.watch) {
    const tr = document.createElement("tr");
    tr.append(
      el("td", null, symbol),
      el("td", null, fmtPrice(row.price)),
      el("td", null, row.order || row.status || "—"),
      el("td", row.dir === "做多" ? "long" : row.dir === "做空" ? "short" : "", row.dir || "—"),
      el("td", null, row.conf != null ? Math.round(row.conf) : "—"),
      el("td", null, row.ts ? new Date(row.ts).toLocaleTimeString("zh-CN", { hour12: false }) : "—"),
    );
    tr.onclick = () => {
      switchToSymbol(symbol);
    };
    tbody.append(tr);
  }
}

async function watchTick() {
  for (const symbol of state.settings.watch) {
    if (state.busy) return;
    try {
      const timeframe = state.settings.watchTimeframe || "15m";
      const bars = await fetchCandles(symbol, timeframe, state.settings.general.barCount + WARMUP + 5);
      const closedTs = bars.find((b) => b.closed)?.ts ?? bars[0]?.ts;
      const prev = state.watch.get(symbol) || {};
      state.watch.set(symbol, { ...prev, price: bars[0]?.close });
      renderWatch();
      if (prev.closedTs === closedTs) continue;      // 没有新 K 线收盘 → 不调模型
      state.watch.set(symbol, { ...state.watch.get(symbol), status: "分析中…" });
      renderWatch();
      const out = await runAnalysis(symbol, timeframe, bars, { silent: true });
      const d = out.stage2?.decision || {};
      state.decisions.set(symbol, { symbol, timeframe, stage1: out.stage1, stage2: out.stage2 });
      const closedNow = bars.find((b) => b.closed)?.ts ?? bars[0]?.ts;
      state.watch.set(symbol, {
        price: bars[0]?.close, order: d.order_type, dir: d.order_direction,
        conf: d.trade_confidence, ts: Date.now(), closedTs: closedNow,
      });
      renderWatch();
      renderQuickSymbols();
      if (ORDER_TYPES.includes(String(d.order_type || "").trim())) {
        setStatus(`📣 ${symbol} ${d.order_direction} ${d.order_type}（置信度 ${Math.round(d.trade_confidence ?? 0)}）`);
      }
      await maybeAutoTrade(symbol, timeframe, out);
    } catch (err) {
      state.watch.set(symbol, { ...(state.watch.get(symbol) || {}), status: `失败：${err.message}` });
      renderWatch();
    }
  }
}

function addWatchSymbol() {
  const sym = normalizeSymbol($("watch-input").value);
  if (!sym) return;
  if (!state.settings.watch.includes(sym)) {
    state.settings.watch.push(sym);
    saveSettings();
  }
  state.watch.set(sym, state.watch.get(sym) || { status: "等待" });
  $("watch-input").value = "";
  renderWatch();
}

// ── UI 绑定 ──────────────────────────────────────────────────────────────────
function bindTabs() {
  document.querySelectorAll(".tabs button").forEach((btn) => {
    btn.onclick = () => {
      document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b === btn));
      for (const id of ["decision", "watch", "stream", "settings"]) {
        $("tab-" + id).classList.toggle("hidden", id !== btn.dataset.tab);
      }
    };
  });
}

function loadSettingsIntoForm() {
  const s = state.settings;
  $("s-base").value = s.ai.baseUrl; $("s-model").value = s.ai.model; $("s-key").value = s.ai.apiKey;
  $("s-okx-key").value = s.okx.apiKey; $("s-okx-secret").value = s.okx.secretKey; $("s-okx-pass").value = s.okx.passphrase;
  $("s-okx-sim").checked = !!s.okx.simulated;
  $("s-maxloss").value = s.risk.maxLossUsd; $("s-leverage").value = s.risk.leverage; $("s-minconf").value = s.risk.minConfidence;
  $("s-auto").checked = !!s.auto.enabled;
  $("s-watch-tf").value = s.watchTimeframe || "15m";
  $("s-maxpos").value = s.auto.maxOpenPositions;
  $("s-dailycap").value = s.auto.dailyLossCapUsd;
  $("s-liveack").checked = !!s.auto.liveAck;
  $("watch-auto").checked = !!s.watchAuto;
}

function bindSettings() {
  $("btn-save-settings").onclick = () => {
    const s = state.settings;
    s.ai.baseUrl = $("s-base").value.trim() || DEFAULTS.ai.baseUrl;
    s.ai.model = $("s-model").value.trim() || DEFAULTS.ai.model;
    s.ai.apiKey = $("s-key").value.trim();
    s.okx.apiKey = $("s-okx-key").value.trim();
    s.okx.secretKey = $("s-okx-secret").value.trim();
    s.okx.passphrase = $("s-okx-pass").value.trim();
    s.okx.simulated = $("s-okx-sim").checked;
    s.risk.maxLossUsd = Number($("s-maxloss").value) || 10;
    s.risk.leverage = Number($("s-leverage").value) || 1;
    s.risk.minConfidence = Number($("s-minconf").value) || 0;
    s.auto.enabled = $("s-auto").checked;
    s.watchTimeframe = $("s-watch-tf").value || "15m";
    s.auto.maxOpenPositions = Number($("s-maxpos").value) || 4;
    s.auto.dailyLossCapUsd = Number($("s-dailycap").value) || 30;
    s.auto.liveAck = $("s-liveack").checked;
    saveSettings();
    renderAutoPanel();
    if (s.auto.enabled && !s.okx.simulated && !s.auto.liveAck) {
      setStatus("⚠️ 自动下单已开但选了实盘：请在下面勾选实盘风险确认，否则不会下单", true);
    } else if (s.auto.enabled) {
      setStatus(`自动交易已开启（${s.okx.simulated ? "模拟盘" : "实盘"} · 周期 ${s.watchTimeframe}）`);
    } else {
      setStatus("设置已保存到本机浏览器");
    }
  };
  $("btn-clear-settings").onclick = () => {
    if (!confirm("清除本机浏览器里保存的全部密钥与设置？")) return;
    localStorage.removeItem("paAgentWeb");
    state.settings = structuredClone(DEFAULTS);
    loadSettingsIntoForm();
    setStatus("已清除本机保存的密钥");
  };
}

function bindWatch() {
  $("btn-watch-add").onclick = addWatchSymbol;
  $("watch-input").onkeydown = (e) => { if (e.key === "Enter") addWatchSymbol(); };
  $("watch-auto").onchange = (e) => {
    state.settings.watchAuto = e.target.checked;
    saveSettings();
    setStatus(e.target.checked ? "自动监控已开启（每分钟检查一次新 K 线）" : "自动监控已关闭");
  };
}

function bindAutoPanel() {
  $("btn-auto-toggle").onclick = async () => {
    const s = state.settings;
    if (s.auto.enabled) {
      s.auto.enabled = false;
      saveSettings();
      pushAutoLog("已停止自动交易", "skip");
      renderAutoPanel();
      return;
    }
    if (!s.okx.apiKey || !s.okx.secretKey || !s.okx.passphrase) {
      alert("请先在「设置」里填好 OKX API 凭据（API Key / Secret / Passphrase）");
      return;
    }
    if (!s.watch.length) {
      alert("监控列表为空：先在「多品种」页添加要监控的品种（如 BTC-USDT-SWAP）");
      return;
    }
    if (!s.okx.simulated) {
      if (!s.auto.liveAck) {
        alert("实盘自动下单需要先在「设置」里勾选『我已知晓：实盘自动下单是真实资金…』");
        return;
      }
      const ok = confirm(
        `即将开启【实盘】自动下单：\n\n` +
        `监控：${s.watch.join(" / ")}\n` +
        `周期：${s.watchTimeframe}\n` +
        `每笔最大亏损：${s.risk.maxLossUsd} USDT · 杠杆 ${s.risk.leverage}x · 置信度门槛 ${s.risk.minConfidence}\n` +
        `最大持仓：${s.auto.maxOpenPositions} · 当日亏损上限：${s.auto.dailyLossCapUsd} USDT\n\n` +
        `达到条件时会不经确认直接下单（止损止盈挂交易所）。确定开启吗？`);
      if (!ok) return;
    }
    s.auto.enabled = true;
    saveSettings();
    try {
      if (window.Notification && Notification.permission === "default") {
        await Notification.requestPermission();
      }
    } catch { /* ignore */ }
    pushAutoLog(
      `已开启自动交易（${s.okx.simulated ? "模拟盘" : "实盘"} · 周期 ${s.watchTimeframe} · ` +
      `监控 ${s.watch.join("/")}）`, "ok");
    renderAutoPanel();
    refreshAccount();
  };
  $("btn-auto-refresh").onclick = () => refreshAccount();
  $("btn-auto-log-clear").onclick = () => {
    state.autoLog = [];
    try { localStorage.removeItem("paAgentAutoLog"); } catch { /* ignore */ }
    renderAutoPanel();
  };
}

async function loadSymbolPresets() {
  const presets = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP",
    "XAU-USDT-SWAP", "XAG-USDT-SWAP", "XPT-USDT-SWAP", "PAXG-USDT",
  ];
  const dl = $("symbol-list");
  for (const p of presets) {
    const o = document.createElement("option");
    o.value = p;
    dl.append(o);
  }
}

function main() {
  initChart();
  bindTabs();
  bindSettings();
  bindWatch();
  bindAutoPanel();
  loadSettingsIntoForm();
  loadSymbolPresets();
  renderWatch();
  for (const s of state.settings.watch) state.watch.set(s, state.watch.get(s) || { status: "等待" });
  renderWatch();
  renderQuickSymbols();
  renderAutoPanel();

  $("btn-fetch").onclick = async () => {
    try {
      setStatus("取 K 线…");
      const bars = await fetchCandles(currentSymbol(), currentTimeframe(),
        state.settings.general.barCount + WARMUP + 5);
      state.bars = bars;
      renderChart(bars);
      setStatus(`已加载 ${bars.length} 根 K 线`);
    } catch (err) { setStatus(`取数失败：${err.message}`, true); }
  };
  $("btn-analyze").onclick = onAnalyze;
  $("btn-trade").onclick = onTrade;
  $("btn-star").onclick = toggleWatchCurrent;
  $("symbol").addEventListener("change", renderQuickSymbols);
  $("timeframe").addEventListener("change", renderQuickSymbols);

  state.engine = new Engine();
  $("btn-analyze").disabled = true;          // 引擎就绪前先禁用，避免点了没反应
  state.engine.worker.postMessage({ type: "boot" });   // 页面一打开就预热引擎
  state.engine.ready.catch(() => {});
  setInterval(() => { if (state.settings.watchAuto) watchTick(); }, 60_000);
  setInterval(() => { if (state.settings.auto.enabled) refreshAccount(); }, 300_000);

  // 调试钩子：浏览器控制台可用 paAgent.state / paAgent.maybeAutoTrade(...) 排查
  globalThis.paAgent = {
    state,
    maybeAutoTrade,
    switchToSymbol,
    renderAutoPanel,
    refreshAccount,
    pushAutoLog,
  };
}

main();
