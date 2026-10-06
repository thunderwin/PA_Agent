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
  watch: [],
  watchAuto: false,
  general: { decisionStance: "balanced", barCount: 100, nextBar: false },
};

const state = {
  settings: loadSettings(),
  engine: null,
  bars: [],
  decision: null,       // 内层 decision
  stage1: null,
  watch: new Map(),     // symbol -> {price, order, dir, conf, ts, status, closedTs}
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
      if (m.type === "ready") {
        $("engine-badge").textContent = m.info.ok
          ? `引擎就绪（策略文件 ${m.info.prompt_files} 个）`
          : `引擎异常：${m.info.error}`;
        $("engine-badge").classList.toggle("ok", !!m.info.ok);
        this._resolveReady(m.info);
        return;
      }
      if (m.type === "boot-error") {
        $("engine-badge").textContent = "引擎加载失败";
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
    if (stage === "event") { if (!silent) setStatus(`分析中…（${text}）`); return; }
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
}

async function onAnalyze() {
  if (state.busy) return;
  const symbol = currentSymbol();
  const timeframe = currentTimeframe();
  if (!symbol) return;
  state.busy = true;
  $("btn-analyze").disabled = true;
  setStatus("正在取 K 线…");
  try {
    const need = state.settings.general.barCount + WARMUP + 5;
    const bars = await fetchCandles(symbol, timeframe, need);
    state.bars = bars;
    renderChart(bars);
    setStatus("分析中（引擎已在浏览器内运行）…");
    const out = await runAnalysis(symbol, timeframe, bars);
    if (out.exception) throw new Error(`${out.exception.type}: ${out.exception.message || ""}`);
    renderDecision(out);
    setStatus(`分析完成：${out.stage2?.decision?.order_type || "—"}`);
  } catch (err) {
    setStatus(`分析失败：${err.message}`, true);
  } finally {
    state.busy = false;
    $("btn-analyze").disabled = false;
  }
}

// ── 下单（浏览器内签名 + 以损定量） ───────────────────────────────────────────
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
    const [instrument, equity] = await Promise.all([fetchInstrument(symbol), okxEquityUsd()]);
    const planOut = await state.engine.plan({
      decision: d, instrument, equityUsd: equity,
      maxLossUsd: Number(s.risk.maxLossUsd), leverage: Number(s.risk.leverage),
      price: state.bars[0]?.close,
    });
    if (!planOut.ok) { setStatus(`无法下单：${planOut.error}`, true); alert(`无法下单：\n${planOut.error}`); return; }
    const p = planOut.plan;
    $("d-plan").textContent = `${p.size} 张 · 止损亏 ${p.riskUsd.toFixed(2)} USDT`;
    const mode = s.okx.simulated ? "模拟盘" : "实盘";
    const msg = [
      `【OKX ${mode} 下单确认】`,
      `${symbol} ${currentTimeframe()}`,
      `方向/类型：${p.side === "buy" ? "做多" : "做空"} · ${p.ordType === "limit" ? "限价单" : p.ordType === "trigger" ? "突破单" : "市价单"}`,
      p.price ? `价格：${fmtPrice(p.price)}` : "",
      `止损：${fmtPrice(p.stopPx)}（距离 ${fmtPrice(p.stopDistance)}）`,
      p.takeProfitPx ? `止盈：${fmtPrice(p.takeProfitPx)}` : "",
      `下单量：${p.size}（${p.baseQty} 基础币）`,
      `名义价值：${Math.round(p.notionalUsd).toLocaleString()} USDT`,
      `预计保证金：${Math.round(p.notionalUsd / Math.max(p.leverage, 1)).toLocaleString()} USDT（${p.leverage}x）`,
      `账户权益：${Math.round(equity).toLocaleString()} USDT`,
      "── 以损定量 ──",
      `止损被打到：亏 ${p.riskUsd.toFixed(2)} USDT`,
    ].filter(Boolean).join("\n");
    if (!confirm(msg + "\n\n确认下单吗？")) { setStatus("已取消下单"); return; }
    if (!s.okx.simulated && !confirm("⚠️ 这是实盘：真实资金，确认后不可撤销。继续吗？")) { setStatus("已取消下单"); return; }

    const body = {
      instId: symbol, tdMode: symbol.split("-").length >= 3 ? "cross" : "cash",
      side: p.side, ordType: p.ordType, sz: String(p.size),
    };
    if (body.tdMode === "cross") body.posSide = "net";
    else body.tgtCcy = "base_ccy";
    if (p.ordType === "limit") body.px = String(p.price);
    if (p.ordType === "trigger") { body.triggerPx = String(p.price); body.orderPx = "-1"; }
    const algo = {};
    if (p.stopPx) { algo.slTriggerPx = String(p.stopPx); algo.slOrdPx = "-1"; }
    if (p.takeProfitPx) { algo.tpTriggerPx = String(p.takeProfitPx); algo.tpOrdPx = "-1"; }
    if (Object.keys(algo).length) body.attachAlgoOrds = [algo];

    if (body.tdMode === "cross") {
      await okxPrivate("POST", "/api/v5/account/set-leverage", {
        body: { instId: symbol, lever: String(p.leverage), mgnMode: "cross" },
      }).catch(() => {});
    }
    const rows = await okxPrivate("POST", "/api/v5/trade/order", { body });
    const row = rows[0] || {};
    if (String(row.sCode ?? "0") !== "0") throw new Error(row.sMsg || JSON.stringify(row));
    setStatus(`下单成功，订单号 ${row.ordId}`);
    alert(`下单成功（${mode}）\n订单号：${row.ordId}`);
  } catch (err) {
    setStatus(`下单失败：${err.message}`, true);
    alert(`下单失败：\n${err.message}`);
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
      $("symbol").value = symbol;
      onAnalyze();
    };
    tbody.append(tr);
  }
}

async function watchTick() {
  for (const symbol of state.settings.watch) {
    if (state.busy) return;
    try {
      const timeframe = currentTimeframe();
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
      const closedNow = bars.find((b) => b.closed)?.ts ?? bars[0]?.ts;
      state.watch.set(symbol, {
        price: bars[0]?.close, order: d.order_type, dir: d.order_direction,
        conf: d.trade_confidence, ts: Date.now(), closedTs: closedNow,
      });
      renderWatch();
      if (ORDER_TYPES.includes(String(d.order_type || "").trim())) {
        setStatus(`📣 ${symbol} ${d.order_direction} ${d.order_type}（置信度 ${Math.round(d.trade_confidence ?? 0)}）`);
      }
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
    saveSettings();
    setStatus("设置已保存到本机浏览器");
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
  loadSettingsIntoForm();
  loadSymbolPresets();
  renderWatch();
  for (const s of state.settings.watch) state.watch.set(s, state.watch.get(s) || { status: "等待" });
  renderWatch();

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

  state.engine = new Engine();
  state.engine.ready.catch(() => {});
  setInterval(() => { if (state.settings.watchAuto) watchTick(); }, 60_000);
}

main();
