from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select, text

from trading_assistant.config import ProviderPriceConfig, Secrets
from trading_assistant.db.models import ProviderBudgetDay, ProviderReservation
from trading_assistant.llm.base import (
    BudgetedLLMBackend,
    LLMResponse,
    TextBlock,
    Usage,
)
from trading_assistant.llm.budget import (
    BudgetLimits,
    ProviderBudgetExceeded,
    ProviderBudgetService,
    ProviderBudgetUnavailable,
    Utf8ByteUpperBoundEstimator,
)
from trading_assistant.llm.factory import resolve_input_estimator


UTC = timezone.utc
NOW = datetime(2026, 7, 27, 12, tzinfo=UTC)
LIMITS = BudgetLimits(calls=10, input_tokens=100_000, output_tokens=10_000)
PAYLOAD = {
    "system": "Trade cautiously: café 🚦",
    "messages": [
        {"role": "user", "content": "Analyze 株"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Checking"},
                {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "quote",
                    "input": {"ticker": "AAPL"},
                },
            ],
        },
    ],
    "tools": [
        {
            "name": "quote",
            "description": "Get a price",
            "input_schema": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
            },
        }
    ],
}


class ScriptedBackend:
    def __init__(self, script) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome()
        return outcome


class CountingSessionFactory:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.delegate()


def _response(input_tokens: int, output_tokens: int) -> LLMResponse:
    return LLMResponse(
        content=[TextBlock(text="ok")],
        usage=Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
    )


def _service(
    session_factory,
    limits: BudgetLimits = LIMITS,
    *,
    now: datetime = NOW,
    prices=None,
) -> ProviderBudgetService:
    return ProviderBudgetService(
        session_factory,
        limits,
        prices=prices,
        clock=lambda: now,
    )


def _backend(
    session_factory,
    response,
    *,
    max_output_tokens: int = 100,
    limits: BudgetLimits = LIMITS,
    now: datetime = NOW,
):
    delegate = ScriptedBackend([response])
    service = _service(session_factory, limits, now=now)
    backend = BudgetedLLMBackend(
        delegate,
        service,
        provider="test",
        category="chat",
        max_output_tokens=max_output_tokens,
        estimator=Utf8ByteUpperBoundEstimator(),
    )
    return backend, delegate, service


def _reservation_state(session_factory, reservation_id: str) -> str:
    with session_factory() as session:
        return session.get(ProviderReservation, reservation_id).state


@pytest.mark.parametrize("provider", ["anthropic", "gemini", "groq"])
def test_each_supported_provider_estimator_covers_complete_utf8_payload(provider):
    estimator = resolve_input_estimator(provider)
    complete_payload_bytes = len(
        json.dumps(
            PAYLOAD,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )

    estimate = estimator.estimate_upper_bound(**PAYLOAD)

    assert estimate >= complete_payload_bytes


def test_missing_estimator_denies_before_provider_construction(
    app_config,
    session_factory,
    monkeypatch,
    patch_selected_llm_backend,
):
    from trading_assistant.llm import factory

    constructed: list[str] = []
    provider = app_config.llm.provider
    monkeypatch.delitem(factory._PROVIDER_INPUT_ESTIMATORS, provider)
    patch_selected_llm_backend(
        app_config,
        lambda *_args, **_kwargs: (
            constructed.append(provider) or object()
        ),
    )

    with pytest.raises(ProviderBudgetUnavailable, match="estimator"):
        factory.build_llm_backend(
            app_config,
            Secrets(),
            provider_budget=_service(session_factory),
            category="analysis",
        )

    assert constructed == []


def test_denied_budget_never_calls_delegate(session_factory):
    delegate = ScriptedBackend([])
    service = _service(
        session_factory,
        BudgetLimits(calls=0, input_tokens=1, output_tokens=1),
    )
    backend = BudgetedLLMBackend(
        delegate,
        service,
        provider="anthropic",
        category="chat",
        max_output_tokens=10,
    )

    with pytest.raises(ProviderBudgetExceeded):
        backend.create(
            system="s",
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            request_id="request-1",
        )

    assert delegate.calls == []
    with session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(ProviderBudgetDay)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(ProviderReservation)
        ) == 0


def test_locked_budget_store_fails_closed_without_delegate_call(
    engine,
    session_factory,
):
    def shorten_busy_timeout(
        dbapi_connection,
        _connection_record,
        _connection_proxy,
    ) -> None:
        dbapi_connection.execute("PRAGMA busy_timeout=1")

    event.listen(engine, "checkout", shorten_busy_timeout)
    delegate = ScriptedBackend([_response(1, 1)])
    backend = BudgetedLLMBackend(
        delegate,
        _service(session_factory),
        provider="test",
        category="chat",
        max_output_tokens=10,
        estimator=Utf8ByteUpperBoundEstimator(),
    )
    try:
        with engine.connect() as blocker:
            blocker.exec_driver_sql("BEGIN IMMEDIATE")
            with pytest.raises(
                ProviderBudgetUnavailable,
                match="store unavailable",
            ):
                backend.create(
                    system="s",
                    messages=[],
                    tools=[],
                    request_id="request-locked-store",
                )
            blocker.rollback()
    finally:
        event.remove(engine, "checkout", shorten_busy_timeout)

    assert delegate.calls == []
    with session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(ProviderReservation)
        ) == 0


