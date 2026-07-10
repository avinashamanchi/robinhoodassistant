"""Telegram notifier (Phase 4).

Disabled by default. ``send`` is a no-op unless ``features.telegram_notifications``
is true AND credentials are present. The token is never logged (redaction filter
also covers it). Network failures are swallowed — a dropped notification must
never break the trading path.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    def __init__(self, enabled: bool, bot_token: str = "", chat_id: str = "") -> None:
        self.enabled = enabled and bool(bot_token) and bool(chat_id)
        self._bot_token = bot_token
        self._chat_id = chat_id

    def send(self, message: str) -> bool:
        """Return True if a message was dispatched, False if disabled or failed."""
        if not self.enabled:
            log.debug("telegram disabled; dropping notification")
            return False
        try:
            import httpx

            resp = httpx.post(
                _API.format(token=self._bot_token),
                json={"chat_id": self._chat_id, "text": message},
                timeout=10.0,
            )
            return resp.status_code == 200
        except Exception as exc:  # never let a notification failure break trading
            log.warning("telegram send failed: %s", type(exc).__name__)
            return False
