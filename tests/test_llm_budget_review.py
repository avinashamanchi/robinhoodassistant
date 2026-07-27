from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from trading_assistant.db.models import ProviderBudgetDay, ProviderReservation
from trading_assistant.llm.anthropic_backend import AnthropicBackend
from trading_assistant.llm.base import (
    BudgetedLLMBackend,
    LLMResponse,
    TextBlock,
    Usage,
    from_openai,
)
from trading_assistant.llm.budget import (
    BudgetLimits,
    ProviderBudgetService,
    ProviderBudgetUnavailable,
)
from trading_assistant.llm.factory import resolve_input_estimator
from trading_assistant.llm.gemini_backend import GeminiBackend, from_gemini
from trading_assistant.llm.groq_backend import GroqBackend
from trading_assistant.llm.payloads import build_gemini_payload


UTC = timezone.utc
NOW = datetime(2026, 7, 27, 12, tzinfo=UTC)
LIMITS = BudgetLimits(
    calls=20,
    input_tokens=200_000,
    output_tokens=20_000,
)

BOUND_SYSTEM = "System café"
BOUND_MESSAGES = [
    {"role": "user", "content": "quote AAPL"},
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "checking"},
            {
                "type": "tool_use",
                "id": "call-1",
                "name": "quote",
                "input": {"ticker": "AAPL"},
            },
        ],
    },
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call-1",
                "content": '{"last": 100}',
            }
        ],
    },
]
BOUND_TOOLS = [
    {
        "name": "quote",
        "description": "Get quote",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": ["string", "null"]},
            },
        },
    }
]

EXPECTED_PROVIDER_PAYLOADS = {
    "anthropic": {
        "system": BOUND_SYSTEM,
        "messages": BOUND_MESSAGES,
        "tools": BOUND_TOOLS,
        "tool_choice": {"type": "any"},
    },
    "groq": {
        "messages": [
            {"role": "system", "content": BOUND_SYSTEM},
            {"role": "user", "content": "quote AAPL"},
            {
                "role": "assistant",
                "content": "checking",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "quote",
                            "arguments": '{"ticker": "AAPL"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": '{"last": 100}',
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "quote",
                    "description": "Get quote",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "ticker": {"type": ["string", "null"]},
                        },
                    },
                },
            }
        ],
        "tool_choice": "required",
    },
    "gemini": {
        "system_instruction": BOUND_SYSTEM,
        "contents": [
            {"role": "user", "parts": [{"text": "quote AAPL"}]},
            {
                "role": "model",
                "parts": [
                    {"text": "checking"},
                    {
                        "function_call": {
                            "name": "quote",
                            "args": {"ticker": "AAPL"},
                        }
                    },
                ],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "function_response": {
                            "name": "quote",
                            "response": {"last": 100},
                        }
                    }
                ],
            },
        ],
        "tools": [
            {
                "function_declarations": [
                    {
                        "name": "quote",
                        "description": "Get quote",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "ticker": {"type": "string"},
                            },
                        },
                    }
                ]
            }
        ],
        "tool_config": {
            "function_calling_config": {"mode": "ANY"},
        },
    },
}


class RecordingBackend:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response or LLMResponse(
            content=[TextBlock(text="ok")],
            usage=Usage(input_tokens=1, output_tokens=1),
        )
        self.error = error
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class CountingSessionFactory:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.delegate()


class UsageReadError(RuntimeError):
    pass


class ResponseUsageRaises:
    content = []
    stop_reason = "end_turn"

    @property
    def usage(self):
        raise UsageReadError("response usage exploded")


class InputUsageRaises:
    output_tokens = 1

    @property
    def input_tokens(self):
        raise UsageReadError("input usage exploded")


def _service(
    session_factory,
    *,
    prices=None,
    limits: BudgetLimits = LIMITS,
) -> ProviderBudgetService:
    return ProviderBudgetService(
        session_factory,
        limits,
        prices=prices,
        clock=lambda: NOW,
    )


def _budgeted(
    session_factory,
    response,
    *,
    service: ProviderBudgetService | None = None,
    max_output_tokens: int = 10,
):
    delegate = RecordingBackend(response=response)
    budgets = service or _service(session_factory)
    backend = BudgetedLLMBackend(
        delegate,
        budgets,
        provider="anthropic",
        category="chat",
        max_output_tokens=max_output_tokens,
    )
    return backend, delegate, budgets