@pytest.mark.parametrize("request_id", ["", " ", "\t"])
def test_budgeted_backend_rejects_empty_request_id_before_store_or_delegate(
    request_id,
    session_factory,
):
    counting_factory = CountingSessionFactory(session_factory)
    delegate = ScriptedBackend([])
    backend = BudgetedLLMBackend(
        delegate,
        _service(counting_factory),
        provider="test",
        category="chat",
        max_output_tokens=10,
        estimator=Utf8ByteUpperBoundEstimator(),
    )

    with pytest.raises(ValueError, match="request_id"):
        backend.create(
            system="s",
            messages=[],
            tools=[],
            request_id=request_id,
        )

    assert counting_factory.calls == 0
    assert delegate.calls == []


def test_budgeted_backend_requires_explicit_request_id_before_store_or_delegate(
    session_factory,
):
    counting_factory = CountingSessionFactory(session_factory)
    delegate = ScriptedBackend([])
    backend = BudgetedLLMBackend(
        delegate,
        _service(counting_factory),
        provider="test",
        category="analysis",
        max_output_tokens=10,
        estimator=Utf8ByteUpperBoundEstimator(),
    )

    with pytest.raises(TypeError, match="request_id"):
        backend.create(
            system="s",
            messages=[],
            tools=[],
        )

    assert counting_factory.calls == 0
    assert delegate.calls == []


@pytest.mark.parametrize(
    "request_id",
    [
        None,
        "",
        " ",
        "a" * 65,
        "request id",
        "request\nid",
        "reque\u0301st",
        "request-😀",
    ],
    ids=[
        "non-string",
        "empty",
        "blank",
        "too-long",
        "internal-space",
        "control",
        "nfd-unicode",
        "emoji",
    ],
)
def test_budgeted_backend_rejects_noncanonical_request_id_before_work(
    session_factory,
    request_id,
):
    counting_factory = CountingSessionFactory(session_factory)
    delegate = ScriptedBackend([])
    estimator = Utf8ByteUpperBoundEstimator()
    backend = BudgetedLLMBackend(
        delegate,
        _service(counting_factory),
        provider="test",
        category="analysis",
        max_output_tokens=10,
        estimator=estimator,
    )

    with pytest.raises(ValueError, match="request_id"):
        backend.create(
            system="s",
            messages=[],
            tools=[],
            request_id=request_id,
        )

    assert counting_factory.calls == 0
    assert delegate.calls == []


def test_budgeted_backend_uses_one_canonical_id_for_delegate_and_reservation(
    session_factory,
):
    delegate = ScriptedBackend([_response(1, 1)])
    backend = BudgetedLLMBackend(
        delegate,
        _service(session_factory),
        provider="test",
        category="analysis",
        max_output_tokens=10,
        estimator=Utf8ByteUpperBoundEstimator(),
    )

    backend.create(
        system="s",
        messages=[],
        tools=[],
        request_id="  request.canonical:one  ",
    )

    assert delegate.calls[0]["request_id"] == "request.canonical:one"
    with session_factory() as session:
        reservation = session.scalar(select(ProviderReservation))
    assert reservation.request_id == "request.canonical:one"


@pytest.mark.parametrize(
    "request_id",
    [
        None,
        "",
        " ",
        "a" * 65,
        "request id",
        "request\nid",
        "reque\u0301st",
        "request-😀",
    ],
    ids=[
        "non-string",
        "empty",
        "blank",
        "too-long",
        "internal-space",
        "control",
        "nfd-unicode",
        "emoji",
    ],
)
def test_provider_budget_rejects_noncanonical_request_id_before_store(
    session_factory,
    request_id,
):
    counting_factory = CountingSessionFactory(session_factory)
    service = _service(counting_factory)

    with pytest.raises(ValueError, match="request_id"):
        service.reserve(
            provider="test",
            category="analysis",
            request_id=request_id,
            input_tokens=1,
            output_tokens=1,
        )

    assert counting_factory.calls == 0


def test_provider_budget_canonicalizes_equivalent_ids_but_charges_each_attempt(
    session_factory,
):
    service = _service(session_factory)
    first = service.reserve(
        provider="test",
        category="analysis",
        request_id="  equivalent.request:id  ",
        input_tokens=1,
        output_tokens=1,
    )
    second = service.reserve(
        provider="test",
        category="analysis",
        request_id="equivalent.request:id",
        input_tokens=1,
        output_tokens=1,
    )

    assert first.request_id == second.request_id == "equivalent.request:id"
    assert first.reservation_id != second.reservation_id
    with session_factory() as session:
        reservations = session.scalars(
            select(ProviderReservation).order_by(
                ProviderReservation.reservation_id
            )
        ).all()
        day = session.scalar(select(ProviderBudgetDay))
    assert {row.request_id for row in reservations} == {
        "equivalent.request:id"
    }
    assert day.calls_used == 2


