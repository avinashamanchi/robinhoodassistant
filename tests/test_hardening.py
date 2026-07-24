"""Phase 5 hardening: partial fills, fill idempotency, cancel/replace,
startup reconciliation, and an end-to-end kill-switch drill."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select

from trading_assistant.assets import AssetClass
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import Position
from trading_assistant.db.models import Fill, Order
from trading_assistant.risk.breakers import BreakerScope


def _submitted(svc, notional="400") -> int:
    order_id = svc.propose_order("AAPL", "buy", "market", notional=notional)["order_id"]
    svc.approve_order(order_id, actor="operator:test", reason="hardening test")  # -> SUBMITTED
    return order_id


@pytest.mark.parametrize(
    "payloads",
    [
        (
            {
                "qty": "NaN",
                "price": "100",
                "broker_fill_id": "corrupt-direct-fill",
            },
        ),
        (
            {
                "qty": "1",
                "price": "100",
                "broker_fill_id": "duplicate-direct-fill",
            },
            {
                "qty": "2",
                "price": "90",
                "broker_fill_id": "duplicate-direct-fill",
            },
        ),
    ],
)
def test_direct_fill_mutation_path_is_not_available(
    make_service,
    payloads,
):
    svc = make_service()
    oid = _submitted(svc)

    for payload in payloads:
        with pytest.raises(AttributeError):
            getattr(svc, "record_fill")(oid, **payload)

    with svc.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Fill)) == 0


# ── cancel / replace ────────────────────────────────────────────
def test_cancel_live_order(make_service):
    svc = make_service()
    oid = _submitted(svc)
    result = svc.cancel_live_order(oid)
    assert result["status"] == "canceled"
    assert "error" in svc.cancel_live_order(oid)       # cannot cancel twice


def test_cancel_broker_io_occurs_without_sqlite_transaction(
    make_service,
    engine,
):
    active_transactions = 0
    observed_during_cancel = None

    class TransactionInspectingBroker(MockBroker):
        def cancel_order(self, order_id):
            nonlocal observed_during_cancel
            observed_during_cancel = active_transactions
            return super().cancel_order(order_id)

    def transaction_began(_connection):
        nonlocal active_transactions
        active_transactions += 1

    def transaction_ended(_connection):
        nonlocal active_transactions
        active_transactions -= 1

    svc = make_service(broker=TransactionInspectingBroker())
    oid = _submitted(svc)
    event.listen(engine, "begin", transaction_began)
    event.listen(engine, "commit", transaction_ended)
    event.listen(engine, "rollback", transaction_ended)
    try:
        result = svc.cancel_live_order(oid)
    finally:
        event.remove(engine, "begin", transaction_began)
        event.remove(engine, "commit", transaction_ended)
        event.remove(engine, "rollback", transaction_ended)

    assert result["status"] == "canceled"
    assert observed_during_cancel == 0
    assert active_transactions == 0


def test_cancel_result_with_cumulative_partial_fill_sets_reconciliation_latch(
    make_service,
):
    from trading_assistant.broker.models import OrderResult, OrderStatus

    class CanceledAfterPartial(MockBroker):
        def get_fill_activities(self, after=None):
            return []

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

    svc = make_service(broker=CanceledAfterPartial())
    oid = _submitted(svc)

    result = svc.cancel_live_order(oid)

    assert result["status"] == OrderStatus.CANCELED.value
    with svc.session_factory() as session:
        order = session.get(Order, oid)
        assert order.acceptance_state == "fill_reconcile_required"


def test_cancel_race_records_broker_fill_instead_of_claiming_canceled(make_service):
    from trading_assistant.broker.models import OrderResult, OrderStatus

    class FilledDuringCancel(MockBroker):
        def cancel_order(self, order_id):
            current = self.get_order_status(order_id)
            filled = OrderResult(
                current.idempotency_key,
                order_id,
                OrderStatus.FILLED,
                filled_qty=Decimal("4"),
                avg_fill_price=Decimal("100"),
            )
            self._orders_by_id[order_id] = filled
            self._orders_by_key[current.idempotency_key] = filled
            return filled

    broker = FilledDuringCancel()
    svc = make_service(broker=broker)
    oid = _submitted(svc)

    result = svc.cancel_live_order(oid)

    assert result["status"] == "filled"
    assert "not canceled" in result["error"]
    assert svc.get_order_status(oid)["status"] == "filled"


def test_replace_order(make_service):
    svc = make_service()
    oid = _submitted(svc)
    result = svc.replace_order(
        oid, ticker="AAPL", side="buy", order_type="market", notional="200"
    )
    assert result["canceled"]["status"] == "canceled"
    assert result["replacement"]["status"] == "proposed"


# ── startup reconciliation ──────────────────────────────────────
def test_reconcile_detects_drift(make_service):
    # Broker reports a position that local fills don't account for.
    broker = MockBroker(positions=[Position("AAPL", Decimal("10"), Decimal("100"), Decimal("100"))])
    svc = make_service(broker=broker)
    result = svc.reconcile_positions()
    assert result["reconciled"] is False
    assert "AAPL" in result["drift"]


def test_reconcile_clean_when_matching(make_service):
    svc = make_service()  # no positions, no fills
    assert svc.reconcile_positions()["reconciled"] is True


# ── kill-switch drill (end-to-end) ──────────────────────────────
def test_killswitch_drill(make_service):
    svc = make_service()
    now = datetime.now(timezone.utc)
    # Insert a realized -$5,000 round trip for today, directly as fills.
    with svc.session_factory() as s:
        s.add(Fill(ticker="AAPL", side="buy", qty=Decimal("100"), price=Decimal("100"), filled_at=now))
        s.add(Fill(ticker="AAPL", side="sell", qty=Decimal("100"), price=Decimal("50"), filled_at=now))
        s.commit()

    tripped = svc.enforce_daily_loss_limits()
    assert tripped["equity"] is True
    assert tripped["crypto"] is False          # crypto independent
    loss_state = svc.breakers.get(BreakerScope.loss(AssetClass.EQUITY))
    assert loss_state is not None
    assert loss_state.actor == "daemon:daily-loss"

    # New equity orders are now blocked...
    blocked = svc.propose_order("AAPL", "buy", "market", notional="100")
    assert blocked["status"] == "rejected"
    assert any("circuit breaker" in r for r in blocked["risk_reasons"])

    # Resetting the persisted breaker does not erase the complete loss snapshot.
    # Risk remains blocked until the account is genuinely back within limits.
    observed = svc.breakers.get(BreakerScope.loss(AssetClass.EQUITY))
    assert observed is not None
    svc.reset_killswitch(
        AssetClass.EQUITY,
        actor="operator:test",
        reason="manual drill health reviewed",
        expected_generation=observed.generation,
    )
    still_blocked = svc.propose_order("AAPL", "buy", "market", notional="100")
    assert still_blocked["status"] == "rejected"
    assert "daily total-loss limit reached" in still_blocked["risk_reasons"]


def test_operational_trip_all_uses_process_safe_global_breaker(make_service):
    svc = make_service()

    svc.trip_all_killswitches("startup reconciliation failed")

    state = svc.breakers.get(BreakerScope.operator_global())
    assert state is not None and state.tripped is True
    assert state.actor == "daemon:operations"


# ── panic button (D5) ───────────────────────────────────────────
def test_panic_flattens_everything(make_service):
    from trading_assistant.db.models import Rule
    from trading_assistant.risk.killswitch import KillSwitch

    svc = make_service()
    oid = _submitted(svc)  # a live SUBMITTED order
    svc.create_conditional_rule(
        "AAPL",
        {"price_below": 999},
        {"side": "sell", "qty": "1"},
    )

    res = svc.panic(actor="operator:test", reason="panic drill")
    assert res["safe"] is True
    assert len(res["confirmed_canceled"]) == 1
    with svc.session_factory() as s:
        assert KillSwitch.is_tripped(s, "operator_global") is True
        assert s.query(Rule).filter_by(state="active").count() == 0

    # Idempotent: a second panic is a no-op on already-flat state.
    res2 = svc.panic(actor="operator:test", reason="repeat panic drill")
    assert res2["safe"] is True
    assert res2["confirmed_canceled"] == []


def test_panic_never_claims_an_unconfirmed_broker_cancel(make_service):
    class CancelFailureBroker(MockBroker):
        def cancel_order(self, order_id):
            raise ConnectionError("broker unavailable")

        def get_order_status(self, order_id):
            raise ConnectionError("broker unavailable")

    svc = make_service(broker=CancelFailureBroker())
    oid = _submitted(svc)

    result = svc.panic(actor="operator:test", reason="cancel failure drill")

    assert result["safe"] is False
    assert result["confirmed_canceled"] == []
    assert result["unconfirmed_order_ids"] == [oid]
    assert svc.get_order_status(oid)["status"] == "submitted"
