"""SQLite-authoritative fixed windows and concurrency leases."""

from __future__ import annotations

import hashlib
import math
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import case, delete, or_, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from trading_assistant.db.models import (
    AuditEvent,
    ConcurrencyLease,
    MutationInterlock,
    RateWindow,
    utcnow,
)


UTC = timezone.utc
_RELEASED_AT = datetime(1970, 1, 1, tzinfo=UTC)


class LimitStoreUnavailable(RuntimeError):
    """The authoritative durable policy store could not be used."""


@dataclass(frozen=True)
class LimitSpec:
    name: str
    principal_requests: int
    global_requests: int
    window_seconds: int
    principal_daily_requests: int | None = None
    global_daily_requests: int | None = None

    def __post_init__(self) -> None:
        values = (
            ("principal_requests", self.principal_requests),
            ("global_requests", self.global_requests),
            ("window_seconds", self.window_seconds),
            (
                "principal_daily_requests",
                self.principal_daily_requests,
            ),
            ("global_daily_requests", self.global_daily_requests),
        )
        for name, value in values:
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int
    reset_at: datetime


@dataclass(frozen=True)
class LeaseDecision:
    """Lease result with an owner-and-generation fencing token.

    Callers must use a unique owner for each lease tenure and present both
    ``owner`` and ``generation`` when renewing or releasing it. Bounded
    pruning may delete an expired row, so generation alone is not globally
    unique.
    """

    acquired: bool
    owner: str
    expires_at: datetime
    generation: int
    retry_after_seconds: int


@dataclass(frozen=True)
class InterlockDecision:
    acquired: bool
    resource_key: str
    owner: str
    generation: int
    operation: str
    state: str
    outcome_code: str
    worker_finished_at: datetime | None


_MUTATION_OPERATIONS = frozenset(
    {
        "order_approve",
        "order_reject",
        "breaker_reset",
        "order_cancel",
        "portfolio_reconcile",
        "order_sync",
        "panic",
        "analysis",
        "plan_approve",
        "plan_cancel",
        "proposal_batch",
        "backtest",
    }
)
_UNCERTAIN_OUTCOMES = frozenset(
    {
        "request_cancelled",
        "handler_failed",
        "lease_renewal_unproven",
        "lease_ownership_lost",
        "panic_settlement_unproven",
        "lease_release_unproven",
        "interlock_settlement_unproven",
    }
)
_RECONCILIATION_EVIDENCE = frozenset(
    {
        "broker_truth_reconciled",
        "portfolio_truth_reconciled",
        "domain_truth_reconciled",
    }
)


def _bucket_key(
    policy_name: str,
    bucket_kind: Literal[
        "principal_window",
        "global_window",
        "principal_day",
        "global_day",
    ],
    principal: str,
) -> str:
    material = f"{policy_name}\0{bucket_kind}\0{principal}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _as_utc(value: datetime | None) -> datetime:
    value = value or utcnow()
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _retry_after(expires_at: datetime, now: datetime) -> int:
    return max(0, math.ceil((expires_at - now).total_seconds()))


def _rollback_quietly(session: Session) -> None:
    try:
        session.rollback()
    except Exception:
        pass


@contextmanager
def _store_session(session_factory: sessionmaker[Session]):
    try:
        with session_factory() as session:
            try:
                yield session
            except (SQLAlchemyError, OSError):
                _rollback_quietly(session)
                raise
    except (SQLAlchemyError, OSError) as exc:
        raise LimitStoreUnavailable(
            "durable limit store unavailable"
        ) from exc


@dataclass(frozen=True)
class _Bucket:
    key: str
    ceiling: int
    started_at: datetime
    expires_at: datetime