def test_provider_budget_accepts_64_but_rejects_65_character_request_id(
    session_factory,
):
    service = _service(session_factory)
    accepted = "r" * 64

    reservation = service.reserve(
        provider="test",
        category="analysis",
        request_id=accepted,
        input_tokens=1,
        output_tokens=1,
    )
    with pytest.raises(ValueError, match="request_id"):
        service.reserve(
            provider="test",
            category="analysis",
            request_id="r" * 65,
            input_tokens=1,
            output_tokens=1,
        )

    assert reservation.request_id == accepted
    assert service.status("test").calls_used == 1


def test_provider_budget_reconciliation_rejects_noncanonical_stored_request_id(
    session_factory,
):
    service = _service(session_factory)
    reservation = service.reserve(
        provider="test",
        category="analysis",
        request_id="canonical-stored-id",
        input_tokens=1,
        output_tokens=1,
    )
    with session_factory() as session:
        session.execute(
            text(
                "UPDATE provider_reservations "
                "SET request_id = ' noncanonical-stored-id ' "
                "WHERE reservation_id = :reservation_id"
            ),
            {"reservation_id": reservation.reservation_id},
        )
        session.commit()

    with pytest.raises(
        ProviderBudgetUnavailable,
        match="corrupt provider budget state",
    ):
        service.status("test")


def test_mark_started_precedes_delegate_invocation(session_factory):
    service = _service(session_factory)
    observed_states: list[str] = []

    def observe_state():
        with session_factory() as session:
            reservation = session.scalar(
                select(ProviderReservation).where(
                    ProviderReservation.request_id == "request-start-order"
                )
            )
            observed_states.append(reservation.state)
        return _response(1, 1)

    delegate = ScriptedBackend([observe_state])
    backend = BudgetedLLMBackend(
        delegate,
        service,
        provider="test",
        category="chat",
        max_output_tokens=10,
        estimator=Utf8ByteUpperBoundEstimator(),
    )

    backend.create(
        system="s",
        messages=[],
        tools=[],
        request_id="request-start-order",
    )

    assert observed_states == ["started"]


def test_settlement_refunds_unused_output_and_survives_restart(session_factory):
    backend, _delegate, service = _backend(
        session_factory,
        _response(3, 2),
        max_output_tokens=100,
    )

    backend.create(
        system="s",
        messages=[{"role": "user", "content": "x"}],
        tools=[],
        request_id="request-2",
    )

    usage = ProviderBudgetService(
        session_factory,
        LIMITS,
        clock=lambda: NOW,
    ).status("test")
    assert usage.calls_used == 1
    assert usage.input_tokens_used == 3
    assert usage.output_tokens_used == 2
    assert usage.reconciliation_required is False
    with session_factory() as session:
        reservation = session.scalar(select(ProviderReservation))
    assert reservation.state == "settled"
    assert reservation.input_actual == 3
    assert reservation.output_actual == 2
    assert service.status("test") == usage


