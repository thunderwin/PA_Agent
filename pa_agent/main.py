"""Application entry point for PA Agent."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication

logger = logging.getLogger(__name__)


def _lock_path() -> Path:
    from pa_agent.config.paths import CONFIG_DIR

    return CONFIG_DIR / ".pa_agent.lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_single_instance_lock() -> tuple[bool, str]:
    """避免同时跑多个实例互相覆盖 config/settings.json。

    Returns
    -------
    (acquired, message)
        拿到锁时 ``acquired=True``；已有实例在跑时 ``acquired=False`` 并给出占用者 PID。
    """
    path = _lock_path()
    try:
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip().splitlines()
            pid = int(existing[0]) if existing and existing[0].isdigit() else 0
            if pid and pid != os.getpid() and _pid_alive(pid):
                return False, f"已有 PA Agent 实例在运行（pid={pid}）"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except OSError as exc:  # 拿不到锁不是致命问题，继续启动
        logger.warning("单实例锁不可用: %s", exc)
        return True, ""
    return True, ""


def release_single_instance_lock() -> None:
    path = _lock_path()
    try:
        if path.exists():
            owner = path.read_text(encoding="utf-8").strip().splitlines()
            if owner and owner[0].isdigit() and int(owner[0]) == os.getpid():
                path.unlink()
    except OSError as exc:
        logger.debug("释放单实例锁失败: %s", exc)


def main(argv: list[str] | None = None) -> int:
    # Early diagnostics before Qt / heavy imports: crash dumps + file logging.
    from pa_agent.util.crash_diagnostics import enable_crash_diagnostics, log_startup_diagnostics
    from pa_agent.util.logging import configure_logging

    enable_crash_diagnostics()
    configure_logging()
    log_startup_diagnostics()

    argv = list(sys.argv if argv is None else argv)
    app = QApplication(argv)
    app.setApplicationName("PA Agent")

    acquired, message = acquire_single_instance_lock()
    if not acquired:
        logger.warning("拒绝重复启动：%s", message)
        from PyQt6.QtWidgets import QMessageBox

        QMessageBox.warning(
            None,
            "PA Agent 已在运行",
            f"{message}。\n\n请先关闭已打开的窗口再启动新实例，"
            "否则两个实例会互相覆盖 config/settings.json 里的设置。",
        )
        return 2

    from pa_agent.gui.theme import apply_theme
    apply_theme(app)

    logger.info("PA Agent starting up")

    # Bootstrap all components (settings, data source, AI client, etc.)
    from pa_agent.app_context import AppContext
    ctx = AppContext.bootstrap()

    # Update logging with the real API key now that settings are loaded
    if ctx.settings is not None:
        from pa_agent.util.logging import configure_logging, update_api_key
        configure_logging(api_key=ctx.settings.provider.api_key)
        from pa_agent.util.crash_diagnostics import log_startup_diagnostics
        log_startup_diagnostics()

    # Build and show the main window
    from pa_agent.gui.main_window import MainWindow
    window = MainWindow(ctx)
    window.show()

    logger.info("Main window shown")
    try:
        return app.exec()
    finally:
        release_single_instance_lock()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
