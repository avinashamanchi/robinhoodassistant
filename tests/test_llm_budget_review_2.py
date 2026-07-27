from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from trading_assistant.db.models import (
    ProviderBudgetDay,
    ProviderReservation,
)
from trading_assistant.llm.base import BudgetedLLMBackend
from trading_assistant.llm.budget import (
    BudgetLimits,
    ProviderBudgetService,
    ProviderBudgetUnavailable,
)
from trading_assistant.llm.gemini_backend import GeminiBackend
from trading_assistant.llm.payloads import (
    build_anthropic_payload,
    build_gemini_payload,
    build_groq_payload,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 27, 12, tzinfo=UTC)
TOOLS = [
    {
        "name": "quote",
        "description": "Get quote",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
        },
    }
]
BUILDERS = {
    "anthropic": build_anthropic_payload,
    "gemini": build_gemini_payload,
    "groq": build_groq_payload,
}


class CountingSessionFactory:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.delegate()


class RecordingEstimator:
    def __init__(self) -> None:
        self.calls = 0

    def estimate_upper_bound(self, **_kwargs) -> int:
        self.calls += 1
        return 1


class RecordingDelegate:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError("invalid payload reached delegate")


def _service(
    session_factory,
    *,
    ttl_seconds: int = 300,
) -> ProviderBudgetService:
    return ProviderBudgetService(
        session_factory,
        BudgetLimits(
            calls=20,
            input_tokens=1_000,
            output_tokens=1_000,
            reservation_ttl_seconds=ttl_seconds,
        ),
        clock=lambda: NOW,
    )


def _corrupt(engine, statement: str, parameters=()) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        try:
            connection.exec_driver_sql(statement, parameters)
        finally:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")


def _reservation(
    session_factory,
    reservation_id: str,
) -> ProviderReservation:
    with session_factory() as session:
        return session.get(ProviderReservation, reservation_id)


def _day(session_factory) -> ProviderBudgetDay:
    with session_factory() as session:
        return session.scalar(select(ProviderBudgetDay))


@pytest.mark.parametrize(
    "tool_choice",
    ["required", "ANY", "none", " auto "],
)
def test_unknown_tool_choice_stops_before_estimation_store_and_delegate(
    tool_choice,
    session_factory,
):
    counting_factory = CountingSessionFactory(session_factory)
    estimator = RecordingEstimator()
    delegate = RecordingDelegate()
    backend = BudgetedLLMBackend(
        delegate,
        _service(counting_factory),
        provider="anthropic",
        category="chat",
        max_output_tokens=10,
        estimator=estimator,
    )

    with pytest.raises(ValueError, match="tool_choice"):
        backend.create(
            system="system",
            messages=[],
            tools=TOOLS,
            tool_choice=tool_choice,
            request_id=f"unknown-tool-choice-{tool_choice}",
        )

    assert estimator.calls == 0
    assert counting_factory.calls == 0
    assert delegate.calls == []
    with session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(ProviderBudgetDay)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(ProviderReservation)
        ) == 0


@pytest.mark.parametrize("tool_choice", ["auto", "any"])
def test_tool_choice_without_tools_stops_before_estimation_store_and_delegate(
    tool_choice,
    session_factory,
):
    counting_factory = CountingSessionFactory(session_factory)
    estimator = RecordingEstimator()
    delegate = RecordingDelegate()
    backend = BudgetedLLMBackend(
        delegate,
        _service(counting_factory),
        provider="anthropic",
        category="chat",
        max_output_tokens=10,
        estimator=estimator,
    )

    with pytest.raises(ValueError, match="tools"):
        backend.create(
            system="system",
            messages=[],
            tools=[],
            tool_choice=tool_choice,
            request_id=f"tool-choice-without-tools-{tool_choice}",
        )

    assert estimator.calls == 0
    assert counting_factory.calls == 0
    assert delegate.calls == []


@pytest.mark.parametrize(
    ("provider", "tool_choice", "expected"),
    [
        ("anthropic", "auto", {"type": "auto"}),
        ("anthropic", "any", {"type": "any"}),
        ("gemini", "auto", "AUTO"),
        ("gemini", "any", "ANY"),
        ("groq", "auto", "auto"),
        ("groq", "any", "required"),
    ],
)
def test_valid_tool_choice_has_explicit_provider_translation(
    provider,
    tool_choice,
    expected,
):
    payload = BUILDERS[provider](
        system="system",
        messages=[],
        tools=TOOLS,
        tool_choice=tool_choice,
    )

    if provider == "anthropic":
        translated = payload["tool_choice"]
    elif provider == "gemini":
        translated = payload["tool_config"]["function_calling_config"][
            "mode"
        ]
    else:
        translated = payload["tool_choice"]
    assert translated == expected