def test_delegate_exception_leaves_reservation_fully_charged_and_unknown(
    session_factory,
):
    error = RuntimeError("provider failed")
    backend, delegate, service = _backend(
        session_factory,
        error,
        max_output_tokens=40,
    )
    expected_input = Utf8ByteUpperBoundEstimator().estimate_upper_bound(
        system="s",
        messages=[],
        tools=[],
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        backend.create(
            system="s",
            messages=[],
            tools=[],
            request_id="request-exception",
        )

    status = service.status("test")
    assert len(delegate.calls) == 1
    assert status.calls_used == 1
    assert status.input_tokens_used == expected_input
    assert status.output_tokens_used == 40
    with session_factory() as session:
        reservation = session.scalar(select(ProviderReservation))
    assert reservation.state == "unknown"
    assert reservation.input_actual is None
    assert reservation.output_actual is None
    assert reservation.settled_at is None


@pytest.mark.parametrize(
    "interruption",
    [
        KeyboardInterrupt("stop"),
        SystemExit("stop"),
        asyncio.CancelledError("stop"),
    ],
    ids=["keyboard-interrupt", "system-exit", "cancelled"],
)
def test_delegate_base_exception_is_preserved_and_reservation_becomes_unknown(
    session_factory,
    interruption,
):
    backend, delegate, _service = _backend(
        session_factory,
        interruption,
        max_output_tokens=40,
    )

    with pytest.raises(type(interruption)) as failure:
        backend.create(
            system="s",
            messages=[],
            tools=[],
            request_id=f"request-{type(interruption).__name__.lower()}",
        )

    assert failure.value is interruption
    assert len(delegate.calls) == 1
    with session_factory() as session:
        reservation = session.scalar(select(ProviderReservation))
    assert reservation.state == "unknown"


def test_mark_started_interruption_after_durable_commit_never_calls_provider(
    session_factory,
    monkeypatch,
):
    service = _service(session_factory)
    original_mark_started = service.mark_started
    interruption = asyncio.CancelledError("cancel-after-start")

    def interrupt_after_commit(reservation_id):
        original_mark_started(reservation_id)
        raise interruption

    monkeypatch.setattr(service, "mark_started", interrupt_after_commit)
    delegate = ScriptedBackend([_response(1, 1)])
    backend = BudgetedLLMBackend(
        delegate,
        service,
        provider="test",
        category="untrusted",
        max_output_tokens=10,
        estimator=Utf8ByteUpperBoundEstimator(),
    )

    with pytest.raises(asyncio.CancelledError) as failure:
        backend.create(
            system="s",
            messages=[],
            tools=[],
            request_id="request-cancel-after-start",
        )

    assert failure.value is interruption
    assert delegate.calls == []
    with session_factory() as session:
        reservation = session.scalar(select(ProviderReservation))
    assert reservation.state == "unknown"


def test_usage_base_exception_is_preserved_and_reservation_becomes_unknown(
    session_factory,
):
    interruption = KeyboardInterrupt("cancel-usage-read")

    class UsageFailure:
        @property
        def input_tokens(self):
            raise interruption

    response = SimpleNamespace(content=[], usage=UsageFailure())
    backend, _delegate, _service = _backend(
        session_factory,
        response,
        max_output_tokens=40,
    )

    with pytest.raises(KeyboardInterrupt) as failure:
        backend.create(
            system="s",
            messages=[],
            tools=[],
            request_id="request-cancel-usage-read",
        )

    assert failure.value is interruption
    with session_factory() as session:
        reservation = session.scalar(select(ProviderReservation))
    assert reservation.state == "unknown"


def test_settlement_failure_marks_started_reservation_unknown_before_raising(
    session_factory,
    monkeypatch,
):
    service = _service(session_factory)
    delegate = ScriptedBackend([_response(1, 1)])
    backend = BudgetedLLMBackend(
        delegate,
        service,
        provider="test",
        category="untrusted",
        max_output_tokens=10,
        estimator=Utf8ByteUpperBoundEstimator(),
    )
    settlement_error = RuntimeError("settlement unavailable")

    def fail_settlement(*_args, **_kwargs):
        raise settlement_error

    monkeypatch.setattr(service, "settle", fail_settlement)

    with pytest.raises(RuntimeError) as failure:
        backend.create(
            system="s",
            messages=[],
            tools=[],
            request_id="request-settlement-failure",
        )

    assert failure.value is settlement_error
    with session_factory() as session:
        reservation = session.scalar(select(ProviderReservation))
    assert reservation.state == "unknown"


def test_mark_unknown_is_idempotent_for_unknown_and_settled_reservations(
    session_factory,
):
    service = _service(session_factory)
    unknown = service.reserve(
        provider="test",
        category="untrusted",
        request_id="request-idempotent-unknown",
        input_tokens=1,
        output_tokens=1,
    )
    service.mark_started(unknown.reservation_id)
    service.mark_unknown(unknown.reservation_id)
    service.mark_unknown(unknown.reservation_id)

    settled = service.reserve(
        provider="test",
        category="untrusted",
        request_id="request-idempotent-settled",
        input_tokens=1,
        output_tokens=1,
    )
    service.mark_started(settled.reservation_id)
    service.settle(
        settled.reservation_id,
        input_tokens=1,
        output_tokens=1,
    )
    service.mark_unknown(settled.reservation_id)

    assert _reservation_state(
        session_factory,
        unknown.reservation_id,
    ) == "unknown"
    assert _reservation_state(
        session_factory,
        settled.reservation_id,
    ) == "settled"


def test_reconciliation_baseexception_never_masks_original_cancellation(
    session_factory,
    monkeypatch,
):
    service = _service(session_factory)
    cancellation = asyncio.CancelledError("provider call cancelled")
    delegate = ScriptedBackend([cancellation])
    backend = BudgetedLLMBackend(
        delegate,
        service,
        provider="test",
        category="untrusted",
        max_output_tokens=10,
        estimator=Utf8ByteUpperBoundEstimator(),
    )
    reconciliation_interrupt = KeyboardInterrupt(
        "reconciliation interrupted"
    )

    def interrupt_reconciliation(_reservation_id):
        raise reconciliation_interrupt

    monkeypatch.setattr(
        service,
        "mark_unknown",
        interrupt_reconciliation,
    )

    with pytest.raises(asyncio.CancelledError) as failure:
        backend.create(
            system="s",
            messages=[],
            tools=[],
            request_id="request-reconciliation-interrupt",
        )

    assert failure.value is cancellation
    assert any(
        "reservation reconciliation failed" in note
        for note in getattr(cancellation, "__notes__", ())
    )
    with session_factory() as session:
        reservation = session.scalar(select(ProviderReservation))
    assert reservation.state == "started"


@pytest.mark.parametrize("trigger", ["status", "reserve"])
def test_expired_started_reservation_converges_after_failed_reconciliation(
    trigger,
    session_factory,
    monkeypatch,
):
    limits = BudgetLimits(
        calls=10,
        input_tokens=100_000,
        output_tokens=10_000,
        reservation_ttl_seconds=5,
    )
    service = _service(session_factory, limits)
    cancellation = asyncio.CancelledError("provider call cancelled")
    delegate = ScriptedBackend([cancellation])
    backend = BudgetedLLMBackend(
        delegate,
        service,
        provider="test",
        category="untrusted",
        max_output_tokens=10,
        estimator=Utf8ByteUpperBoundEstimator(),
    )

    def fail_reconciliation(_reservation_id):
        raise ProviderBudgetUnavailable(
            "transient durable store failure"
        )

    monkeypatch.setattr(service, "mark_unknown", fail_reconciliation)
    with pytest.raises(asyncio.CancelledError) as failure:
        backend.create(
            system="s",
            messages=[],
            tools=[],
            request_id=f"request-stale-started-{trigger}",
        )
    assert failure.value is cancellation

    with session_factory() as session:
        reservation = session.scalar(select(ProviderReservation))
        day = session.scalar(select(ProviderBudgetDay))
        charged = (
            day.calls_used,
            day.input_tokens_used,
            day.output_tokens_used,
        )
    assert reservation.state == "started"

    expired_at = NOW + timedelta(seconds=5)
    later = _service(
        session_factory,
        limits,
        now=expired_at,
    )
    if trigger == "status":
        status = later.status("test")
    else:
        with pytest.raises(
            ProviderBudgetExceeded,
            match="reconciliation",
        ):
            later.reserve(
                provider="test",
                category="chat",
                request_id="request-blocked-after-stale-start",
                input_tokens=1,
                output_tokens=1,
            )
        status = later.status("test")

    assert status.reconciliation_required is True
    assert (
        status.reconciliation_code
        == "provider_started_usage_unknown"
    )
    assert (
        status.calls_used,
        status.input_tokens_used,
        status.output_tokens_used,
    ) == charged
    with session_factory() as session:
        persisted = session.get(
            ProviderReservation,
            reservation.reservation_id,
        )
        count = session.scalar(
            select(func.count()).select_from(ProviderReservation)
        )
    assert persisted.state == "unknown"
    assert count == 1
    with pytest.raises(ProviderBudgetUnavailable):
        later.settle(
            reservation.reservation_id,
            input_tokens=1,
            output_tokens=1,
            now=expired_at,
        )
    with pytest.raises(
        ProviderBudgetExceeded,
        match="reconciliation",
    ):
        later.reserve(
            provider="test",
            category="chat",
            request_id="request-no-capacity-reuse",
            input_tokens=1,
            output_tokens=1,
            now=expired_at,
        )


def test_unexpired_started_reservation_is_not_reaped(
    session_factory,
):
    limits = BudgetLimits(
        calls=10,
        input_tokens=100,
        output_tokens=100,
        reservation_ttl_seconds=5,
    )
    service = _service(session_factory, limits)
    reservation = service.reserve(
        provider="test",
        category="chat",
        request_id="request-active-start",
        input_tokens=10,
        output_tokens=10,
        now=NOW,
    )
    service.mark_started(reservation.reservation_id, now=NOW)

    reaped = service.reconcile_expired_started(
        NOW + timedelta(seconds=4)
    )
    status = service.status(
        "test",
        now=NOW + timedelta(seconds=4),
    )

    assert reaped == 0
    assert status.reconciliation_required is False
    assert _reservation_state(
        session_factory,
        reservation.reservation_id,
    ) == "started"


def test_concurrent_expired_started_reapers_transition_exactly_once(
    session_factory,
):
    limits = BudgetLimits(
        calls=10,
        input_tokens=100,
        output_tokens=100,
        reservation_ttl_seconds=5,
    )
    service = _service(session_factory, limits)
    reservation = service.reserve(
        provider="test",
        category="chat",
        request_id="request-concurrent-stale-start",
        input_tokens=10,
        output_tokens=10,
        now=NOW,
    )
    service.mark_started(reservation.reservation_id, now=NOW)
    expired_at = NOW + timedelta(seconds=5)
    barrier = threading.Barrier(8)

    def reap_once(_index: int) -> int:
        worker = _service(
            session_factory,
            limits,
            now=expired_at,
        )
        barrier.wait()
        return worker.reconcile_expired_started(expired_at)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reap_once, range(8)))

    assert sorted(results) == [0, 0, 0, 0, 0, 0, 0, 1]
    status = service.status("test", now=expired_at)
    assert status.calls_used == 1
    assert status.input_tokens_used == 10
    assert status.output_tokens_used == 10
    assert status.reconciliation_required is True
    assert _reservation_state(
        session_factory,
        reservation.reservation_id,
    ) == "unknown"


