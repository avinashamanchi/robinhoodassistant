"""Durable startup reconciliation gates every production submission."""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import (
    OrderRequest,
    OrderSide,
    OrderType,
)
from trading_assistant.db.models import StartupReconciliationState
from trading_assistant.orders.startup import StartupReconciliationFailed
from trading_assistant.risk.clock import FakeClock
from trading_assistant.service import TradingService


class CountingBroker(MockBroker):
    def __init__(self) -> None:
        super().__init__()
        self.submit_calls = 0

    def submit_order(self, order):
        self.submit_calls += 1
        return super().submit_order(order)


def _service(
    broker,
    session_factory,
    app_config,
    *,
    startup_required: bool,
) -> TradingService:
    broker.set_price("AAPL", Decimal("100"))
    return TradingService(
        broker,
        session_factory,
        app_config,
        FakeClock(is_open=True),
        require_startup_reconciliation=startup_required,
    )


def _context(label: str) -> dict[str, str]:
    return {
        "actor": "runtime:test",
        "reason": f"startup reconciliation {label}",
        "request_id": f"startup-reconciliation-{label}",
    }


def _propose(service: TradingService, key: str) -> int:
    return service.propose_order(
        "AAPL",
        "buy",
        "market",
        notional="10",
        idempotency_key=key,
        actor="operator:test",
        reason="proposal created before process restart",
        request_id=f"proposal-{key}",
    )["order_id"]


def test_unknown_remote_open_order_cannot_look_reconciled_or_submit(
    app_config,
    session_factory,
):
    broker = CountingBroker()
    previous = _service(
        broker,
        session_factory,
        app_config,
        startup_required=False,
    )
    local_order_id = _propose(previous, "local-after-restart")
    broker.submit_order(
        OrderRequest(
            ticker="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            idempotency_key="unknown-remote-order",
            notional=Decimal("10000"),
        )
    )

    restarted = _service(
        broker,
        session_factory,
        app_config,
        startup_required=True,
    )
    generation = restarted.require_startup_reconciliation(
        **_context("unknown-remote-required")
    )
    snapshot = restarted.snapshot_service.assemble_for_confirmation(
        "AAPL",
        exclude_order_id=local_order_id,
    )

    assert snapshot.broker_reconciled is False
    approval = restarted.approve_order(
        local_order_id,
        actor="operator:test",
        reason="must remain blocked before broker truth",
        request_id="blocked-before-startup-reconciliation",
    )
    assert approval["status"] == "rejected"
    assert broker.submit_calls == 1

    with pytest.raises(
        StartupReconciliationFailed,
        match="remote open order",
    ):
        restarted.reconcile_startup_epoch(
            generation,
            **_context("unknown-remote-run"),
        )
    with session_factory() as session:
        state = session.get(
            StartupReconciliationState,
            broker.reconciliation_key,
        )
        assert state is not None
        assert state.status == "failed"
        assert state.completed_generation < state.generation


def test_successful_full_startup_reconciliation_unlocks_exact_generation(
    app_config,
    session_factory,
):
    broker = CountingBroker()
    previous = _service(
        broker,
        session_factory,
        app_config,
        startup_required=False,
    )
    order_id = _propose(previous, "submit-after-startup")
    restarted = _service(
        broker,
        session_factory,
        app_config,
        startup_required=True,
    )
    generation = restarted.require_startup_reconciliation(
        **_context("successful-required")
    )

    report = restarted.reconcile_startup_epoch(
        generation,
        **_context("successful-run"),
    )

    assert report["ready"] is True
    assert report["generation"] == generation
    snapshot = restarted.snapshot_service.assemble_for_confirmation(
        "AAPL",
        exclude_order_id=order_id,
    )
    assert snapshot.broker_reconciled is True

    approval = restarted.approve_order(
        order_id,
        actor="operator:test",
        reason="explicit approval after startup truth",
        request_id="approval-after-startup-reconciliation",
    )
    assert approval["status"] == "submitted"
    assert broker.submit_calls == 1


def test_new_process_generation_relocks_and_stale_completion_cannot_clear_it(
    app_config,
    session_factory,
):
    broker = CountingBroker()
    first = _service(
        broker,
        session_factory,
        app_config,
        startup_required=True,
    )
    first_generation = first.require_startup_reconciliation(
        **_context("first-required")
    )
    first.reconcile_startup_epoch(
        first_generation,
        **_context("first-run"),
    )
    assert first.startup_reconciliation.is_current(first_generation)

    second = _service(
        broker,
        session_factory,
        app_config,
        startup_required=True,
    )
    second_generation = second.require_startup_reconciliation(
        **_context("second-required")
    )

    assert second_generation == first_generation + 1
    assert second.startup_reconciliation.is_current() is False
    assert (
        first.startup_reconciliation.complete(
            first_generation,
            evidence={"stale": True},
            **_context("stale-completion"),
        )
        is False
    )
    assert second.startup_reconciliation.is_current() is False

    second.reconcile_startup_epoch(
        second_generation,
        **_context("second-run"),
    )
    assert second.startup_reconciliation.is_current(
        second_generation
    )
