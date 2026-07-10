"""Broker/clock factory selection + live double-lock enforcement."""

from __future__ import annotations

from trading_assistant.broker.factory import build_broker, build_clock
from trading_assistant.broker.mock import MockBroker
from trading_assistant.config import (
    LIVE_CONFIRM_STRING,
    Secrets,
    TradingMode,
    live_trading_enabled,
)
from trading_assistant.risk.clock import FakeClock, MarketClock


def test_mock_config_builds_mock_broker(app_config):
    assert isinstance(build_broker(app_config, Secrets()), MockBroker)


def test_mock_config_builds_fake_clock(app_config):
    clock = build_clock(app_config, Secrets())
    assert isinstance(clock, FakeClock)
    assert isinstance(clock, MarketClock)


def test_live_lock_requires_both_flags(app_config):
    live = app_config.model_copy(
        update={
            "trading": app_config.trading.model_copy(update={"mode": TradingMode.LIVE})
        }
    )
    # Config says live but no env confirmation -> not live (forces paper).
    assert live_trading_enabled(live, Secrets(live_trading_confirm="")) is False
    assert (
        live_trading_enabled(live, Secrets(live_trading_confirm=LIVE_CONFIRM_STRING))
        is True
    )