def test_provider_reservation_cannot_start_at_or_after_expiry(
    session_factory,
):
    limits = BudgetLimits(
        calls=10,
        input_tokens=100,
        output_tokens=100,
        reservation_ttl_seconds=5,
    )
    service = _service(session_factory, limits)
    reservation = service.reserve(
        provider="test",
        category="chat",
        request_id="request-start-at-expiry",
        input_tokens=1,
        output_tokens=1,
        now=NOW,
    )

    with pytest.raises(ProviderBudgetUnavailable):
        service.mark_started(
            reservation.reservation_id,
            now=NOW + timedelta(seconds=5),
        )

    assert _reservation_state(
        session_factory,
        reservation.reservation_id,
    ) == "reserved"


def test_provider_budget_rejects_started_at_after_expiry(
    session_factory,
):
    limits = BudgetLimits(
        calls=10,
        input_tokens=100,
        output_tokens=100,
        reservation_ttl_seconds=5,
    )
    service = _service(session_factory, limits)
    reservation = service.reserve(
        provider="test",
        category="chat",
        request_id="request-corrupt-started-at",
        input_tokens=1,
        output_tokens=1,
        now=NOW,
    )
    with session_factory() as session:
        row = session.get(
            ProviderReservation,
            reservation.reservation_id,
        )
        row.state = "started"
        row.started_at = NOW + timedelta(seconds=6)
        session.commit()

    with pytest.raises(
        ProviderBudgetUnavailable,
        match="corrupt",
    ):
        service.status("test", now=NOW + timedelta(seconds=4))