class DurableRateLimiter:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @property
    def session_factory(self) -> sessionmaker[Session]:
        return self._session_factory

    def consume_pair(
        self,
        spec: LimitSpec,
        *,
        principal: str,
        now: datetime | None = None,
    ) -> LimitDecision:
        current = _as_utc(now)
        buckets = self._buckets(spec, principal, current)

        with _store_session(self._session_factory) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            consumed: list[tuple[int, datetime]] = []
            for bucket in buckets:
                row = self._consume_bucket(
                    session,
                    policy_name=spec.name,
                    bucket=bucket,
                    now=current,
                )
                if row is None:
                    session.rollback()
                    return self._denied_decision(
                        session,
                        buckets=buckets,
                        now=current,
                    )
                consumed.append(
                    (bucket.ceiling - row.hits, row.expires_at)
                )
            session.commit()

        remaining = min(item[0] for item in consumed)
        reset_at = max(
            item[1]
            for item in consumed
            if item[0] == remaining
        )
        return LimitDecision(
            allowed=True,
            remaining=remaining,
            retry_after_seconds=0,
            reset_at=reset_at,
        )

    @staticmethod
    def _buckets(
        spec: LimitSpec,
        principal: str,
        now: datetime,
    ) -> list[_Bucket]:
        fixed_expires_at = now + timedelta(seconds=spec.window_seconds)
        buckets = [
            _Bucket(
                _bucket_key(
                    spec.name,
                    "principal_window",
                    principal,
                ),
                spec.principal_requests,
                now,
                fixed_expires_at,
            ),
            _Bucket(
                _bucket_key(spec.name, "global_window", ""),
                spec.global_requests,
                now,
                fixed_expires_at,
            ),
        ]
        day_started_at = datetime(
            now.year,
            now.month,
            now.day,
            tzinfo=UTC,
        )
        day_expires_at = day_started_at + timedelta(days=1)
        if spec.principal_daily_requests is not None:
            buckets.append(
                _Bucket(
                    _bucket_key(
                        spec.name,
                        "principal_day",
                        principal,
                    ),
                    spec.principal_daily_requests,
                    day_started_at,
                    day_expires_at,
                )
            )
        if spec.global_daily_requests is not None:
            buckets.append(
                _Bucket(
                    _bucket_key(spec.name, "global_day", ""),
                    spec.global_daily_requests,
                    day_started_at,
                    day_expires_at,
                )
            )
        return buckets

    @staticmethod
    def _consume_bucket(
        session: Session,
        *,
        policy_name: str,
        bucket: _Bucket,
        now: datetime,
    ):
        expired = RateWindow.expires_at <= now
        statement = sqlite_insert(RateWindow).values(
            bucket_key=bucket.key,
            policy_name=policy_name,
            window_started_at=bucket.started_at,
            expires_at=bucket.expires_at,
            hits=1,
            version=0,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[RateWindow.bucket_key],
            set_={
                "policy_name": policy_name,
                "window_started_at": case(
                    (expired, bucket.started_at),
                    else_=RateWindow.window_started_at,
                ),
                "expires_at": case(
                    (expired, bucket.expires_at),
                    else_=RateWindow.expires_at,
                ),
                "hits": case(
                    (expired, 1),
                    else_=RateWindow.hits + 1,
                ),
                "version": RateWindow.version + 1,
            },
            where=or_(
                expired,
                RateWindow.hits < bucket.ceiling,
            ),
        ).returning(
            RateWindow.hits,
            RateWindow.expires_at,
        )
        return session.execute(statement).one_or_none()

    @staticmethod
    def _denied_decision(
        session: Session,
        *,
        buckets: list[_Bucket],
        now: datetime,
    ) -> LimitDecision:
        ceilings = {bucket.key: bucket.ceiling for bucket in buckets}
        rows = session.execute(
            select(
                RateWindow.bucket_key,
                RateWindow.hits,
                RateWindow.expires_at,
            ).where(RateWindow.bucket_key.in_(ceilings))
        ).all()
        blocked_resets = [
            row.expires_at
            for row in rows
            if row.expires_at > now
            and row.hits >= ceilings[row.bucket_key]
        ]
        reset_at = max(blocked_resets) if blocked_resets else now
        return LimitDecision(
            allowed=False,
            remaining=0,
            retry_after_seconds=_retry_after(reset_at, now),
            reset_at=reset_at,
        )

    def prune_expired(
        self,
        now: datetime,
        limit: int = 500,
    ) -> int:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        if limit == 0:
            return 0
        current = _as_utc(now)
        keys = (
            select(RateWindow.bucket_key)
            .where(RateWindow.expires_at <= current)
            .order_by(
                RateWindow.expires_at,
                RateWindow.bucket_key,
            )
            .limit(limit)
        )
        with _store_session(self._session_factory) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            result = session.execute(
                delete(RateWindow).where(
                    RateWindow.bucket_key.in_(keys)
                )
            )
            session.commit()
            return result.rowcount


