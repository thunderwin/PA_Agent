"""Telegram 交易提醒 + 人工确认（2026-10-19）。

``trigger_mode=notify`` 时：出现可执行方案 → 发一条带「✅ 执行 / ❌ 跳过」
按钮的消息给用户；用户点按钮后，主程序才真正下单。全程只接受配置里的
``chat_id`` 发来的回调，其他人点按钮一律忽略。

只用 Telegram Bot API 的 ``sendMessage`` / ``getUpdates`` / ``answerCallbackQuery``，
不需要公网 webhook —— 程序主动长轮询，适合本机后台运行。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"


def skip_cooldown_active(
    prev: dict[str, Any] | None,
    direction: str,
    *,
    now: float,
    cooldown_min: int,
) -> bool:
    """用户点过「跳过」后，同品种同方向是否仍在冷却期（2026-10-20 加）。"""
    if not prev or cooldown_min <= 0:
        return False
    if str(prev.get("direction") or "") != str(direction or ""):
        return False
    return now - float(prev.get("ts", 0) or 0) < cooldown_min * 60


class TelegramApprovalService:
    """发送带按钮的交易提醒，并轮询用户的选择。"""

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        proxy: str | None = None,
        on_action: Callable[[str, str], None],
        on_command: Callable[[str], None] | None = None,
        on_callback: Callable[[str], None] | None = None,
    ) -> None:
        self._token = str(token or "").strip()
        self._chat_id = str(chat_id or "").strip()
        self._proxy = proxy or None
        self._on_action = on_action
        self._on_command = on_command
        self._on_callback = on_callback
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset = 0

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self.configured or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, name="telegram-approval", daemon=True
        )
        self._thread.start()
        logger.warning("Telegram 提醒已启动（chat_id=%s）", self._mask_chat())

    def stop(self) -> None:
        self._stop.set()

    # ── 发送 ──────────────────────────────────────────────────────────────────

    def send_plan(self, plan_id: str, text: str) -> bool:
        """发一条带「执行 / 跳过」按钮的消息。"""
        markup = {
            "inline_keyboard": [[
                {"text": "✅ 执行", "callback_data": f"exec:{plan_id}"},
                {"text": "❌ 跳过", "callback_data": f"skip:{plan_id}"},
            ]]
        }
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
            "reply_markup": json.dumps(markup, ensure_ascii=False),
        }
        return self._call("sendMessage", payload) is not None

    def send_text(self, text: str) -> bool:
        return self._call(
            "sendMessage",
            {"chat_id": self._chat_id, "text": text, "disable_web_page_preview": "true"},
        ) is not None

    def send_html(self, text: str) -> bool:
        """发纯文本消息，但按 HTML 解析（<b>/<code> 等）。"""
        return self._call(
            "sendMessage",
            {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        ) is not None

    def send_keyboard(self, text: str, rows: list[list[tuple[str, str]]]) -> bool:
        """发一条带自定义按钮的消息：rows = [[(label, callback_data), ...], ...]."""
        markup = {
            "inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in row]
                for row in rows
            ]
        }
        return self._call(
            "sendMessage",
            {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
                "reply_markup": json.dumps(markup, ensure_ascii=False),
            },
        ) is not None

    def send_photo(
        self, caption: str, image_path: str, rows: list[list[tuple[str, str]]]
    ) -> bool:
        """把 K 线图 + 说明 + 按钮作为一条图片消息发出去。"""
        try:
            with open(image_path, "rb") as fh:
                image = fh.read()
        except OSError as exc:
            logger.warning("读取图表失败，退回纯文本：%s", exc)
            return self.send_keyboard(caption, rows)
        boundary = "----paagent" + uuid.uuid4().hex
        chunks: list[bytes] = []

        def _field(name: str, value: str) -> None:
            chunks.append(
                (
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                    f"{value}\r\n"
                ).encode()
            )

        _field("chat_id", self._chat_id)
        _field("caption", caption)
        _field("parse_mode", "HTML")
        markup = {
            "inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in row]
                for row in rows
            ]
        }
        _field("reply_markup", json.dumps(markup, ensure_ascii=False))
        chunks.append(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
                f"filename=\"chart.png\"\r\nContent-Type: image/png\r\n\r\n"
            ).encode()
        )
        chunks.append(image)
        chunks.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(chunks)
        url = f"{API_BASE}/bot{self._token}/sendPhoto"
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            if self._proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": self._proxy, "https": self._proxy})
                )
            else:
                opener = urllib.request.build_opener()
            with opener.open(req, timeout=40) as resp:
                parsed = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            if not parsed.get("ok"):
                logger.warning("Telegram 发图失败：%s", parsed.get("description"))
                return self.send_keyboard(caption, rows)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram 发图异常，退回纯文本：%s", exc)
            return self.send_keyboard(caption, rows)

    def _answer_callback(self, callback_id: str, text: str) -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    # ── 轮询 ──────────────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._call(
                    "getUpdates",
                    {"offset": self._offset, "timeout": 25,
                     "allowed_updates": json.dumps(["callback_query", "message"])},
                    timeout=35,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Telegram 轮询失败（稍后重试）：%s", exc)
                time.sleep(5)
                continue
            for update in (data or {}).get("result", []) if isinstance(data, dict) else []:
                self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                cq = update.get("callback_query")
                msg = update.get("message")
                if isinstance(msg, dict):
                    chat_id = str(((msg.get("chat") or {}).get("id", "")))
                    text = str(msg.get("text") or "").strip()
                    if chat_id == self._chat_id and text and self._on_command:
                        try:
                            self._on_command(text)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("处理 Telegram 命令失败：%s", exc)
                    continue
                if not isinstance(cq, dict):
                    continue
                chat_id = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
                if chat_id != self._chat_id:
                    logger.warning("忽略非本人 Telegram 回调（chat_id=%s）", chat_id)
                    continue
                data_str = str(cq.get("data") or "")
                action, _, plan_id = data_str.partition(":")
                if action in ("close", "closeall", "noop"):
                    self._answer_callback(str(cq.get("id") or ""), "处理中…")
                    if self._on_callback:
                        try:
                            self._on_callback(data_str)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("处理 Telegram 按钮失败：%s", exc)
                    continue
                if action not in ("exec", "skip", "market") or not plan_id:
                    continue
                self._answer_callback(
                    str(cq.get("id") or ""),
                    {
                        "exec": "已收到，正在挂单…",
                        "market": "已收到，按市价入场…",
                        "skip": "已跳过",
                    }.get(action, "已收到"),
                )
                try:
                    self._on_action(plan_id, action)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("处理 Telegram 选择失败：%s", exc)

    # ── HTTP ─────────────────────────────────────────────────────────────────

    def _call(
        self, method: str, payload: dict[str, Any] | None = None, *, timeout: float = 20
    ) -> Any:
        if not self._token:
            return None
        url = f"{API_BASE}/bot{self._token}/{method}"
        data = None
        if payload is not None:
            data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        if self._proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": self._proxy, "https": self._proxy})
            )
        else:
            opener = urllib.request.build_opener()
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except ValueError:
            raise RuntimeError(f"Telegram 返回非 JSON：{body[:120]}") from None
        if not parsed.get("ok"):
            raise RuntimeError(
                f"Telegram {method} 失败：{parsed.get('description') or body[:120]}"
            )
        return parsed

    def _mask_chat(self) -> str:
        cid = self._chat_id
        return cid[:3] + "***" + cid[-2:] if len(cid) > 5 else "***"
