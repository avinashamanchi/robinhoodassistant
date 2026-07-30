"""Telegram notifier (Phase 4).

Disabled by default. ``send`` is a no-op unless ``features.telegram_notifications``
is true AND credentials are present. The token is never logged (redaction filter
also covers it). Network failures are swallowed — a dropped notification must
never break the trading path.
"""

from __future__ import annotations

import logging
from typing import Any

from ..security.outbound import (
    OutboundPolicy,
    new_httpx_client,
    require_origin,
)

log = logging.getLogger(__name__)

_API_ORIGIN = "https://api.telegram.org"
_API_POLICY = OutboundPolicy(_API_ORIGIN)


class TelegramNotifier:
    def __init__(
        self,
        enabled: bool,
        bot_token: str = "",
        chat_id: str = "",
        http: Any = None,
        runtime_role: str = "app",
    ) -> None:
        self.enabled = enabled and bool(bot_token) and bool(chat_id)
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._http = http
        self._runtime_role = runtime_role

    def _client(self):
        if self._http is None:
            require_origin(
                self._runtime_role,
                "notifier.telegram",
                _API_ORIGIN,
            )
            self._http = new_httpx_client(_API_POLICY, read_timeout=10.0)
        return self._http

    def send(self, message: str) -> bool:
        """Return True if a message was dispatched, False if disabled or failed."""
        if not self.enabled:
            log.debug("telegram disabled; dropping notification")
            return False
        try:
            url = f"{_API_ORIGIN}/bot{self._bot_token}/sendMessage"
            _API_POLICY.assert_url(url)
            resp = self._client().post(
                url,
                json={"chat_id": self._chat_id, "text": message},
            )
            _API_POLICY.assert_response(resp)
            return resp.status_code == 200
        except Exception:  # never let a notification failure break trading
            log.warning("telegram send failed code=notification_failed")
            return False