@pytest.mark.parametrize(
    ("tool_choice", "expected_mode"),
    [("auto", "AUTO"), ("any", "ANY")],
)
def test_gemini_adapter_preserves_translated_tool_choice(
    tool_choice,
    expected_mode,
):
    class Models:
        def __init__(self) -> None:
            self.last = None

        def generate_content(self, **kwargs):
            self.last = kwargs
            return type(
                "Response",
                (),
                {
                    "candidates": [],
                    "usage_metadata": None,
                    "model_version": "gemini",
                },
            )()

    client = type("Client", (), {"models": Models()})()
    backend = GeminiBackend("key", "model", client=client)

    backend.create(
        system="system",
        messages=[],
        tools=TOOLS,
        tool_choice=tool_choice,
        request_id=f"gemini-{tool_choice}",
    )

    mode = (
        client.models.last["config"]
        .tool_config.function_calling_config.mode
    )
    assert mode.value == expected_mode


@pytest.mark.parametrize("provider", ["anthropic", "gemini", "groq"])
@pytest.mark.parametrize("tool_choice", ["auto", "any"])
def test_each_provider_builder_rejects_tool_choice_without_tools(
    provider,
    tool_choice,
):
    with pytest.raises(ValueError, match="tools"):
        BUILDERS[provider](
            system="system",
            messages=[],
            tools=[],
            tool_choice=tool_choice,
        )


@pytest.mark.parametrize("provider", ["anthropic", "gemini", "groq"])
def test_each_provider_builder_rejects_unknown_tool_choice(provider):
    with pytest.raises(ValueError, match="tool_choice"):
        BUILDERS[provider](
            system="system",
            messages=[],
            tools=TOOLS,
            tool_choice="required",
        )


def test_reserve_rejects_positive_day_aggregate_mismatch_before_insert(
    engine,
    session_factory,
):
    service = _service(session_factory)
    service.reserve(
        provider="anthropic",
        category="chat",
        request_id="reserve-before-aggregate-corruption",
        input_tokens=5,
        output_tokens=7,
        now=NOW,
    )
    _corrupt(
        engine,
        "UPDATE provider_budget_days SET calls_used = 2 "
        "WHERE provider = 'anthropic'",
    )

    with pytest.raises(ProviderBudgetUnavailable, match="corrupt"):
        service.reserve(
            provider="anthropic",
            category="chat",
            request_id="reserve-after-aggregate-corruption",
            input_tokens=1,
            output_tokens=1,
            now=NOW,
        )

    with session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(ProviderReservation)
        ) == 1
        assert session.scalar(
            select(ProviderBudgetDay.calls_used)
        ) == 2


def test_mark_started_rejects_reservation_day_mismatch_before_transition(
    engine,
    session_factory,
):
    service = _service(session_factory)
    reservation = service.reserve(
        provider="anthropic",
        category="chat",
        request_id="start-day-mismatch",
        input_tokens=5,
        output_tokens=7,
        now=NOW,
    )
    _corrupt(
        engine,
        "UPDATE provider_reservations SET budget_day = ? "
        "WHERE reservation_id = ?",
        (
            (NOW + timedelta(days=1)).date().isoformat(),
            reservation.reservation_id,
        ),
    )

    with pytest.raises(ProviderBudgetUnavailable, match="corrupt"):
        service.mark_started(reservation.reservation_id, now=NOW)

    persisted = _reservation(session_factory, reservation.reservation_id)
    assert persisted.state == "reserved"
    assert persisted.started_at is None


def test_mark_started_rejects_invalid_reserved_timestamp_relationship(
    engine,
    session_factory,
):
    service = _service(session_factory)
    reservation = service.reserve(
        provider="anthropic",
        category="chat",
        request_id="start-timestamp-corruption",
        input_tokens=5,
        output_tokens=7,
        now=NOW,
    )
    _corrupt(
        engine,
        "UPDATE provider_reservations SET started_at = ? "
        "WHERE reservation_id = ?",
        (NOW.isoformat(), reservation.reservation_id),
    )

    with pytest.raises(ProviderBudgetUnavailable, match="corrupt"):
        service.mark_started(reservation.reservation_id, now=NOW)

    assert _reservation(
        session_factory,
        reservation.reservation_id,
    ).state == "reserved"