def _reservation(session_factory) -> ProviderReservation:
    with session_factory() as session:
        return session.scalar(select(ProviderReservation))


def _corrupt(engine, statement: str, parameters=()) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        try:
            connection.exec_driver_sql(statement, parameters)
        finally:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")


@pytest.mark.parametrize("provider", ["anthropic", "gemini", "groq"])
def test_provider_estimate_covers_independent_transformed_payload(provider):
    expected_bytes = len(
        json.dumps(
            EXPECTED_PROVIDER_PAYLOADS[provider],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )

    estimate = resolve_input_estimator(provider).estimate_upper_bound(
        system=BOUND_SYSTEM,
        messages=BOUND_MESSAGES,
        tools=BOUND_TOOLS,
    )

    assert estimate >= expected_bytes


VALID_PAYLOAD = {
    "system": "system",
    "messages": [{"role": "user", "content": "hello"}],
    "tools": [
        {
            "name": "quote",
            "description": "Get quote",
            "input_schema": {
                "type": "object",
                "properties": {},
            },
        }
    ],
    "tool_choice": None,
}


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("system-not-string", {**VALID_PAYLOAD, "system": 7}),
        ("messages-not-list", {**VALID_PAYLOAD, "messages": ()}),
        ("message-not-dict", {**VALID_PAYLOAD, "messages": ["hello"]}),
        ("message-missing-role", {**VALID_PAYLOAD, "messages": [{"content": "x"}]}),
        ("message-missing-content", {**VALID_PAYLOAD, "messages": [{"role": "user"}]}),
        (
            "message-content-not-json-shape",
            {
                **VALID_PAYLOAD,
                "messages": [{"role": "user", "content": ("x",)}],
            },
        ),
        ("tools-not-list", {**VALID_PAYLOAD, "tools": ()}),
        ("tool-not-dict", {**VALID_PAYLOAD, "tools": ["quote"]}),
        (
            "tool-missing-schema",
            {
                **VALID_PAYLOAD,
                "tools": [{"name": "quote", "description": "d"}],
            },
        ),
        (
            "non-string-json-key",
            {
                **VALID_PAYLOAD,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "c1",
                                "name": "quote",
                                "input": {1: "AAPL"},
                            }
                        ],
                    }
                ],
            },
        ),
        (
            "non-json-value",
            {
                **VALID_PAYLOAD,
                "tools": [
                    {
                        "name": "quote",
                        "description": "d",
                        "input_schema": {"default": object()},
                    }
                ],
            },
        ),
        (
            "nan",
            {
                **VALID_PAYLOAD,
                "tools": [
                    {
                        "name": "quote",
                        "description": "d",
                        "input_schema": {"default": float("nan")},
                    }
                ],
            },
        ),
        (
            "positive-infinity",
            {
                **VALID_PAYLOAD,
                "tools": [
                    {
                        "name": "quote",
                        "description": "d",
                        "input_schema": {"default": float("inf")},
                    }
                ],
            },
        ),
        (
            "negative-infinity",
            {
                **VALID_PAYLOAD,
                "tools": [
                    {
                        "name": "quote",
                        "description": "d",
                        "input_schema": {"default": float("-inf")},
                    }
                ],
            },
        ),
        ("empty-tool-choice", {**VALID_PAYLOAD, "tool_choice": ""}),
        ("blank-tool-choice", {**VALID_PAYLOAD, "tool_choice": " "}),
        ("non-string-tool-choice", {**VALID_PAYLOAD, "tool_choice": False}),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_invalid_payload_makes_zero_writes_and_zero_delegate_calls(
    name,
    payload,
    session_factory,
):
    del name
    counting_factory = CountingSessionFactory(session_factory)
    delegate = RecordingBackend()
    backend = BudgetedLLMBackend(
        delegate,
        _service(counting_factory),
        provider="anthropic",
        category="chat",
        max_output_tokens=10,
    )

    with pytest.raises(ValueError):
        backend.create(
            **payload,
            request_id="invalid-payload-request",
        )

    assert counting_factory.calls == 0
    assert delegate.calls == []
    with session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(ProviderBudgetDay)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(ProviderReservation)
        ) == 0


