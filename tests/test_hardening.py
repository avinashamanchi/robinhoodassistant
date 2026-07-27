"""Phase 5 hardening: partial fills, fill idempotency, cancel/replace,
startup reconciliation, and an end-to-end kill-switch drill."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select

from trading_assistant.assets import AssetClass
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import OrderResult, OrderStatus, Position
from trading_assistant.db import models as db_models
from trading_assistant.db.models import (
    FILL_RECONCILIATION_REQUIRED,
    AuditEvent,
    Fill,
    Order,
)
from trading_assistant.dependencies import (
    RequiredDependencyUnavailable,
)
from trading_assistant.risk.breakers import BreakerScope


def _submitted(svc, notional="400") -> int:
    order_id = svc.propose_order(
        "AAPL",
        "buy",
        "market",
        notional=notional,
        actor="operator:test",
        reason="hardening test proposal",
        request_id="hardening-test-proposal",
    )["order_id"]
    svc.approve_order(
        order_id,
        actor="operator:test",
        reason="hardening test",
        request_id="hardening-test-approval",
    )  # -> SUBMITTED
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
    result = svc.cancel_live_order(
        oid,
        actor="operator:test",
        reason="hardening cancellation",
        request_id="hardening-test-cancel",
    )
    assert result["status"] == "canceled"
    assert "error" in svc.cancel_live_order(
        oid,
        actor="operator:test",
        reason="hardening duplicate cancellation",
        request_id="hardening-test-cancel-duplicate",
    )  # cannot cancel twice


class ProviderSecretCancelFailure(RuntimeError):
    pass


class ProviderSecretStatusFailure(RuntimeError):
    pass


class IndeterminateCancelBroker(MockBroker):
    def cancel_order(self, order_id):
        raise ProviderSecretCancelFailure(
            "provider-secret-cancel-message"
        )

    def get_order_status(self, order_id):
        raise ProviderSecretStatusFailure(
            "provider-secret-status-message"
        )


def test_indeterminate_cancel_latch_has_exact_atomic_audit_and_sanitized_fault(
    make_service,
    caplog,
):
    service = make_service(broker=IndeterminateCancelBroker())
    order_id = _submitted(service)
    context = {
        "actor": "operator:cancel-latch",
        "reason": "review indeterminate cancellation",
        "request_id": "indeterminate-cancel-latch",
    }

    result = service.cancel_live_order(order_id, **context)

    assert result == {
        "order_id": order_id,
        "status": OrderStatus.SUBMITTED.value,
        "error": "broker cancellation could not be confirmed",
    }
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        latch_audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "order.cancel_latch",
                AuditEvent.target_id == str(order_id),
            )
        )
        persisted_text = " ".join(
            [
                *(row.detail_json for row in session.scalars(
                    select(AuditEvent)
                )),
                *(row.reason for row in session.scalars(
                    select(db_models.RiskEvent)
                )),
                *(row.reason for row in session.scalars(
                    select(db_models.CircuitBreakerState)
                )),
            ]
        )
    assert order.acceptance_state == FILL_RECONCILIATION_REQUIRED
    assert order.last_error_code == "indeterminate_cancel"
    assert (
        latch_audit.actor,
        latch_audit.reason,
        latch_audit.request_id,
        latch_audit.result_code,
    ) == (
        context["actor"],
        context["reason"],
        context["request_id"],
        "indeterminate_cancel",
    )
    exposed = f"{result} {persisted_text} {caplog.text}"
    assert "provider-secret" not in exposed
    assert "ProviderSecretCancelFailure" not in exposed
    assert "ProviderSecretStatusFailure" not in exposed


def test_indeterminate_cancel_latch_rolls_back_on_audit_failure_but_breaker_stays(
    make_service,
):
    service = make_service(broker=IndeterminateCancelBroker())
    order_id = _submitted(service)
    with service.session_factory() as session:
        before = session.get(Order, order_id)
        before_state = (
            before.acceptance_state,
            before.last_error_code,
            before.version,
        )

    def fail_latch_audit(session, flush_context, instances):
        if any(
            isinstance(row, AuditEvent)
            and row.action == "order.cancel_latch"
            for row in session.new
        ):
            raise RuntimeError("injected cancel latch audit failure")

    session_type = service.session_factory.class_
    event.listen(session_type, "before_flush", fail_latch_audit)
    try:
        with pytest.raises(
            RuntimeError,
            match="injected cancel latch audit failure",
        ):
            service.cancel_live_order(
                order_id,
                actor="operator:cancel-latch",
                reason="rollback indeterminate cancellation latch",
                request_id="indeterminate-cancel-latch-rollback",
            )
    finally:
        event.remove(session_type, "before_flush", fail_latch_audit)

    assert service.breakers.is_tripped(
        BreakerScope.broker_drift()
    ) is True
    with service.session_factory() as session:
        after = session.get(Order, order_id)
        breaker = session.get(
            db_models.CircuitBreakerState,
            BreakerScope.broker_drift().key,
        )
        latch_audits = session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "order.cancel_latch",
                AuditEvent.request_id
                == "indeterminate-cancel-latch-rollback",
            )
        ).all()
    assert (
        after.acceptance_state,
        after.last_error_code,
        after.version,
    ) == before_state
    assert breaker.reason == (
        f"indeterminate broker cancellation for order {order_id}"
    )
    assert latch_audits == []


def test_cancel_nested_reconciliation_fault_keeps_operator_provenance(
    make_service,
):
    class PostCancelDriftBroker(MockBroker):
        expose_drift = False

        def cancel_order(self, order_id):
            result = super().cancel_order(order_id)
            self.expose_drift = True
            return result

        def get_open_orders(self):
            rows = super().get_open_orders()
            if self.expose_drift:
                rows.append(
                    OrderResult(
                        "untracked-client-order",
                        "untracked-broker-order",
                        OrderStatus.SUBMITTED,
                    )
                )
            return rows

    service = make_service(broker=PostCancelDriftBroker())
    order_id = _submitted(service)
    with service.session_factory() as session:
        prior_audit_id = session.scalar(
            select(func.max(AuditEvent.id))
        ) or 0

    result = service.cancel_live_order(
        order_id,
        actor="operator:avi",
        reason="operator reviewed nested reconciliation",
        request_id="cancel-nested-reconciliation",
    )

    assert result["status"] == "canceled"
    with service.session_factory() as session:
        audits = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.id > prior_audit_id)
            .order_by(AuditEvent.id)
        ).all()
    assert {
        "circuit_breaker.trip",
        "orders.sync",
        "order.cancel",
    }.issubset({audit.action for audit in audits})
    for audit in audits:
        assert audit.actor == "operator:avi"
        assert audit.reason == "operator reviewed nested reconciliation"
        assert audit.request_id == "cancel-nested-reconciliation"


def test_operator_mutation_services_require_explicit_context(make_service):
    svc = make_service()

    with pytest.raises(TypeError):
        svc.sync_open_orders()
    with pytest.raises(TypeError):
        svc.reconcile_positions()
    with pytest.raises(TypeError):
        svc.cancel_live_order(999)


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
        result = svc.cancel_live_order(
            oid,
            actor="operator:test",
            reason="transaction boundary cancellation",
            request_id="hardening-transaction-cancel",
        )
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

    result = svc.cancel_live_order(
        oid,
        actor="operator:test",
        reason="partial fill cancellation",
        request_id="hardening-partial-fill-cancel",
    )

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

    result = svc.cancel_live_order(
        oid,
        actor="operator:test",
        reason="cancel race reconciliation",
        request_id="hardening-cancel-race",
    )

    assert result["status"] == "filled"
    assert "exact fill confirmation" in result["error"]
    assert svc.get_order_status(oid)["status"] == "filled"


def test_replace_order(make_service):
    svc = make_service()
    oid = _submitted(svc)
    result = svc.replace_order(
        oid,
        ticker="AAPL",
        side="buy",
        order_type="market",
        notional="200",
        actor="operator:test",
        reason="hardening replacement",
        request_id="hardening-replace",
    )
    assert result["canceled"]["status"] == "canceled"
    assert result["replacement"]["status"] == "proposed"


# ── startup reconciliation ──────────────────────────────────────
def test_reconcile_detects_drift(make_service):
    # Broker reports a position that local fills don't account for.
    broker = MockBroker(positions=[Position("AAPL", Decimal("10"), Decimal("100"), Decimal("100"))])
    svc = make_service(broker=broker)
    result = svc.reconcile_positions(
        actor="operator:test",
        reason="hardening drift reconciliation",
        request_id="hardening-reconcile-drift",
    )
    assert result["reconciled"] is False
    assert "AAPL" in result["drift"]


def test_reconcile_clean_when_matching(make_service):
    svc = make_service()  # no positions, no fills
    assert svc.reconcile_positions(
        actor="operator:test",
        reason="hardening clean reconciliation",
        request_id="hardening-reconcile-clean",
    )["reconciled"] is True


# ── kill-switch drill (end-to-end) ──────────────────────────────
def test_killswitch_drill(make_service):
    svc = make_service()
    now = datetime.now(timezone.utc)
    # Insert a realized -$5,000 round trip for today, directly as fills.
    with svc.session_factory() as s:
        s.add(
            Fill(
                ticker="AAPL",
                side="buy",
                qty=Decimal("100"),
                price=Decimal("100"),
                broker_fill_id="killswitch-drill-open",
                filled_at=now,
            )
        )
        s.add(
            Fill(
                ticker="AAPL",
                side="sell",
                qty=Decimal("100"),
                price=Decimal("50"),
                broker_fill_id="killswitch-drill-close",
                filled_at=now,
            )
        )
        s.commit()

    tripped = svc.enforce_daily_loss_limits(
        actor="daemon:daily-loss",
        reason="scheduled daily loss enforcement",
        request_id="daily-loss-cycle",
    )
    assert tripped["equity"] is True
    assert tripped["crypto"] is False          # crypto independent
    loss_state = svc.breakers.get(BreakerScope.loss(AssetClass.EQUITY))
    assert loss_state is not None
    assert loss_state.actor == "daemon:daily-loss"
    with svc.session_factory() as session:
        audit = (
            session.query(AuditEvent)
            .filter_by(action="circuit_breaker.trip")
            .order_by(AuditEvent.id.desc())
            .first()
        )
    assert audit is not None
    assert audit.actor == "daemon:daily-loss"
    assert audit.reason == "scheduled daily loss enforcement"
    assert audit.request_id == "daily-loss-cycle"

    # New equity orders are now blocked...
    blocked = svc.propose_order(
        "AAPL",
        "buy",
        "market",
        notional="100",
        actor="operator:test",
        reason="hardening breaker proposal",
        request_id="hardening-breaker-proposal",
    )
    assert blocked["status"] == "rejected"
    assert any("circuit breaker" in r for r in blocked["risk_reasons"])

    # These deliberately direct, order-less fills cannot satisfy broker/fill
    # health proof. Reset must fail closed rather than clearing the breaker.
    observed = svc.breakers.get(BreakerScope.loss(AssetClass.EQUITY))
    assert observed is not None
    with pytest.raises(RequiredDependencyUnavailable):
        svc.reset_killswitch(
            AssetClass.EQUITY,
            actor="operator:test",
            reason="manual drill health reviewed",
            expected_generation=observed.generation,
            request_id="hardening-breaker-reset",
        )
    still_blocked = svc.propose_order(
        "AAPL",
        "buy",
        "market",
        notional="100",
        actor="operator:test",
        reason="hardening post-reset proposal",
        request_id="hardening-post-reset-proposal",
    )
    assert still_blocked["status"] == "rejected"
    assert any(
        "circuit breaker" in reason
        for reason in still_blocked["risk_reasons"]
    )


def test_operational_trip_all_uses_process_safe_global_breaker(make_service):
    svc = make_service()

    svc.trip_all_killswitches(
        actor="daemon:startup",
        reason="startup reconciliation failed",
        request_id="startup-reconciliation-failure",
    )

    state = svc.breakers.get(BreakerScope.operator_global())
    assert state is not None and state.tripped is True
    assert state.actor == "daemon:startup"
    with svc.session_factory() as session:
        audit = session.query(AuditEvent).filter_by(
            action="circuit_breaker.trip"
        ).one()
    assert audit.actor == "daemon:startup"
    assert audit.reason == "startup reconciliation failed"
    assert audit.request_id == "startup-reconciliation-failure"


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
        actor="operator:test",
        reason="hardening panic rule setup",
        request_id="hardening-panic-rule",
    )

    res = svc.panic(
        actor="operator:test",
        reason="panic drill",
        request_id="hardening-panic",
    )
    assert res["safe"] is True
    assert len(res["confirmed_canceled"]) == 1
    with svc.session_factory() as s:
        assert KillSwitch.is_tripped(s, "operator_global") is True
        assert s.query(Rule).filter_by(state="active").count() == 0

    # Idempotent: a second panic is a no-op on already-flat state.
    res2 = svc.panic(
        actor="operator:test",
        reason="repeat panic drill",
        request_id="hardening-panic-repeat",
    )
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

    result = svc.panic(
        actor="operator:test",
        reason="cancel failure drill",
        request_id="hardening-panic-failure",
    )

    assert result["safe"] is False
    assert result["confirmed_canceled"] == []
    assert result["unconfirmed_order_ids"] == [oid]
    assert svc.get_order_status(oid)["status"] == "submitted"
