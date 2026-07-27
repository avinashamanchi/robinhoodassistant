"""Broker/clock factory selection + live double-lock enforcement."""

from __future__ import annotations

from trading_assistant.broker.factory import build_broker, build_clock
from trading_assistant.broker.mock import MockBroker
from trading_assistant.config import (
    BrokerKind,
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


def test_factory_itself_remains_paper_only_even_with_legacy_live_double_lock(
    app_config,
    monkeypatch,
):
    from trading_assistant.broker.alpaca import AlpacaBroker, AlpacaClock

    observed: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        AlpacaBroker,
        "from_credentials",
        staticmethod(
            lambda *_args, paper, **_kwargs: observed.append(
                ("broker", paper)
            )
            or object()
        ),
    )
    monkeypatch.setattr(
        AlpacaClock,
        "from_credentials",
        staticmethod(
            lambda *_args, paper, **_kwargs: observed.append(
                ("clock", paper)
            )
            or object()
        ),
    )
    legacy_live = app_config.model_copy(
        update={
            "trading": app_config.trading.model_copy(
                update={
                    "mode": TradingMode.LIVE,
                    "broker": BrokerKind.ALPACA,
                }
            )
        }
    )
    secrets = Secrets(live_trading_confirm=LIVE_CONFIRM_STRING)

    build_broker(legacy_live, secrets)
    build_clock(legacy_live, secrets)

    assert observed == [("broker", True), ("clock", True)]
