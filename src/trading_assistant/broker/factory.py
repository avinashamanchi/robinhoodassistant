"""Selects the broker + clock implementation from config, enforcing guardrail #1.

An Alpaca broker is only ever built in LIVE mode when the double-lock is
satisfied; otherwise it is forced to paper. The mock broker needs no creds.
"""

from __future__ import annotations

from ..config import AppConfig, BrokerKind, Secrets, live_trading_enabled
from .base import BrokerClient
from .mock import MockBroker


def build_broker(config: AppConfig, secrets: Secrets) -> BrokerClient:
    if config.trading.broker is BrokerKind.MOCK:
        return MockBroker()

    # Alpaca: paper unless the full double-lock is satisfied.
    from .alpaca import AlpacaBroker  # lazy: keeps mock-only installs SDK-free

    paper = not live_trading_enabled(config, secrets)
    return AlpacaBroker.from_credentials(
        secrets.alpaca_api_key, secrets.alpaca_secret_key, paper=paper
    )


def build_clock(config: AppConfig, secrets: Secrets):
    """Return a MarketClock. Mock broker pairs with an always-open FakeClock."""
    if config.trading.broker is BrokerKind.MOCK:
        from ..risk.clock import FakeClock

        return FakeClock(is_open=True)

    from .alpaca import AlpacaClock

    paper = not live_trading_enabled(config, secrets)
    return AlpacaClock.from_credentials(
        secrets.alpaca_api_key, secrets.alpaca_secret_key, paper=paper
    )