def test_missing_usage_leaves_reservation_fully_charged_and_unknown(
    session_factory,
):
    response = SimpleNamespace(content=[], stop_reason="end_turn", model="test")
    backend, _delegate, service = _backend(
        session_factory,
        response,
        max_output_tokens=30,
    )
    expected_input = Utf8ByteUpperBoundEstimator().estimate_upper_bound(
        system="s",
        messages=[],
        tools=[],
    )

    returned = backend.create(
        system="s",
        messages=[],
        tools=[],
        request_id="request-missing-usage",
    )

    assert returned is response
    status = service.status("test")
    assert status.input_tokens_used == expected_input
    assert status.output_tokens_used == 30
    with session_factory() as session:
        reservation = session.scalar(select(ProviderReservation))
    assert reservation.state == "unknown"


@pytest.mark.parametrize("overrun_axis", ["input", "output"])
def test_actual_overrun_is_fully_charged_reconciles_and_blocks_next_call(
    overrun_axis,
    session_factory,
):
    estimator = Utf8ByteUpperBoundEstimator()
    reserved_input = estimator.estimate_upper_bound(
        system="s",
        messages=[],
        tools=[],
    )
    input_actual = reserved_input + 7 if overrun_axis == "input" else 1
    output_actual = 17 if overrun_axis == "output" else 1
    first_delegate = ScriptedBackend([_response(input_actual, output_actual)])
    limits = BudgetLimits(
        calls=3,
        input_tokens=reserved_input,
        output_tokens=10,
    )
    service = _service(session_factory, limits)
    first = BudgetedLLMBackend(
        first_delegate,
        service,
        provider="test",
        category="chat",
        max_output_tokens=10,
        estimator=estimator,
    )

    first.create(
        system="s",
        messages=[],
        tools=[],
        request_id=f"request-{overrun_axis}-overrun",
    )

    status = service.status("test")
    assert status.input_tokens_used == input_actual
    assert status.output_tokens_used == output_actual
    if overrun_axis == "input":
        assert status.input_tokens_used > status.input_tokens_limit
    else:
        assert status.output_tokens_used > status.output_tokens_limit
    assert status.reconciliation_required is True
    assert status.reconciliation_code == "provider_usage_over_reservation"

    blocked_delegate = ScriptedBackend([_response(1, 1)])
    blocked = BudgetedLLMBackend(
        blocked_delegate,
        service,
        provider="test",
        category="chat",
        max_output_tokens=1,
        estimator=estimator,
    )
    with pytest.raises(ProviderBudgetExceeded, match="reconciliation"):
        blocked.create(
            system="s",
            messages=[],
            tools=[],
            request_id=f"request-blocked-after-{overrun_axis}",
        )
    assert blocked_delegate.calls == []


def test_reconciliation_on_prior_utc_day_blocks_new_day_reservation(
    session_factory,
):
    first_day = _service(session_factory, LIMITS, now=NOW)
    reservation = first_day.reserve(
        provider="test",
        category="chat",
        request_id="request-overrun-day-one",
        input_tokens=1,
        output_tokens=1,
    )
    first_day.mark_started(reservation.reservation_id, now=NOW)
    first_day.settle(
        reservation.reservation_id,
        input_tokens=2,
        output_tokens=1,
        now=NOW,
    )
    next_day = _service(
        session_factory,
        LIMITS,
        now=NOW + timedelta(days=1),
    )

    with pytest.raises(ProviderBudgetExceeded, match="reconciliation"):
        next_day.reserve(
            provider="test",
            category="chat",
            request_id="request-next-day-blocked",
            input_tokens=1,
            output_tokens=1,
        )