def test_settle_rejects_aggregate_undercount_that_actual_would_mask(
    engine,
    session_factory,
):
    service = _service(session_factory)
    reservation = service.reserve(
        provider="anthropic",
        category="chat",
        request_id="settle-masked-undercount",
        input_tokens=10,
        output_tokens=8,
        now=NOW,
    )
    service.mark_started(reservation.reservation_id, now=NOW)
    _corrupt(
        engine,
        "UPDATE provider_budget_days SET input_tokens_used = 5 "
        "WHERE provider = 'anthropic'",
    )

    with pytest.raises(ProviderBudgetUnavailable, match="corrupt"):
        service.settle(
            reservation.reservation_id,
            input_tokens=20,
            output_tokens=4,
            now=NOW,
        )

    persisted = _reservation(session_factory, reservation.reservation_id)
    assert persisted.state == "started"
    assert persisted.input_actual is None
    assert _day(session_factory).input_tokens_used == 5


def test_mark_unknown_rejects_aggregate_mismatch_before_transition(
    engine,
    session_factory,
):
    service = _service(session_factory)
    reservation = service.reserve(
        provider="anthropic",
        category="chat",
        request_id="unknown-aggregate-mismatch",
        input_tokens=5,
        output_tokens=7,
        now=NOW,
    )
    service.mark_started(reservation.reservation_id, now=NOW)
    _corrupt(
        engine,
        "UPDATE provider_budget_days SET output_tokens_used = 8 "
        "WHERE provider = 'anthropic'",
    )

    with pytest.raises(ProviderBudgetUnavailable, match="corrupt"):
        service.mark_unknown(reservation.reservation_id)

    assert _reservation(
        session_factory,
        reservation.reservation_id,
    ).state == "started"


def test_mark_unknown_rejects_invalid_started_actual_relationship(
    engine,
    session_factory,
):
    service = _service(session_factory)
    reservation = service.reserve(
        provider="anthropic",
        category="chat",
        request_id="unknown-actual-corruption",
        input_tokens=5,
        output_tokens=7,
        now=NOW,
    )
    service.mark_started(reservation.reservation_id, now=NOW)
    _corrupt(
        engine,
        "UPDATE provider_reservations SET input_actual = 0 "
        "WHERE reservation_id = ?",
        (reservation.reservation_id,),
    )

    with pytest.raises(ProviderBudgetUnavailable, match="corrupt"):
        service.mark_unknown(reservation.reservation_id)

    persisted = _reservation(session_factory, reservation.reservation_id)
    assert persisted.state == "started"
    assert persisted.input_actual == 0


def test_release_rejects_positive_aggregate_mismatch_before_cleanup(
    engine,
    session_factory,
):
    service = _service(session_factory, ttl_seconds=1)
    reservation = service.reserve(
        provider="anthropic",
        category="chat",
        request_id="release-positive-mismatch",
        input_tokens=5,
        output_tokens=7,
        now=NOW,
    )
    _corrupt(
        engine,
        "UPDATE provider_budget_days SET output_tokens_used = 8 "
        "WHERE provider = 'anthropic'",
    )

    with pytest.raises(ProviderBudgetUnavailable, match="corrupt"):
        service.release_expired_unstarted(NOW + timedelta(seconds=1))

    assert _reservation(
        session_factory,
        reservation.reservation_id,
    ).state == "reserved"
    assert _day(session_factory).output_tokens_used == 8


def test_status_rejects_reservation_without_matching_day(
    engine,
    session_factory,
):
    service = _service(session_factory)
    service.reserve(
        provider="anthropic",
        category="chat",
        request_id="status-orphan-reservation",
        input_tokens=5,
        output_tokens=7,
        now=NOW,
    )
    _corrupt(
        engine,
        "DELETE FROM provider_budget_days "
        "WHERE provider = 'anthropic'",
    )

    with pytest.raises(ProviderBudgetUnavailable, match="corrupt"):
        service.status("anthropic", now=NOW)


def test_status_rejects_invalid_settled_actual_relationship(
    engine,
    session_factory,
):
    service = _service(session_factory)
    reservation = service.reserve(
        provider="anthropic",
        category="chat",
        request_id="status-settled-corruption",
        input_tokens=5,
        output_tokens=7,
        now=NOW,
    )
    service.mark_started(reservation.reservation_id, now=NOW)
    service.settle(
        reservation.reservation_id,
        input_tokens=3,
        output_tokens=2,
        now=NOW,
    )
    _corrupt(
        engine,
        "UPDATE provider_reservations SET output_actual = NULL "
        "WHERE reservation_id = ?",
        (reservation.reservation_id,),
    )

    with pytest.raises(ProviderBudgetUnavailable, match="corrupt"):
        service.status("anthropic", now=NOW)
