"""Transactional rule-group lease and terminal-state primitives."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import exists, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import (
    Order,
    Proposal,
    Rule,
    RuleGroup,
    TradePlanRow,
)

from .models import (
    RuleCommand,
    RuleState,
    validate_persisted_high_water_mark,
)


@dataclass(frozen=True)
class RuleGroupLease:
    group_id: int
    owner: str
    expires_at: datetime
    version: int


@dataclass(frozen=True)
class StoredRule:
    id: int
    group_id: int
    command: RuleCommand


@dataclass(frozen=True)
class PlanCancellationResult:
    plan_id: int
    canceled: bool
    status: str | None
    rules_canceled: int = 0
    error: str | None = None


class RuleRepository:
    def __init__(self, session_factory: sessionmaker[Session], owner: str) -> None:
        owner = owner.strip()
        if not owner:
            raise ValueError("rule lease owner must be non-empty")
        self.session_factory = session_factory
        self.owner = owner

    def active_group_ids(self) -> list[int]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(RuleGroup.id)
                    .where(RuleGroup.state == RuleState.ACTIVE.value)
                    .order_by(RuleGroup.id)
                ).all()
            )

    def lease_group(
        self,
        group_id: int,
        now: datetime,
        ttl: timedelta = timedelta(seconds=30),
    ) -> RuleGroupLease | None:
        if ttl <= timedelta(0):
            raise ValueError("rule lease ttl must be positive")
        expires_at = now + ttl
        with self.session_factory() as session:
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
            session.execute(
                update(RuleGroup)
                .where(
                    RuleGroup.id == group_id,
                    RuleGroup.reconciliation_required.is_(False),
                    unresolved,
                )
                .values(
                    reconciliation_required=True,
                    updated_at=now,
                )
            )
            version = session.execute(
                update(RuleGroup)
                .where(
                    RuleGroup.id == group_id,
                    RuleGroup.state == RuleState.ACTIVE.value,
                    RuleGroup.reconciliation_required.is_(False),
                    or_(
                        RuleGroup.lease_expires_at.is_(None),
                        RuleGroup.lease_expires_at <= now,
                    ),
                )
                .values(
                    lease_owner=self.owner,
                    lease_expires_at=expires_at,
                    version=RuleGroup.version + 1,
                    updated_at=now,
                )
                .returning(RuleGroup.version)
            ).scalar_one_or_none()
            if version is None:
                # Preserve a reconciliation latch discovered above even though
                # the lease CAS itself was (correctly) denied.
                session.commit()
                return None
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
                    Rule.state.in_(
                        (RuleState.ACTIVE.value, RuleState.PROCESSING.value)
                    ),
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
            }
        )
        return StoredRule(row.id, row.group_id, command)

    def release_group(
        self,
        lease: RuleGroupLease,
        *,
        now: datetime,
        high_water_marks: dict[int, object] | None = None,
    ) -> bool:
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
            for rule_id, high_water_mark in validated_high_water_marks.items():
                session.execute(
                    update(Rule)
                    .where(
                        Rule.id == rule_id,
                        Rule.group_id == lease.group_id,
                        Rule.state.in_(
                            (RuleState.ACTIVE.value, RuleState.PROCESSING.value)
                        ),
                    )
                    .values(hwm=high_water_mark)
                )
            session.commit()
            return True

    def claim_terminal(
        self,
        lease: RuleGroupLease,
        winning_rule_id: int,
        *,
        now: datetime,
        terminal_state: RuleState = RuleState.TRIGGERED,
        high_water_mark=None,
        session: Session | None = None,
    ) -> bool:
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
            current.execute(
                update(Rule)
                .where(
                    Rule.group_id == lease.group_id,
                    Rule.id != winning_rule_id,
                    Rule.state.in_(
                        (RuleState.ACTIVE.value, RuleState.PROCESSING.value)
                    ),
                )
                .values(state=RuleState.CANCELED.value)
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

    def cancel_plan(
        self,
        plan_id: int,
        *,
        now: datetime,
    ) -> PlanCancellationResult:
        """Atomically cancel one plan group or lose to a terminal worker CAS."""

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

            groups = session.execute(
                select(
                    RuleGroup.id,
                    RuleGroup.state,
                    RuleGroup.version,
                    RuleGroup.lease_owner,
                    RuleGroup.lease_expires_at,
                )
                .join(Rule, Rule.group_id == RuleGroup.id)
                .where(Rule.plan_id == plan_id)
                .distinct()
            ).all()
            if not groups:
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
                session.commit()
                return PlanCancellationResult(
                    plan_id=plan_id,
                    canceled=True,
                    status=RuleState.CANCELED.value,
                )
            if len(groups) != 1:
                return PlanCancellationResult(
                    plan_id=plan_id,
                    canceled=False,
                    status=plan.status,
                    error="group_conflict",
                )

            group = groups[0]
            if group.state != RuleState.ACTIVE.value:
                return PlanCancellationResult(
                    plan_id=plan_id,
                    canceled=False,
                    status=plan.status,
                    error="group_conflict",
                )

            group_conditions = [
                RuleGroup.id == group.id,
                RuleGroup.state == RuleState.ACTIVE.value,
                RuleGroup.version == group.version,
                (
                    RuleGroup.lease_owner.is_(None)
                    if group.lease_owner is None
                    else RuleGroup.lease_owner == group.lease_owner
                ),
                (
                    RuleGroup.lease_expires_at.is_(None)
                    if group.lease_expires_at is None
                    else RuleGroup.lease_expires_at == group.lease_expires_at
                ),
            ]
            group_claim = session.execute(
                update(RuleGroup)
                .where(*group_conditions)
                .values(
                    state=RuleState.CANCELED.value,
                    terminal_rule_id=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    version=RuleGroup.version + 1,
                    updated_at=now,
                )
            )
            if group_claim.rowcount != 1:
                session.rollback()
                current_status = session.scalar(
                    select(TradePlanRow.status).where(
                        TradePlanRow.id == plan_id
                    )
                )
                return PlanCancellationResult(
                    plan_id=plan_id,
                    canceled=False,
                    status=current_status,
                    error="group_conflict",
                )

            rules_canceled = session.execute(
                update(Rule)
                .where(
                    Rule.plan_id == plan_id,
                    Rule.group_id == group.id,
                    Rule.state.in_(
                        (RuleState.ACTIVE.value, RuleState.PROCESSING.value)
                    ),
                )
                .values(state=RuleState.CANCELED.value)
            ).rowcount
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
            session.commit()
            return PlanCancellationResult(
                plan_id=plan_id,
                canceled=True,
                status=RuleState.CANCELED.value,
                rules_canceled=rules_canceled,
            )
