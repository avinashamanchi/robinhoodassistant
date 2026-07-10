"""Notifier interface + a no-op default. Keeps the daemon testable without network."""

from __future__ import annotations

from typing import Protocol


class Notifier(Protocol):
    def send(self, message: str) -> bool: ...


class NullNotifier:
    """Default notifier — drops everything. Used when notifications are disabled."""

    def send(self, message: str) -> bool:
        return False


class RecordingNotifier:
    """Captures messages in-memory for tests."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, message: str) -> bool:
        self.sent.append(message)
        return True


def build_notifier(config, secrets) -> Notifier:
    """Build the configured notifier. Telegram only if the flag AND creds are set."""
    if not config.features.telegram_notifications:
        return NullNotifier()
    from .telegram import TelegramNotifier

    return TelegramNotifier(
        enabled=True,
        bot_token=secrets.telegram_bot_token,
        chat_id=secrets.telegram_chat_id,
    )
