#!/usr/bin/env python3
"""把静态站点 + Python 分析引擎打包到 web/dist/（可直接部署到 Cloudflare Pages）。

产物结构：
    web/dist/index.html / app.js / worker.js / styles.css / vendor/
    web/dist/engine/repo/pa_agent/**.py            <- 原样的分析引擎
    web/dist/engine/repo/prompt_engineering/*.txt  <- 策略文本
    web/dist/engine/repo/manifest.json             <- 供 worker 批量 fetch
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent
REPO = WEB_DIR.parent
SRC = WEB_DIR / "src"
DIST = WEB_DIR / "dist"

ENGINE_PY_DIRS = ("pa_agent",)
ENGINE_EXTRA_TXT = ("prompt_engineering",)

# 桌面专用、浏览器里用不到的模块（避免把 PyQt / requests 之类拖进包里）
_SKIP_PY = (
    "pa_agent/gui/",
    "pa_agent/data/mt5.py",          # 需要本机 MT5 终端
    "pa_agent/data/eastmoney_extended.py",
)
_SKIP_PARTS = ("/__pycache__/",)


def _keep(rel: str) -> bool:
    if any(rel.startswith(prefix) for prefix in _SKIP_PY):
        return False
    if any(part in rel for part in _SKIP_PARTS):
        return False
    return not rel.endswith(".pyc")


def build() -> None:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    # 1) 静态资源
    shutil.copytree(SRC, DIST, dirs_exist_ok=True)

    # 2) 引擎源码
    repo_out = DIST / "engine" / "repo"
    manifest: list[str] = []
    for pkg in ENGINE_PY_DIRS:
        for path in sorted((REPO / pkg).rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            if not _keep(rel):
                continue
            target = repo_out / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            manifest.append(rel)

    # 3) 策略文本
    for folder in ENGINE_EXTRA_TXT:
        for path in sorted((REPO / folder).rglob("*.txt")):
            rel = path.relative_to(REPO).as_posix()
            target = repo_out / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            manifest.append(rel)

    (repo_out / "manifest.json").write_text(
        json.dumps({"files": manifest}, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    size_mb = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file()) / 1e6
    print(f"打包完成：{DIST}（{len(manifest)} 个引擎文件，共 {size_mb:.1f} MB）")


if __name__ == "__main__":
    build()