def test_anthropic_invalid_payload_does_not_construct_sdk_client(monkeypatch):
    import anthropic

    constructed: list[dict] = []
    monkeypatch.setattr(
        anthropic,
        "Anthropic",
        lambda **kwargs: constructed.append(kwargs) or object(),
    )
    backend = AnthropicBackend("key", "model", 100)

    with pytest.raises(ValueError):
        backend.create(
            system=7,
            messages=[],
            tools=[],
            request_id="invalid-anthropic",
        )

    assert constructed == []


def test_gemini_invalid_payload_does_not_construct_sdk_client(monkeypatch):
    from google import genai

    constructed: list[dict] = []
    monkeypatch.setattr(
        genai,
        "Client",
        lambda **kwargs: constructed.append(kwargs) or object(),
    )
    backend = GeminiBackend("key", "model")

    with pytest.raises(ValueError):
        backend.create(
            system="s",
            messages=[],
            tools=[],
            tool_choice=" ",
            request_id="invalid-gemini",
        )

    assert constructed == []


def test_gemini_nonfinite_transformed_tool_result_does_not_construct_client(
    monkeypatch,
):
    from google import genai

    constructed: list[dict] = []
    monkeypatch.setattr(
        genai,
        "Client",
        lambda **kwargs: constructed.append(kwargs) or object(),
    )
    backend = GeminiBackend("key", "model")
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "quote",
                    "input": {"ticker": "AAPL"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "NaN",
                }
            ],
        },
    ]

    with pytest.raises(ValueError):
        backend.create(
            system="s",
            messages=messages,
            tools=BOUND_TOOLS,
            request_id="nonfinite-gemini-result",
        )

    assert constructed == []


def test_gemini_builder_rejects_nonfinite_transformed_tool_result():
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "quote",
                    "input": {"ticker": "AAPL"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "NaN",
                }
            ],
        },
    ]

    with pytest.raises(ValueError, match="provider payload"):
        build_gemini_payload(
            system="s",
            messages=messages,
            tools=BOUND_TOOLS,
        )


def test_groq_invalid_payload_does_not_construct_sdk_client(monkeypatch):
    import groq

    constructed: list[dict] = []
    monkeypatch.setattr(
        groq,
        "Groq",
        lambda **kwargs: constructed.append(kwargs) or object(),
    )
    backend = GroqBackend("key", "model")

    with pytest.raises(ValueError):
        backend.create(
            system="s",
            messages=[],
            tools=[],
            tool_choice="",
            request_id="invalid-groq",
        )

    assert constructed == []


@pytest.mark.parametrize(
    "usage",
    [
        SimpleNamespace(),
        SimpleNamespace(input_tokens=1),
        SimpleNamespace(output_tokens=1),
        SimpleNamespace(input_tokens=True, output_tokens=1),
        SimpleNamespace(input_tokens=1, output_tokens=False),
        SimpleNamespace(input_tokens="1", output_tokens=1),
        SimpleNamespace(input_tokens=1, output_tokens="1"),
        SimpleNamespace(input_tokens=1.5, output_tokens=1),
        SimpleNamespace(input_tokens=1, output_tokens=1.5),
        SimpleNamespace(input_tokens=-1, output_tokens=1),
        SimpleNamespace(input_tokens=1, output_tokens=-1),
    ],
    ids=[
        "both-missing",
        "output-missing",
        "input-missing",
        "input-boolean",
        "output-boolean",
        "input-string",
        "output-string",
        "input-fractional",
        "output-fractional",
        "input-negative",
        "output-negative",
    ],
)
def test_every_malformed_usage_value_stays_fully_charged_and_unknown(
    usage,
    session_factory,
):
    response = SimpleNamespace(
        content=[],
        stop_reason="end_turn",
        model="test",
        usage=usage,
    )
    backend, delegate, service = _budgeted(
        session_factory,
        response,
        max_output_tokens=10,
    )
    expected_input = resolve_input_estimator(
        "anthropic"
    ).estimate_upper_bound(
        system="s",
        messages=[],
        tools=[],
    )

    returned = backend.create(
        system="s",
        messages=[],
        tools=[],
        request_id="malformed-usage-request",
    )

    assert returned is response
    assert len(delegate.calls) == 1
    status = service.status("anthropic")
    assert status.input_tokens_used == expected_input
    assert status.output_tokens_used == 10
    persisted = _reservation(session_factory)
    assert persisted.state == "unknown"
    assert persisted.input_actual is None
    assert persisted.output_actual is None