def test_only_expired_unstarted_reservations_are_released(session_factory):
    limits = BudgetLimits(
        calls=10,
        input_tokens=100,
        output_tokens=100,
        reservation_ttl_seconds=5,
    )
    service = _service(session_factory, limits)
    reservations = [
        service.reserve(
            provider="test",
            category="chat",
            request_id=f"request-{state}",
            input_tokens=10,
            output_tokens=10,
            now=NOW,
        )
        for state in ("reserved", "started", "unknown", "settled")
    ]
    service.mark_started(reservations[1].reservation_id, now=NOW)
    service.mark_started(reservations[2].reservation_id, now=NOW)
    service.mark_unknown(reservations[2].reservation_id)
    service.mark_started(reservations[3].reservation_id, now=NOW)
    service.settle(
        reservations[3].reservation_id,
        input_tokens=4,
        output_tokens=3,
        now=NOW,
    )

    released = service.release_expired_unstarted(NOW + timedelta(seconds=5))

    assert released == 1
    with session_factory() as session:
        persisted = {
            row.request_id: (row.state, row.settled_at)
            for row in session.scalars(
                select(ProviderReservation).order_by(
                    ProviderReservation.request_id
                )
            )
        }
    assert persisted == {
        "request-reserved": ("released", None),
        "request-settled": (
            "settled",
            NOW,
        ),
        "request-started": ("started", None),
        "request-unknown": ("unknown", None),
    }
    status = service.status("test")
    assert status.calls_used == 3
    assert status.input_tokens_used == 24
    assert status.output_tokens_used == 23


def test_reserve_releases_expired_unstarted_inside_its_transaction(
    session_factory,
):
    limits = BudgetLimits(
        calls=1,
        input_tokens=10,
        output_tokens=10,
        reservation_ttl_seconds=5,
    )
    service = _service(session_factory, limits)
    expired = service.reserve(
        provider="test",
        category="chat",
        request_id="request-expired",
        input_tokens=10,
        output_tokens=10,
        now=NOW,
    )

    replacement = service.reserve(
        provider="test",
        category="chat",
        request_id="request-replacement",
        input_tokens=10,
        output_tokens=10,
        now=NOW + timedelta(seconds=5),
    )

    assert _reservation_state(
        session_factory,
        expired.reservation_id,
    ) == "released"
    assert _reservation_state(
        session_factory,
        replacement.reservation_id,
    ) == "reserved"
    status = service.status("test", now=NOW)
    assert (
        status.calls_used,
        status.input_tokens_used,
        status.output_tokens_used,
    ) == (1, 10, 10)


@pytest.mark.parametrize("ceiling", ["calls", "input", "output"])
def test_parallel_reservations_cannot_cross_any_daily_ceiling(
    ceiling,
    session_factory,
):
    limits = BudgetLimits(
        calls=1 if ceiling == "calls" else 2,
        input_tokens=1 if ceiling == "input" else 2,
        output_tokens=1 if ceiling == "output" else 2,
    )
    services = [
        _service(session_factory, limits),
        _service(session_factory, limits),
    ]
    barrier = threading.Barrier(2)

    def reserve(index: int) -> bool:
        barrier.wait()
        try:
            services[index].reserve(
                provider="test",
                category="chat",
                request_id=f"parallel-{index}",
                input_tokens=1,
                output_tokens=1,
                now=NOW,
            )
        except ProviderBudgetExceeded:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, range(2)))

    assert sorted(outcomes) == [False, True]
    status = services[0].status("test")
    assert (
        status.calls_used,
        status.input_tokens_used,
        status.output_tokens_used,
    ) == (1, 1, 1)
    with session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(ProviderReservation)
        ) == 1


def test_utc_boundary_creates_a_new_provider_budget_day(session_factory):
    before_midnight = datetime(2026, 7, 27, 23, 59, 59, tzinfo=UTC)
    after_midnight = before_midnight + timedelta(seconds=1)
    service = _service(
        session_factory,
        BudgetLimits(calls=1, input_tokens=1, output_tokens=1),
    )
    service.reserve(
        provider="test",
        category="chat",
        request_id="request-day-one",
        input_tokens=1,
        output_tokens=1,
        now=before_midnight,
    )

    service.reserve(
        provider="test",
        category="chat",
        request_id="request-day-two",
        input_tokens=1,
        output_tokens=1,
        now=after_midnight,
    )

    with session_factory() as session:
        days = session.scalars(
            select(ProviderBudgetDay).order_by(ProviderBudgetDay.budget_day)
        ).all()
    assert [row.budget_day for row in days] == [
        date(2026, 7, 27),
        date(2026, 7, 28),
    ]
    assert [row.calls_used for row in days] == [1, 1]


def test_local_time_is_normalized_to_utc_budget_day(session_factory):
    local_time = datetime(
        2026,
        7,
        28,
        1,
        tzinfo=timezone(timedelta(hours=5, minutes=30)),
    )
    service = _service(session_factory)

    service.reserve(
        provider="test",
        category="chat",
        request_id="request-local-time",
        input_tokens=1,
        output_tokens=1,
        now=local_time,
    )

    with session_factory() as session:
        row = session.scalar(select(ProviderBudgetDay))
    assert row.budget_day == date(2026, 7, 27)


