"""Broker-truth reconciliation and truthful panic reporting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select

from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import (
    BrokerFill,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from trading_assistant.db import models as db_models
from trading_assistant.db.models import CircuitBreakerState, Fill, Order, utcnow
from trading_assistant.orders.application import ApprovalCommand
from trading_assistant.risk.breakers import BreakerScope


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


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("side", "sell"),
        ("side", "exercise"),
        ("ticker", "MSFT"),
        ("qty", Decimal("0")),
        ("qty", Decimal("NaN")),
        ("price", Decimal("0")),
        ("price", Decimal("Infinity")),
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

    filled_at = datetime(2026, 7, 24, 17, 0, tzinfo=timezone.utc)
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
        broker_order_id = session.get(Order, order_id).broker_order_id

    filled_at = datetime(2026, 7, 24, 17, 0, tzinfo=timezone.utc)
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
        broker_order_id = session.get(Order, order_id).broker_order_id

    broker.activities = [
        BrokerFill(
            broker_fill_id="rollback-fill",
            broker_order_id=broker_order_id,
            ticker="AAPL",
            side="buy",
            qty=Decimal("1"),
            price=Decimal("100"),
            filled_at=datetime(2026, 7, 24, 18, 0, tzinfo=timezone.utc),
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

    filled_at = datetime(2026, 7, 24, 17, 0, tzinfo=timezone.utc)
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
