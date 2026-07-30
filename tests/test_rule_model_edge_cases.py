from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_assistant.rules.models import (
    RuleCommand,
    normalize_computed_order_decimal,
)


def _payload(**updates):
    payload = {
        "ticker": "AAPL",
        "kind": "price",
        "condition": {
            "type": "price",
            "direction": "below",
            "price": "175",
        },
        "action": {
            "side": "buy",
            "notional": "100",
            "order_type": "market",
        },
    }
    payload.update(updates)
    return payload


@pytest.mark.parametrize(
    "value",
    [
        Decimal(0),
        Decimal("-1"),
        Decimal("NaN"),
        Decimal("Infinity"),
    ],
)
def test_computed_order_decimal_rejects_nonpositive_or_nonfinite_values(value):
    with pytest.raises(
        ValueError,
        match="finite and positive",
    ):
        normalize_computed_order_decimal(value)


def test_computed_order_decimal_rejects_unpersistable_whole_precision():
    with pytest.raises(
        ValueError,
        match="exceeds persisted precision",
    ):
        normalize_computed_order_decimal(
            Decimal("100000000000000"),
        )


def test_rule_command_rejects_whitespace_only_normalized_identifiers():
    with pytest.raises(ValidationError, match="ticker must be non-empty"):
        RuleCommand.model_validate(_payload(ticker=" "))

    with pytest.raises(ValidationError, match="group_key must be non-empty"):
        RuleCommand.model_validate(_payload(group_key=" "))


def test_entry_fill_activation_is_limited_to_exit_rules():
    with pytest.raises(
        ValidationError,
        match="only exit rules",
    ):
        RuleCommand.model_validate(
            _payload(activation="on_entry_fill")
        )


def test_only_intermediate_targets_can_remain_active_after_trigger():
    with pytest.raises(
        ValidationError,
        match="only an intermediate target",
    ):
        RuleCommand.model_validate(
            _payload(terminal_on_trigger=False)
        )
