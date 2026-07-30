"""Behavioral edge coverage for the transactional rule repository."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import (
    AuditEvent,
    CircuitBreakerState,
    FILL_RECONCILIATION_QUARANTINED,
    FILL_RECONCILIATION_REQUIRED,
    FILL_RECONCILIATION_TRUSTED,
    Fill,
    Order,
    PLAN_CANCEL_INDETERMINATE,
    PLAN_CANCEL_REQUESTED,
    Proposal,
    Rule,
    RuleGroup,
    TradePlanRow,
)
from trading_assistant.rules.models import RuleKind, RuleState
from trading_assistant.rules.repository import RuleRepository
from trading_assistant.security.sensitive_fields import persist_sensitive

NOW = datetime(2026, 7, 29, 16, tzinfo=timezone.utc)
CONTEXT = {
    "actor": "daemon:repository-edge-test",
    "reason": "exercise durable repository edge behavior",
    "request_id": "repository-edge-test",
}


def _plan(
    session: Session,
    *,
    symbol: str = "AAPL",
    action: str = "buy",
    status: str = "approved",
    generation: int = 0,
) -> TradePlanRow:
    plan = TradePlanRow(
        symbol=symbol,
        action=action,
        status=status,
        paper_only=True,
        residual_generation=generation,
        created_at=NOW - timedelta(hours=1),
    )
    persist_sensitive(
        session,
        plan,
        {"plan_json": "{}", "sized_json": "{}"},
    )
    session.flush()
    return plan


def _group(
    session: Session,
    key: str,
    *,
    state: str = RuleState.ACTIVE.value,
    reconciliation_required: bool = False,
    lease_owner: str | None = None,
) -> RuleGroup:
    group = RuleGroup(
        group_key=key,
        state=state,
        reconciliation_required=reconciliation_required,
        lease_owner=lease_owner,
        lease_expires_at=(
            NOW + timedelta(minutes=1) if lease_owner is not None else None
        ),
        created_at=NOW - timedelta(hours=1),
        updated_at=NOW - timedelta(minutes=1),
    )
    session.add(group)
    session.flush()
    return group


def _rule(
    session: Session,
    group: RuleGroup,
    *,
    plan: TradePlanRow | None = None,
    plan_id: int | None = None,
    kind: RuleKind = RuleKind.ENTRY,
    state: RuleState = RuleState.ACTIVE,
    ticker: str = "AAPL",
) -> Rule:
    rule = Rule(
        group_id=group.id,
        plan_id=plan.id if plan is not None else plan_id,
        payload_version=1,
        ticker=ticker,
        kind=kind.value,
        condition_json=(
            '{"type":"price","direction":"below","price":"100"}'
        ),
        action_json='{"side":"sell","order_type":"market","qty":"1"}',
        state=state.value,
        created_at=NOW - timedelta(minutes=30),
    )
    session.add(rule)
    session.flush()
    return rule


def _order_with_proposal(
    session: Session,
    key: str,
    *,
    group: RuleGroup,
    rule: Rule | None,
    status: OrderStatus,
    side: str = "sell",
    qty: Decimal = Decimal("1"),
    generation: int = 0,
    acceptance_state: str = "not_started",
    plan_cancel_state: str = "none",
) -> Order:
    order = Order(
        idempotency_key=key,
        ticker=rule.ticker if rule is not None else "AAPL",
        side=side,
        order_type="market",
        qty=qty,
        status=status.value,
        acceptance_state=acceptance_state,
        plan_cancel_state=plan_cancel_state,
        created_at=NOW - timedelta(minutes=10),
        updated_at=NOW - timedelta(minutes=10),
    )
    persist_sensitive(
        session,
        order,
        {"approval_reason": "repository edge fixture"},
    )
    session.flush()
    persist_sensitive(
        session,
        Proposal(
            order_id=order.id,
            source_rule_group_id=group.id,
            source_rule_id=rule.id if rule is not None else None,
            plan_generation=generation,
            expires_at=NOW + timedelta(minutes=15),
        ),
        {"reasoning": "repository edge fixture"},
    )
    session.flush()
    return order


def _fill(
    session: Session,
    order: Order,
    qty: str,
    fill_id: str | None,
    *,
    reconciliation_state: str = FILL_RECONCILIATION_TRUSTED,
) -> Fill:
    fill = Fill(
        order_id=order.id,
        ticker=order.ticker,
        side=order.side,
        qty=Decimal(qty),
        price=Decimal("100"),
        broker_fill_id=fill_id,
        reconciliation_state=reconciliation_state,
        filled_at=NOW - timedelta(minutes=5),
    )
    session.add(fill)
    session.flush()
    return fill


def _ready_exit_plan(
    session: Session,
    key: str,
    *,
    action: str = "buy",
) -> tuple[
    TradePlanRow,
    RuleGroup,
    Rule,
    Order,
    RuleGroup,
    Rule,
    Order,
]:
    exit_side = "sell" if action == "buy" else "buy"
    entry_side = action
    plan = _plan(
        session,
        action=action,
        generation=2,
    )
    entry_group = _group(
        session,
        f"{key}-entry",
        state=RuleState.TRIGGERED.value,
    )
    entry_rule = _rule(
        session,
        entry_group,
        plan=plan,
        kind=RuleKind.ENTRY,
        state=RuleState.TRIGGERED,
    )
    entry_order = _order_with_proposal(
        session,
        f"{key}-entry-order",
        group=entry_group,
        rule=entry_rule,
        status=OrderStatus.FILLED,
        side=entry_side,
    )
    _fill(session, entry_order, "5", f"{key}-entry-fill")

    exit_group = _group(session, f"{key}-exit")
    exit_rule = _rule(
        session,
        exit_group,
        plan=plan,
        kind=RuleKind.STOP,
        state=RuleState.PROCESSING,
    )
    exit_order = _order_with_proposal(
        session,
        f"{key}-exit-order",
        group=exit_group,
        rule=exit_rule,
        status=OrderStatus.PROPOSED,
        side=exit_side,
        qty=Decimal("5"),
        generation=2,
    )
    return (
        plan,
        entry_group,
        entry_rule,
        entry_order,
        exit_group,
        exit_rule,
        exit_order,
    )


def test_broker_cancel_candidates_use_trusted_fill_truth_and_generation(
    session_factory,
):
    with session_factory() as session:
        active_plan = _plan(session, generation=0)
        active_group = _group(session, "cancel-candidates-active")
        active_entry = _rule(
            session,
            active_group,
            plan=active_plan,
            kind=RuleKind.ENTRY,
        )
        live_entry = _order_with_proposal(
            session,
            "cancel-candidate-live-entry",
            group=active_group,
            rule=active_entry,
            status=OrderStatus.SUBMITTED,
            side="buy",
        )
        _fill(session, live_entry, "5", "cancel-candidate-entry-fill")
        active_exit = _rule(
            session,
            active_group,
            plan=active_plan,
            kind=RuleKind.STOP,
        )
        filled_exit = _order_with_proposal(
            session,
            "cancel-candidate-filled-exit",
            group=active_group,
            rule=active_exit,
            status=OrderStatus.FILLED,
        )
        _fill(session, filled_exit, "1", "cancel-candidate-exit-fill")
        current_exit = _order_with_proposal(
            session,
            "cancel-candidate-current-exit",
            group=active_group,
            rule=active_exit,
            status=OrderStatus.SUBMITTED,
            generation=0,
        )

        stale_plan = _plan(session, symbol="MSFT", generation=3)
        stale_group = _group(session, "cancel-candidates-stale")
        stale_rule = _rule(
            session,
            stale_group,
            plan=stale_plan,
            kind=RuleKind.TARGET,
            ticker="MSFT",
        )
        stale_exit = _order_with_proposal(
            session,
            "cancel-candidate-stale-exit",
            group=stale_group,
            rule=stale_rule,
            status=OrderStatus.SUBMITTED,
            generation=2,
        )

        missing_group = _group(session, "cancel-candidates-missing-plan")
        missing_rule = _rule(
            session,
            missing_group,
            plan_id=999_999,
            kind=RuleKind.STOP,
        )
        missing_plan_order = _order_with_proposal(
            session,
            "cancel-candidate-missing-plan",
            group=missing_group,
            rule=missing_rule,
            status=OrderStatus.PARTIALLY_FILLED,
        )

        untrusted_plan = _plan(session, symbol="NVDA")
        untrusted_group = _group(session, "cancel-candidates-untrusted")
        untrusted_entry_rule = _rule(
            session,
            untrusted_group,
            plan=untrusted_plan,
            kind=RuleKind.ENTRY,
            ticker="NVDA",
        )
        untrusted_live_entry = _order_with_proposal(
            session,
            "cancel-candidate-untrusted-entry",
            group=untrusted_group,
            rule=untrusted_entry_rule,
            status=OrderStatus.SUBMITTED,
            side="buy",
        )
        untrusted_exit_rule = _rule(
            session,
            untrusted_group,
            plan=untrusted_plan,
            kind=RuleKind.STOP,
            ticker="NVDA",
        )
        untrusted_exit = _order_with_proposal(
            session,
            "cancel-candidate-untrusted-exit",
            group=untrusted_group,
            rule=untrusted_exit_rule,
            status=OrderStatus.FILLED,
        )
        _fill(
            session,
            untrusted_exit,
            "3",
            "cancel-candidate-quarantined",
            reconciliation_state=FILL_RECONCILIATION_QUARANTINED,
        )
        session.commit()

    repository = RuleRepository(session_factory, owner="cancel-candidate-worker")

    assert repository.plan_order_ids_requiring_broker_cancel() == sorted(
        [live_entry.id, stale_exit.id, missing_plan_order.id]
    )
    assert current_exit.id not in (
        repository.plan_order_ids_requiring_broker_cancel()
    )
    assert untrusted_live_entry.id not in (
        repository.plan_order_ids_requiring_broker_cancel()
    )


def test_plan_order_queries_distinguish_entry_and_reconciliation_state(
    session_factory,
):
    with session_factory() as session:
        plan = _plan(session)
        group = _group(session, "plan-query-edges")
        entry_rule = _rule(
            session,
            group,
            plan=plan,
            kind=RuleKind.ENTRY,
        )
        reconciled_entry = _order_with_proposal(
            session,
            "plan-query-reconciliation-entry",
            group=group,
            rule=entry_rule,
            status=OrderStatus.FILLED,
            side="buy",
            acceptance_state=FILL_RECONCILIATION_REQUIRED,
            plan_cancel_state=PLAN_CANCEL_REQUESTED,
        )
        exit_rule = _rule(
            session,
            group,
            plan=plan,
            kind=RuleKind.STOP,
        )
        live_exit = _order_with_proposal(
            session,
            "plan-query-live-exit",
            group=group,
            rule=exit_rule,
            status=OrderStatus.SUBMITTED,
            plan_cancel_state=PLAN_CANCEL_INDETERMINATE,
        )
        terminal_entry = _order_with_proposal(
            session,
            "plan-query-terminal-entry",
            group=group,
            rule=entry_rule,
            status=OrderStatus.CANCELED,
            side="buy",
        )
        session.commit()

    repository = RuleRepository(session_factory, owner="plan-query-worker")

    assert repository.plan_cancellation_intent_order_ids() == [
        reconciled_entry.id,
        live_exit.id,
    ]
    assert repository.plan_nonterminal_order_ids(plan.id) == [
        reconciled_entry.id,
        live_exit.id,
    ]
    assert repository.plan_entry_nonterminal_order_ids(plan.id) == [
        reconciled_entry.id
    ]
    assert terminal_entry.id not in repository.plan_nonterminal_order_ids(
        plan.id
    )


def test_plan_execution_truth_ignores_untrusted_quantity_but_reports_it(
    session_factory,
):
    with session_factory() as session:
        plan = _plan(session)
        group = _group(
            session,
            "execution-truth",
            reconciliation_required=True,
        )
        entry_rule = _rule(
            session,
            group,
            plan=plan,
            kind=RuleKind.ENTRY,
        )
        entry_order = _order_with_proposal(
            session,
            "execution-truth-entry",
            group=group,
            rule=entry_rule,
            status=OrderStatus.FILLED,
            side="buy",
        )
        _fill(session, entry_order, "3", "execution-truth-entry-a")
        _fill(session, entry_order, "2", "execution-truth-entry-b")
        untrusted = _fill(session, entry_order, "99", None)
        exit_rule = _rule(
            session,
            group,
            plan=plan,
            kind=RuleKind.TARGET,
        )
        exit_order = _order_with_proposal(
            session,
            "execution-truth-exit",
            group=group,
            rule=exit_rule,
            status=OrderStatus.FILLED,
        )
        _fill(session, exit_order, "2", "execution-truth-exit-fill")
        session.commit()

    repository = RuleRepository(session_factory, owner="truth-worker")
    truth = repository.plan_execution_truth(plan.id)

    assert truth.entry_filled_qty == Decimal("5")
    assert truth.exit_filled_qty == Decimal("2")
    assert truth.residual_qty == Decimal("3")
    assert truth.untrusted_fill_order_ids == (entry_order.id,)
    assert truth.unresolved_order_ids == (entry_order.id,)
    assert truth.reconciliation_required is True

    with session_factory() as session:
        session.delete(session.get(Fill, untrusted.id))
        session.get(RuleGroup, group.id).reconciliation_required = False
        session.commit()

    reconciled = repository.plan_execution_truth(plan.id)
    assert reconciled.untrusted_fill_order_ids == ()
    assert reconciled.unresolved_order_ids == ()
    assert reconciled.reconciliation_required is False


def test_cancellation_blocker_classifies_state_and_trips_on_over_exit(
    session_factory,
):
    with session_factory() as session:
        canceled = _plan(session, symbol="MSFT", status="canceled")
        invalid = _plan(session, symbol="GOOGL", status="completed")
        clean = _plan(session, symbol="AMZN", status="proposed")

        reconciliation = _plan(session, symbol="META")
        reconciliation_group = _group(
            session,
            "blocker-reconciliation",
            reconciliation_required=True,
        )
        reconciliation_rule = _rule(
            session,
            reconciliation_group,
            plan=reconciliation,
            kind=RuleKind.ENTRY,
            ticker="META",
        )
        _order_with_proposal(
            session,
            "blocker-reconciliation-order",
            group=reconciliation_group,
            rule=reconciliation_rule,
            status=OrderStatus.FILLED,
            side="buy",
        )

        open_plan = _plan(session, symbol="TSLA")
        open_group = _group(session, "blocker-open")
        open_rule = _rule(
            session,
            open_group,
            plan=open_plan,
            kind=RuleKind.ENTRY,
            ticker="TSLA",
        )
        open_order = _order_with_proposal(
            session,
            "blocker-open-order",
            group=open_group,
            rule=open_rule,
            status=OrderStatus.FILLED,
            side="buy",
        )
        _fill(session, open_order, "2", "blocker-open-fill")

        over_exit_plan = _plan(session, symbol="NFLX")
        over_exit_group = _group(session, "blocker-over-exit")
        over_exit_rule = _rule(
            session,
            over_exit_group,
            plan=over_exit_plan,
            kind=RuleKind.STOP,
            ticker="NFLX",
        )
        over_exit_order = _order_with_proposal(
            session,
            "blocker-over-exit-order",
            group=over_exit_group,
            rule=over_exit_rule,
            status=OrderStatus.FILLED,
        )
        _fill(session, over_exit_order, "1", "blocker-over-exit-fill")
        session.commit()

    repository = RuleRepository(session_factory, owner="blocker-worker")

    assert repository.plan_cancellation_blocker(
        999_999, now=NOW, **CONTEXT
    ).error == "not_found"
    assert repository.plan_cancellation_blocker(
        canceled.id, now=NOW, **CONTEXT
    ) is None
    assert repository.plan_cancellation_blocker(
        invalid.id, now=NOW, **CONTEXT
    ).error == "invalid_state"
    assert repository.plan_cancellation_blocker(
        reconciliation.id, now=NOW, **CONTEXT
    ).error == "reconciliation_required"
    assert repository.plan_cancellation_blocker(
        open_plan.id, now=NOW, **CONTEXT
    ).error == "position_open"
    assert repository.plan_cancellation_blocker(
        over_exit_plan.id, now=NOW, **CONTEXT
    ).error == "over_exit"
    assert repository.plan_cancellation_blocker(
        clean.id, now=NOW, **CONTEXT
    ) is None

    with session_factory() as session:
        breaker = session.get(CircuitBreakerState, "broker_drift")
        assert breaker is not None
        assert breaker.tripped is True
        assert breaker.generation == 1


def test_ready_exit_validation_and_allocation_are_exact_for_long_and_short(
    session_factory,
):
    with session_factory() as session:
        long = _ready_exit_plan(session, "ready-long", action="buy")
        short = _ready_exit_plan(session, "ready-short", action="sell")
        session.commit()

    repository = RuleRepository(session_factory, owner="exit-validator")
    long_exit = long[-1]
    short_exit = short[-1]

    assert repository.validate_plan_exit_submission(
        long_exit.id,
        Decimal("5"),
        Decimal("5"),
    ) is None
    assert repository.validate_plan_exit_submission(
        short_exit.id,
        Decimal("5"),
        Decimal("-5"),
    ) is None
    assert repository.is_plan_exit_order(long_exit.id) is True
    assert repository.is_plan_exit_order(long[3].id) is False
    assert repository.is_plan_exit_order(999_999) is False
    assert repository.plan_allocation_truth(
        "aapl", "sell"
    ) == (Decimal("5"), True)
    assert repository.plan_allocation_truth(
        "AAPL", "buy"
    ) == (Decimal("5"), True)
    assert repository.plan_allocation_truth(
        "AAPL", "hold"
    ) == (Decimal("0"), False)


def test_exit_validation_fails_closed_for_stale_sibling_and_live_entry(
    session_factory,
):
    with session_factory() as session:
        seeded = _ready_exit_plan(session, "exit-state-failures")
        (
            plan,
            _entry_group,
            _entry_rule,
            entry_order,
            exit_group,
            _exit_rule,
            exit_order,
        ) = seeded
        session.commit()

    repository = RuleRepository(session_factory, owner="exit-state-validator")

    with session_factory() as session:
        session.get(RuleGroup, exit_group.id).reconciliation_required = True
        session.commit()
    assert repository.validate_plan_exit_submission(
        exit_order.id, Decimal("1"), Decimal("5")
    ) == "plan exit lifecycle is not execution-ready"

    with session_factory() as session:
        session.get(RuleGroup, exit_group.id).reconciliation_required = False
        proposal = session.scalar(
            select(Proposal).where(Proposal.order_id == exit_order.id)
        )
        proposal.plan_generation = 1
        session.commit()
    assert repository.validate_plan_exit_submission(
        exit_order.id, Decimal("1"), Decimal("5")
    ) == "plan exit intent uses a stale residual generation"

    with session_factory() as session:
        proposal = session.scalar(
            select(Proposal).where(Proposal.order_id == exit_order.id)
        )
        proposal.plan_generation = plan.residual_generation
        sibling = _order_with_proposal(
            session,
            "exit-state-sibling",
            group=session.get(RuleGroup, exit_group.id),
            rule=None,
            status=OrderStatus.PROPOSED,
        )
        session.commit()
    assert repository.validate_plan_exit_submission(
        exit_order.id, Decimal("1"), Decimal("5")
    ) == "another plan exit intent is still nonterminal"

    with session_factory() as session:
        session.get(Order, sibling.id).status = OrderStatus.CANCELED.value
        session.get(Order, entry_order.id).status = OrderStatus.SUBMITTED.value
        session.commit()
    assert repository.validate_plan_exit_submission(
        exit_order.id, Decimal("1"), Decimal("5")
    ) == "plan entry order is still nonterminal"


def test_exit_validation_rejects_bad_residual_size_and_broker_allocation(
    session_factory,
):
    with session_factory() as session:
        seeded = _ready_exit_plan(session, "exit-quantity-failures")
        exit_order = seeded[-1]
        session.commit()

    repository = RuleRepository(session_factory, owner="exit-quantity-validator")

    assert repository.validate_plan_exit_submission(
        exit_order.id, None, Decimal("5")
    ) == "exit quantity exceeds trusted plan residual"
    assert repository.validate_plan_exit_submission(
        exit_order.id, Decimal("6"), Decimal("5")
    ) == "exit quantity exceeds trusted plan residual"
    assert repository.validate_plan_exit_submission(
        exit_order.id, Decimal("5"), Decimal("4")
    ) == "plan allocation exceeds reconciled broker position"

    with session_factory() as session:
        exit_fill = _fill(
            session,
            session.get(Order, exit_order.id),
            "6",
            "exit-quantity-over-fill",
        )
        session.commit()
    assert repository.validate_plan_exit_submission(
        exit_order.id, Decimal("1"), Decimal("5")
    ) == "plan has negative trusted residual quantity"

    with session_factory() as session:
        session.get(Fill, exit_fill.id).qty = Decimal("5")
        session.commit()
    assert repository.validate_plan_exit_submission(
        exit_order.id, Decimal("1"), Decimal("5")
    ) == "plan has no trusted residual quantity"

    with session_factory() as session:
        session.delete(session.get(Fill, exit_fill.id))
        session.get(Order, exit_order.id).status = OrderStatus.SUBMITTED.value
        session.commit()
    assert repository.validate_plan_exit_submission(
        exit_order.id, Decimal("1"), Decimal("5")
    ) == "plan allocation cannot be proven from reconciled fill truth"
    assert repository.plan_allocation_truth(
        "AAPL", "sell"
    ) == (Decimal("0"), False)


def test_release_group_updates_hwm_once_and_stale_releases_are_noops(
    session_factory,
):
    with session_factory() as session:
        group = _group(session, "release-idempotency")
        rule = _rule(session, group, kind=RuleKind.TRAILING)
        session.commit()

    repository = RuleRepository(session_factory, owner="release-worker")
    first = repository.lease_group(group.id, now=NOW, **CONTEXT)
    assert first is not None
    assert repository.release_group(
        first,
        now=NOW,
        high_water_marks={rule.id: Decimal("111.25")},
        **CONTEXT,
    ) is True
    assert repository.release_group(
        first,
        now=NOW,
        high_water_marks={rule.id: Decimal("999")},
        **CONTEXT,
    ) is False

    second = repository.lease_group(
        group.id,
        now=NOW + timedelta(seconds=1),
        **CONTEXT,
    )
    assert second is not None
    assert repository.release_group(
        second,
        now=NOW + timedelta(seconds=1),
        high_water_marks={rule.id: Decimal("111.25")},
        **CONTEXT,
    ) is True

    with session_factory() as session:
        stored_group = session.get(RuleGroup, group.id)
        stored_rule = session.get(Rule, rule.id)
        hwm_audits = session.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.action == "rule.high_water_mark",
                AuditEvent.target_id == str(rule.id),
            )
        )
        assert stored_group.lease_owner is None
        assert stored_group.lease_expires_at is None
        assert stored_rule.hwm == Decimal("111.250000")
        assert hwm_audits == 1


def test_claim_proposal_is_fenced_idempotent_and_atomic_on_rule_mismatch(
    session_factory,
):
    with session_factory() as session:
        plan = _plan(session)
        success_group = _group(session, "claim-proposal-success")
        success_rule = _rule(
            session,
            success_group,
            plan=plan,
            kind=RuleKind.STOP,
        )
        no_plan_group = _group(session, "claim-proposal-no-plan")
        no_plan_rule = _rule(
            session,
            no_plan_group,
            kind=RuleKind.STOP,
        )
        intent_group = _group(session, "claim-proposal-existing-intent")
        intent_rule = _rule(
            session,
            intent_group,
            plan=plan,
            kind=RuleKind.STOP,
        )
        session.commit()

    repository = RuleRepository(session_factory, owner="proposal-worker")
    success_lease = repository.lease_group(
        success_group.id, now=NOW, **CONTEXT
    )
    assert success_lease is not None
    assert repository.claim_proposal(
        success_lease,
        success_rule.id,
        now=NOW,
        high_water_mark=Decimal("120"),
        **CONTEXT,
    ) is True
    assert repository.claim_proposal(
        success_lease,
        success_rule.id,
        now=NOW,
        **CONTEXT,
    ) is False

    no_plan_lease = repository.lease_group(
        no_plan_group.id, now=NOW, **CONTEXT
    )
    assert no_plan_lease is not None
    assert repository.claim_proposal(
        no_plan_lease,
        no_plan_rule.id,
        now=NOW,
        **CONTEXT,
    ) is False

    intent_lease = repository.lease_group(
        intent_group.id, now=NOW, **CONTEXT
    )
    assert intent_lease is not None
    with session_factory() as session:
        _order_with_proposal(
            session,
            "claim-proposal-existing-order",
            group=session.get(RuleGroup, intent_group.id),
            rule=intent_rule,
            status=OrderStatus.PROPOSED,
        )
        session.commit()
    assert repository.claim_proposal(
        intent_lease,
        intent_rule.id,
        now=NOW,
        **CONTEXT,
    ) is False

    with session_factory() as session:
        successful_rule = session.get(Rule, success_rule.id)
        no_plan_stored_group = session.get(RuleGroup, no_plan_group.id)
        intent_stored_group = session.get(RuleGroup, intent_group.id)
        assert successful_rule.state == RuleState.PROCESSING.value
        assert successful_rule.hwm == Decimal("120.000000")
        assert no_plan_stored_group.lease_owner == no_plan_lease.owner
        assert no_plan_stored_group.version == no_plan_lease.version
        assert session.get(Rule, no_plan_rule.id).state == RuleState.ACTIVE.value
        assert intent_stored_group.lease_owner == intent_lease.owner
        assert intent_stored_group.version == intent_lease.version


def test_claim_progress_preserves_active_group_and_rolls_back_wrong_winner(
    session_factory,
):
    with session_factory() as session:
        progress_group = _group(session, "claim-progress-success")
        progress_rule = _rule(
            session,
            progress_group,
            kind=RuleKind.TARGET,
        )
        mismatch_group = _group(session, "claim-progress-mismatch")
        mismatch_rule = _rule(
            session,
            mismatch_group,
            kind=RuleKind.TARGET,
        )
        session.commit()

    repository = RuleRepository(session_factory, owner="progress-worker")
    progress_lease = repository.lease_group(
        progress_group.id, now=NOW, **CONTEXT
    )
    assert progress_lease is not None
    assert repository.claim_progress(
        progress_lease,
        progress_rule.id,
        now=NOW,
        rule_state=RuleState.TRIGGERED,
        high_water_mark=Decimal("130"),
        **CONTEXT,
    ) is True
    assert repository.claim_progress(
        progress_lease,
        progress_rule.id,
        now=NOW,
        rule_state=RuleState.TRIGGERED,
        **CONTEXT,
    ) is False

    mismatch_lease = repository.lease_group(
        mismatch_group.id, now=NOW, **CONTEXT
    )
    assert mismatch_lease is not None
    assert repository.claim_progress(
        mismatch_lease,
        progress_rule.id,
        now=NOW,
        rule_state=RuleState.FAILED,
        **CONTEXT,
    ) is False
    with pytest.raises(
        ValueError,
        match="progressing rule must become triggered or failed",
    ):
        repository.claim_progress(
            mismatch_lease,
            mismatch_rule.id,
            now=NOW,
            rule_state=RuleState.CANCELED,
            **CONTEXT,
        )

    with session_factory() as session:
        progressed_group = session.get(RuleGroup, progress_group.id)
        progressed_rule = session.get(Rule, progress_rule.id)
        mismatched_group = session.get(RuleGroup, mismatch_group.id)
        assert progressed_group.state == RuleState.ACTIVE.value
        assert progressed_group.lease_owner is None
        assert progressed_rule.state == RuleState.TRIGGERED.value
        assert progressed_rule.hwm == Decimal("130.000000")
        assert mismatched_group.lease_owner == mismatch_lease.owner
        assert mismatched_group.version == mismatch_lease.version
        assert session.get(Rule, mismatch_rule.id).state == RuleState.ACTIVE.value


def test_cancel_plan_atomically_cancels_group_rules_and_unlinked_proposal_once(
    session_factory,
):
    with session_factory() as session:
        plan = _plan(session, status="proposed")
        group = _group(
            session,
            "cancel-plan-success",
            lease_owner="crashed-worker",
        )
        first_rule = _rule(
            session,
            group,
            plan=plan,
            kind=RuleKind.ENTRY,
        )
        second_rule = _rule(
            session,
            group,
            plan=plan,
            kind=RuleKind.STOP,
            state=RuleState.PROCESSING,
        )
        pending_order = _order_with_proposal(
            session,
            "cancel-plan-unlinked-proposal",
            group=group,
            rule=None,
            status=OrderStatus.PROPOSED,
        )
        session.commit()

    repository = RuleRepository(session_factory, owner="cancel-plan-worker")
    result = repository.cancel_plan(plan.id, now=NOW, **CONTEXT)

    assert result.canceled is True
    assert result.status == RuleState.CANCELED.value
    assert result.rules_canceled == 2
    with session_factory() as session:
        audit_count = session.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.request_id == CONTEXT["request_id"]
            )
        )
        stored_group = session.get(RuleGroup, group.id)
        assert session.get(TradePlanRow, plan.id).status == "canceled"
        assert stored_group.state == RuleState.CANCELED.value
        assert stored_group.lease_owner is None
        assert stored_group.lease_expires_at is None
        assert session.get(Rule, first_rule.id).state == "canceled"
        assert session.get(Rule, second_rule.id).state == "canceled"
        assert session.get(Order, pending_order.id).status == "canceled"

    retried = repository.cancel_plan(plan.id, now=NOW, **CONTEXT)
    assert retried.canceled is True
    assert retried.status == RuleState.CANCELED.value
    assert retried.rules_canceled == 0
    with session_factory() as session:
        assert session.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.request_id == CONTEXT["request_id"]
            )
        ) == audit_count


def test_cancel_plan_refuses_reconciliation_open_position_and_live_order(
    session_factory,
):
    with session_factory() as session:
        invalid = _plan(session, symbol="MSFT", status="completed")

        reconciliation = _plan(session, symbol="META")
        reconciliation_group = _group(
            session,
            "cancel-plan-reconciliation",
            reconciliation_required=True,
        )
        reconciliation_rule = _rule(
            session,
            reconciliation_group,
            plan=reconciliation,
            ticker="META",
        )
        _order_with_proposal(
            session,
            "cancel-plan-reconciliation-order",
            group=reconciliation_group,
            rule=reconciliation_rule,
            status=OrderStatus.FILLED,
            side="buy",
        )

        open_plan = _plan(session, symbol="TSLA")
        open_group = _group(session, "cancel-plan-open")
        open_rule = _rule(
            session,
            open_group,
            plan=open_plan,
            ticker="TSLA",
        )
        open_order = _order_with_proposal(
            session,
            "cancel-plan-open-order",
            group=open_group,
            rule=open_rule,
            status=OrderStatus.FILLED,
            side="buy",
        )
        _fill(session, open_order, "1", "cancel-plan-open-fill")

        live_plan = _plan(session, symbol="GOOGL")
        live_group = _group(session, "cancel-plan-live")
        live_rule = _rule(
            session,
            live_group,
            plan=live_plan,
            ticker="GOOGL",
        )
        _order_with_proposal(
            session,
            "cancel-plan-live-order",
            group=live_group,
            rule=live_rule,
            status=OrderStatus.PROPOSED,
            side="buy",
        )
        session.commit()

    repository = RuleRepository(session_factory, owner="cancel-refusal-worker")

    assert repository.cancel_plan(
        999_999, now=NOW, **CONTEXT
    ).error == "not_found"
    assert repository.cancel_plan(
        invalid.id, now=NOW, **CONTEXT
    ).error == "invalid_state"
    assert repository.cancel_plan(
        reconciliation.id, now=NOW, **CONTEXT
    ).error == "reconciliation_required"
    assert repository.cancel_plan(
        open_plan.id, now=NOW, **CONTEXT
    ).error == "position_open"
    assert repository.cancel_plan(
        live_plan.id, now=NOW, **CONTEXT
    ).error == "orders_live"

    with session_factory() as session:
        assert session.get(TradePlanRow, live_plan.id).status == "approved"
        assert session.get(RuleGroup, live_group.id).state == "active"
