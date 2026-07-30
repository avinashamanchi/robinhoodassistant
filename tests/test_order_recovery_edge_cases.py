"""Durable order-recovery edge cases over isolated SQLite state.

These tests intentionally use only public recovery/persistence interfaces and
deterministic broker doubles.  No broker mutation method is called.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from trading_assistant.assets import AssetClass
from trading_assistant.broker.base import BrokerDataIntegrityError
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import (
    FILL_RECONCILIATION_REQUIRED,
    AuditEvent,
    CircuitBreakerState,
    Fill,
    Heartbeat,
    Order,
    Proposal,
    Rule,
    RuleGroup,
    StartupReconciliationState,
)
from trading_assistant.db.session import (
    create_db_engine,
    make_session_factory,
)
from trading_assistant.dependencies import RequiredDependencyUnavailable
from trading_assistant.orders.repository import OrderRepository
from trading_assistant.orders.safety_state import (
    enumerate_unsafe_local_state,
    read_persisted_safety_truth,
)
from trading_assistant.orders.startup import (
    StartupReconciliationGate,
    validate_startup_reconciliation_snapshot,
)
from trading_assistant.risk.breakers import BreakerScope
from trading_assistant.risk.engine import BreakerTripIntent
from trading_assistant.security.sensitive_fields import persist_sensitive


NOW = datetime(2026, 7, 29, 16, 0, tzinfo=timezone.utc)


def _context(label: str) -> dict[str, str]:
    return {
        "actor": "operator:recovery-test",
        "reason": f"order recovery {label}",
        "request_id": f"order-recovery-{label}",
    }


def _persist_order(
    session_factory,
    key: str,
    *,
    status: OrderStatus,
    acceptance_state: str = "not_started",
    broker_order_id: str | None = None,
    last_error_code: str = "",
) -> int:
    with session_factory() as session:
        order = Order(
            idempotency_key=key,
            ticker="AAPL",
            side="buy",
            order_type="market",
            qty=Decimal("1"),
            status=status.value,
            broker_order_id=broker_order_id,
            acceptance_state=acceptance_state,
            last_error_code=last_error_code,
            created_at=NOW,
            updated_at=NOW,
        )
        persist_sensitive(
            session,
            order,
            {"approval_reason": "isolated recovery fixture"},
        )
        session.commit()
        return order.id


def _persist_proposal(
    session_factory,
    order_id: int,
    *,
    group_id: int | None = None,
) -> None:
    with session_factory() as session:
        persist_sensitive(
            session,
            Proposal(
                order_id=order_id,
                source_rule_group_id=group_id,
                ttl_minutes=15,
                created_at=NOW,
                expires_at=NOW + timedelta(days=1),
            ),
            {"reasoning": "isolated recovery proposal"},
        )
        session.commit()


def _persist_breaker(
    session,
    *,
    scope_key: str,
    kind: str,
    target: str,
    tripped: bool,
    generation: int,
) -> None:
    persist_sensitive(
        session,
        CircuitBreakerState(
            scope_key=scope_key,
            kind=kind,
            target=target,
            tripped=tripped,
            actor="operator:recovery-test",
            generation=generation,
            updated_at=NOW,
        ),
        {"reason": "isolated persisted breaker"},
    )


def _startup_gate(
    session_factory,
    *,
    enabled: bool,
) -> StartupReconciliationGate:
    return StartupReconciliationGate(
        session_factory,
        "mock-recovery",
        enabled=enabled,
        clock=lambda: NOW,
    )


def _empty_temp_session_factory(tmp_path, name: str):
    engine = create_db_engine(f"sqlite:///{tmp_path}/{name}.db")
    return engine, make_session_factory(engine)


@pytest.mark.parametrize(
    "changes",
    [
        {"generation": True},
        {"started_at": "not-a-timestamp"},
        {
            "status": "current",
            "completed_generation": 0,
            "completed_at": NOW,
        },
        {
            "status": "failed",
            "completed_generation": 1,
        },
        {"expected_generation": 2},
    ],
)
def test_startup_snapshot_validation_rejects_malformed_or_stale_rows(
    changes,
):
    """A malformed durable row must never become startup authority."""
    values = {
        "generation": 1,
        "completed_generation": 0,
        "status": "required",
        "started_at": NOW,
        "completed_at": None,
        "updated_at": NOW,
        "observed_at": NOW + timedelta(seconds=1),
        "expected_generation": 1,
    }
    values.update(changes)

    validation = validate_startup_reconciliation_snapshot(**values)

    assert validation.valid is False
    assert validation.current is False


def test_startup_snapshot_validation_normalizes_naive_database_times():
    """SQLite UTC values without tzinfo remain valid only as UTC."""
    naive = NOW.replace(tzinfo=None)

    validation = validate_startup_reconciliation_snapshot(
        generation=3,
        completed_generation=3,
        status="current",
        started_at=naive,
        completed_at=naive,
        updated_at=naive,
        observed_at=NOW,
        expected_generation=3,
    )

    assert validation.valid is True
    assert validation.current is True
    assert validation.started_at == NOW
    assert validation.completed_at == NOW
    assert validation.updated_at == NOW


def test_disabled_and_empty_startup_gates_have_explicit_safe_contracts(
    session_factory,
):
    """Disabled mode is explicit; enabled-but-empty state remains blocked."""
    disabled = _startup_gate(session_factory, enabled=False)
    assert disabled.posture() == {
        "status": "not_required",
        "generation": 0,
        "completed_generation": 0,
        "failure_code": None,
    }
    assert disabled.current_generation() == 0
    assert disabled.is_current() is True
    assert disabled.complete(1, evidence={}, **_context("disabled-complete"))
    assert (
        disabled.fail(
            1,
            "broker_timeout",
            evidence={},
            **_context("disabled-fail"),
        )
        is False
    )
    with pytest.raises(RuntimeError, match="gate is disabled"):
        disabled.require(**_context("disabled-require"))
    with pytest.raises(ValueError, match="generation must be positive"):
        disabled.complete(
            0,
            evidence={},
            **_context("disabled-zero-generation"),
        )
    with pytest.raises(ValueError, match="failure code is invalid"):
        disabled.fail(
            1,
            "INVALID CODE",
            evidence={},
            **_context("disabled-invalid-code"),
        )

    enabled = _startup_gate(session_factory, enabled=True)
    assert enabled.posture() == {
        "status": "required",
        "generation": 0,
        "completed_generation": 0,
        "failure_code": None,
    }
    assert enabled.current_generation() == 0
    assert enabled.is_current() is False
    assert (
        enabled.complete(
            1,
            evidence={},
            **_context("empty-complete"),
        )
        is False
    )
    assert (
        enabled.fail(
            1,
            "broker_timeout",
            evidence={},
            **_context("empty-fail"),
        )
        is False
    )
    with pytest.raises(ValueError, match="must be non-empty"):
        enabled.require(
            actor=" ",
            reason="missing actor",
            request_id="empty-startup-context",
        )


def test_startup_failure_and_recovery_survive_restarts_idempotently(
    session_factory,
):
    """Failure stays blocking until a newer durable generation completes."""
    first = _startup_gate(session_factory, enabled=True)
    first_generation = first.require(**_context("first-require"))
    assert first.fail(
        first_generation,
        "broker_timeout",
        evidence={"attempt": 1},
        **_context("first-fail"),
    )

    restarted = _startup_gate(session_factory, enabled=True)
    assert restarted.posture() == {
        "status": "failed",
        "generation": 1,
        "completed_generation": 0,
        "failure_code": "broker_timeout",
    }
    assert restarted.is_current(first_generation) is False

    recovered_generation = restarted.require(
        **_context("recovery-require")
    )
    assert recovered_generation == 2
    assert (
        first.fail(
            first_generation,
            "late_worker_failure",
            evidence={"stale": True},
            **_context("stale-fail"),
        )
        is False
    )
    assert restarted.complete(
        recovered_generation,
        evidence={"open_orders": 0, "positions": 0},
        **_context("recovery-complete"),
    )
    assert restarted.complete(
        recovered_generation,
        evidence={"replay": True},
        **_context("recovery-complete-replay"),
    )
    assert (
        restarted.fail(
            recovered_generation,
            "late_worker_failure",
            evidence={"stale": True},
            **_context("completed-fail"),
        )
        is False
    )

    second_restart = _startup_gate(session_factory, enabled=True)
    assert second_restart.is_current(recovered_generation) is True
    assert second_restart.posture() == {
        "status": "current",
        "generation": 2,
        "completed_generation": 2,
        "failure_code": None,
    }
    with session_factory() as session:
        state = session.get(
            StartupReconciliationState,
            "mock-recovery",
        )
        audit_count = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action.like("startup_reconciliation.%")
            )
        )
    assert state is not None
    assert state.generation == 2
    assert state.completed_generation == 2
    assert audit_count == 4


def test_startup_current_check_fails_closed_after_temp_schema_loss(
    tmp_path,
):
    """A missing durable state table cannot be mistaken for current proof."""
    engine, empty_factory = _empty_temp_session_factory(
        tmp_path,
        "missing-startup-schema",
    )

    try:
        assert (
            _startup_gate(empty_factory, enabled=True).is_current()
            is False
        )
    finally:
        engine.dispose()


def test_persisted_safety_truth_exposes_all_confirmed_unsafe_state(
    session_factory,
):
    """Known unsafe rows remain visible with canonical breaker detail."""
    live_order_id = _persist_order(
        session_factory,
        "safety-live-order",
        status=OrderStatus.SUBMITTED,
        acceptance_state="accepted",
        broker_order_id="remote-live-order",
    )
    latched_order_id = _persist_order(
        session_factory,
        "safety-latched-order",
        status=OrderStatus.CANCELED,
        acceptance_state=FILL_RECONCILIATION_REQUIRED,
        last_error_code="waiting_for_exact_fill",
    )
    with session_factory() as session:
        orphan = Fill(
            order_id=None,
            ticker="AAPL",
            side="buy",
            qty=Decimal("1"),
            price=Decimal("100"),
            broker_fill_id="safety-orphan-fill",
            filled_at=NOW,
        )
        group = RuleGroup(
            group_key="safety-active-group",
            state="active",
            reconciliation_required=True,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add_all([orphan, group])
        session.flush()
        rule = Rule(
            group_id=group.id,
            ticker="AAPL",
            condition_json='{"price_below":"90"}',
            action_json='{"side":"buy","notional":"10"}',
            state="processing",
            created_at=NOW,
        )
        session.add(rule)
        _persist_breaker(
            session,
            scope_key="operator_global",
            kind="operator_global",
            target="",
            tripped=True,
            generation=1,
        )
        _persist_breaker(
            session,
            scope_key="data:equity",
            kind="data",
            target="equity",
            tripped=False,
            generation=0,
        )
        session.add(
            Heartbeat(
                source="daemon",
                at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
        )
        session.commit()
        orphan_id = orphan.id
        group_id = group.id
        rule_id = rule.id

    with session_factory() as session:
        truth = read_persisted_safety_truth(session)

    assert truth.state == "unsafe"
    assert truth.complete is True
    assert truth.operator_global_tripped is True
    assert truth.operator_global_generation == 1
    assert tuple(item.scope for item in truth.active_breakers) == (
        "operator_global",
    )
    assert truth.breaker("data:equity") is not None
    assert truth.breaker("missing") is None
    assert truth.unsafe_local_state.unsafe_order_ids == (
        live_order_id,
        latched_order_id,
    )
    assert truth.unsafe_local_state.unsafe_fill_ids == (orphan_id,)
    assert truth.unsafe_local_state.active_rule_ids == (rule_id,)
    assert truth.unsafe_local_state.unsafe_rule_group_ids == (group_id,)
    payload = truth.as_dict()
    assert payload["state"] == "unsafe"
    assert payload["local_enumeration"] == "confirmed"
    assert payload["remote_broker_open_orders"] == "unverified"
    assert payload["active_breakers"] == [
        {
            "scope": "operator_global",
            "kind": "operator_global",
            "target": "",
            "generation": 1,
        }
    ]


@pytest.mark.parametrize(
    (
        "scope_key",
        "kind",
        "target",
        "tripped",
        "generation",
        "expected_state",
    ),
    [
        ("loss", "loss", "", False, 0, "unknown"),
        (
            "operator_global:equity",
            "operator_global",
            "",
            False,
            0,
            "unknown",
        ),
        ("mystery", "mystery", "", False, 0, "unknown"),
        ("data:equity", "data", "equity", True, 0, "unsafe"),
        (
            "broker_drift",
            "broker_drift",
            "equity",
            False,
            0,
            "unknown",
        ),
    ],
)
def test_malformed_breaker_or_future_heartbeat_never_looks_clear(
    session_factory,
    scope_key,
    kind,
    target,
    tripped,
    generation,
    expected_state,
):
    """Malformed breaker identity and future liveness are explicit unknowns."""
    with session_factory() as session:
        _persist_breaker(
            session,
            scope_key=scope_key,
            kind=kind,
            target=target,
            tripped=tripped,
            generation=generation,
        )
        session.add(
            Heartbeat(
                source="daemon",
                at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            )
        )
        session.commit()

    truth = read_persisted_safety_truth(session_factory)

    assert truth.state == expected_state
    assert truth.complete is False
    assert truth.heartbeat_at is None
    assert "active_breakers" in truth.unknown_categories
    assert "heartbeat" in truth.unknown_categories


def test_safety_reads_return_explicit_unknown_after_temp_schema_loss(
    tmp_path,
):
    """A failed snapshot acquisition cannot collapse to an empty safe state."""
    engine, empty_factory = _empty_temp_session_factory(
        tmp_path,
        "missing-safety-schema",
    )
    try:
        local = enumerate_unsafe_local_state(empty_factory)
        truth = read_persisted_safety_truth(empty_factory)
    finally:
        engine.dispose()

    assert local.enumeration == "unknown"
    assert local.has_unsafe_state is True
    assert local.unknown_categories == (
        "live_or_unknown_orders",
        "latched_orders",
        "unsafe_fills",
        "active_rules",
        "unsafe_rule_groups",
    )
    assert truth.state == "unknown"
    assert truth.complete is False
    assert truth.operator_global_tripped is None
    assert truth.unknown_categories == (
        "live_or_unknown_orders",
        "latched_orders",
        "unsafe_fills",
        "active_rules",
        "unsafe_rule_groups",
        "active_breakers",
        "heartbeat",
    )


def test_repository_invalid_states_reject_without_mutating_order(
    session_factory,
):
    """Invalid context/state must fail before an order can advance."""
    order_id = _persist_order(
        session_factory,
        "repository-invalid-state",
        status=OrderStatus.PROPOSED,
    )
    repository = OrderRepository(session_factory)

    with pytest.raises(ValueError, match="must be non-empty"):
        repository.record_approval(
            order_id,
            " ",
            "invalid context",
            "repository-invalid-context",
            NOW,
        )
    with pytest.raises(ValueError, match="stable breaker scope"):
        repository.claim_submission(
            order_id,
            NOW,
            (),
            **_context("invalid-scope"),
        )
    with pytest.raises(ValueError, match="invalid submission result"):
        repository.record_submission_result(
            order_id,
            OrderStatus.PROPOSED,
            None,
            "",
            NOW,
            **_context("invalid-result-status"),
        )
    with pytest.raises(ValueError, match="must be non-empty"):
        repository.record_submission_result(
            order_id,
            OrderStatus.SUBMITTED,
            "remote-invalid",
            "",
            NOW,
            actor="",
            reason="invalid result context",
            request_id="repository-invalid-result-context",
        )
    with pytest.raises(ValueError, match="reason must be non-empty"):
        repository.record_invalid_broker_data(
            order_id,
            " ",
            NOW,
            broker_order_id=None,
            error_code="invalid_broker_data",
            actor="operator:test",
            context_reason="invalid payload",
            request_id="repository-empty-broker-reason",
        )
    with pytest.raises(ValueError, match="actor, reason, and request_id"):
        repository.record_invalid_broker_data(
            order_id,
            "bad payload",
            NOW,
            broker_order_id=None,
            error_code="invalid_broker_data",
            actor="",
            context_reason="invalid payload",
            request_id="repository-empty-broker-actor",
        )
    with pytest.raises(ValueError, match="unsupported broker data"):
        repository.record_invalid_broker_data(
            order_id,
            "bad payload",
            NOW,
            broker_order_id=None,
            error_code="unexpected_error",
            actor="operator:recovery-test",
            context_reason="unsupported broker code",
            request_id="order-recovery-unsupported-broker-code",
        )
    assert (
        repository.resolve_acceptance(
            order_id,
            None,
            OrderStatus.CANCELED,
            Decimal("0"),
            NOW,
            **_context("missing-broker-id"),
        )
        is False
    )
    with pytest.raises(RuntimeError, match="changed during risk rejection"):
        repository.record_pre_submission_rejection(
            order_id,
            ("stale quote",),
            NOW,
            **_context("wrong-rejection-state"),
        )
    assert (
        repository.expire_approved(
            order_id,
            NOW,
            **_context("wrong-expiry-state"),
        )
        is False
    )

    with session_factory() as session:
        order = session.get(Order, order_id)
        audit_count = session.scalar(
            select(func.count()).select_from(AuditEvent)
        )
    assert order is not None
    assert order.status == OrderStatus.PROPOSED.value
    assert order.version == 0
    assert audit_count == 0


def test_repository_persists_breaker_before_blocked_claim_across_restart(
    session_factory,
):
    """A risk trip and denied claim commit atomically and remain blocking."""
    order_id = _persist_order(
        session_factory,
        "repository-breaker-claim",
        status=OrderStatus.APPROVAL_RECORDED,
    )
    trip = BreakerTripIntent(
        BreakerScope.data(AssetClass.EQUITY),
        "quote integrity failed",
    )
    first = OrderRepository(session_factory)

    assert (
        first.claim_submission(
            order_id,
            NOW,
            ("operator_global", "data:equity"),
            breaker_trips=(trip,),
            **_context("trip-before-claim"),
        )
        is False
    )
    restarted = OrderRepository(session_factory)
    assert (
        restarted.claim_submission(
            order_id,
            NOW,
            ("operator_global", "data:equity"),
            **_context("blocked-after-restart"),
        )
        is False
    )

    with session_factory() as session:
        order = session.get(Order, order_id)
        breaker = session.get(CircuitBreakerState, "data:equity")
    assert order is not None
    assert order.status == OrderStatus.APPROVAL_RECORDED.value
    assert order.submission_attempt == 0
    assert breaker is not None
    assert breaker.tripped is True
    assert breaker.generation == 1


def test_repository_submission_claim_latches_group_exactly_once(
    session_factory,
):
    """A rule-backed claim durably creates one reconciliation obligation."""
    with session_factory() as session:
        group = RuleGroup(
            group_key="repository-claim-group",
            state="active",
            reconciliation_required=False,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(group)
        session.commit()
        group_id = group.id
    order_id = _persist_order(
        session_factory,
        "repository-group-claim",
        status=OrderStatus.APPROVAL_RECORDED,
    )
    _persist_proposal(session_factory, order_id, group_id=group_id)
    first = OrderRepository(session_factory)

    assert first.claim_submission(
        order_id,
        NOW,
        ("operator_global", "data:equity"),
        **_context("group-claim"),
    )
    assert (
        OrderRepository(session_factory).claim_submission(
            order_id,
            NOW,
            ("operator_global", "data:equity"),
            **_context("group-claim-replay"),
        )
        is False
    )

    with session_factory() as session:
        order = session.get(Order, order_id)
        group = session.get(RuleGroup, group_id)
        group_audits = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action
                == "rule_group.reconciliation_latch"
            )
        )
    assert order is not None
    assert order.status == OrderStatus.SUBMITTING.value
    assert order.submission_attempt == 1
    assert group is not None
    assert group.reconciliation_required is True
    assert group_audits == 1


class _ReadOnlyRecoveryBroker(MockBroker):
    """Fail immediately if a recovery path attempts a broker mutation."""

    def __init__(self):
        super().__init__()
        self.mutation_calls = []

    def submit_order(self, order):
        self.mutation_calls.append(("submit_order", order))
        raise AssertionError("recovery path attempted submit_order")

    def submit_bracket(self, order, take_profit, stop_loss):
        self.mutation_calls.append(
            ("submit_bracket", order, take_profit, stop_loss)
        )
        raise AssertionError("recovery path attempted submit_bracket")

    def cancel_order(self, order_id):
        self.mutation_calls.append(("cancel_order", order_id))
        raise AssertionError("recovery path attempted cancel_order")


class _NoRemoteOrderBroker(_ReadOnlyRecoveryBroker):
    """Read-only deterministic broker view with no matching remote order."""


class _InvalidAcceptanceBroker(_ReadOnlyRecoveryBroker):
    """Read-only deterministic broker view returning malformed order truth."""

    def get_order_by_client_id(self, client_order_id):
        raise BrokerDataIntegrityError(
            "malformed cumulative fill",
            broker_order_id=None,
        )


class _UnavailableAcceptanceBroker(_ReadOnlyRecoveryBroker):
    """Read-only deterministic broker view with an unavailable lookup."""

    def get_order_by_client_id(self, client_order_id):
        raise RuntimeError("deterministic broker outage")


def test_reconciliation_group_latch_recovers_once_after_durable_resolution(
    make_service,
):
    """A linked group remains latched until its unknown order is resolved."""
    broker = _NoRemoteOrderBroker()
    service = make_service(broker=broker)
    with service.session_factory() as session:
        group = RuleGroup(
            group_key="reconciliation-recovery-group",
            state="active",
            reconciliation_required=True,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(group)
        session.commit()
        group_id = group.id
    order_id = _persist_order(
        service.session_factory,
        "reconciliation-group-order",
        status=OrderStatus.ACCEPTANCE_UNKNOWN,
        acceptance_state=OrderStatus.ACCEPTANCE_UNKNOWN.value,
    )
    _persist_proposal(
        service.session_factory,
        order_id,
        group_id=group_id,
    )

    assert service.reconciliation.reconcile_unknown(
        **_context("group-still-unknown")
    ) == (0, (order_id,))
    with service.session_factory() as session:
        assert (
            session.get(RuleGroup, group_id).reconciliation_required
            is True
        )

    assert service.order_application.repository.resolve_acceptance(
        order_id,
        "recovered-remote-order",
        OrderStatus.CANCELED,
        Decimal("0"),
        NOW,
        **_context("group-order-resolved"),
    )
    restarted = make_service(broker=broker)
    assert restarted.reconciliation.reconcile_unknown(
        **_context("group-clear")
    ) == (0, ())
    assert restarted.reconciliation.reconcile_unknown(
        **_context("group-clear-replay")
    ) == (0, ())

    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        group = session.get(RuleGroup, group_id)
        clear_audits = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action == "rule_group.reconcile",
                AuditEvent.target_id == str(group_id),
            )
        )
    assert order is not None
    assert order.status == OrderStatus.CANCELED.value
    assert order.acceptance_state == "accepted"
    assert group is not None
    assert group.reconciliation_required is False
    assert clear_audits == 1
    assert broker.mutation_calls == []


def test_invalid_acceptance_payload_latches_once_and_survives_restart(
    make_service,
):
    """Malformed broker truth stays unresolved without repeated local mutation."""
    broker = _InvalidAcceptanceBroker()
    service = make_service(broker=broker)
    order_id = _persist_order(
        service.session_factory,
        "reconciliation-invalid-acceptance",
        status=OrderStatus.ACCEPTANCE_UNKNOWN,
        acceptance_state=OrderStatus.ACCEPTANCE_UNKNOWN.value,
    )

    first = service.reconciliation.reconcile_unknown(
        **_context("invalid-acceptance-first")
    )
    with service.session_factory() as session:
        first_order = session.get(Order, order_id)
        first_version = first_order.version
    restarted = make_service(broker=broker)
    replay = restarted.reconciliation.reconcile_unknown(
        **_context("invalid-acceptance-replay")
    )

    assert first == (0, (order_id,))
    assert replay == first
    assert (
        restarted.breakers.is_tripped(BreakerScope.broker_drift())
        is True
    )
    with restarted.session_factory() as session:
        order = session.get(Order, order_id)
        order_audits = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action == "order.reconcile",
                AuditEvent.target_id == str(order_id),
            )
        )
        fill_count = session.scalar(
            select(func.count()).select_from(Fill)
        )
    assert order is not None
    assert order.status == OrderStatus.ACCEPTANCE_UNKNOWN.value
    assert order.acceptance_state == FILL_RECONCILIATION_REQUIRED
    assert order.last_error_code == "invalid_cumulative_fill"
    assert order.version == first_version
    assert order_audits == 1
    assert fill_count == 0
    assert broker.mutation_calls == []


def test_unavailable_acceptance_lookup_rolls_back_without_false_resolution(
    make_service,
):
    """Dependency failure preserves the unknown order and no breaker claim."""
    broker = _UnavailableAcceptanceBroker()
    service = make_service(broker=broker)
    order_id = _persist_order(
        service.session_factory,
        "reconciliation-unavailable-acceptance",
        status=OrderStatus.ACCEPTANCE_UNKNOWN,
        acceptance_state=OrderStatus.ACCEPTANCE_UNKNOWN.value,
    )

    with pytest.raises(RequiredDependencyUnavailable):
        service.reconciliation.reconcile_unknown(
            **_context("unavailable-acceptance")
        )

    with service.session_factory() as session:
        order = session.get(Order, order_id)
        audit_count = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.target_id == str(order_id))
        )
    assert order is not None
    assert order.status == OrderStatus.ACCEPTANCE_UNKNOWN.value
    assert order.acceptance_state == OrderStatus.ACCEPTANCE_UNKNOWN.value
    assert order.version == 0
    assert audit_count == 0
    assert (
        service.breakers.is_tripped(BreakerScope.broker_drift())
        is False
    )
    assert broker.mutation_calls == []
