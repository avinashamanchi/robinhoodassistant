"""Select broker + clock implementations for this paper-only release."""

from __future__ import annotations

from ..config import AppConfig, BrokerKind, Secrets
from ..security.secrets import secret_value
from .base import BrokerClient
from .mock import MockBroker


def build_broker(
    config: AppConfig,
    secrets: Secrets,
    *,
    runtime_role: str = "app",
) -> BrokerClient:
    if config.trading.broker is BrokerKind.MOCK:
        return MockBroker()

    from .alpaca import AlpacaBroker  # lazy: keeps mock-only installs SDK-free

    return AlpacaBroker.from_credentials(
        secret_value(secrets.alpaca_api_key),
        secret_value(secrets.alpaca_secret_key),
        paper=True,
        timeout_seconds=config.trading.request_timeout_seconds,
        runtime_role=runtime_role,
    )


def build_clock(
    config: AppConfig,
    secrets: Secrets,
    *,
    runtime_role: str = "app",
):
    """Return a MarketClock. Mock broker pairs with an always-open FakeClock."""
    if config.trading.broker is BrokerKind.MOCK:
        from ..risk.clock import FakeClock

        return FakeClock(is_open=True)

    from .alpaca import AlpacaClock

    return AlpacaClock.from_credentials(
        secret_value(secrets.alpaca_api_key),
        secret_value(secrets.alpaca_secret_key),
        paper=True,
        timeout_seconds=config.trading.request_timeout_seconds,
        runtime_role=runtime_role,
    )
