from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_assistant.rules.models import (
    PriceCondition,
    RuleAction,
    RuleCommand,
    RuleKind,
    TimeCondition,
    TrailingCondition,
)


def _command(**updates) -> RuleCommand:
    payload = {
        "ticker": "AAPL",
        "kind": "price",
        "condition": {"type": "price", "direction": "below", "price": "175"},
        "action": {
            "side": "buy",
            "notional": "100",
            "order_type": "market",
        },
    }
    payload.update(updates)
    return RuleCommand.model_validate(payload)


def test_unknown_rule_condition_is_rejected():
    with pytest.raises(ValidationError):
        RuleCommand.model_validate(
            {
                "ticker": "AAPL",
                "kind": "price",
                "condition": {"type": "mystery", "value": 1},
                "action": {
                    "side": "buy",
                    "notional": "100",
                    "order_type": "market",
                },
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"side": "buy", "order_type": "market"},
        {
            "side": "buy",
            "order_type": "market",
            "qty": "1",
            "notional": "100",
        },
    ],
)
def test_rule_action_requires_exactly_qty_or_notional(payload):
    with pytest.raises(ValidationError, match="exactly one"):
        RuleAction.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"side": "buy", "order_type": "limit", "qty": "1"},
        {
            "side": "buy",
            "order_type": "market",
            "qty": "1",
            "limit_price": "100",
        },
    ],
)
def test_rule_action_enforces_limit_price_shape(payload):
    with pytest.raises(ValidationError, match="limit_price"):
        RuleAction.model_validate(payload)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            PriceCondition,
            {
                "type": "price",
                "direction": "below",
                "price": "10",
                "unknown": True,
            },
        ),
        (
            TrailingCondition,
            {"type": "trailing", "percent": "10", "unknown": True},
        ),
        (
            TimeCondition,
            {
                "type": "time",
                "deadline": "2026-07-25T00:00:00Z",
                "unknown": True,
            },
        ),
        (
            RuleAction,
            {
                "side": "buy",
                "order_type": "market",
                "qty": "1",
                "unknown": True,
            },
        ),
    ],
)
def test_all_nested_rule_models_forbid_extra_fields(model, payload):
    with pytest.raises(ValidationError, match="Extra inputs"):
        model.model_validate(payload)


def test_rule_command_forbids_extra_fields_and_normalizes_ticker():
    with pytest.raises(ValidationError, match="Extra inputs"):
        _command(unknown=True)

    assert _command(ticker="aapl").ticker == "AAPL"


@pytest.mark.parametrize(
    ("kind", "condition"),
    [
        (
            RuleKind.PRICE,
            {"type": "price", "direction": "below", "price": "1"},
        ),
        (
            RuleKind.ENTRY,
            {"type": "price", "direction": "above", "price": "1"},
        ),
        (
            RuleKind.TARGET,
            {"type": "price", "direction": "above", "price": "1"},
        ),
        (
            RuleKind.STOP,
            {"type": "price", "direction": "below", "price": "1"},
        ),
        (RuleKind.TRAILING, {"type": "trailing", "percent": "5"}),
        (
            RuleKind.TIME,
            {"type": "time", "deadline": "2026-07-25T00:00:00Z"},
        ),
    ],
)
def test_only_kind_appropriate_discriminated_conditions_are_accepted(kind, condition):
    assert _command(kind=kind, condition=condition).kind is kind


def test_condition_kind_mismatch_is_rejected():
    with pytest.raises(ValidationError, match="condition type"):
        _command(kind="time")


def test_preapproval_is_rejected_by_typed_model_and_application_boundary(make_service):
    svc = make_service()

    with pytest.raises(ValidationError, match="pre_approved"):
        _command(pre_approved=True)

    command = _command().model_copy(update={"pre_approved": True})
    with pytest.raises(ValueError, match="pre_approved"):
        svc.rule_application.create_rule(command)


def test_rule_application_rejects_raw_unknown_json_without_persisting(make_service):
    svc = make_service()

    with pytest.raises(ValidationError):
        svc.create_conditional_rule(
            "AAPL",
            {"type": "mystery", "value": 1},
            {"side": "buy", "order_type": "market", "notional": "100"},
        )
    assert svc.list_rules() == []


def test_rule_command_types_fraction_and_high_water_mark():
    command = _command(fraction="0.5", high_water_mark="123.45")

    assert command.fraction == Decimal("0.5")
    assert command.high_water_mark == Decimal("123.45")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fraction", "0.0000001"),
        ("fraction", "0.1234567"),
        ("high_water_mark", "0.0000001"),
        ("high_water_mark", "100000000000000"),
    ],
)
def test_rule_command_rejects_numeric_values_the_database_cannot_preserve(
    field, value
):
    with pytest.raises(ValidationError):
        _command(**{field: value})
