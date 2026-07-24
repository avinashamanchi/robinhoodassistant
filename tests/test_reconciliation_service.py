"""Broker-truth reconciliation and truthful panic reporting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select

from trading_assistant.broker.alpaca import AlpacaBroker
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import (
    BrokerFill,
    FILL_ECONOMIC_QUANTUM,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from trading_assistant.db import models as db_models
from trading_assistant.db.models import (
    FILL_RECONCILIATION_QUARANTINED,
    FILL_RECONCILIATION_REQUIRED,
    CircuitBreakerState,
    Fill,
    Order,
    utcnow,
)
from trading_assistant.orders.application import ApprovalCommand
from trading_assistant.risk.breakers import BreakerScope
from trading_assistant.risk.staleness import (
    DEFAULT_MAX_FUTURE_SKEW_SECONDS,
)


def _approved_order_id(service) -> int:
    order_id = service.propose_order(
        "AAPL", "buy", "market", notional="100"
    )["order_id"]
    service.order_application.approve(
        ApprovalCommand(order_id, "operator:avi", "reviewed", utcnow())
    )
    return order_id


def _submitted_order_id(service) -> int:
    order_id = service.propose_order(
        "AAPL", "buy", "market", notional="100"
    )["order_id"]
    service.approve_order(
        order_id, actor="operator:avi", reason="reviewed for submission"
    )
    return order_id


class AcceptThenDisconnectBroker(MockBroker):
    def submit_order(self, order):
        super().submit_order(order)
        raise ConnectionError("response lost after acceptance")


class CancelFailsBroker(MockBroker):
    def cancel_order(self, order_id):
        raise ConnectionError("broker unavailable")

    def get_order_status(self, order_id):
        raise ConnectionError("broker unavailable")


def _acceptance_unknown_with_exact_local_fill_ahead(service, broker) -> int:
    broker.set_price("AAPL", Decimal("100"))
    order_id = _approved_order_id(service)
    service.order_submission.submit(order_id)
    with service.session_factory() as session:
        session.add(
            Fill(
                order_id=order_id,
                ticker="AAPL",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("100"),
                broker_fill_id=f"acceptance-local-fill-{order_id}",
                filled_at=utcnow(),
            )
        )
        session.commit()
    return order_id


def test_reconcile_unknown_finds_remote_acceptance(make_service):
    broker = AcceptThenDisconnectBroker()
    broker.set_price("AAPL", Decimal("100"))
    service = make_service(broker=broker)
    order_id = _approved_order_id(service)
    service.order_submission.submit(order_id)

    report = service.reconciliation.reconcile()

    assert report.resolved_unknown == 1
    with service.session_factory() as session:
        row = session.get(Order, order_id)
        assert row.status == "submitted"
        assert row.broker_order_id is not None


@pytest.mark.parametrize(
    ("broker_order_id", "returned_client_id"),
    [
        (None, "local-client-id"),
        ("", "local-client-id"),
        ("   ", "local-client-id"),
        ("remote-order-id", ""),
        ("remote-order-id", "wrong-client-id"),
    ],
)
def test_acceptance_unknown_rejects_invalid_remote_identity_durably(
    make_service,
    broker_order_id,
    returned_client_id,
):
    broker = AcceptThenDisconnectBroker()
    broker.set_price("AAPL", Decimal("100"))
    service = make_service(broker=broker)
    order_id = service.propose_order(
        "AAPL",
        "buy",
        "market",
        notional="100",
        idempotency_key="local-client-id",
    )["order_id"]
    service.order_application.approve(
        ApprovalCommand(
            order_id,
            "operator:avi",
            "reviewed identity recovery",
            utcnow(),
        )
    )
    service.order_submission.submit(order_id)
    broker._orders_by_key["local-client-id"] = OrderResult(
        returned_client_id,
        broker_order_id,
        OrderStatus.SUBMITTED,
    )

    report = service.reconciliation.reconcile()
    restarted = make_service(broker=broker)
    snapshot = restarted.snapshot_service.assemble_for_execution("AAPL")

    assert report.resolved_unknown == 0
    assert report.unresolved_unknown == (order_id,)
    assert any("invalid broker identity" in item for item in report.broker_drift)
    assert restarted.breakers.is_tripped(BreakerScope.broker_drift()) is True
    assert snapshot.broker_reconciled is False
    assert snapshot.daily_pnl_complete is False
    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.ACCEPTANCE_UNKNOWN.value
        assert order.broker_order_id is None
        assert order.acceptance_state == "fill_reconcile_required"
        assert order.last_error_code == "invalid_broker_identity"


@pytest.mark.parametrize(
    ("remote_status", "filled_qty"),
    [
        (OrderStatus.PARTIALLY_FILLED, Decimal("0.5")),
        (OrderStatus.FILLED, Decimal("1")),
    ],
)
def test_reconcile_unknown_fill_latch_survives_restart_until_exact_activity(
    make_service,
    remote_status,
    filled_qty,
):
    class FilledThenDisconnectBroker(MockBroker):
        def __init__(self):
            super().__init__()
            self.activities = []

        def submit_order(self, order):
            accepted = super().submit_order(order)
            filled = OrderResult(
                accepted.idempotency_key,
                accepted.broker_order_id,
                remote_status,
                filled_qty=filled_qty,
                avg_fill_price=Decimal("100"),
            )
            self._orders_by_key[order.idempotency_key] = filled
            self._orders_by_id[accepted.broker_order_id] = filled
            raise ConnectionError("response lost after fill")

        def get_fill_activities(self, after=None):
            return list(self.activities)

    broker = FilledThenDisconnectBroker()
    broker.set_price("AAPL", Decimal("100"))
    service = make_service(broker=broker)
    order_id = _approved_order_id(service)
    service.order_submission.submit(order_id)

    report = service.reconciliation.reconcile()

    assert report.resolved_unknown == 1
    assert report.inserted_fills == 0
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == remote_status.value
        assert order.acceptance_state == "fill_reconcile_required"
        broker_order_id = order.broker_order_id
        assert session.scalar(
            select(func.count()).select_from(Fill).where(
                Fill.order_id == order_id
            )
        ) == 0

    restarted = make_service(broker=broker)
    incomplete = restarted.snapshot_service.assemble_for_execution("AAPL")
    assert incomplete.broker_reconciled is False
    assert incomplete.daily_pnl_complete is False

    broker.activities = [
        BrokerFill(
            broker_fill_id=f"unknown-acceptance-{remote_status.value}",
            broker_order_id=broker_order_id,
            ticker="AAPL",
            side="buy",
            qty=filled_qty,
            price=Decimal("100"),
            filled_at=utcnow(),
        )
    ]
    exact = restarted.reconciliation.reconcile()

    assert exact.inserted_fills == 1
    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        fill = session.scalar(
            select(Fill).where(Fill.order_id == order_id)
        )
        assert order.acceptance_state == "accepted"
        assert (
            fill.broker_fill_id
            == f"unknown-acceptance-{remote_status.value}"
        )
        assert fill.qty == filled_qty
        assert fill.price == Decimal("100.000000")


@pytest.mark.parametrize(
    "terminal_status",
    [OrderStatus.CANCELED, OrderStatus.REJECTED],
)
def test_reconcile_unknown_atomically_latches_terminal_cumulative_fill(
    make_service,
    terminal_status,
):
    class TerminalThenDisconnectBroker(MockBroker):
        def __init__(self):
            super().__init__()
            self.activities = []

        def submit_order(self, order):
            accepted = super().submit_order(order)
            terminal = OrderResult(
                accepted.idempotency_key,
                accepted.broker_order_id,
                terminal_status,
                filled_qty=Decimal("0.5"),
                avg_fill_price=Decimal("90"),
            )
            self._orders_by_key[order.idempotency_key] = terminal
            self._orders_by_id[accepted.broker_order_id] = terminal
            raise ConnectionError("response lost after terminal partial fill")

        def get_fill_activities(self, after=None):
            return list(self.activities)

    broker = TerminalThenDisconnectBroker()
    broker.set_price("AAPL", Decimal("100"))
    service = make_service(broker=broker)
    order_id = _approved_order_id(service)
    service.order_submission.submit(order_id)
    with service.session_factory() as session:
        session.add(
            Fill(
                order_id=order_id,
                ticker="AAPL",
                side="buy",
                qty=Decimal("0.25"),
                price=Decimal("90"),
                broker_fill_id="already-authoritative-quarter",
                filled_at=utcnow(),
            )
        )
        session.commit()

    assert service.reconciliation.reconcile_unknown() == (1, ())

    with service.session_factory() as session:
        order = session.get(Order, order_id)
        broker_order_id = order.broker_order_id
        assert order.status == terminal_status.value
        assert order.acceptance_state == "fill_reconcile_required"

    restarted = make_service(broker=broker)
    incomplete = restarted.snapshot_service.assemble_for_execution("AAPL")
    assert incomplete.broker_reconciled is False
    assert incomplete.daily_pnl_complete is False

    broker.activities = [
        BrokerFill(
            broker_fill_id="delayed-authoritative-quarter",
            broker_order_id=broker_order_id,
            ticker="AAPL",
            side="buy",
            qty=Decimal("0.25"),
            price=Decimal("90"),
            filled_at=utcnow(),
        )
    ]

    exact = restarted.reconciliation.reconcile()
    replay = restarted.reconciliation.reconcile()

    assert exact.inserted_fills == 1
    assert replay.inserted_fills == 0
    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == terminal_status.value
        assert order.acceptance_state == "accepted"
        assert session.scalar(
            select(func.sum(Fill.qty)).where(Fill.order_id == order_id)
        ) == Decimal("0.5")


@pytest.mark.parametrize(
    (
        "local_qty",
        "remote_qty",
        "initial_acceptance",
        "expected_status",
        "expected_acceptance",
        "expected_error",
        "expected_drift",
    ),
    [
        (
            None,
            Decimal("0.000000500"),
            "acceptance_unknown",
            OrderStatus.CANCELED,
            FILL_RECONCILIATION_REQUIRED,
            "",
            False,
        ),
        (
            Decimal("0.000000500"),
            Decimal("0.000000500"),
            FILL_RECONCILIATION_REQUIRED,
            OrderStatus.CANCELED,
            "accepted",
            "",
            False,
        ),
        (
            Decimal("0.000000500"),
            Decimal("0.000000500") + FILL_ECONOMIC_QUANTUM,
            "acceptance_unknown",
            OrderStatus.CANCELED,
            FILL_RECONCILIATION_REQUIRED,
            "",
            False,
        ),
        (
            Decimal("0.000000500"),
            Decimal("0.000000500") - FILL_ECONOMIC_QUANTUM,
            "acceptance_unknown",
            OrderStatus.ACCEPTANCE_UNKNOWN,
            FILL_RECONCILIATION_REQUIRED,
            "cumulative_fill_contradiction",
            True,
        ),
    ],
    ids=[
        "sub-micro-terminal",
        "exact",
        "one-quantum-ahead",
        "one-quantum-behind",
    ],
)
def test_acceptance_recovery_uses_canonical_fill_quantum(
    make_service,
    local_qty,
    remote_qty,
    initial_acceptance,
    expected_status,
    expected_acceptance,
    expected_error,
    expected_drift,
):
    broker = ActivityBroker()
    service = make_service(broker=broker)
    submitted_at = utcnow() - timedelta(seconds=1)
    with service.session_factory() as session:
        order = Order(
            idempotency_key=f"acceptance-quantum-{remote_qty}",
            ticker="AAPL",
            side="buy",
            order_type="market",
            notional=Decimal("100"),
            status=OrderStatus.ACCEPTANCE_UNKNOWN.value,
            acceptance_state=initial_acceptance,
            submission_started_at=submitted_at,
        )
        session.add(order)
        session.flush()
        if local_qty is not None:
            session.add(
                Fill(
                    order_id=order.id,
                    ticker="AAPL",
                    side="buy",
                    qty=local_qty,
                    price=Decimal("100"),
                    broker_fill_id=f"acceptance-quantum-fill-{remote_qty}",
                    filled_at=utcnow(),
                )
            )
        session.commit()
        order_id = order.id
        client_order_id = order.idempotency_key

    remote = OrderResult(
        client_order_id,
        f"acceptance-quantum-broker-{remote_qty}",
        OrderStatus.CANCELED,
        filled_qty=remote_qty,
        avg_fill_price=Decimal("100"),
    )
    broker._orders_by_key[client_order_id] = remote
    broker._orders_by_id[remote.broker_order_id] = remote

    assert service.reconciliation.reconcile_unknown() == (1, ())

    assert (
        service.breakers.is_tripped(BreakerScope.broker_drift())
        is expected_drift
    )
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == expected_status.value
        assert order.acceptance_state == expected_acceptance
        assert order.last_error_code == expected_error


def test_panic_reports_unconfirmed_cancel_as_not_safe(make_service):
    broker = CancelFailsBroker()
    broker.set_price("AAPL", Decimal("100"))
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)

    report = service.reconciliation.panic("operator:avi", "manual drill")

    assert report.safe is False
    assert report.unconfirmed_order_ids == (order_id,)
    assert report.message != "everything halted"


def test_panic_fill_discovery_persists_fill_reconciliation_latch(
    make_service,
):
    class FilledDuringPanicBroker(ActivityBroker):
        def cancel_order(self, order_id):
            current = self.get_order_status(order_id)
            filled = OrderResult(
                current.idempotency_key,
                order_id,
                OrderStatus.FILLED,
                filled_qty=Decimal("1"),
                avg_fill_price=Decimal("100"),
            )
            self._orders_by_id[order_id] = filled
            self._orders_by_key[current.idempotency_key] = filled
            return filled

    broker = FilledDuringPanicBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)

    service.reconciliation.panic("operator:avi", "fill race")

    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.FILLED.value
        assert order.acceptance_state == "fill_reconcile_required"

    restarted = make_service(broker=broker)
    snapshot = restarted.snapshot_service.assemble_for_execution("AAPL")
    assert snapshot.daily_pnl_complete is False


def test_panic_canceled_after_partial_fill_stays_latched_until_activity(
    make_service,
):
    class CanceledAfterPartialBroker(ActivityBroker):
        def cancel_order(self, order_id):
            current = self.get_order_status(order_id)
            canceled = OrderResult(
                current.idempotency_key,
                order_id,
                OrderStatus.CANCELED,
                filled_qty=Decimal("0.5"),
                avg_fill_price=Decimal("90"),
            )
            self._orders_by_id[order_id] = canceled
            self._orders_by_key[current.idempotency_key] = canceled
            return canceled

    broker = CanceledAfterPartialBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)

    service.reconciliation.panic("operator:avi", "cancel after fill")

    with service.session_factory() as session:
        order = session.get(Order, order_id)
        broker_order_id = order.broker_order_id
        assert order.status == OrderStatus.CANCELED.value
        assert order.acceptance_state == "fill_reconcile_required"

    broker.activities = [
        BrokerFill(
            broker_fill_id="panic-canceled-after-partial",
            broker_order_id=broker_order_id,
            ticker="AAPL",
            side="buy",
            qty=Decimal("0.5"),
            price=Decimal("90"),
            filled_at=utcnow(),
        )
    ]
    restarted = make_service(broker=broker)

    exact = restarted.reconciliation.reconcile()
    replay = restarted.reconciliation.reconcile()

    assert exact.inserted_fills == 1
    assert replay.inserted_fills == 0
    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.acceptance_state == "accepted"
        assert session.scalar(
            select(func.count()).select_from(Fill).where(
                Fill.order_id == order_id
            )
        ) == 1


def test_panic_rejects_invalid_cumulative_fill_truth_and_trips_drift(
    make_service,
):
    class InvalidCumulativeDuringPanicBroker(ActivityBroker):
        def cancel_order(self, order_id):
            current = self.get_order_status(order_id)
            invalid = OrderResult(
                current.idempotency_key,
                order_id,
                OrderStatus.CANCELED,
                filled_qty=Decimal("-1"),
            )
            self._orders_by_id[order_id] = invalid
            self._orders_by_key[current.idempotency_key] = invalid
            return invalid

    broker = InvalidCumulativeDuringPanicBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)

    report = service.reconciliation.panic(
        "operator:avi",
        "invalid cumulative drill",
    )

    assert report.safe is False
    assert service.breakers.is_tripped(BreakerScope.broker_drift()) is True
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.acceptance_state == "fill_reconcile_required"


def test_panic_rejects_cumulative_fill_below_exact_local_truth(
    make_service,
):
    class ContradictoryCancelBroker(ActivityBroker):
        def cancel_order(self, order_id):
            current = self.get_order_status(order_id)
            contradicted = OrderResult(
                current.idempotency_key,
                order_id,
                OrderStatus.CANCELED,
                filled_qty=Decimal("0.5"),
                avg_fill_price=Decimal("100"),
            )
            self._orders_by_id[order_id] = contradicted
            self._orders_by_key[current.idempotency_key] = contradicted
            return contradicted

    broker = ContradictoryCancelBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        session.add(
            Fill(
                order_id=order_id,
                ticker="AAPL",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("100"),
                broker_fill_id="panic-exact-local-fill",
                filled_at=utcnow(),
            )
        )
        session.commit()

    report = service.reconciliation.panic(
        "operator:avi",
        "contradictory cumulative drill",
    )

    assert report.safe is False
    assert service.breakers.is_tripped(BreakerScope.broker_drift()) is True
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.SUBMITTED.value
        assert order.acceptance_state == "fill_reconcile_required"
        assert order.last_error_code == "cumulative_fill_contradiction"
        assert session.scalar(
            select(func.sum(Fill.qty)).where(Fill.order_id == order_id)
        ) == Decimal("1")


@pytest.mark.parametrize(
    (
        "local_qty",
        "quarantined_qty",
        "remote_qty",
        "initial_acceptance",
        "expected_status",
        "expected_acceptance",
        "expected_error",
        "expected_drift",
    ),
    [
        (
            None,
            None,
            Decimal("0.000000500"),
            OrderStatus.SUBMITTED.value,
            OrderStatus.CANCELED,
            FILL_RECONCILIATION_REQUIRED,
            "",
            False,
        ),
        (
            Decimal("0.000000500"),
            None,
            Decimal("0.000000500"),
            FILL_RECONCILIATION_REQUIRED,
            OrderStatus.CANCELED,
            "accepted",
            "",
            False,
        ),
        (
            Decimal("0.000000500"),
            Decimal("0.000000500"),
            Decimal("0.000000500"),
            FILL_RECONCILIATION_REQUIRED,
            OrderStatus.CANCELED,
            FILL_RECONCILIATION_REQUIRED,
            "waiting_for_exact_fill",
            False,
        ),
        (
            Decimal("0.000000500"),
            None,
            Decimal("0.000000500") + FILL_ECONOMIC_QUANTUM,
            OrderStatus.SUBMITTED.value,
            OrderStatus.CANCELED,
            FILL_RECONCILIATION_REQUIRED,
            "",
            False,
        ),
        (
            Decimal("0.000000500"),
            None,
            Decimal("0.000000500") - FILL_ECONOMIC_QUANTUM,
            OrderStatus.SUBMITTED.value,
            OrderStatus.SUBMITTED,
            FILL_RECONCILIATION_REQUIRED,
            "cumulative_fill_contradiction",
            True,
        ),
    ],
    ids=[
        "sub-micro-terminal",
        "exact",
        "exact-with-quarantined-fill",
        "one-quantum-ahead",
        "one-quantum-behind",
    ],
)
def test_panic_uses_canonical_fill_quantum(
    make_service,
    local_qty,
    quarantined_qty,
    remote_qty,
    initial_acceptance,
    expected_status,
    expected_acceptance,
    expected_error,
    expected_drift,
):
    class QuantumCancelBroker(ActivityBroker):
        def cancel_order(self, order_id):
            current = self.get_order_status(order_id)
            canceled = OrderResult(
                current.idempotency_key,
                order_id,
                OrderStatus.CANCELED,
                filled_qty=remote_qty,
                avg_fill_price=Decimal("100"),
            )
            self._orders_by_id[order_id] = canceled
            self._orders_by_key[current.idempotency_key] = canceled
            return canceled

    broker = QuantumCancelBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        order.acceptance_state = initial_acceptance
        order.last_error_code = (
            "waiting_for_exact_fill"
            if initial_acceptance == FILL_RECONCILIATION_REQUIRED
            else ""
        )
        if local_qty is not None:
            session.add(
                Fill(
                    order_id=order_id,
                    ticker="AAPL",
                    side="buy",
                    qty=local_qty,
                    price=Decimal("100"),
                    broker_fill_id=f"panic-quantum-fill-{remote_qty}",
                    filled_at=utcnow(),
                )
            )
        if quarantined_qty is not None:
            session.add(
                Fill(
                    order_id=order_id,
                    ticker="AAPL",
                    side="buy",
                    qty=quarantined_qty,
                    price=Decimal("100"),
                    broker_fill_id=f"panic-quarantined-fill-{remote_qty}",
                    reconciliation_state=FILL_RECONCILIATION_QUARANTINED,
                    filled_at=utcnow(),
                )
            )
        session.commit()

    service.reconciliation.panic(
        "operator:avi",
        f"quantum comparison {remote_qty}",
    )

    assert (
        service.breakers.is_tripped(BreakerScope.broker_drift())
        is expected_drift
    )
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == expected_status.value
        assert order.acceptance_state == expected_acceptance
        assert order.last_error_code == expected_error


def test_panic_requires_actor_and_reason_before_latching(make_service):
    service = make_service()

    with pytest.raises(ValueError, match="actor and reason"):
        service.reconciliation.panic("", "manual drill")
    with pytest.raises(ValueError, match="actor and reason"):
        service.reconciliation.panic("operator:avi", " ")

    with service.session_factory() as session:
        assert session.scalar(
            select(CircuitBreakerState).where(
                CircuitBreakerState.scope_key == "operator_global"
            )
        ) is None


def test_panic_cancels_remote_only_open_order_by_explicit_id(make_service):
    broker = MockBroker()
    remote = broker.submit_order(
        OrderRequest(
            ticker="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            idempotency_key="remote-only",
            notional=Decimal("100"),
        )
    )
    service = make_service(broker=broker)

    report = service.reconciliation.panic("operator:avi", "remote cleanup")

    assert report.safe is True
    assert report.confirmed_canceled == (remote.broker_order_id,)
    assert report.unconfirmed_order_ids == ()
    assert report.remote_open_order_ids == ()


def test_panic_latches_global_breaker_before_broker_enumeration_failure(make_service):
    class EnumerationFailsBroker(MockBroker):
        def get_open_orders(self):
            raise ConnectionError("broker unavailable")

    service = make_service(broker=EnumerationFailsBroker())

    report = service.reconciliation.panic("operator:avi", "connectivity loss")

    assert report.safe is False
    with service.session_factory() as session:
        state = session.scalar(
            select(CircuitBreakerState).where(
                CircuitBreakerState.scope_key == "operator_global"
            )
        )
        assert state is not None and state.tripped is True

    blocked = service.propose_order("AAPL", "buy", "market", notional="100")
    assert blocked["status"] == "rejected"
    assert any("circuit breaker" in reason for reason in blocked["risk_reasons"])


def test_panic_preserves_unverified_remote_only_id_as_potentially_open(make_service):
    class VanishingRemoteBroker(MockBroker):
        def __init__(self):
            super().__init__()
            self.enumerations = 0

        def get_open_orders(self):
            self.enumerations += 1
            if self.enumerations == 1:
                return super().get_open_orders()
            return []

        def cancel_order(self, order_id):
            raise ConnectionError("cancel response unavailable")

        def get_order_status(self, order_id):
            raise ConnectionError("status unavailable")

    broker = VanishingRemoteBroker()
    remote = broker.submit_order(
        OrderRequest(
            ticker="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            idempotency_key="remote-unverified",
            notional=Decimal("100"),
        )
    )
    service = make_service(broker=broker)

    report = service.reconciliation.panic("operator:avi", "remote verification")

    assert report.safe is False
    assert report.remote_open_order_ids == (remote.broker_order_id,)


def test_panic_is_unsafe_for_remote_open_order_without_explicit_id(make_service):
    class MissingIdBroker(MockBroker):
        def get_open_orders(self):
            return [
                OrderResult(
                    "missing-id",
                    None,
                    OrderStatus.SUBMITTED,
                )
            ]

        def cancel_order(self, order_id):
            raise AssertionError("must not cancel without an explicit broker id")

    service = make_service(broker=MissingIdBroker())

    report = service.reconciliation.panic("operator:avi", "missing broker id")

    assert report.safe is False
    assert "unaddressable_remote_open=true" in report.message


def test_panic_never_guesses_a_cancel_id_for_unresolved_unknown(make_service):
    class RecordingBroker(MockBroker):
        def __init__(self):
            super().__init__()
            self.cancel_calls = []

        def cancel_order(self, order_id):
            self.cancel_calls.append(order_id)
            return super().cancel_order(order_id)

    broker = RecordingBroker()
    service = make_service(broker=broker)
    with service.session_factory() as session:
        unknown = Order(
            idempotency_key="unresolved-client-id",
            ticker="AAPL",
            side="buy",
            order_type="market",
            notional=Decimal("100"),
            status=OrderStatus.ACCEPTANCE_UNKNOWN.value,
        )
        session.add(unknown)
        session.commit()
        unknown_id = unknown.id

    report = service.reconciliation.panic("operator:avi", "unknown acceptance")

    assert broker.cancel_calls == []
    assert report.safe is False
    assert report.unconfirmed_order_ids == (unknown_id,)


def test_reconcile_syncs_terminal_broker_status(make_service):
    broker = MockBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        local = session.get(Order, order_id)
        broker_order_id = local.broker_order_id
    broker.cancel_order(broker_order_id)

    report = service.reconciliation.reconcile()

    assert report.synced_orders == 1
    with service.session_factory() as session:
        assert session.get(Order, order_id).status == OrderStatus.CANCELED.value


def test_reconcile_reports_remote_open_order_without_broker_id(make_service):
    class MissingIdBroker(MockBroker):
        def get_open_orders(self):
            return [OrderResult("missing-id", None, OrderStatus.SUBMITTED)]

    service = make_service(broker=MissingIdBroker())
    report = service.reconciliation.reconcile()
    restarted = make_service(broker=MissingIdBroker())
    snapshot = restarted.snapshot_service.assemble_for_execution("AAPL")

    assert any("missing broker order id" in drift for drift in report.broker_drift)
    assert service.breakers.is_tripped(BreakerScope.broker_drift()) is True
    assert snapshot.broker_reconciled is False
    assert snapshot.daily_pnl_complete is False


class ActivityBroker(MockBroker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.activities: list[BrokerFill] = []
        self.activity_calls: list[datetime | None] = []
        self.fail_activities = False

    def get_fill_activities(self, after=None):
        self.activity_calls.append(after)
        if self.fail_activities:
            raise ConnectionError("activity stream unavailable")
        return list(self.activities)


def _crypto_order_with_remote_fill(
    service,
    broker,
    *,
    broker_order_id: str,
    client_order_id: str,
    filled_qty: Decimal,
) -> tuple[int, datetime]:
    submitted_at = utcnow() - timedelta(seconds=1)
    with service.session_factory() as session:
        order = Order(
            idempotency_key=client_order_id,
            ticker="BTC/USD",
            side=OrderSide.BUY.value,
            order_type=OrderType.MARKET.value,
            notional=Decimal("100"),
            status=OrderStatus.SUBMITTED.value,
            broker_order_id=broker_order_id,
            submission_started_at=submitted_at,
            acceptance_state="accepted",
        )
        session.add(order)
        session.commit()
        order_id = order.id
    remote = OrderResult(
        client_order_id,
        broker_order_id,
        OrderStatus.FILLED,
        filled_qty=filled_qty,
        avg_fill_price=Decimal("80000"),
    )
    broker._orders_by_id[broker_order_id] = remote
    broker._orders_by_key[client_order_id] = remote
    return order_id, submitted_at + timedelta(milliseconds=500)


def test_compact_crypto_fill_matches_slash_order_and_replays_as_noop(
    make_service,
):
    broker = ActivityBroker()
    broker.set_price("BTC/USD", Decimal("80000"))
    service = make_service(broker=broker)
    order_id, filled_at = _crypto_order_with_remote_fill(
        service,
        broker,
        broker_order_id="crypto-equivalent-broker",
        client_order_id="crypto-equivalent-client",
        filled_qty=Decimal("0.001"),
    )
    compact = BrokerFill(
        broker_fill_id="crypto-equivalent-fill",
        broker_order_id="crypto-equivalent-broker",
        ticker="BTCUSD",
        side="buy",
        qty=Decimal("0.001"),
        price=Decimal("80000"),
        filled_at=filled_at,
    )
    broker.activities = [compact]

    first = service.reconciliation.reconcile()
    restarted = make_service(broker=broker)
    broker.activities = [
        BrokerFill(
            broker_fill_id=compact.broker_fill_id,
            broker_order_id=compact.broker_order_id,
            ticker="BTC/USD",
            side=compact.side,
            qty=compact.qty,
            price=compact.price,
            filled_at=compact.filled_at,
        )
    ]
    replay = restarted.reconciliation.reconcile()

    assert first.inserted_fills == 1
    assert first.broker_drift == ()
    assert replay.inserted_fills == 0
    assert replay.broker_drift == ()
    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        fill = session.scalar(
            select(Fill).where(
                Fill.broker_fill_id == compact.broker_fill_id
            )
        )
        assert order.acceptance_state == "accepted"
        assert fill.ticker == "BTC/USD"
        assert session.scalar(
            select(func.count()).select_from(Fill).where(
                Fill.broker_fill_id == compact.broker_fill_id
            )
        ) == 1


@pytest.mark.parametrize("wrong_pair", ["ETHUSD", "ETH/USD", "BT/CUSD"])
def test_crypto_fill_equivalence_still_rejects_wrong_pair(
    make_service,
    wrong_pair,
):
    broker = ActivityBroker()
    broker.set_price("BTC/USD", Decimal("80000"))
    service = make_service(broker=broker)
    order_id, filled_at = _crypto_order_with_remote_fill(
        service,
        broker,
        broker_order_id=f"wrong-pair-{wrong_pair}-broker",
        client_order_id=f"wrong-pair-{wrong_pair}-client",
        filled_qty=Decimal("0.001"),
    )
    broker.activities = [
        BrokerFill(
            broker_fill_id=f"wrong-pair-{wrong_pair}-fill",
            broker_order_id=f"wrong-pair-{wrong_pair}-broker",
            ticker=wrong_pair,
            side="buy",
            qty=Decimal("0.001"),
            price=Decimal("80000"),
            filled_at=filled_at,
        )
    ]

    report = service.reconciliation.reconcile()
    restarted = make_service(broker=broker)

    assert report.inserted_fills == 0
    assert any("does not match local ticker" in item for item in report.broker_drift)
    assert restarted.breakers.is_tripped(
        BreakerScope.broker_drift()
    ) is True
    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.acceptance_state == FILL_RECONCILIATION_REQUIRED
        assert session.scalar(
            select(func.count()).select_from(Fill).where(
                Fill.order_id == order_id
            )
        ) == 0


@pytest.mark.parametrize(
    (
        "local_qty",
        "remote_qty",
        "initial_acceptance",
        "expected_status",
        "expected_acceptance",
        "expected_error",
        "expected_drift",
    ),
    [
        (
            None,
            Decimal("0.000000500"),
            "accepted",
            OrderStatus.CANCELED,
            FILL_RECONCILIATION_REQUIRED,
            "",
            True,
        ),
        (
            Decimal("0.000000500"),
            Decimal("0.000000500"),
            FILL_RECONCILIATION_REQUIRED,
            OrderStatus.CANCELED,
            "accepted",
            "",
            False,
        ),
        (
            Decimal("0.000000500"),
            Decimal("0.000000500") + FILL_ECONOMIC_QUANTUM,
            "accepted",
            OrderStatus.CANCELED,
            FILL_RECONCILIATION_REQUIRED,
            "",
            True,
        ),
        (
            Decimal("0.000000500"),
            Decimal("0.000000500") - FILL_ECONOMIC_QUANTUM,
            "accepted",
            OrderStatus.SUBMITTED,
            FILL_RECONCILIATION_REQUIRED,
            "cumulative_fill_contradiction",
            True,
        ),
    ],
    ids=[
        "sub-micro-terminal",
        "exact-clears-latch",
        "one-quantum-ahead",
        "one-quantum-behind",
    ],
)
def test_ordinary_reconciliation_uses_canonical_fill_quantum(
    make_service,
    local_qty,
    remote_qty,
    initial_acceptance,
    expected_status,
    expected_acceptance,
    expected_error,
    expected_drift,
):
    broker = ActivityBroker()
    service = make_service(broker=broker)
    submitted_at = utcnow() - timedelta(seconds=1)
    filled_at = utcnow()
    broker_order_id = f"ordinary-quantum-broker-{remote_qty}"
    client_order_id = f"ordinary-quantum-client-{remote_qty}"
    with service.session_factory() as session:
        order = Order(
            idempotency_key=client_order_id,
            ticker="AAPL",
            side="buy",
            order_type="market",
            notional=Decimal("100"),
            status=OrderStatus.SUBMITTED.value,
            broker_order_id=broker_order_id,
            acceptance_state=initial_acceptance,
            last_error_code=(
                "waiting_for_exact_fill"
                if initial_acceptance == FILL_RECONCILIATION_REQUIRED
                else ""
            ),
            submission_started_at=submitted_at,
        )
        session.add(order)
        session.flush()
        order_id = order.id
        if local_qty is not None:
            fill_id = f"ordinary-quantum-fill-{remote_qty}"
            session.add(
                Fill(
                    order_id=order_id,
                    ticker="AAPL",
                    side="buy",
                    qty=local_qty,
                    price=Decimal("100"),
                    broker_fill_id=fill_id,
                    filled_at=filled_at,
                )
            )
            broker.activities = [
                BrokerFill(
                    broker_fill_id=fill_id,
                    broker_order_id=broker_order_id,
                    ticker="AAPL",
                    side="buy",
                    qty=local_qty,
                    price=Decimal("100"),
                    filled_at=filled_at,
                )
            ]
        session.commit()

    remote = OrderResult(
        client_order_id,
        broker_order_id,
        OrderStatus.CANCELED,
        filled_qty=remote_qty,
        avg_fill_price=Decimal("100"),
    )
    broker._orders_by_id[broker_order_id] = remote
    broker._orders_by_key[client_order_id] = remote

    report = service.reconciliation.reconcile()

    assert bool(report.broker_drift) is expected_drift
    assert (
        service.breakers.is_tripped(BreakerScope.broker_drift())
        is expected_drift
    )
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == expected_status.value
        assert order.acceptance_state == expected_acceptance
        assert order.last_error_code == expected_error


def test_missing_alpaca_activity_id_never_inserts_advances_or_clears_latch(
    make_service,
):
    class RawActivityTrading:
        def __init__(self):
            self.broker_order_id = ""

        def get(self, _path, _params):
            return [
                {
                    "id": None,
                    "transaction_time": "2026-07-24T17:00:00Z",
                    "price": "100",
                    "qty": "1",
                    "side": "buy",
                    "symbol": "AAPL",
                    "order_id": self.broker_order_id,
                }
            ]

    class MissingActivityIdBroker(ActivityBroker):
        def __init__(self):
            super().__init__()
            self.raw_trading = RawActivityTrading()

        def get_fill_activities(self, after=None):
            return AlpacaBroker(
                self.raw_trading,
                object(),
            ).get_fill_activities(after)

    broker = MissingActivityIdBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        broker.raw_trading.broker_order_id = order.broker_order_id
        order.acceptance_state = "fill_reconcile_required"
        order.last_error_code = "remote_fill_ahead"
        session.commit()

    first = service.reconciliation.reconcile()
    restarted = make_service(broker=broker)
    replay = restarted.reconciliation.reconcile()

    assert first.inserted_fills == 0
    assert replay.inserted_fills == 0
    assert any("fill activity identity" in item for item in first.broker_drift)
    assert restarted.breakers.is_tripped(BreakerScope.broker_drift()) is True
    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.acceptance_state == "fill_reconcile_required"
        assert order.last_error_code == "invalid_fill_activity"
        assert session.scalar(
            select(func.count()).select_from(Fill)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(
                db_models.ReconciliationCursor
            )
        ) == 0


@pytest.mark.parametrize(
    "filled_qty",
    [Decimal("NaN"), Decimal("Infinity"), Decimal("-1")],
)
def test_invalid_cumulative_fill_truth_latches_and_trips_drift_across_restart(
    make_service,
    filled_qty,
):
    broker = ActivityBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        broker_order_id = order.broker_order_id
        client_order_id = order.idempotency_key
    invalid = OrderResult(
        client_order_id,
        broker_order_id,
        OrderStatus.CANCELED,
        filled_qty=filled_qty,
    )
    broker._orders_by_id[broker_order_id] = invalid
    broker._orders_by_key[client_order_id] = invalid

    first = service.reconciliation.reconcile()
    restarted = make_service(broker=broker)
    snapshot = restarted.snapshot_service.assemble_for_execution("AAPL")
    replay = restarted.reconciliation.reconcile()

    assert any("invalid cumulative filled_qty" in item for item in first.broker_drift)
    assert replay.inserted_fills == 0
    assert restarted.breakers.is_tripped(BreakerScope.broker_drift()) is True
    assert snapshot.broker_reconciled is False
    assert snapshot.daily_pnl_complete is False
    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.acceptance_state == "fill_reconcile_required"
        assert session.scalar(
            select(func.count()).select_from(Fill).where(Fill.order_id == order_id)
        ) == 0


def test_acceptance_resolution_rejects_invalid_cumulative_truth_durably(
    make_service,
):
    class InvalidThenDisconnectBroker(ActivityBroker):
        def submit_order(self, order):
            accepted = super().submit_order(order)
            invalid = OrderResult(
                accepted.idempotency_key,
                accepted.broker_order_id,
                OrderStatus.CANCELED,
                filled_qty=Decimal("NaN"),
            )
            self._orders_by_id[accepted.broker_order_id] = invalid
            self._orders_by_key[accepted.idempotency_key] = invalid
            raise ConnectionError("response lost before invalid status lookup")

    broker = InvalidThenDisconnectBroker()
    service = make_service(broker=broker)
    order_id = _approved_order_id(service)
    service.order_submission.submit(order_id)

    assert service.reconciliation.reconcile_unknown() == (1, ())

    restarted = make_service(broker=broker)
    snapshot = restarted.snapshot_service.assemble_for_execution("AAPL")
    assert restarted.breakers.is_tripped(BreakerScope.broker_drift()) is True
    assert snapshot.broker_reconciled is False
    assert snapshot.daily_pnl_complete is False
    with restarted.session_factory() as session:
        assert (
            session.get(Order, order_id).acceptance_state
            == "fill_reconcile_required"
        )


def test_standalone_acceptance_contradiction_latch_and_drift_share_commit(
    make_service,
):
    broker = AcceptThenDisconnectBroker()
    service = make_service(broker=broker)
    order_id = _acceptance_unknown_with_exact_local_fill_ahead(
        service,
        broker,
    )
    session_type = service.session_factory.class_
    observed_breaker_in_latch_transaction: list[bool] = []

    def crash_before_contradiction_commit(session):
        acceptance_state = session.scalar(
            select(Order.acceptance_state).where(Order.id == order_id)
        )
        if acceptance_state != "fill_reconcile_required":
            return
        observed_breaker_in_latch_transaction.append(
            session.get(
                CircuitBreakerState,
                BreakerScope.broker_drift().key,
            )
            is not None
        )
        raise RuntimeError("simulated crash before contradiction commit")

    event.listen(session_type, "before_commit", crash_before_contradiction_commit)
    try:
        with pytest.raises(
            RuntimeError,
            match="simulated crash before contradiction commit",
        ):
            service.reconciliation.reconcile_unknown()
    finally:
        event.remove(
            session_type,
            "before_commit",
            crash_before_contradiction_commit,
        )

    assert observed_breaker_in_latch_transaction == [True]
    restarted = make_service(broker=broker)
    with restarted.session_factory() as session:
        rolled_back = session.get(Order, order_id)
        assert rolled_back.status == OrderStatus.ACCEPTANCE_UNKNOWN.value
        assert (
            rolled_back.acceptance_state
            != "fill_reconcile_required"
        )
        assert session.get(
            CircuitBreakerState,
            BreakerScope.broker_drift().key,
        ) is None

    assert restarted.reconciliation.reconcile_unknown() == (1, ())
    after_commit = make_service(broker=broker)
    assert after_commit.breakers.is_tripped(
        BreakerScope.broker_drift()
    ) is True
    with after_commit.session_factory() as session:
        persisted = session.get(Order, order_id)
        assert persisted.status == OrderStatus.ACCEPTANCE_UNKNOWN.value
        assert persisted.acceptance_state == "fill_reconcile_required"
        assert persisted.last_error_code == "cumulative_fill_contradiction"


def test_full_acceptance_contradiction_trips_drift_before_later_crash(
    make_service,
):
    broker = AcceptThenDisconnectBroker()
    service = make_service(broker=broker)
    order_id = _acceptance_unknown_with_exact_local_fill_ahead(
        service,
        broker,
    )

    def crash_after_acceptance_recovery(_drift):
        raise RuntimeError("simulated crash after acceptance recovery")

    service.reconciliation._reconcile_fill_activities = (
        crash_after_acceptance_recovery
    )
    with pytest.raises(
        RuntimeError,
        match="simulated crash after acceptance recovery",
    ):
        service.reconciliation.reconcile()

    restarted = make_service(broker=broker)
    assert restarted.breakers.is_tripped(
        BreakerScope.broker_drift()
    ) is True
    with restarted.session_factory() as session:
        persisted = session.get(Order, order_id)
        assert persisted.status == OrderStatus.ACCEPTANCE_UNKNOWN.value
        assert persisted.acceptance_state == "fill_reconcile_required"
        assert persisted.last_error_code == "cumulative_fill_contradiction"


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("side", "sell"),
        ("side", "exercise"),
        ("ticker", "MSFT"),
        ("qty", Decimal("0")),
        ("qty", Decimal("NaN")),
        ("qty", Decimal("0.0000000001")),
        ("qty", Decimal("1000000")),
        ("price", Decimal("0")),
        ("price", Decimal("Infinity")),
        ("price", Decimal("1.0000000001")),
        ("price", Decimal("1000000")),
    ],
)
def test_invalid_exact_fill_is_rejected_latched_and_replayed_fail_closed(
    make_service,
    field,
    invalid_value,
):
    broker = ActivityBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        broker_order_id = order.broker_order_id
        client_order_id = order.idempotency_key
    values = {
        "broker_fill_id": f"invalid-{field}-{invalid_value}",
        "broker_order_id": broker_order_id,
        "ticker": "AAPL",
        "side": "buy",
        "qty": Decimal("1"),
        "price": Decimal("100"),
        "filled_at": utcnow(),
    }
    values[field] = invalid_value
    broker.activities = [BrokerFill(**values)]
    filled = OrderResult(
        client_order_id,
        broker_order_id,
        OrderStatus.FILLED,
        filled_qty=Decimal("1"),
        avg_fill_price=Decimal("100"),
    )
    broker._orders_by_id[broker_order_id] = filled
    broker._orders_by_key[client_order_id] = filled

    first = service.reconciliation.reconcile()
    restarted = make_service(broker=broker)
    snapshot = restarted.snapshot_service.assemble_for_execution("AAPL")
    replay = restarted.reconciliation.reconcile()

    assert first.inserted_fills == 0
    assert replay.inserted_fills == 0
    assert any("invalid fill activity" in item for item in first.broker_drift)
    assert restarted.breakers.is_tripped(BreakerScope.broker_drift()) is True
    assert snapshot.broker_reconciled is False
    assert snapshot.daily_pnl_complete is False
    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.acceptance_state == "fill_reconcile_required"
        assert session.scalar(
            select(func.count()).select_from(Fill).where(Fill.order_id == order_id)
        ) == 0
        assert session.scalar(
            select(func.count())
            .select_from(db_models.ReconciliationCursor)
            .where(db_models.ReconciliationCursor.stream == "fills")
        ) == 0


def test_mutated_duplicate_fill_replay_is_rejected_without_corrupting_ledger(
    make_service,
):
    broker = ActivityBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        broker_order_id = order.broker_order_id
        client_order_id = order.idempotency_key
    valid = BrokerFill(
        broker_fill_id="immutable-fill-id",
        broker_order_id=broker_order_id,
        ticker="AAPL",
        side="buy",
        qty=Decimal("1"),
        price=Decimal("100"),
        filled_at=utcnow(),
    )
    broker.activities = [valid]
    filled = OrderResult(
        client_order_id,
        broker_order_id,
        OrderStatus.FILLED,
        filled_qty=Decimal("1"),
        avg_fill_price=Decimal("100"),
    )
    broker._orders_by_id[broker_order_id] = filled
    broker._orders_by_key[client_order_id] = filled
    assert service.reconciliation.reconcile().inserted_fills == 1

    broker.activities = [
        BrokerFill(
            broker_fill_id=valid.broker_fill_id,
            broker_order_id=valid.broker_order_id,
            ticker=valid.ticker,
            side="sell",
            qty=valid.qty,
            price=valid.price,
            filled_at=valid.filled_at,
        )
    ]
    replay = service.reconciliation.reconcile()

    assert replay.inserted_fills == 0
    assert any("invalid fill activity" in item for item in replay.broker_drift)
    assert service.breakers.is_tripped(BreakerScope.broker_drift()) is True
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        fill = session.scalar(
            select(Fill).where(Fill.broker_fill_id == valid.broker_fill_id)
        )
        assert order.acceptance_state == "fill_reconcile_required"
        assert fill.side == "buy"
        assert fill.ticker == "AAPL"
        assert fill.qty == Decimal("1.000000")
        assert fill.price == Decimal("100.000000")


def test_duplicate_fill_timestamp_is_normalized_and_mismatch_fails_closed(
    make_service,
):
    broker = ActivityBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        broker_order_id = order.broker_order_id
        client_order_id = order.idempotency_key
        filled_at = order.submission_started_at + timedelta(seconds=1)

    original = BrokerFill(
        broker_fill_id="timestamp-immutable-fill",
        broker_order_id=broker_order_id,
        ticker="AAPL",
        side="buy",
        qty=Decimal("1"),
        price=Decimal("100"),
        filled_at=filled_at,
    )
    broker.activities = [original]
    filled = OrderResult(
        client_order_id,
        broker_order_id,
        OrderStatus.FILLED,
        filled_qty=Decimal("1"),
        avg_fill_price=Decimal("100"),
    )
    broker._orders_by_id[broker_order_id] = filled
    broker._orders_by_key[client_order_id] = filled
    assert service.reconciliation.reconcile().inserted_fills == 1

    equivalent_offset_time = filled_at.astimezone(
        timezone(timedelta(hours=-7))
    )
    broker.activities = [
        BrokerFill(
            broker_fill_id=original.broker_fill_id,
            broker_order_id=original.broker_order_id,
            ticker=original.ticker,
            side=original.side,
            qty=original.qty,
            price=original.price,
            filled_at=equivalent_offset_time,
        )
    ]
    equivalent = service.reconciliation.reconcile()
    assert equivalent.inserted_fills == 0
    assert equivalent.broker_drift == ()

    changed_time = filled_at + timedelta(microseconds=1)
    broker.activities = [
        BrokerFill(
            broker_fill_id=original.broker_fill_id,
            broker_order_id=original.broker_order_id,
            ticker=original.ticker,
            side=original.side,
            qty=original.qty,
            price=original.price,
            filled_at=changed_time,
        )
    ]
    restarted = make_service(broker=broker)
    mismatch = restarted.reconciliation.reconcile()
    replayed = make_service(broker=broker).reconciliation.reconcile()

    assert mismatch.inserted_fills == 0
    assert replayed.inserted_fills == 0
    assert any("changed timestamp" in item for item in mismatch.broker_drift)
    assert any("changed timestamp" in item for item in replayed.broker_drift)
    assert restarted.breakers.is_tripped(BreakerScope.broker_drift()) is True
    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        fill = session.scalar(
            select(Fill).where(
                Fill.broker_fill_id == original.broker_fill_id
            )
        )
        assert order.acceptance_state == "fill_reconcile_required"
        assert order.last_error_code == "invalid_fill_activity"
        assert fill.filled_at == filled_at
        assert fill.reconciliation_state == "trusted"


@pytest.mark.parametrize(
    ("mutated_field", "mutated_value", "expected_error"),
    [
        ("qty", Decimal("0.123456788"), "changed quantity"),
        ("price", Decimal("999999.999999998"), "changed price"),
    ],
)
def test_crypto_precision_replay_is_noop_and_mutation_trips_drift(
    make_service,
    mutated_field,
    mutated_value,
    expected_error,
):
    broker = ActivityBroker()
    broker.set_price("BTCUSD", Decimal("80000"))
    service = make_service(broker=broker)
    submitted_at = utcnow() - timedelta(seconds=1)
    with service.session_factory() as session:
        order = Order(
            idempotency_key=f"crypto-precision-{mutated_field}-client",
            ticker="BTCUSD",
            side="buy",
            order_type="market",
            notional=Decimal("100"),
            status=OrderStatus.SUBMITTED.value,
            broker_order_id=f"crypto-precision-{mutated_field}-broker",
            submission_started_at=submitted_at,
            acceptance_state="accepted",
        )
        session.add(order)
        session.commit()
        order_id = order.id
        broker_order_id = order.broker_order_id
        client_order_id = order.idempotency_key
        filled_at = utcnow()

    exact = BrokerFill(
        broker_fill_id=f"crypto-precision-{mutated_field}",
        broker_order_id=broker_order_id,
        ticker="BTCUSD",
        side="buy",
        qty=Decimal("0.123456789"),
        price=Decimal("999999.999999999"),
        filled_at=filled_at,
    )
    broker.activities = [exact]
    filled = OrderResult(
        client_order_id,
        broker_order_id,
        OrderStatus.FILLED,
        filled_qty=exact.qty,
        avg_fill_price=exact.price,
    )
    broker._orders_by_id[broker_order_id] = filled
    broker._orders_by_key[client_order_id] = filled

    first = service.reconciliation.reconcile()
    restarted = make_service(broker=broker)
    unchanged = restarted.reconciliation.reconcile()

    assert first.inserted_fills == 1
    assert unchanged.inserted_fills == 0
    assert unchanged.broker_drift == ()
    with restarted.session_factory() as session:
        persisted = session.scalar(
            select(Fill).where(Fill.broker_fill_id == exact.broker_fill_id)
        )
        assert persisted.qty == exact.qty
        assert persisted.price == exact.price

    broker.activities = [
        BrokerFill(
            broker_fill_id=exact.broker_fill_id,
            broker_order_id=exact.broker_order_id,
            ticker=exact.ticker,
            side=exact.side,
            qty=(
                mutated_value
                if mutated_field == "qty"
                else exact.qty
            ),
            price=(
                mutated_value
                if mutated_field == "price"
                else exact.price
            ),
            filled_at=exact.filled_at,
        )
    ]
    mutation = restarted.reconciliation.reconcile()

    assert mutation.inserted_fills == 0
    assert any(expected_error in item for item in mutation.broker_drift)
    assert restarted.breakers.is_tripped(BreakerScope.broker_drift()) is True
    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        persisted = session.scalar(
            select(Fill).where(Fill.broker_fill_id == exact.broker_fill_id)
        )
        assert order.acceptance_state == "fill_reconcile_required"
        assert order.last_error_code == "invalid_fill_activity"
        assert persisted.qty == exact.qty
        assert persisted.price == exact.price


@pytest.mark.parametrize(
    ("timestamp_case", "expected_error"),
    [
        ("stale-day-boundary", "predates order submission"),
        ("future", "beyond allowed future skew"),
    ],
)
def test_first_seen_fill_rejects_out_of_bounds_timestamp_across_restart(
    make_service,
    timestamp_case,
    expected_error,
):
    broker = ActivityBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        broker_order_id = order.broker_order_id
        client_order_id = order.idempotency_key
        submission_boundary = order.submission_started_at
        order.acceptance_state = "fill_reconcile_required"
        order.last_error_code = "waiting_for_exact_fill"
        legacy_fill_id = f"{timestamp_case}-legacy-fill"
        session.add(
            Fill(
                order_id=order_id,
                ticker="AAPL",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("100"),
                broker_fill_id=legacy_fill_id,
                reconciliation_state="quarantined",
                filled_at=submission_boundary,
            )
        )
        session.commit()

    if timestamp_case == "stale-day-boundary":
        invalid_at_utc = submission_boundary - timedelta(days=1)
        invalid_at = invalid_at_utc.astimezone(
            timezone(timedelta(hours=-7))
        )
    else:
        invalid_at = (
            utcnow()
            + timedelta(
                seconds=DEFAULT_MAX_FUTURE_SKEW_SECONDS + 300
            )
        ).astimezone(timezone(timedelta(hours=5, minutes=30)))

    exact_fill_id = f"{timestamp_case}-exact-fill"
    broker.activities = [
        BrokerFill(
            broker_fill_id=exact_fill_id,
            broker_order_id=broker_order_id,
            ticker="AAPL",
            side="buy",
            qty=Decimal("1"),
            price=Decimal("100"),
            filled_at=invalid_at,
        )
    ]
    remote = OrderResult(
        client_order_id,
        broker_order_id,
        OrderStatus.FILLED,
        filled_qty=Decimal("1"),
        avg_fill_price=Decimal("100"),
    )
    broker._orders_by_id[broker_order_id] = remote
    broker._orders_by_key[client_order_id] = remote

    first = service.reconciliation.reconcile()
    restarted = make_service(broker=broker)
    replay = restarted.reconciliation.reconcile()
    snapshot = restarted.snapshot_service.assemble_for_execution("AAPL")

    assert first.inserted_fills == 0
    assert replay.inserted_fills == 0
    assert any(expected_error in item for item in first.broker_drift)
    assert any(expected_error in item for item in replay.broker_drift)
    assert restarted.breakers.is_tripped(BreakerScope.broker_drift()) is True
    assert snapshot.broker_reconciled is False
    assert snapshot.daily_pnl_complete is False
    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        fills = session.scalars(
            select(Fill).where(Fill.order_id == order_id)
        ).all()
        assert order.acceptance_state == "fill_reconcile_required"
        assert order.last_error_code == "legacy_unidentified_fill"
        assert len(fills) == 1
        assert fills[0].broker_fill_id == legacy_fill_id
        assert fills[0].reconciliation_state == "quarantined"
        assert session.scalar(
            select(func.count())
            .select_from(db_models.ReconciliationCursor)
            .where(db_models.ReconciliationCursor.stream == "fills")
        ) == 0


@pytest.mark.parametrize(
    ("remote_status", "filled_qty"),
    [
        (OrderStatus.PARTIALLY_FILLED, Decimal("0.5")),
        (OrderStatus.FILLED, Decimal("1")),
    ],
)
def test_status_discovery_latch_survives_restart_until_exact_activity(
    make_service,
    remote_status,
    filled_qty,
):
    broker = ActivityBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        broker_order_id = order.broker_order_id
        client_order_id = order.idempotency_key
    remote = OrderResult(
        client_order_id,
        broker_order_id,
        remote_status,
        filled_qty=filled_qty,
        avg_fill_price=Decimal("100"),
    )
    broker._orders_by_id[broker_order_id] = remote
    broker._orders_by_key[client_order_id] = remote

    first = service.reconciliation.reconcile()

    assert first.inserted_fills == 0
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == remote_status.value
        assert order.acceptance_state == "fill_reconcile_required"

    restarted = make_service(broker=broker)
    incomplete = restarted.snapshot_service.assemble_for_execution("AAPL")
    assert incomplete.broker_reconciled is False
    assert incomplete.daily_pnl_complete is False

    broker.activities = [
        BrokerFill(
            broker_fill_id=f"status-{remote_status.value}",
            broker_order_id=broker_order_id,
            ticker="AAPL",
            side="buy",
            qty=filled_qty,
            price=Decimal("100"),
            filled_at=utcnow(),
        )
    ]
    exact = restarted.reconciliation.reconcile()

    assert exact.inserted_fills == 1
    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.acceptance_state == "accepted"
        assert session.scalar(
            select(func.count()).select_from(Fill).where(
                Fill.order_id == order_id
            )
        ) == 1


def test_canceled_after_partial_fill_stays_latched_until_exact_activity(
    make_service,
):
    broker = ActivityBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        broker_order_id = order.broker_order_id
        client_order_id = order.idempotency_key
    canceled_after_fill = OrderResult(
        client_order_id,
        broker_order_id,
        OrderStatus.CANCELED,
        filled_qty=Decimal("0.5"),
        avg_fill_price=Decimal("90"),
    )
    broker._orders_by_id[broker_order_id] = canceled_after_fill
    broker._orders_by_key[client_order_id] = canceled_after_fill

    delayed = service.reconciliation.reconcile()

    assert delayed.inserted_fills == 0
    assert any(
        "requires 0.5 authoritative quantity" in item
        for item in delayed.broker_drift
    )
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.CANCELED.value
        assert order.acceptance_state == "fill_reconcile_required"

    restarted = make_service(broker=broker)
    incomplete = restarted.snapshot_service.assemble_for_execution("AAPL")
    assert incomplete.broker_reconciled is False
    assert incomplete.daily_pnl_complete is False

    broker.activities = [
        BrokerFill(
            broker_fill_id="canceled-after-partial",
            broker_order_id=broker_order_id,
            ticker="AAPL",
            side="buy",
            qty=Decimal("0.5"),
            price=Decimal("90"),
            filled_at=utcnow(),
        )
    ]

    exact = restarted.reconciliation.reconcile()
    replay = restarted.reconciliation.reconcile()

    assert exact.inserted_fills == 1
    assert replay.inserted_fills == 0
    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.CANCELED.value
        assert order.acceptance_state == "accepted"
        assert session.scalar(
            select(func.count()).select_from(Fill).where(
                Fill.order_id == order_id
            )
        ) == 1


def test_partial_fill_expiration_preserves_pnl_and_releases_remainder(
    make_service,
):
    broker = ActivityBroker(
        positions=[
            Position(
                "AAPL",
                Decimal("0.6"),
                Decimal("100"),
                Decimal("100"),
            )
        ]
    )
    service = make_service(broker=broker)
    now = utcnow()
    with service.session_factory() as session:
        order = Order(
            idempotency_key="partial-expired-client",
            ticker="AAPL",
            side="sell",
            order_type="market",
            qty=Decimal("1"),
            status=OrderStatus.PARTIALLY_FILLED.value,
            broker_order_id="partial-expired-broker",
            submission_started_at=now - timedelta(minutes=3),
            acceptance_state="accepted",
        )
        session.add(order)
        session.flush()
        session.add_all(
            [
                Fill(
                    ticker="AAPL",
                    side="buy",
                    qty=Decimal("1"),
                    price=Decimal("100"),
                    broker_fill_id="partial-expired-opening-lot",
                    filled_at=now - timedelta(minutes=4),
                ),
                Fill(
                    order_id=order.id,
                    ticker="AAPL",
                    side="sell",
                    qty=Decimal("0.4"),
                    price=Decimal("90"),
                    broker_fill_id="partial-expired-exact-fill",
                    filled_at=now - timedelta(minutes=1),
                ),
            ]
        )
        session.commit()
        order_id = order.id

    exact = BrokerFill(
        broker_fill_id="partial-expired-exact-fill",
        broker_order_id="partial-expired-broker",
        ticker="AAPL",
        side="sell",
        qty=Decimal("0.4"),
        price=Decimal("90"),
        filled_at=now - timedelta(minutes=1),
    )
    broker.activities = [exact]
    expired = OrderResult(
        "partial-expired-client",
        "partial-expired-broker",
        OrderStatus.EXPIRED,
        filled_qty=Decimal("0.4"),
        avg_fill_price=Decimal("90"),
    )
    broker._orders_by_id[expired.broker_order_id] = expired
    broker._orders_by_key[expired.idempotency_key] = expired

    before = service.snapshot_service.assemble_for_execution("AAPL")
    first = service.reconciliation.reconcile()
    after = service.snapshot_service.assemble_for_execution("AAPL")
    replay = service.reconciliation.reconcile()
    after_replay = service.snapshot_service.assemble_for_execution("AAPL")

    assert before.reserved_sell_qty_by_ticker == {
        "AAPL": Decimal("0.600000")
    }
    assert before.realized_pnl_today == Decimal("-4")
    assert first.inserted_fills == 0
    assert first.broker_drift == ()
    assert after.reserved_sell_qty_by_ticker == {}
    assert after.realized_pnl_today == Decimal("-4")
    assert replay.inserted_fills == 0
    assert replay.broker_drift == ()
    assert after_replay.reserved_sell_qty_by_ticker == {}
    assert after_replay.realized_pnl_today == Decimal("-4")
    with service.session_factory() as session:
        persisted = session.get(Order, order_id)
        fills = session.scalars(
            select(Fill).where(
                Fill.broker_fill_id.in_(
                    (
                        "partial-expired-opening-lot",
                        "partial-expired-exact-fill",
                    )
                )
            )
        ).all()
        assert persisted.status == OrderStatus.EXPIRED.value
        assert persisted.acceptance_state == "accepted"
        assert len(fills) == 2


@pytest.mark.parametrize(
    "terminal_status",
    [
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    ],
)
def test_indeterminate_cancel_zero_fill_recovers_from_authoritative_empty_stream(
    make_service,
    terminal_status,
):
    broker = ActivityBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        broker_order_id = order.broker_order_id
        client_order_id = order.idempotency_key
        order.acceptance_state = "fill_reconcile_required"
        order.last_error_code = "indeterminate_cancel"
        session.commit()
    service.breakers.trip(
        BreakerScope.broker_drift(),
        "cancel result and status were indeterminate",
        "service:cancel",
    )
    remote = OrderResult(
        client_order_id,
        broker_order_id,
        terminal_status,
        filled_qty=Decimal("0"),
    )
    broker._orders_by_id[broker_order_id] = remote
    broker._orders_by_key[client_order_id] = remote

    restarted = make_service(broker=broker)
    before = restarted.snapshot_service.assemble_for_execution("AAPL")
    first = restarted.reconciliation.reconcile()
    replay = restarted.reconciliation.reconcile()

    assert before.broker_reconciled is False
    assert before.daily_pnl_complete is False
    assert first.inserted_fills == 0
    assert replay.inserted_fills == 0
    assert not any(
        "requires 0 authoritative quantity" in item
        for item in first.broker_drift
    )
    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == terminal_status.value
        assert order.acceptance_state == "accepted"
        assert order.last_error_code == ""
        assert session.scalar(
            select(func.count()).select_from(Fill).where(
                Fill.order_id == order_id
            )
        ) == 0

    drift = restarted.breakers.get(BreakerScope.broker_drift())
    assert drift is not None and drift.tripped is True
    restarted.breakers.reset(
        BreakerScope.broker_drift(),
        actor="operator:reconciliation",
        reason="authoritative terminal zero-fill status reviewed",
        prior_health={
            "order_id": order_id,
            "status": terminal_status.value,
            "authoritative_fill_qty": "0",
        },
        expected_generation=drift.generation,
    )
    complete = restarted.snapshot_service.assemble_for_execution("AAPL")
    assert complete.broker_reconciled is True
    assert complete.daily_pnl_complete is True


def test_indeterminate_cancel_zero_fill_waits_for_successful_activity_read(
    make_service,
):
    broker = ActivityBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        broker_order_id = order.broker_order_id
        client_order_id = order.idempotency_key
        order.acceptance_state = "fill_reconcile_required"
        order.last_error_code = "indeterminate_cancel"
        session.commit()
    broker._orders_by_id[broker_order_id] = OrderResult(
        client_order_id,
        broker_order_id,
        OrderStatus.CANCELED,
        filled_qty=Decimal("0"),
    )
    broker.fail_activities = True

    report = service.reconciliation.reconcile()

    assert any("fill activities unavailable" in item for item in report.broker_drift)
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.acceptance_state == "fill_reconcile_required"
        assert order.last_error_code == "indeterminate_cancel"


def test_remote_cumulative_below_exact_local_fill_is_durable_contradiction(
    make_service,
):
    broker = ActivityBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        broker_order_id = order.broker_order_id
        client_order_id = order.idempotency_key
        session.add(
            Fill(
                order_id=order_id,
                ticker="AAPL",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("100"),
                broker_fill_id="exact-local-fill-ahead-of-remote",
                filled_at=utcnow(),
            )
        )
        session.commit()
    contradicted = OrderResult(
        client_order_id,
        broker_order_id,
        OrderStatus.CANCELED,
        filled_qty=Decimal("0.5"),
        avg_fill_price=Decimal("100"),
    )
    broker._orders_by_id[broker_order_id] = contradicted
    broker._orders_by_key[client_order_id] = contradicted

    first = service.reconciliation.reconcile()
    restarted = make_service(broker=broker)
    replay = restarted.reconciliation.reconcile()
    snapshot = restarted.snapshot_service.assemble_for_execution("AAPL")

    assert any(
        "cumulative 0.5 is below authoritative local quantity 1" in item
        for item in first.broker_drift
    )
    assert any("below authoritative local quantity" in item for item in replay.broker_drift)
    assert restarted.breakers.is_tripped(BreakerScope.broker_drift()) is True
    assert snapshot.broker_reconciled is False
    assert snapshot.daily_pnl_complete is False
    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.SUBMITTED.value
        assert order.acceptance_state == "fill_reconcile_required"
        assert order.last_error_code == "cumulative_fill_contradiction"
        fills = session.scalars(
            select(Fill).where(Fill.order_id == order_id)
        ).all()
        assert len(fills) == 1
        assert fills[0].broker_fill_id == "exact-local-fill-ahead-of-remote"


def test_legacy_null_fill_is_quarantined_then_superseded_without_double_pnl(
    make_service,
):
    broker = ActivityBroker()
    service = make_service(broker=broker)
    with service.session_factory() as session:
        session.add(
            Fill(
                ticker="AAPL",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("100"),
                broker_fill_id="trusted-opening-fill",
                filled_at=utcnow() - timedelta(minutes=2),
            )
        )
        order = Order(
            idempotency_key="legacy-null-fill-client",
            ticker="AAPL",
            side="sell",
            order_type="market",
            qty=Decimal("1"),
            status=OrderStatus.CANCELED.value,
            broker_order_id="legacy-null-fill-order",
            acceptance_state="accepted",
        )
        session.add(order)
        session.flush()
        session.add(
            Fill(
                order_id=order.id,
                ticker="AAPL",
                side="sell",
                qty=Decimal("1"),
                price=Decimal("1"),
                broker_fill_id=None,
                filled_at=utcnow() - timedelta(minutes=1),
            )
        )
        session.commit()
        order_id = order.id
    remote = OrderResult(
        "legacy-null-fill-client",
        "legacy-null-fill-order",
        OrderStatus.CANCELED,
        filled_qty=Decimal("1"),
        avg_fill_price=Decimal("1"),
    )
    broker._orders_by_id["legacy-null-fill-order"] = remote
    broker._orders_by_key["legacy-null-fill-client"] = remote

    untrusted = service.snapshot_service.assemble_for_execution("AAPL")
    first = service.reconciliation.reconcile()
    restarted = make_service(broker=broker)
    still_untrusted = restarted.snapshot_service.assemble_for_execution("AAPL")

    assert untrusted.realized_pnl_today == Decimal("0")
    assert untrusted.broker_reconciled is False
    assert untrusted.daily_pnl_complete is False
    assert any("quarantined legacy fill" in item for item in first.broker_drift)
    assert still_untrusted.realized_pnl_today == Decimal("0")
    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        legacy = session.scalar(
            select(Fill).where(Fill.broker_fill_id.is_(None))
        )
        assert order.acceptance_state == "fill_reconcile_required"
        assert legacy.reconciliation_state == "quarantined"

    broker.activities = [
        BrokerFill(
            broker_fill_id="authoritative-hidden-loss-fill",
            broker_order_id="legacy-null-fill-order",
            ticker="AAPL",
            side="sell",
            qty=Decimal("1"),
            price=Decimal("1"),
            filled_at=utcnow(),
        )
    ]
    recovered = restarted.reconciliation.reconcile()
    replay = restarted.reconciliation.reconcile()
    after_exact = restarted.snapshot_service.assemble_for_execution("AAPL")

    assert recovered.inserted_fills == 1
    assert replay.inserted_fills == 0
    assert after_exact.realized_pnl_today == Decimal("-99")
    assert after_exact.broker_reconciled is False
    assert after_exact.daily_pnl_complete is False
    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        fills = session.scalars(select(Fill).order_by(Fill.id)).all()
        legacy = next(fill for fill in fills if fill.broker_fill_id is None)
        exact = next(
            fill
            for fill in fills
            if fill.broker_fill_id == "authoritative-hidden-loss-fill"
        )
        assert order.acceptance_state == "accepted"
        assert legacy.reconciliation_state == "superseded"
        assert exact.reconciliation_state == "trusted"

    drift = restarted.breakers.get(BreakerScope.broker_drift())
    assert drift is not None and drift.tripped is True
    restarted.breakers.reset(
        BreakerScope.broker_drift(),
        actor="operator:reconciliation",
        reason="legacy fill matched to authoritative activity",
        prior_health={
            "order_id": order_id,
            "broker_fill_id": "authoritative-hidden-loss-fill",
        },
        expected_generation=drift.generation,
    )
    complete = restarted.snapshot_service.assemble_for_execution("AAPL")
    assert complete.realized_pnl_today == Decimal("-99")
    assert complete.broker_reconciled is True
    assert complete.daily_pnl_complete is True


def test_pre_0005_supplied_fill_id_requires_exact_activity_then_replays_once(
    make_service,
):
    broker = ActivityBroker()
    service = make_service(broker=broker)
    with service.session_factory() as session:
        session.add(
            Fill(
                ticker="AAPL",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("100"),
                broker_fill_id="trusted-opening-fill-for-supplied-id",
                filled_at=utcnow() - timedelta(minutes=3),
            )
        )
        order = Order(
            idempotency_key="pre-0005-supplied-client",
            ticker="AAPL",
            side="sell",
            order_type="market",
            qty=Decimal("1"),
            status=OrderStatus.CANCELED.value,
            broker_order_id="pre-0005-supplied-order",
            acceptance_state="fill_reconcile_required",
            last_error_code="legacy_unverified_fill",
        )
        session.add(order)
        session.flush()
        session.add(
            Fill(
                ticker="AAPL",
                side="sell",
                qty=Decimal("1"),
                price=Decimal("1"),
                broker_fill_id="pre-0005-caller-supplied-id",
                reconciliation_state="quarantined",
                filled_at=utcnow() - timedelta(minutes=2),
            )
        )
        session.commit()
        order_id = order.id

    remote = OrderResult(
        "pre-0005-supplied-client",
        "pre-0005-supplied-order",
        OrderStatus.CANCELED,
        filled_qty=Decimal("1"),
        avg_fill_price=Decimal("1"),
    )
    broker._orders_by_id["pre-0005-supplied-order"] = remote
    broker._orders_by_key["pre-0005-supplied-client"] = remote
    exact_time = utcnow()
    broker.activities = [
        BrokerFill(
            broker_fill_id="pre-0005-caller-supplied-id",
            broker_order_id="pre-0005-supplied-order",
            ticker="AAPL",
            side="sell",
            qty=Decimal("1"),
            price=Decimal("1"),
            filled_at=exact_time,
        )
    ]

    before = service.snapshot_service.assemble_for_execution("AAPL")
    first = service.reconciliation.reconcile()
    restarted = make_service(broker=broker)
    replay = restarted.reconciliation.reconcile()
    after = restarted.snapshot_service.assemble_for_execution("AAPL")

    assert before.realized_pnl_today == Decimal("0")
    assert before.broker_reconciled is False
    assert before.daily_pnl_complete is False
    assert first.inserted_fills == 0
    assert replay.inserted_fills == 0
    assert after.realized_pnl_today == Decimal("-99")
    assert after.broker_reconciled is True
    assert after.daily_pnl_complete is True
    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        recovered = session.scalar(
            select(Fill).where(
                Fill.broker_fill_id
                == "pre-0005-caller-supplied-id"
            )
        )
        assert order.acceptance_state == "accepted"
        assert recovered.reconciliation_state == "trusted"
        assert recovered.filled_at == exact_time
        assert session.scalar(
            select(func.count()).select_from(Fill).where(
                Fill.broker_fill_id
                == "pre-0005-caller-supplied-id"
            )
        ) == 1


def test_trusted_duplicate_replay_never_supersedes_additional_legacy_rows(
    make_service,
):
    broker = ActivityBroker()
    service = make_service(broker=broker)
    with service.session_factory() as session:
        order = Order(
            idempotency_key="single-exact-multiple-legacy-client",
            ticker="AAPL",
            side="sell",
            order_type="market",
            qty=Decimal("1"),
            status=OrderStatus.CANCELED.value,
            broker_order_id="single-exact-multiple-legacy-order",
            acceptance_state="fill_reconcile_required",
            last_error_code="legacy_unverified_fill",
        )
        session.add(order)
        session.flush()
        for index in range(3):
            session.add(
                Fill(
                    order_id=order.id,
                    ticker="AAPL",
                    side="sell",
                    qty=Decimal("1"),
                    price=Decimal("90"),
                    broker_fill_id=f"matching-legacy-{index}",
                    reconciliation_state="quarantined",
                    filled_at=utcnow() - timedelta(minutes=index + 1),
                )
            )
        session.commit()
        order_id = order.id

    remote = OrderResult(
        "single-exact-multiple-legacy-client",
        "single-exact-multiple-legacy-order",
        OrderStatus.CANCELED,
        filled_qty=Decimal("1"),
        avg_fill_price=Decimal("90"),
    )
    broker._orders_by_id["single-exact-multiple-legacy-order"] = remote
    broker._orders_by_key["single-exact-multiple-legacy-client"] = remote
    exact = BrokerFill(
        broker_fill_id="one-authoritative-activity",
        broker_order_id="single-exact-multiple-legacy-order",
        ticker="AAPL",
        side="sell",
        qty=Decimal("1"),
        price=Decimal("90"),
        filled_at=utcnow(),
    )
    broker.activities = [exact]

    first = service.reconciliation.reconcile()
    first_replay = service.reconciliation.reconcile()
    restarted = make_service(broker=broker)
    second_replay = restarted.reconciliation.reconcile()

    assert first.inserted_fills == 1
    assert first_replay.inserted_fills == 0
    assert second_replay.inserted_fills == 0
    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        rows = session.scalars(
            select(Fill).where(Fill.order_id == order_id).order_by(Fill.id)
        ).all()
        exact_row = next(
            row
            for row in rows
            if row.broker_fill_id == "one-authoritative-activity"
        )
        assert order.acceptance_state == "fill_reconcile_required"
        assert order.last_error_code == "legacy_unidentified_fill"
        assert exact_row.reconciliation_state == "trusted"
        assert exact_row.ticker == exact.ticker
        assert exact_row.side == exact.side
        assert exact_row.qty == exact.qty
        assert exact_row.price == exact.price
        assert exact_row.filled_at == exact.filled_at
        assert [
            row.reconciliation_state
            for row in rows
            if row.broker_fill_id.startswith("matching-legacy-")
        ] == ["superseded", "quarantined", "quarantined"]

    snapshot = restarted.snapshot_service.assemble_for_execution("AAPL")
    assert snapshot.broker_reconciled is False
    assert snapshot.daily_pnl_complete is False


@pytest.mark.parametrize(
    ("synchronous_status", "filled_qty", "expected_loss"),
    [
        (OrderStatus.PARTIALLY_FILLED, Decimal("0.5"), Decimal("-5")),
        (OrderStatus.FILLED, Decimal("1"), Decimal("-10")),
    ],
)
def test_synchronous_loss_stays_incomplete_until_exact_fill_reconciliation(
    make_service,
    synchronous_status,
    filled_qty,
    expected_loss,
):
    class SynchronousLossBroker(ActivityBroker):
        def submit_order(self, order):
            accepted = super().submit_order(order)
            result = OrderResult(
                accepted.idempotency_key,
                accepted.broker_order_id,
                synchronous_status,
                filled_qty=filled_qty,
                avg_fill_price=Decimal("90"),
            )
            self._orders_by_key[order.idempotency_key] = result
            self._orders_by_id[accepted.broker_order_id] = result
            return result

    broker = SynchronousLossBroker(
        positions=[
            Position(
                "AAPL",
                Decimal("1"),
                Decimal("100"),
                Decimal("100"),
            )
        ]
    )
    service = make_service(broker=broker)
    opened_at = utcnow() - timedelta(minutes=1)
    with service.session_factory() as session:
        session.add(
            Fill(
                ticker="AAPL",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("100"),
                broker_fill_id="opening-lot",
                filled_at=opened_at,
            )
        )
        session.commit()

    order_id = service.propose_order(
        "AAPL", "sell", "market", qty="1"
    )["order_id"]
    service.order_application.approve(
        ApprovalCommand(
            order_id,
            "operator:avi",
            "reviewed synchronous loss",
            utcnow(),
        )
    )
    submission = service.order_submission.submit(order_id)

    assert submission.status is synchronous_status
    incomplete = service.snapshot_service.assemble_for_execution("AAPL")
    assert incomplete.realized_pnl_today == Decimal("0")
    assert incomplete.broker_reconciled is False
    assert incomplete.daily_pnl_complete is False
    blocked = service.propose_order(
        "AAPL", "buy", "market", notional="100"
    )
    assert blocked["status"] == OrderStatus.REJECTED.value
    assert {
        "broker reconciliation is not current",
        "daily P&L snapshot is incomplete",
    }.issubset(blocked["risk_reasons"])

    unresolved = service.reconciliation.reconcile()
    still_incomplete = service.snapshot_service.assemble_for_execution("AAPL")
    assert unresolved.inserted_fills == 0
    assert any(
        "exact activities contain 0" in item
        for item in unresolved.broker_drift
    )
    assert still_incomplete.broker_reconciled is False
    assert still_incomplete.daily_pnl_complete is False

    with service.session_factory() as session:
        order = session.get(Order, order_id)
        broker_order_id = order.broker_order_id
        assert order.acceptance_state == "fill_reconcile_required"
    broker.activities = [
        BrokerFill(
            broker_fill_id=f"exact-{synchronous_status.value}",
            broker_order_id=broker_order_id,
            ticker="AAPL",
            side="sell",
            qty=filled_qty,
            price=Decimal("90"),
            filled_at=utcnow(),
        )
    ]

    first = service.reconciliation.reconcile()
    reconciled = service.snapshot_service.assemble_for_execution("AAPL")
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.acceptance_state == "accepted"
        assert session.scalar(
            select(func.count()).select_from(Fill)
        ) == 2

    assert first.inserted_fills == 1
    assert reconciled.realized_pnl_today == expected_loss
    assert reconciled.broker_reconciled is False
    assert reconciled.daily_pnl_complete is False

    drift_state = service.breakers.get(BreakerScope.broker_drift())
    assert drift_state is not None and drift_state.tripped is True
    service.breakers.reset(
        BreakerScope.broker_drift(),
        actor="operator:reconciliation",
        reason="exact fill activities reviewed",
        prior_health={
            "order_id": order_id,
            "authoritative_fill_qty": str(filled_qty),
        },
        expected_generation=drift_state.generation,
    )
    after_health_reset = service.snapshot_service.assemble_for_execution(
        "AAPL"
    )
    assert after_health_reset.broker_reconciled is True
    assert after_health_reset.daily_pnl_complete is True

    duplicate = service.reconciliation.reconcile()
    after_duplicate = service.snapshot_service.assemble_for_execution("AAPL")

    assert duplicate.inserted_fills == 0
    assert after_duplicate.realized_pnl_today == expected_loss
    assert after_duplicate.broker_reconciled is True
    assert after_duplicate.daily_pnl_complete is True
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.acceptance_state == "accepted"
        assert session.scalar(
            select(func.count()).select_from(Fill)
        ) == 2


def test_fill_reconciliation_is_incremental_and_restart_idempotent(make_service):
    assert hasattr(db_models, "ReconciliationCursor")
    cursor_type = db_models.ReconciliationCursor
    broker = ActivityBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        broker_order_id = order.broker_order_id
        client_order_id = order.idempotency_key
        filled_at = order.submission_started_at + timedelta(seconds=1)

    broker.activities = [
        BrokerFill(
            broker_fill_id="activity-1",
            broker_order_id=broker_order_id,
            ticker="AAPL",
            side="buy",
            qty=Decimal("1"),
            price=Decimal("100"),
            filled_at=filled_at,
        )
    ]
    filled = OrderResult(
        client_order_id,
        broker_order_id,
        OrderStatus.FILLED,
        filled_qty=Decimal("1"),
        avg_fill_price=Decimal("100"),
    )
    broker._orders_by_id[broker_order_id] = filled
    broker._orders_by_key[client_order_id] = filled

    first = service.reconciliation.reconcile()
    with service.session_factory() as session:
        first_cursor_version = session.scalar(
            select(cursor_type.version).where(cursor_type.stream == "fills")
        )
    restarted = make_service(broker=broker)
    second = restarted.reconciliation.reconcile()

    assert first.inserted_fills == 1
    assert second.inserted_fills == 0
    assert broker.activity_calls == [None, filled_at]
    with service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Fill)) == 1
        cursor = session.scalar(
            select(cursor_type).where(cursor_type.stream == "fills")
        )
        assert cursor.broker == "mock"
        assert cursor.last_activity_id == "activity-1"
        assert cursor.last_activity_at == filled_at
        assert cursor.version == first_cursor_version


def test_late_visible_fill_with_equal_timestamp_is_not_lost(make_service):
    broker = ActivityBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        broker_order_id = order.broker_order_id
        filled_at = order.submission_started_at + timedelta(seconds=1)

    first = BrokerFill(
        broker_fill_id="z-first",
        broker_order_id=broker_order_id,
        ticker="AAPL",
        side="buy",
        qty=Decimal("0.5"),
        price=Decimal("100"),
        filled_at=filled_at,
    )
    late = BrokerFill(
        broker_fill_id="a-late",
        broker_order_id=broker_order_id,
        ticker="AAPL",
        side="buy",
        qty=Decimal("0.5"),
        price=Decimal("101"),
        filled_at=filled_at,
    )
    broker.activities = [first]
    assert service.reconciliation.reconcile().inserted_fills == 1
    broker.activities = [first, late]

    report = service.reconciliation.reconcile()

    assert report.inserted_fills == 1
    with service.session_factory() as session:
        fill_ids = set(
            session.scalars(
                select(Fill.broker_fill_id).where(Fill.order_id == order_id)
            ).all()
        )
        assert fill_ids == {"z-first", "a-late"}


def test_fill_and_cursor_roll_back_together_on_commit_failure(make_service):
    broker = ActivityBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        broker_order_id = order.broker_order_id
        filled_at = order.submission_started_at + timedelta(seconds=1)

    broker.activities = [
        BrokerFill(
            broker_fill_id="rollback-fill",
            broker_order_id=broker_order_id,
            ticker="AAPL",
            side="buy",
            qty=Decimal("1"),
            price=Decimal("100"),
            filled_at=filled_at,
        )
    ]
    session_type = service.session_factory.class_

    def fail_after_flush(session):
        if any(
            isinstance(row, db_models.ReconciliationCursor)
            for row in session.new
        ):
            session.flush()
            raise RuntimeError("forced commit failure")

    event.listen(session_type, "before_commit", fail_after_flush)
    try:
        report = service.reconciliation.reconcile()
    finally:
        event.remove(session_type, "before_commit", fail_after_flush)

    assert report.inserted_fills == 0
    assert any("batch not committed" in drift for drift in report.broker_drift)
    with service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Fill)) == 0
        assert session.scalar(
            select(func.count()).select_from(db_models.ReconciliationCursor)
        ) == 0


def test_fill_activity_network_failure_leaves_cursor_unchanged(make_service):
    assert hasattr(db_models, "ReconciliationCursor")
    cursor_type = db_models.ReconciliationCursor
    broker = ActivityBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        broker_order_id = order.broker_order_id
        filled_at = order.submission_started_at + timedelta(seconds=1)

    broker.activities = [
        BrokerFill(
            broker_fill_id="activity-1",
            broker_order_id=broker_order_id,
            ticker="AAPL",
            side="buy",
            qty=Decimal("0.5"),
            price=Decimal("100"),
            filled_at=filled_at,
        )
    ]
    service.reconciliation.reconcile()
    with service.session_factory() as session:
        before = session.scalar(select(cursor_type).where(cursor_type.stream == "fills"))
        before_state = (
            before.last_activity_id,
            before.last_activity_at,
            before.version,
        )

    broker.fail_activities = True
    report = service.reconciliation.reconcile()

    assert any("fill activities" in drift for drift in report.broker_drift)
    with service.session_factory() as session:
        after = session.scalar(select(cursor_type).where(cursor_type.stream == "fills"))
        assert (
            after.last_activity_id,
            after.last_activity_at,
            after.version,
        ) == before_state


def test_service_compatibility_methods_serialize_reports(make_service):
    service = make_service()

    sync = service.sync_open_orders()
    panic = service.panic(actor="operator:avi", reason="serialization drill")

    assert sync["resolved_unknown"] == 0
    assert sync["unresolved_unknown"] == []
    assert sync["synced_orders"] == 0
    assert sync["inserted_fills"] == 0
    assert sync["broker_drift"] == []
    assert panic["safe"] is True
    assert panic["confirmed_canceled"] == []
    assert panic["unconfirmed_order_ids"] == []
    assert panic["remote_open_order_ids"] == []
