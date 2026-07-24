"""A4: explicit order state machine — legal transitions pass, illegal raise."""

from __future__ import annotations

import pytest

from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import (
    IllegalStateTransition,
    Order,
    OrderStateMachine,
    TERMINAL_STATES,
)

S = OrderStatus

LEGAL = [
    (S.PROPOSED, S.APPROVAL_RECORDED),
    (S.PROPOSED, S.REJECTED),
    (S.PROPOSED, S.EXPIRED),
    (S.PROPOSED, S.CANCELED),
    (S.APPROVAL_RECORDED, S.SUBMITTING),
    (S.APPROVAL_RECORDED, S.EXPIRED),
    (S.SUBMITTING, S.SUBMITTED),
    (S.SUBMITTING, S.PARTIALLY_FILLED),
    (S.SUBMITTING, S.FILLED),
    (S.SUBMITTING, S.ACCEPTANCE_UNKNOWN),
    (S.SUBMITTING, S.CANCELED),
    (S.SUBMITTING, S.REJECTED),
    (S.SUBMITTING, S.EXPIRED),
    (S.ACCEPTANCE_UNKNOWN, S.SUBMITTED),
    (S.ACCEPTANCE_UNKNOWN, S.PARTIALLY_FILLED),
    (S.ACCEPTANCE_UNKNOWN, S.FILLED),
    (S.ACCEPTANCE_UNKNOWN, S.REJECTED),
    (S.ACCEPTANCE_UNKNOWN, S.CANCELED),
    (S.SUBMITTED, S.PARTIALLY_FILLED),
    (S.SUBMITTED, S.FILLED),
    (S.SUBMITTED, S.CANCELED),
    (S.SUBMITTED, S.REJECTED),
    (S.PARTIALLY_FILLED, S.PARTIALLY_FILLED),
    (S.PARTIALLY_FILLED, S.FILLED),
    (S.PARTIALLY_FILLED, S.CANCELED),
    (S.PARTIALLY_FILLED, S.EXPIRED),
]

ILLEGAL = [
    (S.PROPOSED, S.SUBMITTED),      # cannot skip approval
    (S.PROPOSED, S.FILLED),
    (S.APPROVAL_RECORDED, S.FILLED),  # must claim submission first
    (S.APPROVED, S.SUBMITTED),  # legacy status is deserialization-only
    (S.FILLED, S.CANCELED),         # terminal
    (S.EXPIRED, S.APPROVED),        # terminal
    (S.REJECTED, S.APPROVED),       # terminal
    (S.CANCELED, S.SUBMITTED),      # terminal
]


def _order(status: OrderStatus) -> Order:
    return Order(
        idempotency_key=f"k-{status.value}",
        ticker="AAPL",
        side="buy",
        order_type="market",
        status=status.value,
    )


@pytest.mark.parametrize("current,new", LEGAL)
def test_legal_transitions(current, new):
    order = _order(current)
    OrderStateMachine.transition(order, new)
    assert order.status == new.value


@pytest.mark.parametrize("current,new", ILLEGAL)
def test_illegal_transitions_raise(current, new):
    order = _order(current)
    with pytest.raises(IllegalStateTransition):
        OrderStateMachine.transition(order, new)


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES, key=lambda s: s.value))
def test_terminal_states_have_no_exits(terminal):
    for target in OrderStatus:
        assert not OrderStateMachine.can_transition(terminal, target)


def test_legacy_approved_has_no_runtime_transitions():
    for target in OrderStatus:
        assert not OrderStateMachine.can_transition(OrderStatus.APPROVED, target)
