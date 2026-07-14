"""
Gyna — telegram_notifier.py
Fire-and-forget Telegram notifications. Silently disabled when no token is
configured; every send runs on a daemon thread so the 50ms loop never blocks.
Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.parse
import urllib.request

from config import TELEGRAM_TOKEN, TELEGRAM_CHAT

log = logging.getLogger("Gyna.Telegram")


class TelegramNotifier:
    def __init__(self, token: str = TELEGRAM_TOKEN, chat_id: str = TELEGRAM_CHAT):
        self.token   = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)
        if not self.enabled:
            log.info("Telegram disabled (no TELEGRAM_TOKEN/TELEGRAM_CHAT_ID)")

    def send(self, text: str) -> None:
        """Queue a message. Never raises, never blocks the caller."""
        if not self.enabled:
            return
        threading.Thread(target=self._post, args=(text,), daemon=True).start()

    def _post(self, text: str) -> None:
        try:
            body = urllib.parse.urlencode({
                "chat_id": self.chat_id,
                "text":    text[:4000],
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data=body, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                json.loads(resp.read())
        except Exception as e:
            log.warning(f"Telegram send failed: {e}")