def test_estimated_usd_uses_matching_effective_dated_price_metadata(
    session_factory,
):
    prices = {
        "test:model-a": ProviderPriceConfig(
            model="model-a",
            effective_date=date(2026, 7, 27),
            input_usd_per_million=Decimal("2.00"),
            output_usd_per_million=Decimal("8.00"),
            source_url="https://example.com/test-model-price",
        ),
        "other:model-a": ProviderPriceConfig(
            model="model-a",
            effective_date=date(2026, 7, 27),
            input_usd_per_million=Decimal("999"),
            output_usd_per_million=Decimal("999"),
            source_url="https://example.com/other-model-price",
        ),
    }
    service = _service(session_factory, prices=prices)
    reservation = service.reserve(
        provider="test",
        category="chat",
        request_id="request-priced",
        input_tokens=1_000,
        output_tokens=1_000,
        now=NOW,
    )
    service.mark_started(reservation.reservation_id, now=NOW)
    service.settle(
        reservation.reservation_id,
        input_tokens=750,
        output_tokens=250,
        now=NOW,
    )

    status = service.status("test", model="model-a", now=NOW)

    assert status.calls_used == 1
    assert status.input_tokens_used == 750
    assert status.output_tokens_used == 250
    assert status.estimated_usd == Decimal("0.003500")
    assert status.price_model == "model-a"
    assert status.price_effective_date == date(2026, 7, 27)


def test_estimated_usd_is_unavailable_before_configured_effective_date(
    session_factory,
):
    prices = {
        "test:model-a": ProviderPriceConfig(
            model="model-a",
            effective_date=date(2026, 7, 28),
            input_usd_per_million=Decimal("2.00"),
            output_usd_per_million=Decimal("8.00"),
            source_url="https://example.com/test-model-future-price",
        ),
    }
    service = _service(session_factory, prices=prices)
    service.reserve(
        provider="test",
        category="chat",
        request_id="request-unpriced-day",
        input_tokens=1_000,
        output_tokens=1_000,
        now=NOW,
    )

    status = service.status("test", model="model-a", now=NOW)

    assert status.estimated_usd is None
    assert status.price_model == ""
    assert status.price_effective_date is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("calls", -1),
        ("input_tokens", -1),
        ("output_tokens", -1),
        ("reservation_ttl_seconds", 0),
        ("reservation_ttl_seconds", -1),
    ],
)
def test_invalid_budget_limits_are_rejected(field, value):
    values = {
        "calls": 1,
        "input_tokens": 1,
        "output_tokens": 1,
        "reservation_ttl_seconds": 300,
    }
    values[field] = value

    with pytest.raises(ValueError):
        BudgetLimits(**values)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"provider": ""}, "provider"),
        ({"category": ""}, "category"),
        ({"request_id": ""}, "request_id"),
        ({"input_tokens": -1}, "input_tokens"),
        ({"output_tokens": -1}, "output_tokens"),
    ],
)
def test_invalid_reservation_input_is_rejected_before_session(
    overrides,
    match,
    session_factory,
):
    counting_factory = CountingSessionFactory(session_factory)
    service = _service(counting_factory)
    kwargs = {
        "provider": "test",
        "category": "chat",
        "request_id": "request-valid",
        "input_tokens": 1,
        "output_tokens": 1,
        "now": NOW,
    }
    kwargs.update(overrides)

    with pytest.raises(ValueError, match=match):
        service.reserve(**kwargs)

    assert counting_factory.calls == 0


def test_invalid_actual_usage_is_rejected_before_session(session_factory):
    counting_factory = CountingSessionFactory(session_factory)
    service = _service(counting_factory)

    with pytest.raises(ValueError, match="input_tokens"):
        service.settle(
            "reservation-does-not-matter",
            input_tokens=-1,
            output_tokens=0,
            now=NOW,
        )

    assert counting_factory.calls == 0


def test_wrong_state_transitions_fail_closed_without_counter_edits(
    session_factory,
):
    service = _service(session_factory)
    reservation = service.reserve(
        provider="test",
        category="chat",
        request_id="request-transition",
        input_tokens=5,
        output_tokens=7,
        now=NOW,
    )
    service.mark_started(reservation.reservation_id, now=NOW)

    with pytest.raises(ProviderBudgetUnavailable):
        service.mark_started(reservation.reservation_id, now=NOW)

    service.mark_unknown(reservation.reservation_id)
    with pytest.raises(ProviderBudgetUnavailable):
        service.settle(
            reservation.reservation_id,
            input_tokens=1,
            output_tokens=1,
            now=NOW,
        )

    status = service.status("test")
    assert (
        status.calls_used,
        status.input_tokens_used,
        status.output_tokens_used,
    ) == (1, 5, 7)
