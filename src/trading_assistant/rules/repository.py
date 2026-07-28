"""Transactional rule-group lease and terminal-state primitives."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN

from sqlalchemy import exists, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import (
    AuditEvent,
    FILL_RECONCILIATION_REQUIRED,
    Fill,
    NONTERMINAL_STATES,
    Order,
    OrderStateMachine,
    PLAN_CANCEL_INDETERMINATE,
    PLAN_CANCEL_REQUESTED,
    Proposal,
    Rule,
    RuleGroup,
    TERMINAL_STATES,
    TradePlanRow,
    fill_has_trusted_identity,
)
from trading_assistant.db.lifecycle_proofs import (
    augment_lifecycle_detail,
)
from trading_assistant.risk.breakers import (
    BreakerScope,
    trip_in_session,
)
from trading_assistant.risk.submission_barrier import (
    SubmissionBarrier,
    serialized_writer,
)
from trading_assistant.security.sensitive_fields import persist_sensitive

from .models import (
    RuleCommand,
    RuleAction,
    RuleKind,
    RuleState,
    normalize_computed_order_decimal,
    validate_persisted_high_water_mark,
)


@dataclass(frozen=True)
class RuleGroupLease:
    group_id: int
    owner: str
    expires_at: datetime
    version: int


class RuleLeaseChronologyError(ValueError):
    """The caller's clock sample predates durable group state."""


@dataclass(frozen=True)
class StoredRule:
    id: int
    group_id: int
    command: RuleCommand
    plan_id: int | None = None


@dataclass(frozen=True)
class PlanCancellationResult:
    plan_id: int
    canceled: bool
    status: str | None
    rules_canceled: int = 0
    error: str | None = None


@dataclass(frozen=True)
class FillActivationResult:
    groups_activated: int = 0
    rules_activated: int = 0
    rules_resized: int = 0
    rules_settled: int = 0
    plans_completed: int = 0


@dataclass(frozen=True)
class PlanExecutionTruth:
    entry_filled_qty: Decimal = Decimal(0)
    exit_filled_qty: Decimal = Decimal(0)
    residual_qty: Decimal = Decimal(0)
    unresolved_order_ids: tuple[int, ...] = ()
    untrusted_fill_order_ids: tuple[int, ...] = ()
    reconciliation_required: bool = False


_NONTERMINAL_ORDER_STATES = frozenset(
    state.value for state in NONTERMINAL_STATES
)
_TERMINAL_ORDER_STATES = frozenset(
    state.value for state in TERMINAL_STATES
)
_EXIT_KINDS = frozenset(
    {
        RuleKind.TARGET.value,
        RuleKind.STOP.value,
        RuleKind.TRAILING.value,
        RuleKind.TIME.value,
    }
)
_LOCAL_CANCELABLE_ORDER_STATES = frozenset(
    {
        OrderStatus.PROPOSED.value,
        OrderStatus.APPROVAL_RECORDED.value,
    }
)
_BROKER_CANCELABLE_ORDER_STATES = frozenset(
    {
        OrderStatus.APPROVED.value,
        OrderStatus.SUBMITTING.value,
        OrderStatus.ACCEPTANCE_UNKNOWN.value,
        OrderStatus.SUBMITTED.value,
        OrderStatus.PARTIALLY_FILLED.value,
    }
)


def _order_requires_exact_reconciliation(order: Order) -> bool:
    return (
        order.status in _NONTERMINAL_ORDER_STATES
        or order.acceptance_state == FILL_RECONCILIATION_REQUIRED
    )


def _plan_execution_truth(
    session: Session,
    plan_id: int,
) -> PlanExecutionTruth:
    rows = list(
        session.execute(
            select(Proposal, Order, Rule)
            .join(Order, Order.id == Proposal.order_id)
            .join(Rule, Rule.id == Proposal.source_rule_id)
            .where(Rule.plan_id == plan_id)
            .order_by(Order.id)
        ).all()
    )
    order_ids = tuple(order.id for _proposal, order, _rule in rows)
    fill_qty_by_order: dict[int, Decimal] = {}
    untrusted_fill_order_ids: set[int] = set()
    if order_ids:
        for fill in session.scalars(
            select(Fill).where(Fill.order_id.in_(order_ids))
        ).all():
            if (
                fill.order_id is None
                or not fill_has_trusted_identity(fill)
            ):
                if fill.order_id is not None:
                    untrusted_fill_order_ids.add(fill.order_id)
                continue
            fill_qty_by_order[fill.order_id] = (
                fill_qty_by_order.get(fill.order_id, Decimal(0))
                + fill.qty
            )
    entry_filled_qty = Decimal(0)
    exit_filled_qty = Decimal(0)
    unresolved: list[int] = []
    group_ids: set[int] = set()
    for _proposal, order, rule in rows:
        group_ids.add(rule.group_id)
        qty = fill_qty_by_order.get(order.id, Decimal(0))
        if rule.kind == RuleKind.ENTRY.value:
            entry_filled_qty += qty
        elif rule.kind in _EXIT_KINDS:
            exit_filled_qty += qty
        if _order_requires_exact_reconciliation(order):
            unresolved.append(order.id)
    unresolved.extend(untrusted_fill_order_ids)
    reconciliation_required = bool(
        untrusted_fill_order_ids
        or (
            group_ids
            and session.scalar(
                select(RuleGroup.id)
                .where(
                    RuleGroup.id.in_(group_ids),
                    RuleGroup.reconciliation_required.is_(True),
                )
                .limit(1)
            )
            is not None
        )
    )
    return PlanExecutionTruth(
        entry_filled_qty=entry_filled_qty,
        exit_filled_qty=exit_filled_qty,
        residual_qty=entry_filled_qty - exit_filled_qty,
        unresolved_order_ids=tuple(sorted(set(unresolved))),
        untrusted_fill_order_ids=tuple(
            sorted(untrusted_fill_order_ids)
        ),
        reconciliation_required=reconciliation_required,
    )


def _plan_allocation_is_exact(
    session: Session,
    truth: PlanExecutionTruth,
) -> bool:
    """Whether plan allocation is exact enough for a serialized submission.

    PROPOSED and APPROVAL_RECORDED orders cannot have reached the broker. The
    global submission barrier serializes the validation/claim/send sequence, so
    those local-only intents do not make fill allocation uncertain. Any
    broker-side state, reconciliation latch, or untrusted fill remains
    blocking.
    """
    if truth.reconciliation_required or truth.residual_qty < 0:
        return False
    untrusted = set(truth.untrusted_fill_order_ids)
    for order_id in truth.unresolved_order_ids:
        if order_id in untrusted:
            return False
        order = session.get(Order, order_id)
        if (
            order is None
            or order.status not in _LOCAL_CANCELABLE_ORDER_STATES
            or order.acceptance_state
            == FILL_RECONCILIATION_REQUIRED
        ):
            return False
    return True


def _require_context(
    actor: str,
    reason: str,
    request_id: str,
) -> tuple[str, str, str]:
    actor = actor.strip()
    reason = reason.strip()
    request_id = request_id.strip()
    if not actor or not reason or not request_id:
        raise ValueError(
            "rule mutation actor, reason, and request_id must be non-empty"
        )
    return actor, reason, request_id


