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
    ConcurrencyLease,
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
    """Lease result.

    ``generation`` is diagnostic only, not a fencing token. Bounded pruning
    may delete an expired row, so a later acquisition may restart it at 1.
    """

    acquired: bool
    owner: str
    expires_at: datetime
    generation: int
    retry_after_seconds: int


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

    def release(self, resource_key: str, *, owner: str) -> bool:
        statement = (
            update(ConcurrencyLease)
            .where(
                ConcurrencyLease.resource_key == resource_key,
                ConcurrencyLease.owner == owner,
            )
            .values(
                owner="",
                expires_at=_RELEASED_AT,
                generation=ConcurrencyLease.generation + 1,
            )
        )
        with _store_session(self._session_factory) as session:
            session.execute(text("BEGIN IMMEDIATE"))
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
