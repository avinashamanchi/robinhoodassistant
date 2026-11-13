from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from trading_assistant.db.models import (
    ProviderBudgetDay,
    ProviderReservation,
)
from trading_assistant.llm.budget import (
    BudgetLimits,
    ProviderBudgetService,
    ProviderBudgetUnavailable,
)
from trading_assistant.llm.factory import resolve_input_estimator


UTC = timezone.utc
NOW = datetime(2026, 7, 27, 12, tzinfo=UTC)
RECONCILIATION_CODE = "provider_usage_over_reservation"
SYSTEM = "System café"
MESSAGES = [{"role": "user", "content": "quote AAPL"}]
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

ANTHROPIC_BASE = {
    "system": SYSTEM,
    "messages": MESSAGES,
    "tools": TOOLS,
}
GROQ_BASE = {
    "messages": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "quote AAPL"},
    ],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "quote",
                "description": "Get quote",
                "parameters": {
                    "type": "object",
                    "properties": {"ticker": {"type": "string"}},
                },
            },
        }
    ],
}
GEMINI_BASE = {
    "system_instruction": SYSTEM,
    "contents": [
        {
            "role": "user",
            "parts": [{"text": "quote AAPL"}],
        }
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
                            "ticker": {"type": "string"}
                        },
                    },
                }
            ]
        }
    ],
}

EXPECTED_PROVIDER_PAYLOADS = {
    "anthropic": {
        "auto": {
            **ANTHROPIC_BASE,
            "tool_choice": {"type": "auto"},
        },
        "any": {
            **ANTHROPIC_BASE,
            "tool_choice": {"type": "any"},
        },
    },
    "gemini": {
        "auto": {
            **GEMINI_BASE,
            "tool_config": {
                "function_calling_config": {"mode": "AUTO"}
            },
        },
        "any": {
            **GEMINI_BASE,
            "tool_config": {
                "function_calling_config": {"mode": "ANY"}
            },
        },
    },
    "groq": {
        "auto": {
            **GROQ_BASE,
            "tool_choice": "auto",
        },
        "any": {
            **GROQ_BASE,
            "tool_choice": "required",
        },
    },
}


def _service(session_factory) -> ProviderBudgetService:
    return ProviderBudgetService(
        session_factory,
        BudgetLimits(
            calls=20,
            input_tokens=1_000,
            output_tokens=1_000,
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


def _reserve_and_settle(
    service: ProviderBudgetService,
    *,
    request_id: str,
    input_reserved: int = 5,
    output_reserved: int = 5,
    input_actual: int = 3,
    output_actual: int = 2,
) -> str:
    reservation = service.reserve(
        provider="anthropic",
        category="chat",
        request_id=request_id,
        input_tokens=input_reserved,
        output_tokens=output_reserved,
        now=NOW,
    )
    service.mark_started(reservation.reservation_id, now=NOW)
    service.settle(
        reservation.reservation_id,
        input_tokens=input_actual,
        output_tokens=output_actual,
        now=NOW,
    )
    return reservation.reservation_id


@pytest.mark.parametrize("provider", ["anthropic", "gemini", "groq"])
@pytest.mark.parametrize("tool_choice", ["auto", "any"])
def test_estimator_covers_each_independent_valid_tool_choice_payload(
    provider,
    tool_choice,
):
    expected_bytes = len(
        json.dumps(
            EXPECTED_PROVIDER_PAYLOADS[provider][tool_choice],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )

    estimate = resolve_input_estimator(
        provider
    ).estimate_upper_bound(
        system=SYSTEM,
        messages=MESSAGES,
        tools=TOOLS,
    )

    assert estimate >= expected_bytes


def test_reserve_rejects_overrun_after_flag_and_code_are_cleared(
    engine,
    session_factory,
):
    service = _service(session_factory)
    _reserve_and_settle(
        service,
        request_id="overrun-cleared-before-reserve",
        input_actual=6,
        output_actual=5,
    )
    _corrupt(
        engine,
        "UPDATE provider_budget_days "
        "SET reconciliation_required = 0, reconciliation_code = '' "
        "WHERE provider = 'anthropic'",
    )

    with pytest.raises(ProviderBudgetUnavailable, match="corrupt"):
        service.reserve(
            provider="anthropic",
            category="chat",
            request_id="must-not-authorize-after-cleared-overrun",
            input_tokens=1,
            output_tokens=1,
            now=NOW,
        )

    with session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(ProviderReservation)
        ) == 1


def test_status_rejects_overrun_after_reconciliation_flag_is_cleared(
    engine,
    session_factory,
):
    service = _service(session_factory)
    _reserve_and_settle(
        service,
        request_id="overrun-flag-cleared",
        input_actual=6,
        output_actual=5,
    )
    _corrupt(
        engine,
        "UPDATE provider_budget_days "
        "SET reconciliation_required = 0 "
        "WHERE provider = 'anthropic'",
    )

    with pytest.raises(ProviderBudgetUnavailable, match="corrupt"):
        service.status("anthropic", now=NOW)


def test_status_rejects_overrun_after_reconciliation_code_is_cleared(
    engine,
    session_factory,
):
    service = _service(session_factory)
    _reserve_and_settle(
        service,
        request_id="overrun-code-cleared",
        input_actual=6,
        output_actual=5,
    )
    _corrupt(
        engine,
        "UPDATE provider_budget_days "
        "SET reconciliation_code = '' "
        "WHERE provider = 'anthropic'",
    )

    with pytest.raises(ProviderBudgetUnavailable, match="corrupt"):
        service.status("anthropic", now=NOW)


def test_transition_rejects_overrun_with_changed_reconciliation_code(
    engine,
    session_factory,
):
    service = _service(session_factory)
    overrun = service.reserve(
        provider="anthropic",
        category="chat",
        request_id="overrun-before-changed-code",
        input_tokens=5,
        output_tokens=5,
        now=NOW,
    )
    target = service.reserve(
        provider="anthropic",
        category="chat",
        request_id="target-after-changed-code",
        input_tokens=1,
        output_tokens=1,
        now=NOW,
    )
    service.mark_started(overrun.reservation_id, now=NOW)
    service.settle(
        overrun.reservation_id,
        input_tokens=6,
        output_tokens=5,
        now=NOW,
    )
    _corrupt(
        engine,
        "UPDATE provider_budget_days "
        "SET reconciliation_code = 'unknown_reconciliation' "
        "WHERE provider = 'anthropic'",
    )

    with pytest.raises(ProviderBudgetUnavailable, match="corrupt"):
        service.mark_started(target.reservation_id, now=NOW)

    with session_factory() as session:
        persisted = session.get(
            ProviderReservation,
            target.reservation_id,
        )
    assert persisted.state == "reserved"
    assert persisted.started_at is None


def test_status_rejects_stale_reconciliation_without_overrun(
    engine,
    session_factory,
):
    service = _service(session_factory)
    _reserve_and_settle(
        service,
        request_id="normal-usage-before-stale-reconciliation",
    )
    _corrupt(
        engine,
        "UPDATE provider_budget_days "
        "SET reconciliation_required = 1, reconciliation_code = ? "
        "WHERE provider = 'anthropic'",
        (RECONCILIATION_CODE,),
    )

    with pytest.raises(ProviderBudgetUnavailable, match="corrupt"):
        service.status("anthropic", now=NOW)
