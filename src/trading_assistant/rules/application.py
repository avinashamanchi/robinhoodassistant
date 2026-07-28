"""Application boundary for validated rule persistence and proposal creation."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Iterable, Mapping, MutableMapping, TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trading_assistant.broker.models import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)
from trading_assistant.db.models import (
    AuditEvent,
    Order,
    Proposal,
    RiskEvent,
    Rule,
    RuleGroup,
    TradePlanRow,
    utcnow,
)
from trading_assistant.risk.submission_barrier import (
    serialized_writer,
)
from trading_assistant.risk.breakers import trip_in_session
from trading_assistant.security.sensitive_fields import persist_sensitive

from .models import (
    normalize_computed_order_decimal,
    RuleAction,
    RuleCommand,
    RuleKind,
    RuleOutcome,
    RuleState,
)
from .repository import (
    reconcile_plan_lifecycle_in_session,
    RuleGroupLease,
    RuleRepository,
)

if TYPE_CHECKING:
    from trading_assistant.service import TradingService


def _audit(
    session: Session,
    *,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    request_id: str,
    reason: str,
    result_code: str,
    detail_json: str = "{}",
    created_at=None,
) -> None:
    persist_sensitive(
        session,
        AuditEvent(
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            request_id=request_id,
            result_code=result_code,
            created_at=created_at or utcnow(),
        ),
        {"reason": reason, "detail_json": detail_json},
    )


def _risk_event(
    session: Session,
    *,
    order_id: int | None,
    event_type: str,
    reason: str,
) -> None:
    persist_sensitive(
        session,
        RiskEvent(order_id=order_id, event_type=event_type),
        {"reason": reason},
    )


class RuleApplicationService:
    """The only runtime boundary allowed to persist or fire rule commands."""

    def __init__(
        self,
        service: "TradingService",
        repository: RuleRepository,
        *,
        crash_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.service = service
        self.repository = repository
        self.submission_barrier = service.submission_barrier
        self.crash_hook = crash_hook

    @staticmethod
    def _validated(command: RuleCommand | dict) -> RuleCommand:
        validated = RuleCommand.model_validate(
            command.model_dump(mode="python")
            if isinstance(command, RuleCommand)
            else command
        )
        if validated.pre_approved:
            raise ValueError(
                "pre_approved=true is disabled while global auto_execute=false"
            )
        return validated

    @serialized_writer
    def create_rule(
        self,
        command: RuleCommand | dict,
        *,
        actor: str,
        reason: str,
        request_id: str,
        plan_id: int | None = None,
    ) -> int:
        with self.service.session_factory() as session:
            rows = self.persist_commands(
                session,
                [command],
                actor=actor,
                reason=reason,
                request_id=request_id,
                plan_id=plan_id,
            )
            session.commit()
            return rows[0].id

    def persist_commands(
        self,
        session: Session,
        commands: Iterable[RuleCommand | dict],
        *,
        actor: str,
        reason: str,
        request_id: str,
        plan_id: int | None = None,
    ) -> list[Rule]:
        actor = actor.strip()
        reason = reason.strip()
        request_id = request_id.strip()
        if not actor or not reason or not request_id:
            raise ValueError(
                "rule persistence actor, reason, and request_id "
                "must be non-empty"
            )
        validated = [self._validated(command) for command in commands]
        if not validated:
            return []

        generated_group_key = f"rule-{uuid.uuid4().hex}"
        groups: dict[str, RuleGroup] = {}
        group_activation: dict[str, str] = {}
        rows: list[Rule] = []
        for command in validated:
            group_key = command.group_key or generated_group_key
            prior_activation = group_activation.setdefault(
                group_key,
                command.activation,
            )
            if prior_activation != command.activation:
                raise ValueError(
                    f"rule group {group_key!r} mixes activation policies"
                )
            desired_group_state = (
                RuleState.PENDING.value
                if command.activation == "on_entry_fill"
                else RuleState.ACTIVE.value
            )
            group = groups.get(group_key)
            if group is None:
                group = session.scalar(
                    select(RuleGroup).where(RuleGroup.group_key == group_key)
                )
                if group is None:
                    group = RuleGroup(
                        group_key=group_key,
                        state=desired_group_state,
                    )
                    session.add(group)
                    session.flush()
                    _audit(
                            session,
                            actor=actor,
                            action="rule_group.create",
                            target_type="rule_group",
                            target_id=str(group.id),
                            request_id=request_id,
                            reason=reason,
                            result_code=group.state,
                    )
                elif (
                    command.activation == "immediate"
                    and group.state != RuleState.ACTIVE.value
                ) or (
                    command.activation == "on_entry_fill"
                    and group.state
                    not in {
                        RuleState.PENDING.value,
                        RuleState.ACTIVE.value,
                    }
                ):
                    raise ValueError(
                        f"rule group {group_key!r} cannot accept "
                        f"{command.activation!r} rules"
                    )
                existing_plan_ids = set(
                    session.scalars(
                        select(Rule.plan_id)
                        .where(Rule.group_id == group.id)
                        .distinct()
                    ).all()
                )
                if existing_plan_ids and existing_plan_ids != {plan_id}:
                    raise ValueError(
                        f"rule group {group_key!r} has different plan ownership"
                    )
                existing_activations = set(
                    session.scalars(
                        select(Rule.activation)
                        .where(Rule.group_id == group.id)
                        .distinct()
                    ).all()
                )
                if (
                    existing_activations
                    and existing_activations
                    != {command.activation}
                ):
                    raise ValueError(
                        f"rule group {group_key!r} has a different "
                        "persisted activation policy"
                    )
                groups[group_key] = group

            payload = command.model_dump(mode="json")
            row = Rule(
                group_id=group.id,
                payload_version=1,
                ticker=command.ticker,
                condition_json=json.dumps(
                    payload["condition"], separators=(",", ":"), sort_keys=True
                ),
                action_json=json.dumps(
                    payload["action"], separators=(",", ":"), sort_keys=True
                ),
                state=(
                    RuleState.PENDING.value
                    if (
                        command.activation == "on_entry_fill"
                        and group.state == RuleState.PENDING.value
                    )
                    else RuleState.ACTIVE.value
                ),
                plan_id=plan_id,
                kind=command.kind.value,
                fraction=command.fraction,
                hwm=command.high_water_mark,
                deadline=(
                    command.condition.deadline
                    if command.kind is RuleKind.TIME
                    else None
                ),
                pre_approved=command.pre_approved,
                activation=command.activation,
                terminal_on_trigger=command.terminal_on_trigger,
            )
            session.add(row)
            session.flush()
            _audit(
                    session,
                    actor=actor,
                    action="rule.create",
                    target_type="rule",
                    target_id=str(row.id),
                    request_id=request_id,
                    reason=reason,
                    result_code=row.state,
            )
            rows.append(row)
        return rows

    @serialized_writer
    def propose_from_lease(
        self,
        lease: RuleGroupLease,
        rule_id: int,
        command: RuleCommand | dict,
        *,
        actor: str,
        reason: str,
        request_id: str,
        now: datetime | None = None,
        reference_price: Decimal | None = None,
        reference_quote=None,
        quote_overrides: Mapping[str, object] | None = None,
        quote_cache: MutableMapping[str, object] | None = None,
        high_water_mark: Decimal | None = None,
    ) -> RuleOutcome:
        actor = actor.strip()
        operation_reason = reason.strip()
        request_id = request_id.strip()
        if not actor or not operation_reason or not request_id:
            raise ValueError(
                "rule proposal actor, reason, and request_id "
                "must be non-empty"
            )
        now = now or datetime.now(timezone.utc)
        validated = self._validated(command)
        stored = self.repository.load_rule(lease, rule_id)
        if stored is None:
            return RuleOutcome(
                group_id=lease.group_id,
                rule_id=rule_id,
                error="lease_conflict",
            )
        if stored.command != validated:
            raise ValueError("rule command does not match validated persisted payload")

        if (
            stored.plan_id is not None
            and validated.kind
            in {
                RuleKind.TARGET,
                RuleKind.STOP,
                RuleKind.TRAILING,
                RuleKind.TIME,
            }
        ):
            quiesced = self.service.quiesce_trade_plan_orders(
                stored.plan_id,
                entry_only=True,
                actor=actor,
                reason=operation_reason,
                request_id=request_id,
            )
            if quiesced["failed"]:
                self.repository.release_group(
                    lease,
                    now=now,
                    actor=actor,
                    reason=operation_reason,
                    request_id=request_id,
                )
                return RuleOutcome(
                    group_id=lease.group_id,
                    rule_id=rule_id,
                    error="entry_order_cancel_unconfirmed",
                )

        action = self._bounded_action(validated, reference_price)
        if action is None:
            self.repository.release_group(
                lease,
                now=now,
                actor=actor,
                reason=operation_reason,
                request_id=request_id,
            )
            return RuleOutcome(
                group_id=lease.group_id,
                rule_id=rule_id,
                error="no unreserved position to exit",
            )

        attempt = 1
        if stored.plan_id is not None:
            with self.service.session_factory() as attempt_session:
                attempt = (
                    attempt_session.scalar(
                        select(func.count())
                        .select_from(Proposal)
                        .where(Proposal.source_rule_id == rule_id)
                    )
                    or 0
                ) + 1
        base_idempotency_key = (
            f"rule-group-{lease.group_id}-rule-{rule_id}"
        )
        request = OrderRequest(
            ticker=validated.ticker,
            side=OrderSide(action.side),
            order_type=OrderType(action.order_type),
            idempotency_key=(
                base_idempotency_key
                if attempt == 1
                else f"{base_idempotency_key}-attempt-{attempt}"
            ),
            qty=action.qty,
            notional=action.notional,
            limit_price=action.limit_price,
        )
        asset_class = self.service._asset_class(request.ticker)
        snapshot_quote_overrides = dict(quote_overrides or {})
        if quote_cache is not None:
            for symbol, quote in quote_cache.items():
                snapshot_quote_overrides.setdefault(symbol, quote)
        if reference_quote is not None:
            snapshot_quote_overrides.setdefault(request.ticker, reference_quote)
        with self.service.session_factory() as read_session:
            snapshot = self.service.assemble_snapshot(
                read_session,
                [request.ticker],
                asset_class,
                quote_overrides=snapshot_quote_overrides or None,
            )
            if quote_cache is not None:
                for symbol, quote in snapshot.quotes.items():
                    quote_cache.setdefault(symbol.upper(), quote)
            risk = self.service._risk_for(asset_class).check(
                request,
                snapshot,
            )

        if self.crash_hook is not None:
            self.crash_hook("before_transaction")

        with self.service.session_factory() as session:
            sibling_count = session.scalar(
                select(func.count())
                .select_from(Rule)
                .where(
                    Rule.group_id == lease.group_id,
                    Rule.id != rule_id,
                    Rule.state.in_(
                        (
                            RuleState.PENDING.value,
                            RuleState.ACTIVE.value,
                            RuleState.PROCESSING.value,
                        )
                    ),
                )
            )
            terminal_state = (
                RuleState.FAILED if risk.rejected else RuleState.TRIGGERED
            )
            if stored.plan_id is not None:
                terminal_group = False
                claimed = self.repository.claim_proposal(
                    lease,
                    rule_id,
                    now=now,
                    high_water_mark=high_water_mark,
                    session=session,
                    actor=actor,
                    reason=operation_reason,
                    request_id=request_id,
                )
            else:
                exit_kind = validated.kind in {
                    RuleKind.TARGET,
                    RuleKind.STOP,
                    RuleKind.TRAILING,
                    RuleKind.TIME,
                }
                terminal_group = (
                    validated.terminal_on_trigger
                    and (not risk.rejected or not exit_kind)
                )
                claimed = (
                    self.repository.claim_terminal(
                        lease,
                        rule_id,
                        now=now,
                        terminal_state=terminal_state,
                        high_water_mark=high_water_mark,
                        session=session,
                        actor=actor,
                        reason=operation_reason,
                        request_id=request_id,
                    )
                    if terminal_group
                    else self.repository.claim_progress(
                        lease,
                        rule_id,
                        now=now,
                        rule_state=terminal_state,
                        high_water_mark=high_water_mark,
                        session=session,
                        actor=actor,
                        reason=operation_reason,
                        request_id=request_id,
                    )
                )
            if not claimed:
                session.rollback()
                if stored.plan_id is not None:
                    self.repository.release_group(
                        lease,
                        now=now,
                        actor=actor,
                        reason=operation_reason,
                        request_id=request_id,
                    )
                return RuleOutcome(
                    group_id=lease.group_id,
                    rule_id=rule_id,
                    error="lease_conflict",
                )

            order = Order(
                idempotency_key=request.idempotency_key,
                ticker=request.ticker,
                side=request.side.value,
                order_type=request.order_type.value,
                qty=request.qty,
                notional=request.notional,
                limit_price=request.limit_price,
                status=(
                    OrderStatus.REJECTED.value
                    if risk.rejected
                    else OrderStatus.PROPOSED.value
                ),
            )
            persist_sensitive(
                session,
                order,
                {"approval_reason": "approval pending"},
            )
            risk_config = (
                self.service.config.crypto_risk
                if asset_class.value == "crypto"
                else self.service.config.risk
            )
            ttl = (risk_config or self.service.config.risk).proposal_ttl_minutes
            plan_generation = 0
            if stored.plan_id is not None:
                plan = session.get(TradePlanRow, stored.plan_id)
                if plan is None:
                    session.rollback()
                    return RuleOutcome(
                        group_id=lease.group_id,
                        rule_id=rule_id,
                        error="plan_not_found",
                    )
                plan_generation = plan.residual_generation
            persist_sensitive(
                session,
                Proposal(
                    order_id=order.id,
                    source_rule_group_id=lease.group_id,
                    source_rule_id=rule_id,
                    plan_generation=plan_generation,
                    ttl_minutes=ttl,
                    created_at=now,
                    expires_at=now + timedelta(minutes=ttl),
                ),
                {"reasoning": operation_reason},
            )
            for risk_reason in risk.reasons:
                _risk_event(
                        session,
                        order_id=order.id,
                        event_type="rejection",
                        reason=risk_reason,
                )
            for warning in risk.warnings:
                _risk_event(
                        session,
                        order_id=order.id,
                        event_type="warning",
                        reason=warning,
                )
            for intent in risk.breaker_trips:
                trip_in_session(
                    session,
                    intent.scope,
                    intent.reason,
                    actor,
                    request_id=request_id,
                    now=now,
                    audit_reason=operation_reason,
                )
            _audit(
                    session,
                    actor=actor,
                    action="order.propose",
                    target_type="order",
                    target_id=str(order.id),
                    request_id=request_id,
                    reason=operation_reason,
                    result_code=order.status,
                    created_at=now,
                    detail_json=json.dumps(
                        {
                            "source": "conditional_rule",
                            "rule_id": rule_id,
                            "rule_group_id": lease.group_id,
                        },
                        sort_keys=True,
                    ),
            )
            if stored.plan_id is not None:
                reconcile_plan_lifecycle_in_session(
                    session,
                    now=now,
                    actor=actor,
                    reason=operation_reason,
                    request_id=request_id,
                )
            session.commit()
            proposal = {
                "order_id": order.id,
                "status": order.status,
                "approved_by_risk": risk.approved,
                "risk_reasons": list(risk.reasons),
                "risk_warnings": list(risk.warnings),
                "executed": False,
            }

        if self.crash_hook is not None:
            self.crash_hook("after_transaction")
        return RuleOutcome(
            group_id=lease.group_id,
            rule_id=rule_id,
            proposal=proposal,
            oco_canceled=(
                int(sibling_count or 0)
                if terminal_group and stored.plan_id is None
                else 0
            ),
        )

    def _bounded_action(
        self, command: RuleCommand, reference_price: Decimal | None
    ) -> RuleAction | None:
        action = command.action
        if command.kind not in {
            RuleKind.TARGET,
            RuleKind.STOP,
            RuleKind.TRAILING,
            RuleKind.TIME,
        }:
            return action

        available = self.service.available_reduce_qty(
            command.ticker,
            action.side,
            reference_price=reference_price,
        )
        if available <= 0:
            return None
        if action.qty is not None:
            qty = normalize_computed_order_decimal(min(action.qty, available))
            if qty is None:
                return None
            return RuleAction.model_validate(
                {**action.model_dump(mode="python"), "qty": qty}
            )

        price = reference_price
        if price is None:
            price = self.service.broker.get_quote(command.ticker).last
        if price <= 0:
            raise ValueError("reference price must be positive")
        assert action.notional is not None
        qty = normalize_computed_order_decimal(
            min(action.notional / price, available)
        )
        if qty is None:
            return None
        return RuleAction.model_validate(
            {
                **action.model_dump(mode="python"),
                "qty": qty,
                "notional": None,
            }
        )
