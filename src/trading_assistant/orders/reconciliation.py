"""Broker-truth reconciliation and fail-closed operator panic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from trading_assistant.broker.base import BrokerClient
from trading_assistant.broker.models import BrokerFill, OrderResult, OrderStatus
from trading_assistant.db.models import (
    Fill,
    Order,
    OrderStateMachine,
    ReconciliationCursor,
    Rule,
    RuleGroup,
    Proposal,
)
from trading_assistant.risk.breakers import BreakerScope, BreakerService

from .repository import OrderRepository

_LOCAL_LIVE_STATUSES = (
    OrderStatus.SUBMITTING.value,
    OrderStatus.ACCEPTANCE_UNKNOWN.value,
    OrderStatus.SUBMITTED.value,
    OrderStatus.PARTIALLY_FILLED.value,
)
_REMOTE_OPEN_STATUSES = {
    OrderStatus.SUBMITTED,
    OrderStatus.PARTIALLY_FILLED,
}


@dataclass(frozen=True)
class ReconciliationReport:
    resolved_unknown: int
    unresolved_unknown: tuple[int, ...]
    synced_orders: int
    inserted_fills: int
    broker_drift: tuple[str, ...]


@dataclass(frozen=True)
class PanicReport:
    safe: bool
    confirmed_canceled: tuple[str, ...]
    unconfirmed_order_ids: tuple[int, ...]
    remote_open_order_ids: tuple[str, ...]
    message: str


class ReconciliationService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: BrokerClient,
        repository: OrderRepository,
        breakers: BreakerService | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.broker = broker
        self.repository = repository
        self.breakers = breakers or BreakerService(session_factory)
        self.broker_key = broker.reconciliation_key

    def reconcile_unknown(self) -> tuple[int, tuple[int, ...]]:
        resolved, unresolved, _ = self._resolve_unknown()
        self._clear_reconciled_rule_groups()
        return resolved, unresolved

    def _resolve_unknown(
        self,
    ) -> tuple[int, tuple[int, ...], tuple[tuple[int, OrderResult], ...]]:
        with self.session_factory() as session:
            rows = session.execute(
                select(Order.id, Order.idempotency_key).where(
                    Order.status.in_(
                        (
                            OrderStatus.SUBMITTING.value,
                            OrderStatus.ACCEPTANCE_UNKNOWN.value,
                        )
                    )
                )
            ).all()

        resolved = 0
        unresolved: list[int] = []
        resolved_results: list[tuple[int, OrderResult]] = []
        for order_id, client_order_id in rows:
            try:
                remote = self.broker.get_order_by_client_id(client_order_id)
            except Exception:
                unresolved.append(order_id)
                continue
            if remote is None or remote.broker_order_id is None:
                unresolved.append(order_id)
                continue
            if self.repository.resolve_acceptance(
                order_id,
                remote.broker_order_id,
                remote.status,
                datetime.now(timezone.utc),
            ):
                resolved += 1
                resolved_results.append((order_id, remote))
        return (
            resolved,
            tuple(sorted(unresolved)),
            tuple(resolved_results),
        )

    def reconcile(self) -> ReconciliationReport:
        resolved, unresolved, resolved_results = self._resolve_unknown()
        self._clear_reconciled_rule_groups()
        drift: list[str] = []
        inserted_fills = self._reconcile_fill_activities(drift)
        synced, synthetic_fills = self._reconcile_statuses(
            drift, prefetched_results=resolved_results
        )
        inserted_fills += synthetic_fills
        self._detect_open_order_drift(drift)
        return ReconciliationReport(
            resolved_unknown=resolved,
            unresolved_unknown=unresolved,
            synced_orders=synced,
            inserted_fills=inserted_fills,
            broker_drift=tuple(drift),
        )

    def _clear_reconciled_rule_groups(self) -> None:
        """Clear only groups whose linked outbox has no unresolved acceptance.

        This method intentionally lives in ReconciliationService: submission and
        worker code may set the latch, but cannot clear it.
        """
        with self.session_factory() as session:
            group_ids = session.scalars(
                select(RuleGroup.id).where(
                    RuleGroup.reconciliation_required.is_(True)
                )
            ).all()
            for group_id in group_ids:
                unresolved = session.scalar(
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
                    .limit(1)
                )
                if unresolved is None:
                    session.execute(
                        update(RuleGroup)
                        .where(
                            RuleGroup.id == group_id,
                            RuleGroup.reconciliation_required.is_(True),
                        )
                        .values(
                            reconciliation_required=False,
                            updated_at=datetime.now(timezone.utc),
                        )
                    )
            session.commit()

    def _cursor_snapshot(self) -> tuple[str | None, datetime | None, int | None]:
        with self.session_factory() as session:
            cursor = session.get(
                ReconciliationCursor, (self.broker_key, "fills")
            )
            if cursor is None:
                return None, None, None
            return (
                cursor.last_activity_id,
                cursor.last_activity_at,
                cursor.version,
            )

    def _reconcile_fill_activities(self, drift: list[str]) -> int:
        activity_reader = getattr(self.broker, "get_fill_activities", None)
        if not callable(activity_reader):
            return 0

        _last_activity_id, after, expected_version = self._cursor_snapshot()
        try:
            activities = list(activity_reader(after=after))
        except Exception as exc:
            drift.append(f"fill activities unavailable: {type(exc).__name__}")
            return 0

        # Activity IDs are opaque, not chronological. The broker query overlaps
        # the timestamp boundary and the fill table's unique broker ID is the
        # authority for deduplication, including late-visible equal-time fills.
        batch = sorted(activities, key=lambda activity: activity.filled_at)
        if not batch:
            return 0

        inserted = 0
        inserted_activities: list[BrokerFill] = []
        advance_cursor = True
        with self.session_factory() as session:
            try:
                for activity in batch:
                    order = session.scalar(
                        select(Order).where(
                            Order.broker_order_id == activity.broker_order_id
                        )
                    )
                    if order is None:
                        drift.append(
                            "fill activity "
                            f"{activity.broker_fill_id} has unknown broker order "
                            f"{activity.broker_order_id}"
                        )
                        advance_cursor = False
                        continue
                    self._remove_synthetic_fills(session, order)
                    duplicate = session.scalar(
                        select(Fill.id).where(
                            Fill.broker_fill_id == activity.broker_fill_id
                        )
                    )
                    if duplicate is not None:
                        continue
                    session.add(
                        Fill(
                            order_id=order.id,
                            ticker=activity.ticker,
                            side=activity.side,
                            qty=activity.qty,
                            price=activity.price,
                            broker_fill_id=activity.broker_fill_id,
                            filled_at=activity.filled_at,
                        )
                    )
                    inserted += 1
                    inserted_activities.append(activity)

                if advance_cursor:
                    cursor_candidate = None
                    if after is None:
                        cursor_candidate = batch[-1]
                    else:
                        at_or_after_cursor = [
                            activity
                            for activity in inserted_activities
                            if activity.filled_at >= after
                        ]
                        if at_or_after_cursor:
                            cursor_candidate = at_or_after_cursor[-1]
                    if cursor_candidate is not None:
                        self._advance_cursor(
                            session,
                            cursor_candidate,
                            expected_version=expected_version,
                        )
                session.commit()
            except Exception as exc:
                session.rollback()
                drift.append(f"fill activity batch not committed: {type(exc).__name__}")
                return 0
        return inserted

    @staticmethod
    def _remove_synthetic_fills(session: Session, order: Order) -> None:
        synthetic = session.scalars(
            select(Fill).where(
                Fill.order_id == order.id,
                Fill.broker_fill_id.like(f"{order.broker_order_id}:%"),
            )
        ).all()
        for fill in synthetic:
            session.delete(fill)
        if synthetic:
            session.flush()

    def _advance_cursor(
        self,
        session: Session,
        activity: BrokerFill,
        *,
        expected_version: int | None,
    ) -> None:
        cursor = session.get(
            ReconciliationCursor, (self.broker_key, "fills")
        )
        if cursor is None:
            if expected_version is not None:
                raise RuntimeError("reconciliation cursor changed concurrently")
            session.add(
                ReconciliationCursor(
                    broker=self.broker_key,
                    stream="fills",
                    last_activity_id=activity.broker_fill_id,
                    last_activity_at=activity.filled_at,
                    version=1,
                )
            )
            return
        if cursor.version != expected_version:
            raise RuntimeError("reconciliation cursor changed concurrently")
        cursor.last_activity_id = activity.broker_fill_id
        cursor.last_activity_at = activity.filled_at
        cursor.version += 1

    def _reconcile_statuses(
        self,
        drift: list[str],
        *,
        prefetched_results: tuple[tuple[int, OrderResult], ...] = (),
    ) -> tuple[int, int]:
        with self.session_factory() as session:
            missing_ids = session.scalars(
                select(Order.id).where(
                    Order.status.in_(
                        (
                            OrderStatus.SUBMITTED.value,
                            OrderStatus.PARTIALLY_FILLED.value,
                        )
                    ),
                    Order.broker_order_id.is_(None),
                )
            ).all()
            rows = session.execute(
                select(Order.id, Order.broker_order_id).where(
                    Order.status.in_(
                        (
                            OrderStatus.SUBMITTED.value,
                            OrderStatus.PARTIALLY_FILLED.value,
                        )
                    ),
                    Order.broker_order_id.is_not(None),
                )
            ).all()

        for order_id in missing_ids:
            drift.append(f"local open order {order_id} has no broker order id")

        results: list[tuple[int, OrderResult]] = list(prefetched_results)
        prefetched_order_ids = {
            order_id for order_id, _remote in prefetched_results
        }
        for order_id, broker_order_id in rows:
            if order_id in prefetched_order_ids:
                continue
            try:
                results.append(
                    (order_id, self.broker.get_order_status(broker_order_id))
                )
            except Exception as exc:
                drift.append(
                    f"status unavailable for local order {order_id}: "
                    f"{type(exc).__name__}"
                )

        inserted = 0
        exact_reader = callable(getattr(self.broker, "get_fill_activities", None))
        for order_id, remote in results:
            with self.session_factory() as session:
                order = session.get(Order, order_id)
                if order is None:
                    continue
                recorded = Decimal(
                    str(
                        session.scalar(
                            select(func.coalesce(func.sum(Fill.qty), 0)).where(
                                Fill.order_id == order.id
                            )
                        )
                    )
                )
                new_qty = remote.filled_qty - recorded
                if exact_reader and new_qty > Decimal("0.000001"):
                    drift.append(
                        f"broker order {order.id} reports {remote.filled_qty} filled "
                        f"but exact activities contain {recorded}"
                    )
                    continue
                if (
                    not exact_reader
                    and new_qty > Decimal("0.000001")
                    and remote.avg_fill_price is not None
                ):
                    prior_fills = session.scalars(
                        select(Fill).where(Fill.order_id == order.id)
                    ).all()
                    recorded_notional = sum(
                        (fill.qty * fill.price for fill in prior_fills), Decimal(0)
                    )
                    cumulative_notional = remote.filled_qty * remote.avg_fill_price
                    incremental_notional = cumulative_notional - recorded_notional
                    if incremental_notional <= 0:
                        drift.append(
                            f"broker cumulative fill moved behind local ledger "
                            f"for order {order.id}"
                        )
                        continue
                    session.add(
                        Fill(
                            order_id=order.id,
                            ticker=order.ticker,
                            side=order.side,
                            qty=new_qty,
                            price=incremental_notional / new_qty,
                            broker_fill_id=(
                                f"{order.broker_order_id}:{remote.filled_qty}"
                            ),
                        )
                    )
                    inserted += 1

                target = remote.status
                current = OrderStatus(order.status)
                if (
                    target is not current
                    and OrderStateMachine.can_transition(current, target)
                ):
                    OrderStateMachine.transition(order, target)
                order.last_reconciled_at = datetime.now(timezone.utc)
                order.version += 1
                session.commit()
        return len(results), inserted

    def _detect_open_order_drift(self, drift: list[str]) -> None:
        try:
            remote_open = self.broker.get_open_orders()
        except Exception as exc:
            drift.append(f"open order enumeration unavailable: {type(exc).__name__}")
            return
        if any(remote.broker_order_id is None for remote in remote_open):
            drift.append("remote open order is missing broker order id")
        remote_ids = {
            remote.broker_order_id
            for remote in remote_open
            if remote.broker_order_id is not None
        }
        if not remote_ids:
            return
        with self.session_factory() as session:
            known_ids = set(
                session.scalars(
                    select(Order.broker_order_id).where(
                        Order.broker_order_id.in_(remote_ids)
                    )
                ).all()
            )
        for broker_order_id in sorted(remote_ids - known_ids):
            drift.append(f"remote open order {broker_order_id} has no local order")

    def panic(self, actor: str, reason: str) -> PanicReport:
        actor = actor.strip()
        reason = reason.strip()
        if not actor or not reason:
            raise ValueError("panic actor and reason must be non-empty")

        # The durable global latch is the first side effect. No broker call occurs
        # until its transaction is closed.
        self.breakers.trip(
            BreakerScope.operator_global(),
            reason=f"panic by {actor}: {reason}",
            actor=actor,
        )

        with self.session_factory() as session:
            session.execute(
                update(Rule)
                .where(Rule.state.in_(("active", "processing")))
                .values(state="canceled")
            )
            session.execute(
                update(RuleGroup)
                .where(RuleGroup.state == "active")
                .values(
                    state="canceled",
                    lease_owner=None,
                    lease_expires_at=None,
                    version=RuleGroup.version + 1,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

        self.reconcile_unknown()

        with self.session_factory() as session:
            local_rows = session.execute(
                select(Order.id, Order.broker_order_id).where(
                    Order.status.in_(_LOCAL_LIVE_STATUSES)
                )
            ).all()
        local_by_broker_id: dict[str, list[int]] = {}
        for local_id, broker_order_id in local_rows:
            if broker_order_id:
                local_by_broker_id.setdefault(broker_order_id, []).append(local_id)

        enumeration_failed = False
        try:
            remote_open = self.broker.get_open_orders()
        except Exception:
            remote_open = []
            enumeration_failed = True
        unaddressable_remote_open = any(
            remote.broker_order_id is None for remote in remote_open
        )
        remote_by_id = {
            remote.broker_order_id: remote
            for remote in remote_open
            if remote.broker_order_id is not None
        }
        explicit_ids = sorted(set(local_by_broker_id) | set(remote_by_id))

        confirmed_canceled: list[str] = []
        verified_terminal: set[str] = set()
        potentially_open: set[str] = set(explicit_ids)
        for broker_order_id in explicit_ids:
            try:
                self.broker.cancel_order(broker_order_id)
            except Exception:
                pass
            try:
                verified = self.broker.get_order_status(broker_order_id)
            except Exception:
                continue
            if verified.status not in _REMOTE_OPEN_STATUSES:
                verified_terminal.add(broker_order_id)
                potentially_open.discard(broker_order_id)
                if verified.status is OrderStatus.CANCELED:
                    confirmed_canceled.append(broker_order_id)
                self._persist_verified_status(
                    local_by_broker_id.get(broker_order_id, ()), verified
                )

        try:
            final_remote_open = self.broker.get_open_orders()
        except Exception:
            enumeration_failed = True
            final_remote_open = [
                remote_by_id[broker_order_id]
                for broker_order_id in explicit_ids
                if broker_order_id in remote_by_id
                and broker_order_id not in verified_terminal
            ]
        unaddressable_remote_open = unaddressable_remote_open or any(
            remote.broker_order_id is None for remote in final_remote_open
        )
        remote_open_ids = tuple(
            sorted(
                {
                    remote.broker_order_id
                    for remote in final_remote_open
                    if remote.broker_order_id is not None
                    and remote.status in _REMOTE_OPEN_STATUSES
                }
                | potentially_open
            )
        )

        with self.session_factory() as session:
            local_unconfirmed = tuple(
                session.scalars(
                    select(Order.id)
                    .where(Order.status.in_(_LOCAL_LIVE_STATUSES))
                    .order_by(Order.id)
                ).all()
            )

        safe = (
            not enumeration_failed
            and not unaddressable_remote_open
            and not local_unconfirmed
            and not remote_open_ids
        )
        if safe:
            message = "panic verified: no local unknown/open or broker open orders remain"
        else:
            message = (
                "panic incomplete: safety could not be confirmed; "
                f"broker_enumeration={'unconfirmed' if enumeration_failed else 'confirmed'} "
                f"unaddressable_remote_open={str(unaddressable_remote_open).lower()} "
                f"local_unconfirmed={list(local_unconfirmed)} "
                f"remote_open={list(remote_open_ids)}"
            )
        return PanicReport(
            safe=safe,
            confirmed_canceled=tuple(sorted(confirmed_canceled)),
            unconfirmed_order_ids=local_unconfirmed,
            remote_open_order_ids=remote_open_ids,
            message=message,
        )

    def _persist_verified_status(
        self, local_order_ids: tuple[int, ...] | list[int], remote: OrderResult
    ) -> None:
        if not local_order_ids:
            return
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.execute(
                update(Order)
                .where(Order.id.in_(local_order_ids))
                .values(
                    status=remote.status.value,
                    last_reconciled_at=now,
                    updated_at=now,
                    version=Order.version + 1,
                )
            )
            session.commit()