@pytest.mark.parametrize(
    "response",
    [
        ResponseUsageRaises(),
        SimpleNamespace(
            content=[],
            stop_reason="end_turn",
            usage=InputUsageRaises(),
        ),
    ],
    ids=["usage-property-raises", "token-property-raises"],
)
def test_usage_access_error_marks_unknown_before_propagating(
    response,
    session_factory,
):
    backend, _delegate, service = _budgeted(session_factory, response)

    with pytest.raises(UsageReadError):
        backend.create(
            system="s",
            messages=[],
            tools=[],
            request_id="usage-access-error",
        )

    assert _reservation(session_factory).state == "unknown"
    assert service.status("anthropic").calls_used == 1


def test_original_delegate_exception_survives_unknown_mark_failure(
    session_factory,
    monkeypatch,
):
    original = RuntimeError("original provider failure")
    delegate = RecordingBackend(error=original)
    service = _service(session_factory)
    backend = BudgetedLLMBackend(
        delegate,
        service,
        provider="anthropic",
        category="chat",
        max_output_tokens=10,
    )

    def fail_unknown(_reservation_id):
        raise ProviderBudgetUnavailable("unknown write failed")

    monkeypatch.setattr(service, "mark_unknown", fail_unknown)

    with pytest.raises(RuntimeError) as caught:
        backend.create(
            system="s",
            messages=[],
            tools=[],
            request_id="original-error-request",
        )

    assert caught.value is original
    persisted = _reservation(session_factory)
    assert persisted.state == "started"
    assert persisted.input_actual is None
    assert persisted.output_actual is None


@pytest.mark.parametrize(
    "usage",
    [
        SimpleNamespace(prompt_tokens=1),
        SimpleNamespace(completion_tokens=1),
        SimpleNamespace(prompt_tokens=True, completion_tokens=1),
        SimpleNamespace(prompt_tokens=1.5, completion_tokens=1),
        SimpleNamespace(prompt_tokens=-1, completion_tokens=1),
    ],
    ids=[
        "openai-output-missing",
        "openai-input-missing",
        "openai-boolean",
        "openai-fractional",
        "openai-negative",
    ],
)
def test_openai_normalizer_never_fabricates_partial_or_malformed_usage(usage):
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="ok", tool_calls=None)
            )
        ],
        usage=usage,
        model="test",
    )

    assert from_openai(response).usage is None


@pytest.mark.parametrize(
    "usage",
    [
        SimpleNamespace(prompt_token_count=1),
        SimpleNamespace(candidates_token_count=1),
        SimpleNamespace(prompt_token_count=True, candidates_token_count=1),
        SimpleNamespace(prompt_token_count=1.5, candidates_token_count=1),
        SimpleNamespace(prompt_token_count=-1, candidates_token_count=1),
    ],
    ids=[
        "gemini-output-missing",
        "gemini-input-missing",
        "gemini-boolean",
        "gemini-fractional",
        "gemini-negative",
    ],
)
def test_gemini_normalizer_never_fabricates_partial_or_malformed_usage(usage):
    response = SimpleNamespace(
        candidates=[],
        usage_metadata=usage,
        model_version="test",
    )

    assert from_gemini(response).usage is None


@pytest.mark.parametrize("corrupt_field", ["calls_used", "input_tokens_used", "output_tokens_used"])
def test_reserve_fails_closed_on_negative_loaded_day_counter(
    corrupt_field,
    engine,
    session_factory,
):
    service = _service(session_factory)
    service.reserve(
        provider="anthropic",
        category="chat",
        request_id="before-corruption",
        input_tokens=5,
        output_tokens=5,
        now=NOW,
    )
    _corrupt(
        engine,
        f"UPDATE provider_budget_days SET {corrupt_field} = -1 "
        "WHERE provider = 'anthropic'",
    )

    with pytest.raises(ProviderBudgetUnavailable, match="corrupt"):
        service.reserve(
            provider="anthropic",
            category="chat",
            request_id="after-corruption",
            input_tokens=1,
            output_tokens=1,
            now=NOW,
        )

    with session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(ProviderReservation)
        ) == 1


