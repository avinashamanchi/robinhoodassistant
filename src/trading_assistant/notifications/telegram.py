"""Telegram notifier — scaffolded now, fully implemented in Phase 4.

Disabled by default. ``send`` is a no-op unless ``features.telegram_notifications``
is true AND credentials are present, so wiring it into the daemon early is safe.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, enabled: bool, bot_token: str = "", chat_id: str = "") -> None:
        self.enabled = enabled and bool(bot_token) and bool(chat_id)
        self._bot_token = bot_token
        self._chat_id = chat_id

    def send(self, message: str) -> bool:
        """Return True if a message was dispatched. Phase 1: no-op when disabled."""
        if not self.enabled:
            log.debug("telegram disabled; dropping notification")
            return False
        # Phase 4: actual HTTP call to the Telegram Bot API.
        raise NotImplementedError("Telegram delivery is implemented in Phase 4")
