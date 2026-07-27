"""Broker/clock factory selection + live double-lock enforcement."""

from __future__ import annotations

import pytest

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


def _provider_budget(app_config, session_factory):
    from trading_assistant.llm.budget import (
        BudgetLimits,
        ProviderBudgetService,
    )

    configured = app_config.security.provider_budget
    return ProviderBudgetService(
        session_factory,
        BudgetLimits(
            calls=configured.daily_calls,
            input_tokens=configured.daily_input_tokens,
            output_tokens=configured.daily_output_tokens,
            reservation_ttl_seconds=configured.reservation_ttl_seconds,
        ),
        prices=configured.prices,
    )


@pytest.mark.parametrize(
    "category",
    ["chat", "analysis", "untrusted", "backtest"],
)
def test_llm_factory_wraps_selected_provider_exactly_once(
    app_config,
    session_factory,
    monkeypatch,
    category,
):
    from trading_assistant.llm import factory
    from trading_assistant.llm.base import BudgetedLLMBackend

    if category == "backtest":
        budget_config = app_config.security.provider_budget.model_copy(
            update={"backtest_llm_enabled": True}
        )
        app_config = app_config.model_copy(
            update={
                "security": app_config.security.model_copy(
                    update={"provider_budget": budget_config}
                )
            }
        )
    raw_backend = object()
    constructed: list[str] = []
    monkeypatch.setattr(
        factory,
        "_make_backend",
        lambda provider, *_args: (
            constructed.append(provider) or raw_backend
        ),
    )
    provider_budget = _provider_budget(app_config, session_factory)

    backend = factory.build_llm_backend(
        app_config,
        Secrets(),
        provider_budget=provider_budget,
        category=category,
    )

    assert isinstance(backend, BudgetedLLMBackend)
    assert backend.delegate is raw_backend
    assert not isinstance(backend.delegate, BudgetedLLMBackend)
    assert backend.budgets is provider_budget
    assert backend.category == category
    assert constructed == [app_config.llm.provider]


@pytest.mark.parametrize("category", ["", " ", "unknown", None, []])
def test_llm_factory_rejects_non_allowlisted_category_before_construction(
    app_config,
    session_factory,
    monkeypatch,
    category,
):
    from trading_assistant.llm import factory

    estimator_calls: list[str] = []
    raw_calls: list[str] = []
    monkeypatch.setattr(
        factory,
        "resolve_input_estimator",
        lambda provider: estimator_calls.append(provider) or object(),
    )
    monkeypatch.setattr(
        factory,
        "_make_backend",
        lambda provider, *_args: raw_calls.append(provider) or object(),
    )

    with pytest.raises(ValueError, match="category"):
        factory.build_llm_backend(
            app_config,
            Secrets(),
            provider_budget=_provider_budget(
                app_config,
                session_factory,
            ),
            category=category,
        )

    assert estimator_calls == []
    assert raw_calls == []


def test_disabled_backtest_backend_denies_without_delegate_or_estimator(
    app_config,
    session_factory,
    monkeypatch,
):
    from trading_assistant.llm import factory
    from trading_assistant.llm.budget import ProviderBudgetExceeded

    estimator_calls: list[str] = []
    raw_calls: list[str] = []
    monkeypatch.setattr(
        factory,
        "resolve_input_estimator",
        lambda provider: estimator_calls.append(provider) or object(),
    )
    monkeypatch.setattr(
        factory,
        "_make_backend",
        lambda provider, *_args: raw_calls.append(provider) or object(),
    )

    backend = factory.build_llm_backend(
        app_config,
        Secrets(),
        provider_budget=_provider_budget(app_config, session_factory),
        category="backtest",
    )

    assert not hasattr(backend, "delegate")
    with pytest.raises(ProviderBudgetExceeded, match="disabled"):
        backend.create(
            system="system",
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            request_id="disabled-backtest",
        )
    assert estimator_calls == []
    assert raw_calls == []