class ConcurrencyLeaseService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @property
    def session_factory(self) -> sessionmaker[Session]:
        return self._session_factory

    def acquire(
        self,
        resource_key: str,
        *,
        owner: str,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> LeaseDecision:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        current = _as_utc(now)
        expires_at = current + timedelta(seconds=ttl_seconds)
        statement = sqlite_insert(ConcurrencyLease).values(
            resource_key=resource_key,
            owner=owner,
            expires_at=expires_at,
            generation=1,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[ConcurrencyLease.resource_key],
            set_={
                "owner": owner,
                "expires_at": expires_at,
                "generation": ConcurrencyLease.generation + 1,
            },
            where=or_(
                ConcurrencyLease.expires_at <= current,
                ConcurrencyLease.owner == owner,
            ),
        ).returning(
            ConcurrencyLease.owner,
            ConcurrencyLease.expires_at,
            ConcurrencyLease.generation,
        )

        with _store_session(self._session_factory) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.execute(statement).one_or_none()
            if row is None:
                session.rollback()
                observed = self._inspect(
                    session,
                    resource_key,
                    current,
                )
                return LeaseDecision(
                    acquired=False,
                    owner=observed.owner,
                    expires_at=observed.expires_at,
                    generation=observed.generation,
                    retry_after_seconds=(
                        observed.retry_after_seconds
                    ),
                )
            session.commit()

        return LeaseDecision(
            acquired=True,
            owner=row.owner,
            expires_at=row.expires_at,
            generation=row.generation,
            retry_after_seconds=0,
        )

    def renew(
        self,
        resource_key: str,
        *,
        owner: str,
        generation: int,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> LeaseDecision:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        with _store_session(self._session_factory) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            current = _as_utc(now)
            expires_at = current + timedelta(seconds=ttl_seconds)
            statement = (
                update(ConcurrencyLease)
                .where(
                    ConcurrencyLease.resource_key == resource_key,
                    ConcurrencyLease.owner == owner,
                    ConcurrencyLease.generation == generation,
                    ConcurrencyLease.expires_at > current,
                )
                .values(expires_at=expires_at)
                .returning(
                    ConcurrencyLease.owner,
                    ConcurrencyLease.expires_at,
                    ConcurrencyLease.generation,
                )
            )
            row = session.execute(statement).one_or_none()
            if row is None:
                session.rollback()
                observed = self._inspect(
                    session,
                    resource_key,
                    current,
                )
                return LeaseDecision(
                    acquired=False,
                    owner=observed.owner,
                    expires_at=observed.expires_at,
                    generation=observed.generation,
                    retry_after_seconds=(
                        observed.retry_after_seconds
                    ),
                )
            session.commit()
        return LeaseDecision(
            acquired=True,
            owner=row.owner,
            expires_at=row.expires_at,
            generation=row.generation,
            retry_after_seconds=0,
        )

    def release(
        self,
        resource_key: str,
        *,
        owner: str,
        generation: int,
        now: datetime | None = None,
    ) -> bool:
        with _store_session(self._session_factory) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            current = _as_utc(now)
            statement = (
                update(ConcurrencyLease)
                .where(
                    ConcurrencyLease.resource_key == resource_key,
                    ConcurrencyLease.owner == owner,
                    ConcurrencyLease.generation == generation,
                    ConcurrencyLease.expires_at > current,
                )
                .values(
                    owner="",
                    expires_at=_RELEASED_AT,
                    generation=ConcurrencyLease.generation + 1,
                )
            )
            result = session.execute(statement)
            session.commit()
            return result.rowcount == 1

    def inspect(
        self,
        resource_key: str,
        *,
        now: datetime | None = None,
    ) -> LeaseDecision:
        current = _as_utc(now)
        with _store_session(self._session_factory) as session:
            return self._inspect(session, resource_key, current)

    @staticmethod
    def _inspect(
        session: Session,
        resource_key: str,
        now: datetime,
    ) -> LeaseDecision:
        row = session.execute(
            select(
                ConcurrencyLease.owner,
                ConcurrencyLease.expires_at,
                ConcurrencyLease.generation,
            ).where(ConcurrencyLease.resource_key == resource_key)
        ).one_or_none()
        if row is None:
            return LeaseDecision(
                acquired=False,
                owner="",
                expires_at=now,
                generation=0,
                retry_after_seconds=0,
            )
        held = row.expires_at > now
        return LeaseDecision(
            acquired=held,
            owner=row.owner,
            expires_at=row.expires_at,
            generation=row.generation,
            retry_after_seconds=(
                _retry_after(row.expires_at, now) if held else 0
            ),
        )

    def prune_expired(
        self,
        now: datetime,
        limit: int = 500,
    ) -> int:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        if limit == 0:
            return 0
        current = _as_utc(now)
        keys = (
            select(ConcurrencyLease.resource_key)
            .where(ConcurrencyLease.expires_at <= current)
            .order_by(
                ConcurrencyLease.expires_at,
                ConcurrencyLease.resource_key,
            )
            .limit(limit)
        )
        with _store_session(self._session_factory) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            result = session.execute(
                delete(ConcurrencyLease).where(
                    ConcurrencyLease.resource_key.in_(keys)
                )
            )
            session.commit()
            return result.rowcount


class MutationInterlockService:
    """Durable, non-expiring fence for a protected domain mutation."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @property
    def session_factory(self) -> sessionmaker[Session]:
        return self._session_factory

    def claim(
        self,
        resource_key: str,
        *,
        owner: str,
        generation: int,
        operation: str,
    ) -> InterlockDecision:
        self._validate_identity(
            resource_key,
            owner=owner,
            generation=generation,
        )
        if operation not in _MUTATION_OPERATIONS:
            raise ValueError("unsupported mutation operation")

        with _store_session(self._session_factory) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            statement = (
                sqlite_insert(MutationInterlock)
                .values(
                    resource_key=resource_key,
                    owner=owner,
                    generation=generation,
                    operation=operation,
                    state="active",
                    outcome_code="",
                    worker_finished_at=None,
                    created_at=utcnow(),
                    updated_at=utcnow(),
                )
                .on_conflict_do_nothing(
                    index_elements=[MutationInterlock.resource_key]
                )
                .returning(MutationInterlock.resource_key)
            )
            inserted = session.execute(statement).one_or_none()
            if inserted is None:
                existing = session.get(MutationInterlock, resource_key)
                session.rollback()
                if existing is None:
                    raise LimitStoreUnavailable(
                        "durable interlock conflict could not be observed"
                    )
                return self._decision(existing, acquired=False)
            created = session.get(MutationInterlock, resource_key)
            if created is None:
                session.rollback()
                raise LimitStoreUnavailable(
                    "durable interlock insert could not be observed"
                )
            decision = self._decision(created, acquired=True)
            session.commit()
            return decision

    def inspect(self, resource_key: str) -> InterlockDecision | None:
        if not resource_key or len(resource_key) > 128:
            raise ValueError("invalid interlock resource key")
        with _store_session(self._session_factory) as session:
            row = session.get(MutationInterlock, resource_key)
            return (
                None
                if row is None
                else self._decision(row, acquired=False)
            )

    def settle(
        self,
        resource_key: str,
        *,
        owner: str,
        generation: int,
        outcome_code: str,
    ) -> bool:
        self._validate_identity(
            resource_key,
            owner=owner,
            generation=generation,
        )
        if outcome_code != "handler_completed":
            raise ValueError("unsupported settled outcome")
        with _store_session(self._session_factory) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            current = utcnow()
            result = session.execute(
                update(MutationInterlock)
                .where(
                    MutationInterlock.resource_key == resource_key,
                    MutationInterlock.owner == owner,
                    MutationInterlock.generation == generation,
                    MutationInterlock.state == "active",
                )
                .values(
                    state="settled",
                    outcome_code=outcome_code,
                    worker_finished_at=current,
                    updated_at=current,
                )
            )
            session.commit()
            return result.rowcount == 1

    def mark_uncertain(
        self,
        resource_key: str,
        *,
        owner: str,
        generation: int,
        outcome_code: str,
        worker_finished: bool,
    ) -> bool:
        self._validate_identity(
            resource_key,
            owner=owner,
            generation=generation,
        )
        if outcome_code not in _UNCERTAIN_OUTCOMES:
            raise ValueError("unsupported uncertain outcome")
        with _store_session(self._session_factory) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(MutationInterlock, resource_key)
            if (
                row is None
                or row.owner != owner
                or row.generation != generation
            ):
                session.rollback()
                return False
            current = utcnow()
            row.state = "uncertain"
            row.outcome_code = outcome_code
            if worker_finished:
                row.worker_finished_at = current
            row.updated_at = current
            session.commit()
            return True

    def release_settled(
        self,
        resource_key: str,
        *,
        owner: str,
        generation: int,
        now: datetime | None = None,
    ) -> bool:
        """Atomically release an exact live lease and delete its settled latch."""

        self._validate_identity(
            resource_key,
            owner=owner,
            generation=generation,
        )
        with _store_session(self._session_factory) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            current = _as_utc(now)
            released = session.execute(
                update(ConcurrencyLease)
                .where(
                    ConcurrencyLease.resource_key == resource_key,
                    ConcurrencyLease.owner == owner,
                    ConcurrencyLease.generation == generation,
                    ConcurrencyLease.expires_at > current,
                )
                .values(
                    owner="",
                    expires_at=_RELEASED_AT,
                    generation=ConcurrencyLease.generation + 1,
                )
            )
            if released.rowcount != 1:
                session.rollback()
                return False
            cleared = session.execute(
                delete(MutationInterlock).where(
                    MutationInterlock.resource_key == resource_key,
                    MutationInterlock.owner == owner,
                    MutationInterlock.generation == generation,
                    MutationInterlock.state == "settled",
                    MutationInterlock.outcome_code
                    == "handler_completed",
                    MutationInterlock.worker_finished_at.is_not(None),
                )
            )
            if cleared.rowcount != 1:
                session.rollback()
                return False
            session.commit()
            return True

    def reconcile_clear(
        self,
        resource_key: str,
        *,
        owner: str,
        generation: int,
        actor: str,
        request_id: str,
        evidence_code: str,
        worker_termination_proven: bool,
    ) -> bool:
        """Clear an exact latch with proven truth and an atomic audit event."""

        self._validate_identity(
            resource_key,
            owner=owner,
            generation=generation,
        )
        if evidence_code not in _RECONCILIATION_EVIDENCE:
            raise ValueError("unsupported reconciliation evidence")
        if (
            not actor
            or len(actor) > 128
            or not request_id
            or len(request_id) > 64
        ):
            raise ValueError("invalid reconciliation audit identity")

        with _store_session(self._session_factory) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(MutationInterlock, resource_key)
            if (
                row is None
                or row.owner != owner
                or row.generation != generation
                or (
                    row.worker_finished_at is None
                    and not worker_termination_proven
                )
            ):
                session.rollback()
                return False
            session.add(
                AuditEvent(
                    actor=actor,
                    action="mutation_interlock.reconcile",
                    target_type="mutation_interlock",
                    target_id=hashlib.sha256(
                        resource_key.encode("utf-8")
                    ).hexdigest(),
                    request_id=request_id,
                    idempotency_key="",
                    reason=evidence_code,
                    result_code="cleared",
                    latency_ms=0,
                    detail_json="{}",
                    created_at=utcnow(),
                )
            )
            session.delete(row)
            session.commit()
            return True

    @staticmethod
    def _validate_identity(
        resource_key: str,
        *,
        owner: str,
        generation: int,
    ) -> None:
        if not resource_key or len(resource_key) > 128:
            raise ValueError("invalid interlock resource key")
        if not owner or len(owner) > 64:
            raise ValueError("invalid interlock owner")
        if generation < 0:
            raise ValueError("invalid interlock generation")

    @staticmethod
    def _decision(
        row: MutationInterlock,
        *,
        acquired: bool,
    ) -> InterlockDecision:
        return InterlockDecision(
            acquired=acquired,
            resource_key=row.resource_key,
            owner=row.owner,
            generation=row.generation,
            operation=row.operation,
            state=row.state,
            outcome_code=row.outcome_code,
            worker_finished_at=row.worker_finished_at,
        )