def test_reserve_fails_closed_on_corrupt_nonexpired_reservation(
    engine,
    session_factory,
):
    service = _service(session_factory)
    reservation = service.reserve(
        provider="anthropic",
        category="chat",
        request_id="corrupt-existing-reservation",
        input_tokens=5,
        output_tokens=5,
        now=NOW,
    )
    _corrupt(
        engine,
        "UPDATE provider_reservations SET output_reserved = -1 "
        "WHERE reservation_id = ?",
        (reservation.reservation_id,),
    )

    with pytest.raises(ProviderBudgetUnavailable, match="corrupt"):
        service.reserve(
            provider="anthropic",
            category="chat",
            request_id="blocked-by-corrupt-reservation",
            input_tokens=1,
            output_tokens=1,
            now=NOW,
        )

    with session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(ProviderReservation)
        ) == 1


def test_settle_fails_closed_on_corrupt_loaded_reservation(
    engine,
    session_factory,
):
    service = _service(session_factory)
    reservation = service.reserve(
        provider="anthropic",
        category="chat",
        request_id="corrupt-settlement",
        input_tokens=5,
        output_tokens=5,
        now=NOW,
    )
    service.mark_started(reservation.reservation_id, now=NOW)
    _corrupt(
        engine,
        "UPDATE provider_reservations SET input_reserved = -1 "
        "WHERE reservation_id = ?",
        (reservation.reservation_id,),
    )

    with pytest.raises(ProviderBudgetUnavailable, match="corrupt"):
        service.settle(
            reservation.reservation_id,
            input_tokens=1,
            output_tokens=1,
            now=NOW,
        )

    assert _reservation(session_factory).state == "started"


def test_release_fails_closed_instead_of_clamping_corrupt_counters(
    engine,
    session_factory,
):
    service = _service(
        session_factory,
        limits=BudgetLimits(
            calls=2,
            input_tokens=20,
            output_tokens=20,
            reservation_ttl_seconds=1,
        ),
    )
    reservation = service.reserve(
        provider="anthropic",
        category="chat",
        request_id="corrupt-release",
        input_tokens=5,
        output_tokens=5,
        now=NOW,
    )
    _corrupt(
        engine,
        "UPDATE provider_budget_days SET input_tokens_used = 2 "
        "WHERE provider = 'anthropic'",
    )

    with pytest.raises(ProviderBudgetUnavailable, match="corrupt"):
        service.release_expired_unstarted(NOW + timedelta(seconds=1))

    assert _reservation(session_factory).state == "reserved"
    with session_factory() as session:
        day = session.get(
            ProviderBudgetDay,
            ("anthropic", NOW.date()),
        )
    assert day.input_tokens_used == 2
    assert reservation.input_reserved == 5


def test_release_fails_closed_on_invalid_persisted_state(
    engine,
    session_factory,
):
    service = _service(session_factory)
    reservation = service.reserve(
        provider="anthropic",
        category="chat",
        request_id="invalid-state-release",
        input_tokens=1,
        output_tokens=1,
        now=NOW,
    )
    _corrupt(
        engine,
        "UPDATE provider_reservations SET state = 'corrupt' "
        "WHERE reservation_id = ?",
        (reservation.reservation_id,),
    )

    with pytest.raises(ProviderBudgetUnavailable, match="corrupt"):
        service.release_expired_unstarted(NOW + timedelta(days=1))

    assert _reservation(session_factory).state == "corrupt"


def test_status_fails_closed_on_negative_loaded_counter(
    engine,
    session_factory,
):
    service = _service(session_factory)
    service.reserve(
        provider="anthropic",
        category="chat",
        request_id="corrupt-status",
        input_tokens=1,
        output_tokens=1,
        now=NOW,
    )
    _corrupt(
        engine,
        "UPDATE provider_budget_days SET output_tokens_used = -1 "
        "WHERE provider = 'anthropic'",
    )

    with pytest.raises(ProviderBudgetUnavailable, match="corrupt"):
        service.status("anthropic", now=NOW)


