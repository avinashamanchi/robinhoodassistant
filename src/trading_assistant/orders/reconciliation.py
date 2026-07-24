"""Broker-truth reconciliation and fail-closed operator panic."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from trading_assistant.assets import canonicalize_broker_symbol
from trading_assistant.broker.base import BrokerClient, BrokerDataIntegrityError
from trading_assistant.broker.models import (
    BrokerFill,
    FillQuantityRelation,
    OrderResult,
    OrderStatus,
    fill_quantity_relation,
    normalize_fill_economic,
    order_result_identity_error,
    valid_cumulative_filled_qty,
    valid_fill_economic,
)
from trading_assistant.db.models import (
    FILL_RECONCILIATION_REQUIRED,
    FILL_RECONCILIATION_QUARANTINED,
    FILL_RECONCILIATION_SUPERSEDED,
    FILL_RECONCILIATION_TRUSTED,
    Fill,
    Order,
    OrderStateMachine,
    ReconciliationCursor,
    Rule,
    RuleGroup,
    Proposal,
    fill_has_trusted_identity,
    fill_requires_reconciliation,
)
from trading_assistant.risk.breakers import BreakerScope, BreakerService
from trading_assistant.risk.staleness import (
    DEFAULT_MAX_FUTURE_SKEW_SECONDS,
)
from trading_assistant.risk.submission_barrier import SubmissionBarrier

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
_FILL_STATUSES = {
    OrderStatus.PARTIALLY_FILLED,
    OrderStatus.FILLED,
}


def _normalized_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
        self.submission_barrier = SubmissionBarrier(session_factory)

    def reconcile_unknown(self) -> tuple[int, tuple[int, ...]]:
        with self.submission_barrier.hold_writer():
            drift: list[str] = []
            resolved, unresolved, _ = self._resolve_unknown(drift)
            self._clear_reconciled_rule_groups()
            self._trip_reconciliation_faults(drift, unresolved)
            return resolved, unresolved

    def _resolve_unknown(
        self,
        drift: list[str],
    ) -> tuple[int, tuple[int, ...], tuple[tuple[int, OrderResult], ...]]:
        with self.session_factory() as session:
            rows = session.execute(
                select(
                    Order.id,
                    Order.idempotency_key,
                    Order.ticker,
                ).where(
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
        for order_id, client_order_id, ticker in rows:
            try:
                remote = self.broker.get_order_by_client_id(client_order_id)
            except BrokerDataIntegrityError as exc:
                self._latch_order_ids(
                    (order_id,),
                    "invalid_cumulative_fill",
                )
                drift.append(
                    f"local order {order_id} invalid cumulative filled_qty: {exc}"
                )
                unresolved.append(order_id)
                continue
            except Exception:
                unresolved.append(order_id)
                continue
            if remote is None:
                unresolved.append(order_id)
                continue
            identity_error = order_result_identity_error(
                remote,
                client_order_id,
                ticker,
            )
            if identity_error is not None:
                self._latch_order_ids(
                    (order_id,),
                    "invalid_broker_identity",
                )
                drift.append(
                    f"local order {order_id} has invalid broker identity: "
                    f"{identity_error}"
                )
                unresolved.append(order_id)
                continue
            if not valid_cumulative_filled_qty(remote.filled_qty):
                drift.append(
                    f"local order {order_id} invalid cumulative filled_qty "
                    f"{remote.filled_qty}"
                )
            if self.repository.resolve_acceptance(
                order_id,
                remote.broker_order_id,
                remote.status,
                remote.filled_qty,
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
        with self.submission_barrier.hold_writer():
            drift: list[str] = []
            self._quarantine_legacy_fills()
            resolved, unresolved, resolved_results = self._resolve_unknown(drift)
            (
                inserted_fills,
                invalid_fill_order_ids,
                exact_fill_stream_complete,
            ) = (
                self._reconcile_fill_activities(drift)
            )
            synced, synthetic_fills = self._reconcile_statuses(
                drift,
                prefetched_results=resolved_results,
                blocked_fill_order_ids=invalid_fill_order_ids,
                exact_fill_stream_complete=exact_fill_stream_complete,
            )
            inserted_fills += synthetic_fills
            self._detect_quarantined_fills(drift)
            self._clear_reconciled_rule_groups()
            self._detect_open_order_drift(drift)
            self._trip_reconciliation_faults(drift, unresolved)
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
                        or_(
                            Order.status.in_(
                                (
                                    OrderStatus.SUBMITTING.value,
                                    OrderStatus.ACCEPTANCE_UNKNOWN.value,
                                )
                            ),
                            Order.acceptance_state
                            == FILL_RECONCILIATION_REQUIRED,
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

    @staticmethod
    def _latch_order_in_session(order: Order, error_code: str) -> bool:
        changed = False
        if order.acceptance_state != FILL_RECONCILIATION_REQUIRED:
            order.acceptance_state = FILL_RECONCILIATION_REQUIRED
            changed = True
        if order.last_error_code != error_code:
            order.last_error_code = error_code
            changed = True
        if changed:
            order.updated_at = datetime.now(timezone.utc)
            order.version += 1
        return changed

    def _latch_order_ids(
        self,
        order_ids: tuple[int, ...] | list[int],
        error_code: str,
    ) -> tuple[int, ...]:
        unique_ids = tuple(sorted(set(order_ids)))
        if not unique_ids:
            return ()
        with self.submission_barrier.hold_writer():
            with self.session_factory() as session:
                orders = session.scalars(
                    select(Order).where(Order.id.in_(unique_ids))
                ).all()
                latched = tuple(order.id for order in orders)
                for order in orders:
                    self._latch_order_in_session(order, error_code)
                session.commit()
                return latched

    def _latch_broker_order_id(
        self,
        broker_order_id: str | None,
        error_code: str,
    ) -> tuple[int, ...]:
        if not broker_order_id:
            return ()
        with self.session_factory() as session:
            order_ids = tuple(
                session.scalars(
                    select(Order.id).where(
                        Order.broker_order_id == broker_order_id
                    )
                ).all()
            )
        return self._latch_order_ids(list(order_ids), error_code)

    def _quarantine_legacy_fills(self) -> None:
        """Make every unidentified direct-path row explicitly untrusted."""
        with self.session_factory() as session:
            fills = session.scalars(select(Fill)).all()
            changed = False
            for fill in fills:
                if not fill_requires_reconciliation(fill):
                    continue
                if (
                    fill.reconciliation_state
                    != FILL_RECONCILIATION_QUARANTINED
                ):
                    fill.reconciliation_state = (
                        FILL_RECONCILIATION_QUARANTINED
                    )
                    changed = True
                if fill.order_id is not None:
                    order = session.get(Order, fill.order_id)
                    if order is not None:
                        changed = (
                            self._latch_order_in_session(
                                order,
                                "legacy_unidentified_fill",
                            )
                            or changed
                        )
            if changed:
                session.commit()

    def _detect_quarantined_fills(self, drift: list[str]) -> None:
        with self.session_factory() as session:
            rows = session.execute(
                select(Fill.id, Fill.order_id).where(
                    Fill.reconciliation_state
                    == FILL_RECONCILIATION_QUARANTINED
                )
            ).all()
        for fill_id, order_id in rows:
            drift.append(
                f"quarantined legacy fill {fill_id} for order "
                f"{order_id} lacks matched authoritative activity"
            )

    def _trip_reconciliation_faults(
        self,
        drift: list[str],
        unresolved_order_ids: tuple[int, ...] = (),
    ) -> None:
        faults = list(drift)
        if unresolved_order_ids:
            faults.append(
                "unresolved acceptance orders "
                f"{list(sorted(unresolved_order_ids))}"
            )
        if not faults:
            return
        detail = " | ".join(faults[:3])
        if len(faults) > 3:
            detail += f" | {len(faults) - 3} additional fault(s)"
        self.breakers.trip(
            BreakerScope.broker_drift(),
            f"broker reconciliation fault: {detail}",
            "daemon:reconciliation",
        )

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

    def _reconcile_fill_activities(
        self,
        drift: list[str],
    ) -> tuple[int, frozenset[int], bool]:
        activity_reader = getattr(self.broker, "get_fill_activities", None)
        if not callable(activity_reader):
            return 0, frozenset(), False

        _last_activity_id, after, expected_version = self._cursor_snapshot()
        try:
            activities = list(activity_reader(after=after))
        except BrokerDataIntegrityError as exc:
            blocked_order_ids = self._latch_broker_order_id(
                exc.broker_order_id,
                "invalid_fill_activity",
            )
            drift.append(f"invalid fill activity payload: {exc}")
            return 0, frozenset(blocked_order_ids), False
        except Exception as exc:
            drift.append(f"fill activities unavailable: {type(exc).__name__}")
            return 0, frozenset(), False

        observed_at = datetime.now(timezone.utc)
        # Activity IDs are opaque, not chronological. The broker query overlaps
        # the timestamp boundary and the fill table's unique broker ID is the
        # authority for deduplication, including late-visible equal-time fills.
        batch = sorted(
            activities,
            key=lambda activity: _normalized_utc(activity.filled_at),
        )
        if not batch:
            return 0, frozenset(), True

        inserted = 0
        inserted_activities: list[BrokerFill] = []
        blocked_order_ids: set[int] = set()
        advance_cursor = True
        with self.session_factory() as session:
            try:
                for activity in batch:
                    orders = session.scalars(
                        select(Order).where(
                            Order.broker_order_id == activity.broker_order_id
                        )
                    ).all()
                    if len(orders) != 1:
                        drift.append(
                            "fill activity "
                            f"{activity.broker_fill_id} has unknown or ambiguous "
                            "broker order "
                            f"{activity.broker_order_id}"
                        )
                        advance_cursor = False
                        continue
                    order = orders[0]
                    activity = replace(
                        activity,
                        ticker=canonicalize_broker_symbol(
                            activity.ticker,
                            reference_symbol=order.ticker,
                        ),
                        filled_at=_normalized_utc(activity.filled_at),
                    )
                    validation_error = self._fill_activity_validation_error(
                        activity,
                        order,
                        observed_at,
                    )
                    if validation_error is None:
                        normalized_qty = normalize_fill_economic(activity.qty)
                        normalized_price = normalize_fill_economic(
                            activity.price
                        )
                        assert normalized_qty is not None
                        assert normalized_price is not None
                        activity = replace(
                            activity,
                            qty=normalized_qty,
                            price=normalized_price,
                        )
                    duplicate = None
                    if validation_error is None:
                        duplicate = session.scalar(
                            select(Fill).where(
                                Fill.broker_fill_id == activity.broker_fill_id
                            )
                        )
                    if validation_error is None and duplicate is not None:
                        validation_error = (
                            self._duplicate_fill_validation_error(
                                activity,
                                order,
                                duplicate,
                            )
                        )
                    if validation_error is not None:
                        drift.append(
                            f"invalid fill activity "
                            f"{activity.broker_fill_id}: {validation_error}"
                        )
                        self._latch_order_in_session(
                            order,
                            "invalid_fill_activity",
                        )
                        blocked_order_ids.add(order.id)
                        advance_cursor = False
                        continue
                    if duplicate is not None:
                        if not fill_has_trusted_identity(duplicate):
                            self._replace_with_authoritative_activity(
                                duplicate,
                                order,
                                activity,
                            )
                            inserted_activities.append(activity)
                        continue
                    self._remove_synthetic_fills(session, order)
                    self._supersede_matching_quarantined_fill(
                        session,
                        order,
                        activity,
                    )
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
                return 0, frozenset(blocked_order_ids), False
        return (
            inserted,
            frozenset(blocked_order_ids),
            advance_cursor,
        )

    @staticmethod
    def _replace_with_authoritative_activity(
        fill: Fill,
        order: Order,
        activity: BrokerFill,
    ) -> None:
        """Promote one validated exact activity over its untrusted legacy row."""
        fill.order_id = order.id
        fill.ticker = activity.ticker
        fill.side = activity.side
        fill.qty = activity.qty
        fill.price = activity.price
        fill.broker_fill_id = activity.broker_fill_id
        fill.filled_at = activity.filled_at
        fill.reconciliation_state = FILL_RECONCILIATION_TRUSTED

    @staticmethod
    def _supersede_matching_quarantined_fill(
        session: Session,
        order: Order,
        activity: BrokerFill,
    ) -> None:
        legacy = session.scalar(
            select(Fill)
            .where(
                or_(
                    Fill.order_id == order.id,
                    Fill.order_id.is_(None),
                ),
                Fill.reconciliation_state
                == FILL_RECONCILIATION_QUARANTINED,
                Fill.ticker == activity.ticker,
                Fill.side == activity.side,
                Fill.qty == activity.qty,
                Fill.price == activity.price,
            )
            .order_by(Fill.order_id.is_(None), Fill.id)
            .limit(1)
        )
        if legacy is not None:
            legacy.order_id = order.id
            legacy.reconciliation_state = FILL_RECONCILIATION_SUPERSEDED

    @staticmethod
    def _fill_activity_validation_error(
        activity: BrokerFill,
        order: Order,
        observed_at: datetime,
    ) -> str | None:
        if not isinstance(activity.broker_fill_id, str) or not (
            activity.broker_fill_id.strip()
        ):
            return "missing broker fill identity"
        if (
            not isinstance(activity.broker_order_id, str)
            or activity.broker_order_id != order.broker_order_id
        ):
            return "broker order identity does not match local order"
        if (
            not isinstance(activity.side, str)
            or activity.side not in {"buy", "sell"}
        ):
            return f"unknown side {activity.side!r}"
        if activity.side != order.side:
            return (
                f"side {activity.side!r} does not match local side "
                f"{order.side!r}"
            )
        if (
            not isinstance(activity.ticker, str)
            or activity.ticker.upper() != order.ticker.upper()
        ):
            return (
                f"ticker {activity.ticker!r} does not match local ticker "
                f"{order.ticker!r}"
            )
        if not valid_fill_economic(activity.qty):
            return (
                f"quantity {activity.qty!r} is outside canonical fill "
                "precision or bounds"
            )
        if not valid_fill_economic(activity.price):
            return (
                f"price {activity.price!r} is outside canonical fill "
                "precision or bounds"
            )
        submission_boundary = (
            order.submission_started_at or order.created_at
        )
        allowed_skew = timedelta(
            seconds=DEFAULT_MAX_FUTURE_SKEW_SECONDS
        )
        if activity.filled_at < (
            _normalized_utc(submission_boundary) - allowed_skew
        ):
            return "fill timestamp predates order submission"
        if activity.filled_at > (
            _normalized_utc(observed_at) + allowed_skew
        ):
            return "fill timestamp is beyond allowed future skew"
        return None

    @staticmethod
    def _duplicate_fill_validation_error(
        activity: BrokerFill,
        order: Order,
        duplicate: Fill,
    ) -> str | None:
        if (
            duplicate.order_id != order.id
            and (
                duplicate.order_id is not None
                or fill_has_trusted_identity(duplicate)
            )
        ):
            return "broker fill identity belongs to a different local order"
        if duplicate.ticker.upper() != activity.ticker.upper():
            return "broker fill identity replay changed ticker"
        if duplicate.side != activity.side:
            return "broker fill identity replay changed side"
        if duplicate.qty != activity.qty:
            return "broker fill identity replay changed quantity"
        if duplicate.price != activity.price:
            return "broker fill identity replay changed price"
        if (
            fill_has_trusted_identity(duplicate)
            and _normalized_utc(duplicate.filled_at)
            != _normalized_utc(activity.filled_at)
        ):
            return "broker fill identity replay changed timestamp"
        return None

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
        blocked_fill_order_ids: frozenset[int] = frozenset(),
        exact_fill_stream_complete: bool = False,
    ) -> tuple[int, int]:
        needs_status_reconciliation = or_(
            Order.status.in_(
                (
                    OrderStatus.SUBMITTED.value,
                    OrderStatus.PARTIALLY_FILLED.value,
                )
            ),
            Order.acceptance_state == FILL_RECONCILIATION_REQUIRED,
        )
        with self.session_factory() as session:
            missing_ids = session.scalars(
                select(Order.id).where(
                    needs_status_reconciliation,
                    Order.broker_order_id.is_(None),
                )
            ).all()
            rows = session.execute(
                select(Order.id, Order.broker_order_id).where(
                    needs_status_reconciliation,
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
            except BrokerDataIntegrityError as exc:
                self._latch_order_ids(
                    (order_id,),
                    "invalid_cumulative_fill",
                )
                drift.append(
                    f"local order {order_id} invalid cumulative filled_qty: {exc}"
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
                identity_error = order_result_identity_error(
                    remote,
                    order.idempotency_key,
                    order.ticker,
                )
                if (
                    identity_error is None
                    and remote.broker_order_id != order.broker_order_id
                ):
                    identity_error = (
                        "broker order identity does not match local order"
                    )
                if identity_error is not None:
                    drift.append(
                        f"local order {order.id} has invalid broker identity: "
                        f"{identity_error}"
                    )
                    self._latch_order_in_session(
                        order,
                        "invalid_broker_identity",
                    )
                    session.commit()
                    continue
                if not valid_cumulative_filled_qty(remote.filled_qty):
                    drift.append(
                        f"local order {order.id} invalid cumulative filled_qty "
                        f"{remote.filled_qty}"
                    )
                    self._latch_order_in_session(
                        order,
                        "invalid_cumulative_fill",
                    )
                    session.commit()
                    continue
                target = remote.status
                current = OrderStatus(order.status)
                latch_changed = False
                all_prior_fills = session.scalars(
                    select(Fill).where(Fill.order_id == order.id)
                ).all()
                prior_fills = [
                    fill
                    for fill in all_prior_fills
                    if fill_has_trusted_identity(fill)
                ]
                unmatched_legacy_fills = [
                    fill
                    for fill in all_prior_fills
                    if fill_requires_reconciliation(fill)
                ]
                recorded = sum(
                    (fill.qty for fill in prior_fills), Decimal(0)
                )
                synthetic_prefix = f"{order.broker_order_id}:"
                authoritative_fills = [
                    fill
                    for fill in prior_fills
                    if (
                        fill.broker_fill_id is not None
                        and not fill.broker_fill_id.startswith(
                            synthetic_prefix
                        )
                    )
                ]
                authoritative_qty = sum(
                    (fill.qty for fill in authoritative_fills),
                    Decimal(0),
                )
                authoritative_relation = fill_quantity_relation(
                    remote.filled_qty,
                    authoritative_qty,
                )
                if authoritative_relation is None:
                    drift.append(
                        f"broker order {order.id} has noncanonical cumulative "
                        "or authoritative fill quantity"
                    )
                    self._latch_order_in_session(
                        order,
                        "invalid_cumulative_fill",
                    )
                    session.commit()
                    continue
                remote_exceeds_authoritative = (
                    authoritative_relation is FillQuantityRelation.AHEAD
                )
                remote_below_authoritative = (
                    authoritative_relation is FillQuantityRelation.BEHIND
                )
                if remote_below_authoritative:
                    drift.append(
                        f"broker order {order.id} cumulative "
                        f"{remote.filled_qty} is below authoritative local "
                        f"quantity {authoritative_qty}"
                    )
                    self._latch_order_in_session(
                        order,
                        "cumulative_fill_contradiction",
                    )
                    session.commit()
                    continue
                if target in _FILL_STATUSES or remote_exceeds_authoritative:
                    if (
                        target is not current
                        and OrderStateMachine.can_transition(current, target)
                    ):
                        OrderStateMachine.transition(order, target)
                        latch_changed = True
                    if (
                        order.acceptance_state
                        != FILL_RECONCILIATION_REQUIRED
                    ):
                        order.acceptance_state = (
                            FILL_RECONCILIATION_REQUIRED
                        )
                        order.updated_at = datetime.now(timezone.utc)
                        latch_changed = True
                fill_reconciliation_required = (
                    order.acceptance_state
                    == FILL_RECONCILIATION_REQUIRED
                )
                if fill_reconciliation_required:
                    if unmatched_legacy_fills:
                        self._latch_order_in_session(
                            order,
                            "legacy_unidentified_fill",
                        )
                        drift.append(
                            f"broker order {order.id} still has "
                            f"{len(unmatched_legacy_fills)} quarantined "
                            "legacy fill(s)"
                        )
                        session.commit()
                        continue
                    terminal_zero_fill_resolved = (
                        order.last_error_code == "indeterminate_cancel"
                        and target
                        in {
                            OrderStatus.CANCELED,
                            OrderStatus.REJECTED,
                            OrderStatus.EXPIRED,
                        }
                        and remote.filled_qty == Decimal(0)
                        and authoritative_qty == Decimal(0)
                        and not prior_fills
                        and exact_fill_stream_complete
                    )
                    if order.id in blocked_fill_order_ids:
                        if latch_changed:
                            order.version += 1
                        session.commit()
                        continue
                    if not exact_reader:
                        drift.append(
                            f"broker order {order.id} requires authoritative "
                            "fill activities"
                        )
                        if latch_changed:
                            order.version += 1
                            session.commit()
                        continue
                    if not exact_fill_stream_complete:
                        drift.append(
                            f"broker order {order.id} authoritative fill "
                            "activity stream is incomplete"
                        )
                        if latch_changed:
                            order.version += 1
                            session.commit()
                        continue
                    if (
                        not remote.filled_qty.is_finite()
                        or (
                            remote.filled_qty == Decimal(0)
                            and not terminal_zero_fill_resolved
                        )
                        or len(authoritative_fills) != len(prior_fills)
                        or authoritative_relation
                        is not FillQuantityRelation.EXACT
                    ):
                        drift.append(
                            f"broker order {order.id} fill reconciliation "
                            f"requires {remote.filled_qty} authoritative quantity "
                            f"but exact activities contain {authoritative_qty}"
                        )
                        if latch_changed:
                            order.version += 1
                            session.commit()
                        continue
                new_qty = remote.filled_qty - recorded
                recorded_relation = fill_quantity_relation(
                    remote.filled_qty,
                    recorded,
                )
                if recorded_relation is None:
                    drift.append(
                        f"broker order {order.id} has noncanonical cumulative "
                        "or recorded fill quantity"
                    )
                    self._latch_order_in_session(
                        order,
                        "invalid_cumulative_fill",
                    )
                    session.commit()
                    continue
                if recorded_relation is FillQuantityRelation.BEHIND:
                    drift.append(
                        f"broker order {order.id} cumulative "
                        f"{remote.filled_qty} is below recorded local "
                        f"quantity {recorded}"
                    )
                    self._latch_order_in_session(
                        order,
                        "cumulative_fill_contradiction",
                    )
                    session.commit()
                    continue
                if (
                    exact_reader
                    and recorded_relation is FillQuantityRelation.AHEAD
                ):
                    drift.append(
                        f"broker order {order.id} reports {remote.filled_qty} filled "
                        f"but exact activities contain {recorded}"
                    )
                    if latch_changed:
                        order.version += 1
                        session.commit()
                    continue
                if (
                    not exact_reader
                    and recorded_relation is FillQuantityRelation.AHEAD
                ):
                    if not valid_fill_economic(remote.avg_fill_price):
                        drift.append(
                            f"broker order {order.id} has invalid cumulative "
                            f"average fill price {remote.avg_fill_price}"
                        )
                        self._latch_order_in_session(
                            order,
                            "invalid_cumulative_fill",
                        )
                        session.commit()
                        continue
                    assert remote.avg_fill_price is not None
                    recorded_notional = sum(
                        (fill.qty * fill.price for fill in prior_fills), Decimal(0)
                    )
                    cumulative_notional = remote.filled_qty * remote.avg_fill_price
                    incremental_notional = cumulative_notional - recorded_notional
                    normalized_new_qty = normalize_fill_economic(new_qty)
                    normalized_incremental_price = normalize_fill_economic(
                        incremental_notional / new_qty
                    )
                    if (
                        incremental_notional <= 0
                        or normalized_new_qty is None
                        or normalized_incremental_price is None
                    ):
                        drift.append(
                            "broker cumulative fill is outside canonical "
                            f"precision or moved behind local ledger for order "
                            f"{order.id}"
                        )
                        self._latch_order_in_session(
                            order,
                            "invalid_cumulative_fill",
                        )
                        if latch_changed:
                            order.version += 1
                        session.commit()
                        continue
                    session.add(
                        Fill(
                            order_id=order.id,
                            ticker=order.ticker,
                            side=order.side,
                            qty=normalized_new_qty,
                            price=normalized_incremental_price,
                            broker_fill_id=(
                                f"{order.broker_order_id}:{remote.filled_qty}"
                            ),
                        )
                    )
                    inserted += 1

                if fill_reconciliation_required:
                    order.acceptance_state = "accepted"
                    order.last_error_code = ""
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
        except BrokerDataIntegrityError as exc:
            self._latch_broker_order_id(
                exc.broker_order_id,
                "invalid_cumulative_fill",
            )
            drift.append(f"open order invalid cumulative filled_qty: {exc}")
            return
        except Exception as exc:
            drift.append(f"open order enumeration unavailable: {type(exc).__name__}")
            return
        for remote in remote_open:
            if not valid_cumulative_filled_qty(remote.filled_qty):
                self._latch_broker_order_id(
                    remote.broker_order_id,
                    "invalid_cumulative_fill",
                )
                drift.append(
                    "open order invalid cumulative filled_qty "
                    f"{remote.filled_qty} for {remote.broker_order_id}"
                )
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
        panic_drift: list[str] = []
        try:
            remote_open = self.broker.get_open_orders()
        except BrokerDataIntegrityError as exc:
            self._latch_broker_order_id(
                exc.broker_order_id,
                "invalid_cumulative_fill",
            )
            panic_drift.append(
                f"panic open-order invalid cumulative filled_qty: {exc}"
            )
            remote_open = []
            enumeration_failed = True
        except Exception:
            remote_open = []
            enumeration_failed = True
        invalid_remote_ids: set[str] = set()
        for remote in remote_open:
            if not valid_cumulative_filled_qty(remote.filled_qty):
                if remote.broker_order_id is not None:
                    invalid_remote_ids.add(remote.broker_order_id)
                    self._latch_broker_order_id(
                        remote.broker_order_id,
                        "invalid_cumulative_fill",
                    )
                panic_drift.append(
                    "panic open-order invalid cumulative filled_qty "
                    f"{remote.filled_qty} for {remote.broker_order_id}"
                )
        unaddressable_remote_open = any(
            remote.broker_order_id is None for remote in remote_open
        ) or bool(invalid_remote_ids)
        remote_by_id = {
            remote.broker_order_id: remote
            for remote in remote_open
            if remote.broker_order_id is not None
            and remote.broker_order_id not in invalid_remote_ids
        }
        explicit_ids = sorted(
            set(local_by_broker_id)
            | {
                remote.broker_order_id
                for remote in remote_open
                if remote.broker_order_id is not None
            }
        )

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
            except BrokerDataIntegrityError as exc:
                self._latch_order_ids(
                    local_by_broker_id.get(broker_order_id, []),
                    "invalid_cumulative_fill",
                )
                panic_drift.append(
                    f"panic order {broker_order_id} invalid cumulative "
                    f"filled_qty: {exc}"
                )
                continue
            except Exception:
                continue
            if not valid_cumulative_filled_qty(verified.filled_qty):
                self._latch_order_ids(
                    local_by_broker_id.get(broker_order_id, []),
                    "invalid_cumulative_fill",
                )
                panic_drift.append(
                    f"panic order {broker_order_id} invalid cumulative "
                    f"filled_qty {verified.filled_qty}"
                )
                continue
            if verified.status not in _REMOTE_OPEN_STATUSES:
                verified_terminal.add(broker_order_id)
                potentially_open.discard(broker_order_id)
                if verified.status is OrderStatus.CANCELED:
                    confirmed_canceled.append(broker_order_id)
                panic_drift.extend(
                    self._persist_verified_status(
                        local_by_broker_id.get(broker_order_id, ()),
                        verified,
                    )
                )

        try:
            final_remote_open = self.broker.get_open_orders()
        except BrokerDataIntegrityError as exc:
            self._latch_broker_order_id(
                exc.broker_order_id,
                "invalid_cumulative_fill",
            )
            panic_drift.append(
                f"panic final open-order invalid cumulative filled_qty: {exc}"
            )
            enumeration_failed = True
            final_remote_open = [
                remote_by_id[broker_order_id]
                for broker_order_id in explicit_ids
                if broker_order_id in remote_by_id
                and broker_order_id not in verified_terminal
            ]
        except Exception:
            enumeration_failed = True
            final_remote_open = [
                remote_by_id[broker_order_id]
                for broker_order_id in explicit_ids
                if broker_order_id in remote_by_id
                and broker_order_id not in verified_terminal
            ]
        for remote in final_remote_open:
            if not valid_cumulative_filled_qty(remote.filled_qty):
                if remote.broker_order_id is not None:
                    potentially_open.add(remote.broker_order_id)
                    self._latch_broker_order_id(
                        remote.broker_order_id,
                        "invalid_cumulative_fill",
                    )
                panic_drift.append(
                    "panic final open-order invalid cumulative filled_qty "
                    f"{remote.filled_qty} for {remote.broker_order_id}"
                )
        unaddressable_remote_open = unaddressable_remote_open or any(
            remote.broker_order_id is None for remote in final_remote_open
        ) or bool(panic_drift)
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
        self._trip_reconciliation_faults(panic_drift)
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
    ) -> tuple[str, ...]:
        if not local_order_ids:
            return ()
        if not valid_cumulative_filled_qty(remote.filled_qty):
            self._latch_order_ids(
                list(local_order_ids),
                "invalid_cumulative_fill",
            )
            return (
                "panic verified status has invalid cumulative filled_qty "
                f"{remote.filled_qty}",
            )
        now = datetime.now(timezone.utc)
        faults: list[str] = []
        with self.submission_barrier.hold_writer():
            with self.session_factory() as session:
                orders = session.scalars(
                    select(Order).where(Order.id.in_(local_order_ids))
                ).all()
                for order in orders:
                    identity_error = order_result_identity_error(
                        remote,
                        order.idempotency_key,
                        order.ticker,
                    )
                    if (
                        identity_error is None
                        and remote.broker_order_id
                        != order.broker_order_id
                    ):
                        identity_error = (
                            "broker order identity does not match local order"
                        )
                    if identity_error is not None:
                        self._latch_order_in_session(
                            order,
                            "invalid_broker_identity",
                        )
                        faults.append(
                            f"panic order {order.id} has invalid broker "
                            f"identity: {identity_error}"
                        )
                        continue
                    synthetic_prefix = f"{order.broker_order_id}:"
                    local_fills = session.scalars(
                        select(Fill).where(Fill.order_id == order.id)
                    ).all()
                    trusted_fills = [
                        fill
                        for fill in local_fills
                        if fill_has_trusted_identity(fill)
                    ]
                    authoritative_fills = [
                        fill
                        for fill in trusted_fills
                        if not fill.broker_fill_id.startswith(
                            synthetic_prefix
                        )
                    ]
                    authoritative_qty = sum(
                        (fill.qty for fill in authoritative_fills),
                        Decimal(0),
                    )
                    fill_truth_complete = (
                        not any(
                            fill_requires_reconciliation(fill)
                            for fill in local_fills
                        )
                        and len(authoritative_fills) == len(trusted_fills)
                    )
                    relation = fill_quantity_relation(
                        remote.filled_qty,
                        authoritative_qty,
                    )
                    if relation is None:
                        self._latch_order_in_session(
                            order,
                            "invalid_cumulative_fill",
                        )
                        faults.append(
                            f"panic order {order.id} has noncanonical "
                            "cumulative or authoritative fill quantity"
                        )
                        continue
                    if relation is FillQuantityRelation.BEHIND:
                        self._latch_order_in_session(
                            order,
                            "cumulative_fill_contradiction",
                        )
                        faults.append(
                            f"panic order {order.id} cumulative "
                            f"{remote.filled_qty} is below authoritative local "
                            f"quantity {authoritative_qty}"
                        )
                        continue
                    order.status = remote.status.value
                    order.last_reconciled_at = now
                    order.updated_at = now
                    order.version += 1
                    if (
                        relation is FillQuantityRelation.EXACT
                        and authoritative_qty > Decimal(0)
                        and fill_truth_complete
                    ):
                        order.acceptance_state = "accepted"
                        order.last_error_code = ""
                    elif (
                        remote.status in _FILL_STATUSES
                        or relation is FillQuantityRelation.AHEAD
                    ):
                        order.acceptance_state = (
                            FILL_RECONCILIATION_REQUIRED
                        )
                session.commit()
        return tuple(faults)