def _require_aware_utc_lease_now(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("rule lease now must be timezone-aware")
    try:
        offset = value.utcoffset()
    except (OverflowError, TypeError, ValueError):
        offset = None
    if offset is None:
        raise ValueError("rule lease now must be timezone-aware")
    return value.astimezone(timezone.utc)


def _audit(
    session: Session,
    *,
    actor: str,
    reason: str,
    request_id: str,
    action: str,
    target_type: str,
    target_id: int,
    result_code: str,
) -> None:
    detail = augment_lifecycle_detail(
        session,
        target_type=target_type,
        target_id=target_id,
    )
    persist_sensitive(
        session,
        AuditEvent(
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            request_id=request_id,
            result_code=result_code,
        ),
        {
            "reason": reason,
            "detail_json": json.dumps(
                detail,
                separators=(",", ":"),
                sort_keys=True,
            ),
        },
    )


def _set_rule_state(
    session: Session,
    rule: Rule,
    state: RuleState,
    *,
    actor: str,
    reason: str,
    request_id: str,
) -> bool:
    if rule.state == state.value:
        return False
    rule.state = state.value
    _audit(
        session,
        actor=actor,
        reason=reason,
        request_id=request_id,
        action="rule.lifecycle",
        target_type="rule",
        target_id=rule.id,
        result_code=state.value,
    )
    return True


def _set_group_state(
    session: Session,
    group: RuleGroup,
    state: RuleState,
    *,
    now: datetime,
    actor: str,
    reason: str,
    request_id: str,
    terminal_rule_id: int | None = None,
) -> bool:
    changed = (
        group.state != state.value
        or group.terminal_rule_id != terminal_rule_id
        or group.lease_owner is not None
        or group.lease_expires_at is not None
    )
    if not changed:
        return False
    group.state = state.value
    group.terminal_rule_id = terminal_rule_id
    group.lease_owner = None
    group.lease_expires_at = None
    group.version += 1
    group.updated_at = now
    _audit(
        session,
        actor=actor,
        reason=reason,
        request_id=request_id,
        action="rule_group.lifecycle",
        target_type="rule_group",
        target_id=group.id,
        result_code=state.value,
    )
    return True


def _set_group_reconciliation_required(
    session: Session,
    group: RuleGroup,
    required: bool,
    *,
    now: datetime,
    actor: str,
    reason: str,
    request_id: str,
) -> bool:
    if group.reconciliation_required == required:
        return False
    group.reconciliation_required = required
    group.updated_at = now
    _audit(
        session,
        actor=actor,
        reason=reason,
        request_id=request_id,
        action="rule_group.reconciliation_required",
        target_type="rule_group",
        target_id=group.id,
        result_code="required" if required else "cleared",
    )
    return True


def reconcile_plan_lifecycle_in_session(
    session: Session,
    *,
    now: datetime,
    actor: str,
    reason: str,
    request_id: str,
) -> FillActivationResult:
    """Settle plan rules from trusted fills in the caller's transaction.

    A proposal is never treated as execution. Entry fills activate protection;
    exit fills resize it from actual remaining plan quantity. A plan becomes
    terminal only after broker-linked plan orders are terminal and trusted
    fills prove the plan quantity is flat.
    """
    actor, reason, request_id = _require_context(
        actor, reason, request_id
    )
    session.flush()
    plan_ids = [
        plan_id
        for plan_id in session.scalars(
            select(Rule.plan_id)
            .where(Rule.plan_id.is_not(None))
            .distinct()
        ).all()
        if plan_id is not None
    ]
    groups_activated = 0
    rules_activated = 0
    rules_resized = 0
    rules_settled = 0
    plans_completed = 0

    for plan_id in plan_ids:
        rules = list(
            session.scalars(
                select(Rule)
                .where(Rule.plan_id == plan_id)
                .order_by(Rule.id)
            ).all()
        )
        if not rules:
            continue
        rule_by_id = {rule.id: rule for rule in rules}
        group_ids = {rule.group_id for rule in rules}
        groups = {
            group.id: group
            for group in session.scalars(
                select(RuleGroup).where(
                    RuleGroup.id.in_(group_ids)
                )
            ).all()
        }
        proposal_rows = list(
            session.execute(
                select(Proposal, Order)
                .join(Order, Order.id == Proposal.order_id)
                .where(
                    Proposal.source_rule_id.in_(
                        tuple(rule_by_id)
                    )
                )
                .order_by(Proposal.id)
            ).all()
        )
        orders_by_rule: dict[int, list[Order]] = {
            rule_id: [] for rule_id in rule_by_id
        }
        rule_by_order_id: dict[int, Rule] = {}
        for proposal, order in proposal_rows:
            source_rule_id = proposal.source_rule_id
            if source_rule_id not in rule_by_id:
                continue
            orders_by_rule[source_rule_id].append(order)
            rule_by_order_id[order.id] = rule_by_id[source_rule_id]

        order_ids = tuple(rule_by_order_id)
        fills = (
            list(
                session.scalars(
                    select(Fill).where(
                        Fill.order_id.in_(order_ids)
                    )
                ).all()
            )
            if order_ids
            else []
        )
        fill_qty_by_order: dict[int, Decimal] = {}
        for fill in fills:
            if (
                fill.order_id is None
                or not fill_has_trusted_identity(fill)
            ):
                continue
            fill_qty_by_order[fill.order_id] = (
                fill_qty_by_order.get(
                    fill.order_id,
                    Decimal(0),
                )
                + fill.qty
            )
        fill_qty_by_rule = {
            rule_id: sum(
                (
                    fill_qty_by_order.get(order.id, Decimal(0))
                    for order in orders
                ),
                Decimal(0),
            )
            for rule_id, orders in orders_by_rule.items()
        }
        entry_rules = [
            rule
            for rule in rules
            if rule.kind == RuleKind.ENTRY.value
        ]
        exit_rules = [
            rule for rule in rules if rule.kind in _EXIT_KINDS
        ]
        untrusted_plan_fill_order_ids = {
            fill.order_id
            for fill in fills
            if (
                fill.order_id is not None
                and not fill_has_trusted_identity(fill)
            )
        }
        if untrusted_plan_fill_order_ids:
            detail = (
                f"plan {plan_id} has untrusted fill truth for "
                "orders "
                + ",".join(
                    str(order_id)
                    for order_id in sorted(
                        untrusted_plan_fill_order_ids
                    )
                )
            )
            for group in groups.values():
                _set_group_reconciliation_required(
                    session,
                    group,
                    True,
                    now=now,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                )
            trip_in_session(
                session,
                BreakerScope.broker_drift(),
                detail,
                actor,
                request_id=request_id,
                now=now,
                audit_reason=reason,
            )
        entry_filled_qty = sum(
            (
                fill_qty_by_rule.get(rule.id, Decimal(0))
                for rule in entry_rules
            ),
            Decimal(0),
        )
        exit_filled_qty = sum(
            (
                fill_qty_by_rule.get(rule.id, Decimal(0))
                for rule in exit_rules
            ),
            Decimal(0),
        )
        signed_remaining_qty = entry_filled_qty - exit_filled_qty
        over_exited = signed_remaining_qty < 0
        remaining_qty = max(signed_remaining_qty, Decimal(0))
        plan = session.get(TradePlanRow, plan_id)
        if plan is None:
            raise ValueError(
                f"plan-linked rules reference missing plan {plan_id}"
            )
        if (
            plan.entry_filled_qty != entry_filled_qty
            or plan.exit_filled_qty != exit_filled_qty
        ):
            plan.entry_filled_qty = entry_filled_qty
            plan.exit_filled_qty = exit_filled_qty
            plan.residual_generation += 1
            _audit(
                session,
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="plan.residual_generation",
                target_type="trade_plan",
                target_id=plan.id,
                result_code=str(plan.residual_generation),
            )
        if over_exited:
            trip_in_session(
                session,
                BreakerScope.broker_drift(),
                (
                    f"plan {plan_id} trusted exit fills "
                    f"{exit_filled_qty} exceed trusted entry fills "
                    f"{entry_filled_qty}"
                ),
                actor,
                request_id=request_id,
                now=now,
                audit_reason=reason,
            )

        for rule in entry_rules:
            if rule.state != RuleState.PROCESSING.value:
                continue
            linked = orders_by_rule.get(rule.id, [])
            if not linked or any(
                _order_requires_exact_reconciliation(order)
                for order in linked
            ):
                continue
            target_state = (
                RuleState.TRIGGERED
                if fill_qty_by_rule.get(rule.id, Decimal(0)) > 0
                else RuleState.CANCELED
                if any(
                    order.last_error_code
                    in {
                        "plan_exit_entry_cancel",
                        "plan_cancel",
                    }
                    for order in linked
                )
                else RuleState.FAILED
            )
            if _set_rule_state(
                session,
                rule,
                target_state,
                actor=actor,
                reason=reason,
                request_id=request_id,
            ):
                rules_settled += 1
            group = groups[rule.group_id]
            _set_group_state(
                session,
                group,
                target_state,
                now=now,
                actor=actor,
                reason=reason,
                request_id=request_id,
                terminal_rule_id=rule.id,
            )

        for proposal, order in proposal_rows:
            rule = rule_by_order_id.get(order.id)
            stale_exit_intent = bool(
                rule is not None
                and rule.kind in _EXIT_KINDS
                and proposal.plan_generation
                < plan.residual_generation
            )
            should_cancel = bool(
                rule is not None
                and (
                    (
                        rule.kind == RuleKind.ENTRY.value
                        and exit_filled_qty > 0
                    )
                    or stale_exit_intent
                )
            )
            if (
                not should_cancel
                or order.status
                not in _LOCAL_CANCELABLE_ORDER_STATES
            ):
                continue
            target = (
                OrderStatus.CANCELED
                if order.status
                == OrderStatus.PROPOSED.value
                else OrderStatus.REJECTED
            )
            OrderStateMachine.transition(order, target)
            order.last_error_code = "plan_exit_entry_cancel"
            _audit(
                session,
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="order.plan_entry_cancel",
                target_type="order",
                target_id=order.id,
                result_code=target.value,
            )
            if rule.kind == RuleKind.ENTRY.value:
                if _set_rule_state(
                    session,
                    rule,
                    RuleState.CANCELED,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                ):
                    rules_settled += 1
                _set_group_state(
                    session,
                    groups[rule.group_id],
                    RuleState.CANCELED,
                    now=now,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                    terminal_rule_id=rule.id,
                )

        nonterminal_plan_orders = [
            order
            for _proposal, order in proposal_rows
            if _order_requires_exact_reconciliation(order)
        ]
        plan_reconciliation_required = any(
            group.reconciliation_required
            for group in groups.values()
        )
        terminal_late_residual = bool(
            remaining_qty > 0
            and plan.status in {"completed", "canceled"}
        )
        if terminal_late_residual:
            failure_reason = (
                f"terminal plan {plan_id} received late trusted "
                f"fills and now has residual quantity {remaining_qty}"
            )
            trip_in_session(
                session,
                BreakerScope.broker_drift(),
                failure_reason,
                actor,
                request_id=request_id,
                now=now,
                audit_reason=reason,
            )
            trip_in_session(
                session,
                BreakerScope.operator_global(),
                failure_reason,
                actor,
                request_id=request_id,
                now=now,
                audit_reason=reason,
            )
            plan.status = "protection_required"
            _audit(
                session,
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="plan.protection_required",
                target_type="trade_plan",
                target_id=plan.id,
                result_code="protection_required",
            )
            for rule in exit_rules:
                if rule.kind not in {
                    RuleKind.STOP.value,
                    RuleKind.TRAILING.value,
                    RuleKind.TIME.value,
                }:
                    continue
                if _set_rule_state(
                    session,
                    rule,
                    RuleState.ACTIVE,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                ):
                    rules_activated += 1
                group = groups[rule.group_id]
                _set_group_reconciliation_required(
                    session,
                    group,
                    plan_reconciliation_required,
                    now=now,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                )
                _set_group_state(
                    session,
                    group,
                    RuleState.ACTIVE,
                    now=now,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                )
        for rule in exit_rules:
            if rule.state != RuleState.PROCESSING.value:
                continue
            linked = orders_by_rule.get(rule.id, [])
            if not linked or any(
                _order_requires_exact_reconciliation(order)
                for order in linked
            ):
                continue
            filled_qty = fill_qty_by_rule.get(
                rule.id,
                Decimal(0),
            )
            if (
                filled_qty > 0
                and remaining_qty == 0
                and not nonterminal_plan_orders
            ):
                continue
            target_state = (
                RuleState.TRIGGERED
                if (
                    filled_qty > 0
                    and rule.kind == RuleKind.TARGET.value
                    and not rule.terminal_on_trigger
                )
                else RuleState.ACTIVE
            )
            if _set_rule_state(
                session,
                rule,
                target_state,
                actor=actor,
                reason=reason,
                request_id=request_id,
            ):
                rules_settled += 1
            if target_state is RuleState.ACTIVE:
                _set_group_state(
                    session,
                    groups[rule.group_id],
                    RuleState.ACTIVE,
                    now=now,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                )
                trip_in_session(
                    session,
                    BreakerScope.operator_global(),
                    (
                        f"plan {plan_id} protective exit rule "
                        f"{rule.id} ended without flattening confirmed "
                        "plan quantity and was re-armed"
                    ),
                    actor,
                    request_id=request_id,
                    now=now,
                    audit_reason=reason,
                )

        touched_group_ids: set[int] = set()
        if remaining_qty > 0:
            for rule in exit_rules:
                if rule.state not in {
                    RuleState.PENDING.value,
                    RuleState.ACTIVE.value,
                }:
                    continue
                if (
                    rule.kind == RuleKind.TARGET.value
                    and not rule.terminal_on_trigger
                ):
                    allocated = (
                        entry_filled_qty
                        * (rule.fraction or Decimal(0))
                    ).to_integral_value(rounding=ROUND_DOWN)
                    desired = min(
                        max(
                            allocated
                            - fill_qty_by_rule.get(
                                rule.id,
                                Decimal(0),
                            ),
                            Decimal(0),
                        ),
                        remaining_qty,
                    )
                else:
                    desired = remaining_qty
                if desired <= 0:
                    continue
                normalized = normalize_computed_order_decimal(
                    desired
                )
                if normalized is None:
                    continue
                try:
                    action_payload = json.loads(rule.action_json)
                    action_payload["qty"] = normalized
                    action_payload.pop("notional", None)
                    action = RuleAction.model_validate(
                        action_payload
                    )
                except (
                    json.JSONDecodeError,
                    TypeError,
                    ValueError,
                ) as exc:
                    raise ValueError(
                        f"invalid persisted exit action for rule "
                        f"{rule.id}"
                    ) from exc
                encoded = json.dumps(
                    action.model_dump(mode="json"),
                    separators=(",", ":"),
                    sort_keys=True,
                )
                was_pending = (
                    rule.state == RuleState.PENDING.value
                )
                changed_payload = encoded != rule.action_json
                if was_pending:
                    rule.state = RuleState.ACTIVE.value
                    rules_activated += 1
                elif changed_payload:
                    rules_resized += 1
                if was_pending or changed_payload:
                    rule.action_json = encoded
                    touched_group_ids.add(rule.group_id)
                    _audit(
                        session,
                        actor=actor,
                        reason=reason,
                        request_id=request_id,
                        action=(
                            "rule.activate"
                            if was_pending
                            else "rule.resize"
                        ),
                        target_type="rule",
                        target_id=rule.id,
                        result_code=RuleState.ACTIVE.value,
                    )
        for group_id in touched_group_ids:
            group = groups[group_id]
            if group.state == RuleState.PENDING.value:
                _set_group_state(
                    session,
                    group,
                    RuleState.ACTIVE,
                    now=now,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                )
                groups_activated += 1

        if (
            entry_filled_qty > 0
            and exit_filled_qty > 0
            and signed_remaining_qty == 0
            and not over_exited
            and not nonterminal_plan_orders
            and not plan_reconciliation_required
        ):
            winning_rule = max(
                (
                    rule
                    for rule in exit_rules
                    if fill_qty_by_rule.get(
                        rule.id,
                        Decimal(0),
                    )
                    > 0
                ),
                key=lambda rule: rule.id,
            )
            for rule in rules:
                if rule.id == winning_rule.id:
                    state = RuleState.TRIGGERED
                elif rule.state == RuleState.TRIGGERED.value:
                    continue
                elif rule.state in {
                    RuleState.PENDING.value,
                    RuleState.ACTIVE.value,
                    RuleState.PROCESSING.value,
                }:
                    state = RuleState.CANCELED
                else:
                    continue
                if _set_rule_state(
                    session,
                    rule,
                    state,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                ):
                    rules_settled += 1
            for group in groups.values():
                if group.id == winning_rule.group_id:
                    state = RuleState.TRIGGERED
                    terminal_rule_id = winning_rule.id
                elif group.state == RuleState.TRIGGERED.value:
                    continue
                else:
                    state = RuleState.CANCELED
                    terminal_rule_id = None
                _set_group_state(
                    session,
                    group,
                    state,
                    now=now,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                    terminal_rule_id=terminal_rule_id,
                )
            if plan.status in {
                "approved",
                "protection_required",
            }:
                plan.status = "completed"
                _audit(
                    session,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                    action="plan.complete",
                    target_type="trade_plan",
                    target_id=plan.id,
                    result_code="completed",
                )
                plans_completed += 1

    return FillActivationResult(
        groups_activated=groups_activated,
        rules_activated=rules_activated,
        rules_resized=rules_resized,
        rules_settled=rules_settled,
        plans_completed=plans_completed,
    )


class RuleRepository:
    def __init__(self, session_factory: sessionmaker[Session], owner: str) -> None:
        owner = owner.strip()
        if not owner:
            raise ValueError("rule lease owner must be non-empty")
        self.session_factory = session_factory
        self.owner = owner
        self.submission_barrier = SubmissionBarrier(
            session_factory
        )

    def active_group_ids(self) -> list[int]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(RuleGroup.id)
                    .where(RuleGroup.state == RuleState.ACTIVE.value)
                    .order_by(RuleGroup.id)
                ).all()
            )

    @serialized_writer
    def expire_stale_proposals(
        self,
        *,
        now: datetime,
        actor: str,
        reason: str,
        request_id: str,
    ) -> int:
        """Expire unattended proposals and immediately settle plan protection."""
        actor, reason, request_id = _require_context(
            actor, reason, request_id
        )
        with self.session_factory() as session:
            rows = list(
                session.execute(
                    select(Proposal, Order)
                    .join(Order, Order.id == Proposal.order_id)
                    .where(
                        Order.status.in_(
                            {
                                OrderStatus.PROPOSED.value,
                                OrderStatus.APPROVAL_RECORDED.value,
                            }
                        ),
                        Proposal.expires_at <= now,
                    )
                    .order_by(Order.id)
                ).all()
            )
            for _proposal, order in rows:
                OrderStateMachine.transition(
                    order,
                    OrderStatus.EXPIRED,
                )
                order.updated_at = now
                order.version += 1
                _audit(
                    session,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                    action="order.expire_ttl",
                    target_type="order",
                    target_id=order.id,
                    result_code=OrderStatus.EXPIRED.value,
                )
            if rows:
                reconcile_plan_lifecycle_in_session(
                    session,
                    now=now,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                )
            session.commit()
            return len(rows)

    @serialized_writer
    def refresh_fill_activated_rules(
        self,
        *,
        now: datetime,
        actor: str,
        reason: str,
        request_id: str,
    ) -> FillActivationResult:
        """Reconcile every plan rule from trusted broker-linked fills."""
        actor, reason, request_id = _require_context(
            actor, reason, request_id
        )
        with self.session_factory() as session:
            result = reconcile_plan_lifecycle_in_session(
                session,
                now=now,
                actor=actor,
                reason=reason,
                request_id=request_id,
            )
            session.commit()
            return result

    def plan_order_ids_requiring_broker_cancel(self) -> list[int]:
        """Return live plan orders that must be quiesced after an exit fill."""
        with self.session_factory() as session:
            rows = list(
                session.execute(
                    select(Proposal, Order, Rule)
                    .join(Order, Order.id == Proposal.order_id)
                    .join(
                        Rule,
                        Rule.id == Proposal.source_rule_id,
                    )
                    .where(
                        Rule.plan_id.is_not(None),
                        Order.status.in_(
                            _BROKER_CANCELABLE_ORDER_STATES
                        ),
                    )
                ).all()
            )
            if not rows:
                return []
            all_plan_ids = {
                rule.plan_id
                for _proposal, _order, rule in rows
                if rule.plan_id is not None
            }
            all_rows = list(
                session.execute(
                    select(Proposal, Order, Rule)
                    .join(Order, Order.id == Proposal.order_id)
                    .join(
                        Rule,
                        Rule.id == Proposal.source_rule_id,
                    )
                    .where(Rule.plan_id.in_(all_plan_ids))
                ).all()
            )
            order_ids = tuple(
                order.id
                for _proposal, order, _rule in all_rows
            )
            fills = (
                list(
                    session.scalars(
                        select(Fill).where(
                            Fill.order_id.in_(order_ids)
                        )
                    ).all()
                )
                if order_ids
                else []
            )
            fill_qty_by_order: dict[int, Decimal] = {}
            rule_by_order_id = {
                order.id: rule
                for _proposal, order, rule in all_rows
            }
            for fill in fills:
                if (
                    fill.order_id is None
                    or not fill_has_trusted_identity(fill)
                ):
                    continue
                fill_qty_by_order[fill.order_id] = (
                    fill_qty_by_order.get(
                        fill.order_id,
                        Decimal(0),
                    )
                    + fill.qty
                )
            totals: dict[int, tuple[Decimal, Decimal]] = {}
            for _proposal, order, rule in all_rows:
                assert rule.plan_id is not None
                entry, exit_ = totals.get(
                    rule.plan_id,
                    (Decimal(0), Decimal(0)),
                )
                qty = fill_qty_by_order.get(
                    order.id,
                    Decimal(0),
                )
                if rule.kind == RuleKind.ENTRY.value:
                    entry += qty
                elif rule.kind in _EXIT_KINDS:
                    exit_ += qty
                totals[rule.plan_id] = (entry, exit_)
            plans = {
                plan.id: plan
                for plan in session.scalars(
                    select(TradePlanRow).where(
                        TradePlanRow.id.in_(all_plan_ids)
                    )
                ).all()
            }

            candidates: list[int] = []
            for proposal, order, rule in rows:
                assert rule.plan_id is not None
                plan = plans.get(rule.plan_id)
                if plan is None:
                    candidates.append(order.id)
                    continue
                entry, exit_ = totals.get(
                    rule.plan_id,
                    (Decimal(0), Decimal(0)),
                )
                remaining = entry - exit_
                if (
                    (
                        rule.kind == RuleKind.ENTRY.value
                        and exit_ > 0
                    )
                    or (
                        exit_ > 0
                        and remaining <= 0
                    )
                    or (
                        rule.kind in _EXIT_KINDS
                        and proposal.plan_generation
                        < plan.residual_generation
                    )
                ):
                    candidates.append(order.id)
            return sorted(set(candidates))

    def plan_cancellation_intent_order_ids(self) -> list[int]:
        """Return durable plan-cancel intents left by an interrupted process."""
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(Order.id)
                    .join(
                        Proposal,
                        Proposal.order_id == Order.id,
                    )
                    .join(
                        Rule,
                        Rule.id == Proposal.source_rule_id,
                    )
                    .where(
                        Rule.plan_id.is_not(None),
                        Order.plan_cancel_state.in_(
                            (
                                PLAN_CANCEL_REQUESTED,
                                PLAN_CANCEL_INDETERMINATE,
                            )
                        ),
                    )
                    .order_by(Order.id)
                ).all()
            )

    def plan_nonterminal_order_ids(self, plan_id: int) -> list[int]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(Order.id)
                    .join(
                        Proposal,
                        Proposal.order_id == Order.id,
                    )
                    .join(
                        Rule,
                        Rule.id == Proposal.source_rule_id,
                    )
                    .where(
                        Rule.plan_id == plan_id,
                        or_(
                            Order.status.in_(
                                _NONTERMINAL_ORDER_STATES
                            ),
                            Order.acceptance_state
                            == FILL_RECONCILIATION_REQUIRED,
                        ),
                    )
                    .order_by(Order.id)
                ).all()
            )

    def plan_entry_nonterminal_order_ids(
        self,
        plan_id: int,
    ) -> list[int]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(Order.id)
                    .join(
                        Proposal,
                        Proposal.order_id == Order.id,
                    )
                    .join(
                        Rule,
                        Rule.id == Proposal.source_rule_id,
                    )
                    .where(
                        Rule.plan_id == plan_id,
                        Rule.kind == RuleKind.ENTRY.value,
                        or_(
                            Order.status.in_(
                                _NONTERMINAL_ORDER_STATES
                            ),
                            Order.acceptance_state
                            == FILL_RECONCILIATION_REQUIRED,
                        ),
                    )
                    .order_by(Order.id)
                ).all()
            )

    def plan_execution_truth(
        self,
        plan_id: int,
    ) -> PlanExecutionTruth:
        with self.session_factory() as session:
            return _plan_execution_truth(session, plan_id)

    @serialized_writer
    def plan_cancellation_blocker(
        self,
        plan_id: int,
        *,
        now: datetime,
        actor: str,
        reason: str,
        request_id: str,
    ) -> PlanCancellationResult | None:
        """Return the exact reason a plan may not be canceled, if any."""
        actor, reason, request_id = _require_context(
            actor, reason, request_id
        )
        with self.session_factory() as session:
            plan = session.get(TradePlanRow, plan_id)
            if plan is None:
                return PlanCancellationResult(
                    plan_id=plan_id,
                    canceled=False,
                    status=None,
                    error="not_found",
                )
            if plan.status == RuleState.CANCELED.value:
                return None
            if plan.status not in {"proposed", "approved"}:
                return PlanCancellationResult(
                    plan_id=plan_id,
                    canceled=False,
                    status=plan.status,
                    error="invalid_state",
                )
            truth = _plan_execution_truth(session, plan_id)
            if (
                truth.reconciliation_required
                or any(
                    session.get(Order, order_id).acceptance_state
                    == FILL_RECONCILIATION_REQUIRED
                    for order_id in truth.unresolved_order_ids
                )
            ):
                return PlanCancellationResult(
                    plan_id=plan_id,
                    canceled=False,
                    status=plan.status,
                    error="reconciliation_required",
                )
            if truth.residual_qty > 0:
                return PlanCancellationResult(
                    plan_id=plan_id,
                    canceled=False,
                    status=plan.status,
                    error="position_open",
                )
            if truth.residual_qty < 0:
                trip_in_session(
                    session,
                    BreakerScope.broker_drift(),
                    (
                        f"plan {plan_id} trusted exit fills exceed "
                        "trusted entry fills"
                    ),
                    actor,
                    request_id=request_id,
                    now=now,
                    audit_reason=reason,
                )
                session.commit()
                return PlanCancellationResult(
                    plan_id=plan_id,
                    canceled=False,
                    status=plan.status,
                    error="over_exit",
                )
            return None

    def validate_plan_exit_submission(
        self,
        order_id: int,
        requested_qty: Decimal | None,
        broker_position_qty: Decimal,
    ) -> str | None:
        """Validate a plan exit against plan-owned exact fill quantity."""
        with self.session_factory() as session:
            row = session.execute(
                select(
                    Proposal,
                    Rule,
                    RuleGroup,
                    TradePlanRow,
                    Order,
                )
                .join(Order, Order.id == Proposal.order_id)
                .join(Rule, Rule.id == Proposal.source_rule_id)
                .join(RuleGroup, RuleGroup.id == Rule.group_id)
                .join(TradePlanRow, TradePlanRow.id == Rule.plan_id)
                .where(Proposal.order_id == order_id)
            ).one_or_none()
            if row is None:
                return None
            proposal, rule, group, plan, order = row
            if rule.kind not in _EXIT_KINDS:
                return None
            if (
                plan.status
                not in {"approved", "protection_required"}
                or rule.state != RuleState.PROCESSING.value
                or group.state != RuleState.ACTIVE.value
                or group.reconciliation_required
            ):
                return "plan exit lifecycle is not execution-ready"
            if proposal.plan_generation != plan.residual_generation:
                return "plan exit intent uses a stale residual generation"
            sibling = session.scalar(
                select(Order.id)
                .join(Proposal, Proposal.order_id == Order.id)
                .where(
                    Proposal.source_rule_group_id == group.id,
                    Proposal.order_id != order_id,
                    or_(
                        Order.status.in_(
                            _NONTERMINAL_ORDER_STATES
                        ),
                        Order.acceptance_state
                        == FILL_RECONCILIATION_REQUIRED,
                    ),
                )
                .limit(1)
            )
            if sibling is not None:
                return "another plan exit intent is still nonterminal"
            live_entry = session.scalar(
                select(Order.id)
                .join(Proposal, Proposal.order_id == Order.id)
                .join(Rule, Rule.id == Proposal.source_rule_id)
                .where(
                    Rule.plan_id == rule.plan_id,
                    Rule.kind == RuleKind.ENTRY.value,
                    or_(
                        Order.status.in_(
                            _NONTERMINAL_ORDER_STATES
                        ),
                        Order.acceptance_state
                        == FILL_RECONCILIATION_REQUIRED,
                    ),
                )
                .limit(1)
            )
            if live_entry is not None:
                return "plan entry order is still nonterminal"
            truth = _plan_execution_truth(session, rule.plan_id)
            if truth.residual_qty < 0:
                return "plan has negative trusted residual quantity"
            if truth.residual_qty == 0:
                return "plan has no trusted residual quantity"
            if (
                requested_qty is None
                or requested_qty > truth.residual_qty
            ):
                return "exit quantity exceeds trusted plan residual"
            plan_ids = list(
                session.scalars(
                    select(TradePlanRow.id).where(
                        TradePlanRow.symbol == plan.symbol,
                        TradePlanRow.action == plan.action,
                        TradePlanRow.status.in_(
                            {
                                "approved",
                                "protection_required",
                            }
                        ),
                    )
                ).all()
            )
            allocated = Decimal(0)
            for allocated_plan_id in plan_ids:
                allocated_truth = _plan_execution_truth(
                    session,
                    allocated_plan_id,
                )
                if (
                    not _plan_allocation_is_exact(
                        session,
                        allocated_truth,
                    )
                ):
                    return (
                        "plan allocation cannot be proven from "
                        "reconciled fill truth"
                    )
                allocated += allocated_truth.residual_qty
            available = (
                max(broker_position_qty, Decimal(0))
                if order.side == "sell"
                else max(-broker_position_qty, Decimal(0))
            )
            if allocated > available:
                return (
                    "plan allocation exceeds reconciled broker position"
                )
            return None

    def is_plan_exit_order(self, order_id: int) -> bool:
        with self.session_factory() as session:
            kind = session.scalar(
                select(Rule.kind)
                .join(
                    Proposal,
                    Proposal.source_rule_id == Rule.id,
                )
                .where(
                    Proposal.order_id == order_id,
                    Rule.plan_id.is_not(None),
                )
            )
            return kind in _EXIT_KINDS

    def plan_allocation_truth(
        self,
        symbol: str,
        exit_side: str,
    ) -> tuple[Decimal, bool]:
        """Return aggregate plan-owned quantity and whether it is exact."""
        plan_action = (
            "buy"
            if exit_side == "sell"
            else "sell"
            if exit_side == "buy"
            else None
        )
        if plan_action is None:
            return Decimal(0), False
        with self.session_factory() as session:
            plan_ids = list(
                session.scalars(
                    select(TradePlanRow.id).where(
                        TradePlanRow.symbol == symbol.upper(),
                        TradePlanRow.action == plan_action,
                        TradePlanRow.status.in_(
                            {
                                "approved",
                                "protection_required",
                            }
                        ),
                    )
                ).all()
            )
            allocated = Decimal(0)
            for plan_id in plan_ids:
                truth = _plan_execution_truth(session, plan_id)
                if not _plan_allocation_is_exact(session, truth):
                    return allocated, False
                allocated += truth.residual_qty
            return allocated, True

    @serialized_writer
    def lease_group(
        self,
        group_id: int,
        now: datetime,
        ttl: timedelta = timedelta(seconds=30),
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> RuleGroupLease | None:
        actor, reason, request_id = _require_context(
            actor, reason, request_id
        )
        normalized_now = _require_aware_utc_lease_now(now)
        if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
            raise ValueError("rule lease ttl must be positive")
        try:
            expires_at = normalized_now + ttl
        except (OverflowError, TypeError):
            raise ValueError("rule lease ttl is invalid") from None
        if expires_at <= normalized_now:
            raise ValueError("rule lease expiry must follow now")
        with self.session_factory() as session:
            group = session.get(RuleGroup, group_id)
            if group is None:
                session.rollback()
                return None
            created_at = _require_aware_utc_lease_now(
                group.created_at
            )
            updated_at = _require_aware_utc_lease_now(
                group.updated_at
            )
            if (
                normalized_now < created_at
                or normalized_now < updated_at
            ):
                session.rollback()
                raise RuleLeaseChronologyError(
                    "rule lease now precedes durable group state"
                )
            unresolved = exists(
                select(Order.id)
                .join(Proposal, Proposal.order_id == Order.id)
                .where(
                    Proposal.source_rule_group_id == group_id,
                    Order.status.in_(
                        (
                            OrderStatus.SUBMITTING.value,
                            OrderStatus.ACCEPTANCE_UNKNOWN.value,
                        )
                    ),
                )
            )
            latch = session.execute(
                update(RuleGroup)
                .where(
                    RuleGroup.id == group_id,
                    RuleGroup.created_at <= normalized_now,
                    RuleGroup.updated_at <= normalized_now,
                    RuleGroup.reconciliation_required.is_(False),
                    unresolved,
                )
                .values(
                    reconciliation_required=True,
                    updated_at=normalized_now,
                )
            )
            if latch.rowcount:
                _audit(
                    session,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                    action="rule_group.reconciliation_latch",
                    target_type="rule_group",
                    target_id=group_id,
                    result_code="required",
                )
            version = session.execute(
                update(RuleGroup)
                .where(
                    RuleGroup.id == group_id,
                    RuleGroup.created_at <= normalized_now,
                    RuleGroup.updated_at <= normalized_now,
                    RuleGroup.state == RuleState.ACTIVE.value,
                    RuleGroup.reconciliation_required.is_(False),
                    or_(
                        RuleGroup.lease_expires_at.is_(None),
                        RuleGroup.lease_expires_at <= normalized_now,
                    ),
                )
                .values(
                    lease_owner=self.owner,
                    lease_expires_at=expires_at,
                    version=RuleGroup.version + 1,
                    updated_at=normalized_now,
                )
                .returning(RuleGroup.version)
            ).scalar_one_or_none()
            if version is None:
                # Preserve a reconciliation latch discovered above even though
                # the lease CAS itself was (correctly) denied.
                session.commit()
                return None
            _audit(
                session,
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="rule_group.lease",
                target_type="rule_group",
                target_id=group_id,
                result_code="leased",
            )
            session.commit()
            return RuleGroupLease(
                group_id=group_id,
                owner=self.owner,
                expires_at=expires_at,
                version=version,
            )

    def load_rules(self, lease: RuleGroupLease) -> list[StoredRule]:
        with self.session_factory() as session:
            group = session.scalar(
                select(RuleGroup).where(
                    RuleGroup.id == lease.group_id,
                    RuleGroup.state == RuleState.ACTIVE.value,
                    RuleGroup.lease_owner == lease.owner,
                    RuleGroup.version == lease.version,
                    RuleGroup.reconciliation_required.is_(False),
                )
            )
            if group is None:
                return []
            rows = session.scalars(
                select(Rule)
                .where(
                    Rule.group_id == lease.group_id,
                    Rule.state == RuleState.ACTIVE.value,
                )
                .order_by(Rule.id)
            ).all()
            return [self._stored_rule(group.group_key, row) for row in rows]

    def load_rule(
        self, lease: RuleGroupLease, rule_id: int
    ) -> StoredRule | None:
        return next(
            (stored for stored in self.load_rules(lease) if stored.id == rule_id),
            None,
        )

    @staticmethod
    def _stored_rule(group_key: str, row: Rule) -> StoredRule:
        if row.payload_version != 1:
            raise ValueError(
                f"rule {row.id} has unsupported payload_version "
                f"{row.payload_version}"
            )
        command = RuleCommand.model_validate(
            {
                "ticker": row.ticker,
                "kind": row.kind,
                "condition": json.loads(row.condition_json),
                "action": json.loads(row.action_json),
                "group_key": group_key,
                "pre_approved": row.pre_approved,
                "fraction": row.fraction,
                "high_water_mark": row.hwm,
                "activation": row.activation,
                "terminal_on_trigger": row.terminal_on_trigger,
            }
        )
        return StoredRule(
            row.id,
            row.group_id,
            command,
            row.plan_id,
        )

    @serialized_writer
    def release_group(
        self,
        lease: RuleGroupLease,
        *,
        now: datetime,
        high_water_marks: dict[int, object] | None = None,
        actor: str,
        reason: str,
        request_id: str,
    ) -> bool:
        actor, reason, request_id = _require_context(
            actor, reason, request_id
        )
        validated_high_water_marks = {
            rule_id: validate_persisted_high_water_mark(high_water_mark)
            for rule_id, high_water_mark in (high_water_marks or {}).items()
        }
        with self.session_factory() as session:
            released = session.execute(
                update(RuleGroup)
                .where(
                    RuleGroup.id == lease.group_id,
                    RuleGroup.state == RuleState.ACTIVE.value,
                    RuleGroup.lease_owner == lease.owner,
                    RuleGroup.version == lease.version,
                )
                .values(
                    lease_owner=None,
                    lease_expires_at=None,
                    version=RuleGroup.version + 1,
                    updated_at=now,
                )
            )
            if released.rowcount != 1:
                session.rollback()
                return False
            _audit(
                session,
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="rule_group.release",
                target_type="rule_group",
                target_id=lease.group_id,
                result_code="released",
            )
            for rule_id, high_water_mark in validated_high_water_marks.items():
                changed = session.execute(
                    update(Rule)
                    .where(
                        Rule.id == rule_id,
                        Rule.group_id == lease.group_id,
                        Rule.state.in_(
                            (RuleState.ACTIVE.value, RuleState.PROCESSING.value)
                        ),
                        Rule.hwm.is_distinct_from(high_water_mark),
                    )
                    .values(hwm=high_water_mark)
                )
                if changed.rowcount:
                    _audit(
                        session,
                        actor=actor,
                        reason=reason,
                        request_id=request_id,
                        action="rule.high_water_mark",
                        target_type="rule",
                        target_id=rule_id,
                        result_code="updated",
                    )
            session.commit()
            return True

    @serialized_writer
    def claim_proposal(
        self,
        lease: RuleGroupLease,
        rule_id: int,
        *,
        now: datetime,
        high_water_mark=None,
        session: Session | None = None,
        actor: str,
        reason: str,
        request_id: str,
    ) -> bool:
        """Move one plan rule to PROCESSING without claiming execution."""
        actor, reason, request_id = _require_context(
            actor, reason, request_id
        )
        if high_water_mark is not None:
            high_water_mark = validate_persisted_high_water_mark(
                high_water_mark
            )
        owns_session = session is None
        current = session or self.session_factory()
        try:
            existing_intent = current.scalar(
                select(Order.id)
                .join(Proposal, Proposal.order_id == Order.id)
                .where(
                    Proposal.source_rule_group_id
                    == lease.group_id,
                    or_(
                        Order.status.in_(
                            _NONTERMINAL_ORDER_STATES
                        ),
                        Order.acceptance_state
                        == FILL_RECONCILIATION_REQUIRED,
                    ),
                )
                .limit(1)
            )
            if existing_intent is not None:
                current.rollback()
                return False
            group_result = current.execute(
                update(RuleGroup)
                .where(
                    RuleGroup.id == lease.group_id,
                    RuleGroup.state == RuleState.ACTIVE.value,
                    RuleGroup.lease_owner == lease.owner,
                    RuleGroup.version == lease.version,
                    RuleGroup.reconciliation_required.is_(False),
                )
                .values(
                    lease_owner=None,
                    lease_expires_at=None,
                    version=RuleGroup.version + 1,
                    updated_at=now,
                )
            )
            if group_result.rowcount != 1:
                current.rollback()
                return False
            rule_result = current.execute(
                update(Rule)
                .where(
                    Rule.id == rule_id,
                    Rule.group_id == lease.group_id,
                    Rule.plan_id.is_not(None),
                    Rule.state == RuleState.ACTIVE.value,
                )
                .values(
                    state=RuleState.PROCESSING.value,
                    **(
                        {"hwm": high_water_mark}
                        if high_water_mark is not None
                        else {}
                    ),
                )
            )
            if rule_result.rowcount != 1:
                current.rollback()
                return False
            _audit(
                current,
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="rule_group.proposal_pending",
                target_type="rule_group",
                target_id=lease.group_id,
                result_code=RuleState.ACTIVE.value,
            )
            _audit(
                current,
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="rule.proposal_pending",
                target_type="rule",
                target_id=rule_id,
                result_code=RuleState.PROCESSING.value,
            )
            if owns_session:
                current.commit()
            return True
        except Exception:
            current.rollback()
            raise
        finally:
            if owns_session:
                current.close()

    @serialized_writer
    def claim_terminal(
        self,
        lease: RuleGroupLease,
        winning_rule_id: int,
        *,
        now: datetime,
        terminal_state: RuleState = RuleState.TRIGGERED,
        high_water_mark=None,
        session: Session | None = None,
        actor: str,
        reason: str,
        request_id: str,
    ) -> bool:
        actor, reason, request_id = _require_context(
            actor, reason, request_id
        )
        if terminal_state not in {
            RuleState.TRIGGERED,
            RuleState.CANCELED,
            RuleState.FAILED,
        }:
            raise ValueError("winning rule must transition to a terminal state")
        if high_water_mark is not None:
            high_water_mark = validate_persisted_high_water_mark(high_water_mark)

        owns_session = session is None
        current = session or self.session_factory()
        try:
            winning_rule = current.get(Rule, winning_rule_id)
            if (
                winning_rule is None
                or winning_rule.group_id != lease.group_id
            ):
                current.rollback()
                return False
            group_result = current.execute(
                update(RuleGroup)
                .where(
                    RuleGroup.id == lease.group_id,
                    RuleGroup.state == RuleState.ACTIVE.value,
                    RuleGroup.lease_owner == lease.owner,
                    RuleGroup.version == lease.version,
                    RuleGroup.reconciliation_required.is_(False),
                )
                .values(
                    state=terminal_state.value,
                    terminal_rule_id=winning_rule_id,
                    lease_owner=None,
                    lease_expires_at=None,
                    version=RuleGroup.version + 1,
                    updated_at=now,
                )
            )
            if group_result.rowcount != 1:
                current.rollback()
                return False
            _audit(
                current,
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="rule_group.terminal",
                target_type="rule_group",
                target_id=lease.group_id,
                result_code=terminal_state.value,
            )
            winner = current.execute(
                update(Rule)
                .where(
                    Rule.id == winning_rule_id,
                    Rule.group_id == lease.group_id,
                    Rule.state.in_(
                        (RuleState.ACTIVE.value, RuleState.PROCESSING.value)
                    ),
                )
                .values(
                    state=terminal_state.value,
                    **(
                        {"hwm": high_water_mark}
                        if high_water_mark is not None
                        else {}
                    ),
                )
            )
            if winner.rowcount != 1:
                current.rollback()
                return False
            _audit(
                current,
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="rule.terminal",
                target_type="rule",
                target_id=winning_rule_id,
                result_code=terminal_state.value,
            )
            sibling_ids = list(
                current.scalars(
                    update(Rule)
                    .where(
                        Rule.group_id == lease.group_id,
                        Rule.id != winning_rule_id,
                        Rule.state.in_(
                            (
                                RuleState.PENDING.value,
                                RuleState.ACTIVE.value,
                                RuleState.PROCESSING.value,
                            )
                        ),
                    )
                    .values(state=RuleState.CANCELED.value)
                    .returning(Rule.id)
                )
            )
            for sibling_id in sibling_ids:
                _audit(
                    current,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                    action="rule.cancel",
                    target_type="rule",
                    target_id=sibling_id,
                    result_code=RuleState.CANCELED.value,
                )
            if (
                terminal_state is RuleState.TRIGGERED
                and winning_rule.plan_id is not None
                and winning_rule.kind
                in {
                    RuleKind.TARGET.value,
                    RuleKind.STOP.value,
                    RuleKind.TRAILING.value,
                    RuleKind.TIME.value,
                }
            ):
                plan_group_ids = list(
                    current.scalars(
                        select(Rule.group_id)
                        .where(
                            Rule.plan_id == winning_rule.plan_id
                        )
                        .distinct()
                    ).all()
                )
                other_group_ids = [
                    group_id
                    for group_id in plan_group_ids
                    if group_id != lease.group_id
                ]
                if other_group_ids:
                    canceled_group_ids = list(
                        current.scalars(
                            update(RuleGroup)
                            .where(
                                RuleGroup.id.in_(other_group_ids),
                                RuleGroup.state.in_(
                                    (
                                        RuleState.PENDING.value,
                                        RuleState.ACTIVE.value,
                                    )
                                ),
                            )
                            .values(
                                state=RuleState.CANCELED.value,
                                terminal_rule_id=None,
                                lease_owner=None,
                                lease_expires_at=None,
                                version=RuleGroup.version + 1,
                                updated_at=now,
                            )
                            .returning(RuleGroup.id)
                        )
                    )
                    for group_id in canceled_group_ids:
                        _audit(
                            current,
                            actor=actor,
                            reason=reason,
                            request_id=request_id,
                            action="rule_group.cancel",
                            target_type="rule_group",
                            target_id=group_id,
                            result_code=RuleState.CANCELED.value,
                        )
                    canceled_plan_rule_ids = list(
                        current.scalars(
                            update(Rule)
                            .where(
                                Rule.group_id.in_(other_group_ids),
                                Rule.state.in_(
                                    (
                                        RuleState.PENDING.value,
                                        RuleState.ACTIVE.value,
                                        RuleState.PROCESSING.value,
                                    )
                                ),
                            )
                            .values(state=RuleState.CANCELED.value)
                            .returning(Rule.id)
                        )
                    )
                    for canceled_rule_id in canceled_plan_rule_ids:
                        _audit(
                            current,
                            actor=actor,
                            reason=reason,
                            request_id=request_id,
                            action="rule.cancel",
                            target_type="rule",
                            target_id=canceled_rule_id,
                            result_code=RuleState.CANCELED.value,
                        )

                cancelable_order_ids = list(
                    current.scalars(
                        select(Order.id)
                        .join(
                            Proposal,
                            Proposal.order_id == Order.id,
                        )
                        .where(
                            Proposal.source_rule_group_id.in_(
                                plan_group_ids
                            ),
                            Order.status
                            == OrderStatus.PROPOSED.value,
                        )
                    ).all()
                )
                if cancelable_order_ids:
                    current.execute(
                        update(Order)
                        .where(
                            Order.id.in_(cancelable_order_ids),
                            Order.status
                            == OrderStatus.PROPOSED.value,
                        )
                        .values(
                            status=OrderStatus.CANCELED.value,
                            updated_at=now,
                        )
                    )
                    for order_id in cancelable_order_ids:
                        _audit(
                            current,
                            actor=actor,
                            reason=reason,
                            request_id=request_id,
                            action="order.cancel",
                            target_type="order",
                            target_id=order_id,
                            result_code=OrderStatus.CANCELED.value,
                        )
            if owns_session:
                current.commit()
            return True
        except Exception:
            current.rollback()
            raise
        finally:
            if owns_session:
                current.close()

    @serialized_writer
    def claim_progress(
        self,
        lease: RuleGroupLease,
        winning_rule_id: int,
        *,
        now: datetime,
        rule_state: RuleState,
        high_water_mark=None,
        session: Session | None = None,
        actor: str,
        reason: str,
        request_id: str,
    ) -> bool:
        """Consume one target while preserving the active protective group."""
        actor, reason, request_id = _require_context(
            actor, reason, request_id
        )
        if rule_state not in {
            RuleState.TRIGGERED,
            RuleState.FAILED,
        }:
            raise ValueError(
                "progressing rule must become triggered or failed"
            )
        if high_water_mark is not None:
            high_water_mark = validate_persisted_high_water_mark(
                high_water_mark
            )
        owns_session = session is None
        current = session or self.session_factory()
        try:
            group_result = current.execute(
                update(RuleGroup)
                .where(
                    RuleGroup.id == lease.group_id,
                    RuleGroup.state == RuleState.ACTIVE.value,
                    RuleGroup.lease_owner == lease.owner,
                    RuleGroup.version == lease.version,
                    RuleGroup.reconciliation_required.is_(False),
                )
                .values(
                    lease_owner=None,
                    lease_expires_at=None,
                    version=RuleGroup.version + 1,
                    updated_at=now,
                )
            )
            if group_result.rowcount != 1:
                current.rollback()
                return False
            winner = current.execute(
                update(Rule)
                .where(
                    Rule.id == winning_rule_id,
                    Rule.group_id == lease.group_id,
                    Rule.state.in_(
                        (
                            RuleState.ACTIVE.value,
                            RuleState.PROCESSING.value,
                        )
                    ),
                )
                .values(
                    state=rule_state.value,
                    **(
                        {"hwm": high_water_mark}
                        if high_water_mark is not None
                        else {}
                    ),
                )
            )
            if winner.rowcount != 1:
                current.rollback()
                return False
            _audit(
                current,
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="rule_group.progress",
                target_type="rule_group",
                target_id=lease.group_id,
                result_code=RuleState.ACTIVE.value,
            )
            _audit(
                current,
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="rule.terminal",
                target_type="rule",
                target_id=winning_rule_id,
                result_code=rule_state.value,
            )
            if owns_session:
                current.commit()
            return True
        except Exception:
            current.rollback()
            raise
        finally:
            if owns_session:
                current.close()

    @serialized_writer
    def cancel_plan(
        self,
        plan_id: int,
        *,
        now: datetime,
        actor: str,
        reason: str,
        request_id: str,
    ) -> PlanCancellationResult:
        """Atomically cancel every resumable group owned by one plan."""
        actor, reason, request_id = _require_context(
            actor, reason, request_id
        )

        with self.session_factory() as session:
            plan = session.get(TradePlanRow, plan_id)
            if plan is None:
                return PlanCancellationResult(
                    plan_id=plan_id,
                    canceled=False,
                    status=None,
                    error="not_found",
                )
            if plan.status == RuleState.CANCELED.value:
                return PlanCancellationResult(
                    plan_id=plan_id,
                    canceled=True,
                    status=plan.status,
                )
            if plan.status not in {"proposed", "approved"}:
                return PlanCancellationResult(
                    plan_id=plan_id,
                    canceled=False,
                    status=plan.status,
                    error="invalid_state",
                )

            truth = _plan_execution_truth(session, plan_id)
            if truth.reconciliation_required:
                return PlanCancellationResult(
                    plan_id=plan_id,
                    canceled=False,
                    status=plan.status,
                    error="reconciliation_required",
                )
            if truth.residual_qty > 0:
                return PlanCancellationResult(
                    plan_id=plan_id,
                    canceled=False,
                    status=plan.status,
                    error="position_open",
                )
            if truth.residual_qty < 0:
                trip_in_session(
                    session,
                    BreakerScope.broker_drift(),
                    (
                        f"plan {plan_id} trusted exit fills exceed "
                        "trusted entry fills"
                    ),
                    actor,
                    request_id=request_id,
                    now=now,
                    audit_reason=reason,
                )
                session.commit()
                return PlanCancellationResult(
                    plan_id=plan_id,
                    canceled=False,
                    status=plan.status,
                    error="over_exit",
                )

            group_ids = list(
                session.scalars(
                    select(Rule.group_id)
                    .where(Rule.plan_id == plan_id)
                    .distinct()
                ).all()
            )
            live_order_id = session.scalar(
                select(Order.id)
                .join(
                    Proposal,
                    Proposal.order_id == Order.id,
                )
                .join(
                    Rule,
                    Rule.id == Proposal.source_rule_id,
                )
                .where(
                    Rule.plan_id == plan_id,
                    or_(
                        Order.status.in_(
                            _NONTERMINAL_ORDER_STATES
                        ),
                        Order.acceptance_state
                        == FILL_RECONCILIATION_REQUIRED,
                    ),
                )
                .limit(1)
            )
            if live_order_id is not None:
                return PlanCancellationResult(
                    plan_id=plan_id,
                    canceled=False,
                    status=plan.status,
                    error="orders_live",
                )
            cancelable_group_ids: list[int] = []
            if group_ids:
                cancelable_group_ids = list(
                    session.scalars(
                        select(RuleGroup.id).where(
                            RuleGroup.id.in_(group_ids),
                            RuleGroup.state.in_(
                                (
                                    RuleState.PENDING.value,
                                    RuleState.ACTIVE.value,
                                )
                            ),
                        )
                    ).all()
                )
            if cancelable_group_ids:
                canceled_group_ids = list(
                    session.scalars(
                        update(RuleGroup)
                        .where(
                            RuleGroup.id.in_(cancelable_group_ids),
                            RuleGroup.state.in_(
                                (
                                    RuleState.PENDING.value,
                                    RuleState.ACTIVE.value,
                                )
                            ),
                        )
                        .values(
                            state=RuleState.CANCELED.value,
                            terminal_rule_id=None,
                            lease_owner=None,
                            lease_expires_at=None,
                            version=RuleGroup.version + 1,
                            updated_at=now,
                        )
                        .returning(RuleGroup.id)
                    )
                )
            else:
                canceled_group_ids = []
            for group_id in canceled_group_ids:
                _audit(
                    session,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                    action="rule_group.cancel",
                    target_type="rule_group",
                    target_id=group_id,
                    result_code=RuleState.CANCELED.value,
                )
            canceled_rule_ids = list(
                session.scalars(
                    update(Rule)
                    .where(
                        Rule.group_id.in_(cancelable_group_ids),
                        Rule.state.in_(
                            (
                                RuleState.PENDING.value,
                                RuleState.ACTIVE.value,
                                RuleState.PROCESSING.value,
                            )
                        ),
                    )
                    .values(state=RuleState.CANCELED.value)
                    .returning(Rule.id)
                )
            )
            for canceled_rule_id in canceled_rule_ids:
                _audit(
                    session,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                    action="rule.cancel",
                    target_type="rule",
                    target_id=canceled_rule_id,
                    result_code=RuleState.CANCELED.value,
                )
            cancelable_order_ids: list[int] = []
            if group_ids:
                cancelable_order_ids = list(
                    session.scalars(
                        select(Order.id)
                        .join(
                            Proposal,
                            Proposal.order_id == Order.id,
                        )
                        .where(
                            Proposal.source_rule_group_id.in_(group_ids),
                            Order.status
                            == OrderStatus.PROPOSED.value,
                        )
                    ).all()
                )
            if cancelable_order_ids:
                session.execute(
                    update(Order)
                    .where(
                        Order.id.in_(cancelable_order_ids),
                        Order.status == OrderStatus.PROPOSED.value,
                    )
                    .values(
                        status=OrderStatus.CANCELED.value,
                        updated_at=now,
                    )
                )
                for order_id in cancelable_order_ids:
                    _audit(
                        session,
                        actor=actor,
                        reason=reason,
                        request_id=request_id,
                        action="order.cancel",
                        target_type="order",
                        target_id=order_id,
                        result_code=OrderStatus.CANCELED.value,
                    )
            plan_claim = session.execute(
                update(TradePlanRow)
                .where(
                    TradePlanRow.id == plan_id,
                    TradePlanRow.status == plan.status,
                )
                .values(status=RuleState.CANCELED.value)
            )
            if plan_claim.rowcount != 1:
                session.rollback()
                return PlanCancellationResult(
                    plan_id=plan_id,
                    canceled=False,
                    status=plan.status,
                    error="plan_conflict",
                )
            _audit(
                session,
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="plan.cancel",
                target_type="trade_plan",
                target_id=plan_id,
                result_code=RuleState.CANCELED.value,
            )
            session.commit()
            return PlanCancellationResult(
                plan_id=plan_id,
                canceled=True,
                status=RuleState.CANCELED.value,
                rules_canceled=len(canceled_rule_ids),
            )