@pytest.mark.parametrize("invalid_kind", ["negative-day", "invalid-state"])
def test_orm_metadata_mirrors_provider_budget_checks(
    invalid_kind,
    session_factory,
):
    with session_factory() as session:
        if invalid_kind == "negative-day":
            session.add(
                ProviderBudgetDay(
                    provider="anthropic",
                    budget_day=NOW.date(),
                    calls_used=-1,
                    input_tokens_used=0,
                    output_tokens_used=0,
                )
            )
        else:
            session.add(
                ProviderReservation(
                    reservation_id="invalid-state-row",
                    provider="anthropic",
                    category="chat",
                    request_id="invalid-state-row",
                    budget_day=NOW.date(),
                    state="corrupt",
                    input_reserved=0,
                    output_reserved=0,
                    expires_at=NOW + timedelta(minutes=5),
                )
            )
        with pytest.raises(IntegrityError):
            session.commit()


VALID_PRICE = {
    "model": "model-a",
    "effective_date": date(2026, 7, 27),
    "input_usd_per_million": Decimal("2.00"),
    "output_usd_per_million": Decimal("8.00"),
}


@pytest.mark.parametrize(
    ("key", "overrides"),
    [
        ("", {}),
        ("test:model-a", {"model": ""}),
        ("test:model-a", {"model": 7}),
        ("test:model-a", {"effective_date": "2026-07-27"}),
        (
            "test:model-a",
            {"effective_date": datetime(2026, 7, 27, tzinfo=UTC)},
        ),
        (
            "test:model-a",
            {"input_usd_per_million": Decimal("-1")},
        ),
        (
            "test:model-a",
            {"input_usd_per_million": Decimal("NaN")},
        ),
        (
            "test:model-a",
            {"output_usd_per_million": Decimal("Infinity")},
        ),
        (
            "test:model-a",
            {"output_usd_per_million": Decimal("-Infinity")},
        ),
    ],
    ids=[
        "empty-key",
        "empty-model",
        "non-string-model",
        "string-effective-date",
        "datetime-effective-date",
        "negative-rate",
        "nan-rate",
        "positive-infinite-rate",
        "negative-infinite-rate",
    ],
)
def test_invalid_direct_price_metadata_is_rejected_at_service_construction(
    key,
    overrides,
    session_factory,
):
    metadata = {**VALID_PRICE, **overrides}
    counting_factory = CountingSessionFactory(session_factory)

    with pytest.raises(ValueError, match="price"):
        ProviderBudgetService(
            counting_factory,
            LIMITS,
            prices={key: metadata},
            clock=lambda: NOW,
        )

    assert counting_factory.calls == 0


def test_falsey_non_mapping_price_input_is_rejected(session_factory):
    with pytest.raises(ValueError, match="price"):
        ProviderBudgetService(
            session_factory,
            LIMITS,
            prices=[],
            clock=lambda: NOW,
        )


def test_duplicate_effective_dated_price_is_rejected(session_factory):
    with pytest.raises(ValueError, match="price"):
        ProviderBudgetService(
            session_factory,
            LIMITS,
            prices={
                "anthropic:first": VALID_PRICE,
                "anthropic:second": dict(VALID_PRICE),
            },
            clock=lambda: NOW,
        )


def test_missing_applicable_price_is_explicitly_unavailable(session_factory):
    service = _service(session_factory)

    status = service.status(
        "anthropic",
        model="unpriced-model",
        now=NOW,
    )

    assert status.estimated_usd is None
    assert status.price_model == ""
    assert status.price_effective_date is None


def test_valid_applicable_zero_usage_has_exact_zero_estimate(session_factory):
    service = _service(
        session_factory,
        prices={"anthropic:model-a": VALID_PRICE},
    )

    status = service.status(
        "anthropic",
        model="model-a",
        now=NOW,
    )

    assert status.estimated_usd == Decimal("0")
    assert status.price_model == "model-a"
    assert status.price_effective_date == NOW.date()
