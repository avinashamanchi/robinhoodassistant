"""Truthful, category-aware enumeration of durable local safety state."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from ..broker.models import OrderStatus
from ..db.models import (
    CircuitBreakerState,
    FILL_RECONCILIATION_QUARANTINED,
    FILL_RECONCILIATION_REQUIRED,
    FILL_RECONCILIATION_SUPERSEDED,
    Fill,
    Heartbeat,
    Order,
    Rule,
    RuleGroup,
    utcnow,
)

_LOCAL_SAFETY_CATEGORIES = (
    "live_or_unknown_orders",
    "latched_orders",
    "unsafe_fills",
    "active_rules",
    "unsafe_rule_groups",
)
_ACTIVE_BREAKERS_CATEGORY = "active_breakers"
_HEARTBEAT_CATEGORY = "heartbeat"
_OPERATOR_GLOBAL_SCOPE = "operator_global"
_TARGETED_BREAKER_KINDS = frozenset(
    {"loss", "drawdown", "data", "liquidity"}
)
_UNTARGETED_BREAKER_KINDS = frozenset(
    {"broker_drift", "operator_global"}
)

_LIVE_OR_UNKNOWN_ORDER_STATUSES = (
    OrderStatus.APPROVED.value,
    OrderStatus.APPROVAL_RECORDED.value,
    OrderStatus.SUBMITTING.value,
    OrderStatus.ACCEPTANCE_UNKNOWN.value,
    OrderStatus.SUBMITTED.value,
    OrderStatus.PARTIALLY_FILLED.value,
)

_SAFETY_LATCH_ERROR_CODES = (
    "broker_submission_unknown",
    "cumulative_fill_contradiction",
    "fill_quantity_exceeds_order",
    "indeterminate_cancel",
    "invalid_broker_data",
    "invalid_broker_identity",
    "invalid_cumulative_fill",
    "legacy_unidentified_fill",
    "legacy_unverified_fill",
    "remote_fill_ahead",
    "waiting_for_exact_fill",
)


def _normalize_database_utc(
    value: datetime | str,
    *,
    millisecond_upper_bound: bool = False,
) -> datetime:
    """Normalize a database timestamp to an aware UTC observation bound."""
    if isinstance(value, str):
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError("database UTC timestamp has an invalid type")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    observed_at = parsed.astimezone(timezone.utc)
    if millisecond_upper_bound:
        # SQLite's built-in UTC clock is millisecond-granular. Represent the
        # end of that clock tick so application timestamps committed within
        # the same tick cannot appear later than their containing snapshot.
        observed_at += timedelta(microseconds=999)
    return observed_at


def _establish_snapshot_and_observe_utc(
    session: Session,
) -> datetime:
    """Establish the read snapshot and obtain its UTC bound in one statement."""
    connection = session.connection()
    if connection.dialect.name == "sqlite":
        statement = (
            select(
                func.strftime(
                    "%Y-%m-%dT%H:%M:%fZ",
                    "now",
                ),
                func.count(
                    CircuitBreakerState.scope_key
                ),
            )
            .select_from(CircuitBreakerState)
        )
        observed_at = connection.execute(statement).one()[0]
        return _normalize_database_utc(
            observed_at,
            millisecond_upper_bound=True,
        )

    statement = (
        select(
            func.current_timestamp(),
            func.count(CircuitBreakerState.scope_key),
        )
        .select_from(CircuitBreakerState)
    )
    observed_at = connection.execute(statement).one()[0]
    return _normalize_database_utc(observed_at)


@dataclass(frozen=True)
class UnsafeLocalState:
    live_or_unknown_order_ids: tuple[int, ...] = ()
    latched_order_ids: tuple[int, ...] = ()
    unsafe_fill_ids: tuple[int, ...] = ()
    active_rule_ids: tuple[int, ...] = ()
    unsafe_rule_group_ids: tuple[int, ...] = ()
    unknown_categories: tuple[str, ...] = ()

    @property
    def enumeration(self) -> str:
        return "unknown" if self.unknown_categories else "confirmed"

    @property
    def unsafe_order_ids(self) -> tuple[int, ...]:
        return tuple(
            sorted(
                set(self.live_or_unknown_order_ids)
                | set(self.latched_order_ids)
            )
        )

    @property
    def has_unsafe_state(self) -> bool:
        return bool(
            self.unknown_categories
            or self.live_or_unknown_order_ids
            or self.latched_order_ids
            or self.unsafe_fill_ids
            or self.active_rule_ids
            or self.unsafe_rule_group_ids
        )

    def as_dict(self) -> dict[str, list[int] | list[str]]:
        return {
            "live_or_unknown_order_ids": list(
                self.live_or_unknown_order_ids
            ),
            "latched_order_ids": list(self.latched_order_ids),
            "unsafe_fill_ids": list(self.unsafe_fill_ids),
            "active_rule_ids": list(self.active_rule_ids),
            "unsafe_rule_group_ids": list(
                self.unsafe_rule_group_ids
            ),
            "unknown_categories": list(self.unknown_categories),
        }


@dataclass(frozen=True)
class BreakerTruth:
    scope: str
    kind: str
    target: str
    tripped: bool
    generation: int

    def as_active_dict(self) -> dict[str, str | int]:
        return {
            "scope": self.scope,
            "kind": self.kind,
            "target": self.target,
            "generation": self.generation,
        }


@dataclass(frozen=True)
class PersistedSafetyTruth:
    observed_at: datetime
    state: str
    complete: bool
    heartbeat_at: datetime | None
    operator_global_tripped: bool | None
    operator_global_generation: int | None
    breakers: tuple[BreakerTruth, ...]
    unsafe_local_state: UnsafeLocalState
    unknown_categories: tuple[str, ...]

    @property
    def active_breakers(self) -> tuple[BreakerTruth, ...]:
        return tuple(
            breaker
            for breaker in self.breakers
            if breaker.tripped
        )

    def breaker(self, scope: str) -> BreakerTruth | None:
        return next(
            (
                breaker
                for breaker in self.breakers
                if breaker.scope == scope
            ),
            None,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "observed_at": self.observed_at.isoformat(),
            "state": self.state,
            "complete": self.complete,
            "local_enumeration": (
                self.unsafe_local_state.enumeration
            ),
            "remote_broker_open_orders": "unverified",
            "operator_global_breaker": {
                "tripped": self.operator_global_tripped,
                "generation": self.operator_global_generation,
            },
            "active_breakers": [
                breaker.as_active_dict()
                for breaker in self.active_breakers
            ],
            "unsafe_local_state": (
                self.unsafe_local_state.as_dict()
            ),
            "unknown_categories": list(
                self.unknown_categories
            ),
        }


@contextmanager
def _coherent_read_snapshot(
    session: Session,
) -> Iterator[datetime]:
    """Open or reuse one real database read transaction.

    SQLAlchemy's Session transaction is virtual until a connection is used.
    Python's legacy sqlite3 mode then omits the database-level BEGIN for
    SELECTs. Check the driver transaction directly so an already active
    SQLite transaction is reused and a SELECT-only session receives exactly
    one explicit BEGIN. Other backends retain SQLAlchemy's native transaction
    semantics.
    """

    owns_transaction = not session.in_transaction()
    transaction = session.begin() if owns_transaction else None
    try:
        connection = session.connection()
        if connection.dialect.name == "sqlite":
            driver_connection = (
                connection.connection.driver_connection
            )
            if not driver_connection.in_transaction:
                connection.exec_driver_sql("BEGIN")
        observed_at = _establish_snapshot_and_observe_utc(
            session
        )
        yield observed_at
        if transaction is not None:
            transaction.commit()
    except BaseException:
        if transaction is not None and transaction.is_active:
            transaction.rollback()
        raise


def _breaker_row_is_canonical(
    row: CircuitBreakerState,
) -> bool:
    if (
        type(row.tripped) is not bool
        or type(row.generation) is not int
        or row.generation < 0
        or (row.tripped and row.generation < 1)
        or not isinstance(row.scope_key, str)
        or not row.scope_key
        or not isinstance(row.kind, str)
        or not isinstance(row.target, str)
    ):
        return False
    if row.kind in _TARGETED_BREAKER_KINDS:
        return bool(
            row.target
            and row.scope_key == f"{row.kind}:{row.target}"
        )
    if row.kind in _UNTARGETED_BREAKER_KINDS:
        return (
            row.target == ""
            and row.scope_key == row.kind
        )
    return False


def _enumerate_unsafe_local_state_in_session(
    session: Session,
) -> UnsafeLocalState:
    unknown: list[str] = []
    results: dict[str, tuple[int, ...]] = {
        category: () for category in _LOCAL_SAFETY_CATEGORIES
    }

    def query_ids(category: str, statement) -> None:
        try:
            results[category] = tuple(
                session.scalars(statement).all()
            )
        except Exception:
            unknown.append(category)

    query_ids(
        "live_or_unknown_orders",
        select(Order.id)
        .where(
            Order.status.in_(
                _LIVE_OR_UNKNOWN_ORDER_STATUSES
            )
        )
        .order_by(Order.id),
    )
    query_ids(
        "latched_orders",
        select(Order.id)
        .where(
            or_(
                Order.acceptance_state
                == FILL_RECONCILIATION_REQUIRED,
                Order.last_error_code.in_(
                    _SAFETY_LATCH_ERROR_CODES
                ),
            )
        )
        .order_by(Order.id),
    )
    query_ids(
        "unsafe_fills",
        select(Fill.id)
        .where(
            or_(
                Fill.order_id.is_(None),
                Fill.reconciliation_state
                == FILL_RECONCILIATION_QUARANTINED,
                (
                    Fill.reconciliation_state
                    != FILL_RECONCILIATION_SUPERSEDED
                )
                & or_(
                    Fill.broker_fill_id.is_(None),
                    func.trim(Fill.broker_fill_id) == "",
                ),
            )
        )
        .order_by(Fill.id),
    )
    query_ids(
        "active_rules",
        select(Rule.id)
        .where(
            Rule.state.in_(("active", "processing"))
        )
        .order_by(Rule.id),
    )
    query_ids(
        "unsafe_rule_groups",
        select(RuleGroup.id)
        .where(
            or_(
                RuleGroup.state == "active",
                RuleGroup.reconciliation_required.is_(True),
            )
        )
        .order_by(RuleGroup.id),
    )
    return UnsafeLocalState(
        live_or_unknown_order_ids=results[
            "live_or_unknown_orders"
        ],
        latched_order_ids=results["latched_orders"],
        unsafe_fill_ids=results["unsafe_fills"],
        active_rule_ids=results["active_rules"],
        unsafe_rule_group_ids=results[
            "unsafe_rule_groups"
        ],
        unknown_categories=tuple(unknown),
    )


def enumerate_unsafe_local_state(
    session_factory: sessionmaker[Session],
) -> UnsafeLocalState:
    """Enumerate each required category independently.

    A failed query never turns into an empty, confirmed category. Other
    successful categories remain available so a panic receipt preserves the
    maximum confirmed local truth without claiming completeness.
    """

    try:
        with session_factory() as session:
            with _coherent_read_snapshot(session):
                return _enumerate_unsafe_local_state_in_session(
                    session
                )
    except Exception:
        return UnsafeLocalState(
            unknown_categories=_LOCAL_SAFETY_CATEGORIES
        )


def _read_persisted_safety_truth_in_session(
    session: Session,
) -> PersistedSafetyTruth:
    with _coherent_read_snapshot(session) as observed_at:
        local_state = _enumerate_unsafe_local_state_in_session(
            session
        )
        breaker_rows: tuple[CircuitBreakerState, ...] = ()
        breaker_unknown = False
        try:
            breaker_rows = tuple(
                session.scalars(
                    select(CircuitBreakerState).order_by(
                        CircuitBreakerState.scope_key
                    )
                ).all()
            )
        except Exception:
            breaker_unknown = True

        heartbeat_at: datetime | None = None
        heartbeat_unknown = False
        try:
            heartbeat_at = session.scalar(
                select(Heartbeat.at)
                .order_by(Heartbeat.id.desc())
                .limit(1)
            )
            if (
                heartbeat_at is not None
                and heartbeat_at > observed_at
            ):
                heartbeat_at = None
                heartbeat_unknown = True
        except Exception:
            heartbeat_unknown = True

    breakers = tuple(
        BreakerTruth(
            scope=row.scope_key,
            kind=row.kind,
            target=row.target,
            tripped=row.tripped,
            generation=row.generation,
        )
        for row in breaker_rows
    )
    operator_row = next(
        (
            row
            for row in breaker_rows
            if row.scope_key == _OPERATOR_GLOBAL_SCOPE
        ),
        None,
    )
    unknown_categories = list(
        local_state.unknown_categories
    )
    if breaker_unknown:
        unknown_categories.append(
            _ACTIVE_BREAKERS_CATEGORY
        )
    elif any(
        not _breaker_row_is_canonical(row)
        for row in breaker_rows
    ):
        unknown_categories.append(
            _ACTIVE_BREAKERS_CATEGORY
        )
    if heartbeat_unknown:
        unknown_categories.append(_HEARTBEAT_CATEGORY)

    known_unsafe = bool(
        any(breaker.tripped for breaker in breakers)
        or local_state.live_or_unknown_order_ids
        or local_state.latched_order_ids
        or local_state.unsafe_fill_ids
        or local_state.active_rule_ids
        or local_state.unsafe_rule_group_ids
    )
    if known_unsafe:
        state = "unsafe"
    elif unknown_categories:
        state = "unknown"
    else:
        state = "locally_clear"

    return PersistedSafetyTruth(
        observed_at=observed_at,
        state=state,
        complete=not unknown_categories,
        heartbeat_at=heartbeat_at,
        operator_global_tripped=(
            None
            if breaker_unknown
            else bool(operator_row and operator_row.tripped)
        ),
        operator_global_generation=(
            None
            if breaker_unknown
            else (
                operator_row.generation
                if operator_row is not None
                else 0
            )
        ),
        breakers=breakers,
        unsafe_local_state=local_state,
        unknown_categories=tuple(unknown_categories),
    )


def read_persisted_safety_truth(
    source: sessionmaker[Session] | Session,
) -> PersistedSafetyTruth:
    """Read durable local safety evidence without inferring broker truth."""
    try:
        if isinstance(source, Session):
            return _read_persisted_safety_truth_in_session(
                source
            )
        with source() as session:
            return _read_persisted_safety_truth_in_session(
                session
            )
    except Exception:
        return unknown_persisted_safety_truth()


def unknown_persisted_safety_truth(
    *,
    observed_at: datetime | None = None,
) -> PersistedSafetyTruth:
    """Return an explicit unknown value after a broader DB read failure."""
    local_state = UnsafeLocalState(
        unknown_categories=_LOCAL_SAFETY_CATEGORIES
    )
    return PersistedSafetyTruth(
        observed_at=observed_at or utcnow(),
        state="unknown",
        complete=False,
        heartbeat_at=None,
        operator_global_tripped=None,
        operator_global_generation=None,
        breakers=(),
        unsafe_local_state=local_state,
        unknown_categories=(
            *_LOCAL_SAFETY_CATEGORIES,
            _ACTIVE_BREAKERS_CATEGORY,
            _HEARTBEAT_CATEGORY,
        ),
    )
