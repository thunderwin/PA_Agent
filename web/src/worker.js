/* Pyodide 工作线程：在浏览器里跑真正的 Python 分析引擎。 */
const PYODIDE_BASE = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/";
const SHIM_FILES = ["PyQt6/__init__.py", "PyQt6/QtCore.py"];

let readyPromise = null;

async function boot() {
  self.postMessage({ type: "boot-progress", text: "下载 Python 运行时（首次约 10MB，会缓存）…" });
  importScripts(PYODIDE_BASE + "pyodide.js");
  const py = await loadPyodide({ indexURL: PYODIDE_BASE });
  self.postMessage({ type: "boot-progress", text: "加载依赖（pydantic / numpy）…" });
  await py.loadPackage(["pydantic"]);        // Pyodide 内置包，直接可用

  // 1) 引擎源码 + 策略文本
  const manifest = await (await fetch("engine/repo/manifest.json")).json();
  self.postMessage({ type: "boot-progress", text: `下载分析引擎（${manifest.files.length} 个文件）…` });
  for (const rel of manifest.files) {
    const res = await fetch("engine/repo/" + rel);
    if (!res.ok) throw new Error("拉取引擎文件失败：" + rel);
    const text = await res.text();
    const full = "/repo/" + rel;
    const dir = full.slice(0, full.lastIndexOf("/"));
    py.FS.mkdirTree(dir);
    py.FS.writeFile(full, text);
  }

  // 2) PyQt6 空壳 + 桥接层
  for (const rel of SHIM_FILES) {
    const text = await (await fetch("shims/" + rel)).text();
    py.FS.mkdirTree("/shims/" + rel.slice(0, rel.lastIndexOf("/")));
    py.FS.writeFile("/shims/" + rel, text);
  }
  py.FS.writeFile("/engine_bridge.py", await (await fetch("engine_bridge.py")).text());
  py.runPython('import sys; sys.path.insert(0, "/engine"); sys.path.insert(0, "/")');

  self.postMessage({ type: "boot-progress", text: "引擎自检…" });
  const selftest = JSON.parse(py.runPython("import engine_bridge; engine_bridge.engine_selftest()"));
  self.postMessage({ type: "ready", info: selftest });
  return py;
}

function ensureBoot() {
  if (!readyPromise) {
    readyPromise = boot().catch((err) => {
      readyPromise = null;
      self.postMessage({ type: "boot-error", message: cleanError(err) });
      throw err;
    });
  }
  return readyPromise;
}

/* 把 Pyodide 抛出的整段 traceback 压成一行人话（取最后一行异常信息）。 */
function cleanError(err) {
  const raw = String((err && err.message) || err || "未知错误");
  const lines = raw.split("\n").map((s) => s.trim()).filter(Boolean);
  const last = lines[lines.length - 1] || raw;
  return last.length > 300 ? last.slice(0, 300) + "…" : last;
}

self.onmessage = async (ev) => {
  const msg = ev.data || {};
  try {
    const py = await ensureBoot();

    if (msg.type === "boot") {
      self.postMessage({ type: "boot-done" });
    } else if (msg.type === "analyze") {
      py.globals.set("_payload", JSON.stringify(msg.payload));
      py.globals.set("_progress", (stage, text) => {
        self.postMessage({ type: "progress", id: msg.id, stage: String(stage), text: String(text) });
      });
      const out = py.runPython("engine_bridge.run_analysis(_payload, _progress)");
      self.postMessage({ type: "result", id: msg.id, data: JSON.parse(out) });
    } else if (msg.type === "plan") {
      py.globals.set("_payload", JSON.stringify(msg.payload));
      const out = py.runPython("engine_bridge.plan_trade(_payload)");
      self.postMessage({ type: "plan", id: msg.id, data: JSON.parse(out) });
    } else {
      self.postMessage({ type: "error", id: msg.id, message: "未知消息类型：" + msg.type });
    }
  } catch (err) {
    self.postMessage({
      type: "error",
      id: msg.id,
      message: cleanError(err),
    });
  }
};
