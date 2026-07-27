"""Select broker + clock implementations for this paper-only release."""

from __future__ import annotations

from ..config import AppConfig, BrokerKind, Secrets
from .base import BrokerClient
from .mock import MockBroker


def build_broker(config: AppConfig, secrets: Secrets) -> BrokerClient:
    if config.trading.broker is BrokerKind.MOCK:
        return MockBroker()

    from .alpaca import AlpacaBroker  # lazy: keeps mock-only installs SDK-free

    return AlpacaBroker.from_credentials(
        secrets.alpaca_api_key,
        secrets.alpaca_secret_key,
        paper=True,
        timeout_seconds=config.trading.request_timeout_seconds,
    )


def build_clock(config: AppConfig, secrets: Secrets):
    """Return a MarketClock. Mock broker pairs with an always-open FakeClock."""
    if config.trading.broker is BrokerKind.MOCK:
        from ..risk.clock import FakeClock

        return FakeClock(is_open=True)

    from .alpaca import AlpacaClock

    return AlpacaClock.from_credentials(
        secrets.alpaca_api_key,
        secrets.alpaca_secret_key,
        paper=True,
        timeout_seconds=config.trading.request_timeout_seconds,
    )
