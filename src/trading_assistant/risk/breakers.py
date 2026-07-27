"""Typed, durable circuit breakers with independently resettable scopes."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, sessionmaker

from ..assets import AssetClass
from ..db.models import AuditEvent, CircuitBreakerState, PanicReceipt
from .submission_barrier import SubmissionBarrier


class BreakerKind(str, Enum):
    LOSS = "loss"
    DRAWDOWN = "drawdown"
    DATA = "data"
    LIQUIDITY = "liquidity"
    BROKER_DRIFT = "broker_drift"
    OPERATOR_GLOBAL = "operator_global"


@dataclass(frozen=True)
class BreakerScope:
    kind: BreakerKind
    target: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.target}" if self.target else self.kind.value

    @classmethod
    def data(cls, asset_class: AssetClass) -> "BreakerScope":
        return cls(BreakerKind.DATA, asset_class.value)

    @classmethod
    def loss(cls, asset_class: AssetClass) -> "BreakerScope":
        return cls(BreakerKind.LOSS, asset_class.value)

    @classmethod
    def drawdown(cls, asset_class: AssetClass) -> "BreakerScope":
        return cls(BreakerKind.DRAWDOWN, asset_class.value)

    @classmethod
    def liquidity(cls, target: str) -> "BreakerScope":
        return cls(BreakerKind.LIQUIDITY, target.upper())

    @classmethod
    def broker_drift(cls) -> "BreakerScope":
        return cls(BreakerKind.BROKER_DRIFT)

    @classmethod
    def operator_global(cls) -> "BreakerScope":
        return cls(BreakerKind.OPERATOR_GLOBAL)

    @classmethod
    def parse(cls, value: str) -> "BreakerScope":
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
        ):
            raise ValueError("breaker scope must be a canonical key")
        if value in {
            BreakerKind.BROKER_DRIFT.value,
            BreakerKind.OPERATOR_GLOBAL.value,
        }:
            return cls(BreakerKind(value))
        kind_text, separator, target = value.partition(":")
        if not separator or not target or ":" in target:
            raise ValueError("breaker scope must be a canonical key")
        try:
            kind = BreakerKind(kind_text)
        except ValueError as exc:
            raise ValueError(
                "breaker scope must be a canonical key"
            ) from exc
        if kind in {
            BreakerKind.DATA,
            BreakerKind.LOSS,
            BreakerKind.DRAWDOWN,
        }:
            if target not in {
                AssetClass.EQUITY.value,
                AssetClass.CRYPTO.value,
            }:
                raise ValueError(
                    "breaker scope must target an asset class"
                )
        elif kind is BreakerKind.LIQUIDITY:
            if (
                target != target.upper()
                or re.fullmatch(
                    r"[A-Z0-9][A-Z0-9./_-]{0,31}",
                    target,
                )
                is None
            ):
                raise ValueError(
                    "breaker scope must target a canonical symbol"
                )
        else:
            raise ValueError("breaker scope must be a canonical key")
        scope = cls(kind, target)
        if scope.key != value:
            raise ValueError("breaker scope must be a canonical key")
        return scope


@dataclass(frozen=True)
class BreakerState:
    scope: BreakerScope
    tripped: bool
    reason: str
    actor: str
    generation: int
    updated_at: datetime


class BreakerResetConflict(RuntimeError):
    """A reset was based on a stale or no-longer-tripped observation."""

    def __init__(
        self,
        scope: BreakerScope,
        expected_generation: int,
        current_state: BreakerState | None,
    ) -> None:
        current_generation = (
            current_state.generation if current_state is not None else None
        )
        super().__init__(
            f"breaker reset conflict for {scope.key}: expected generation "
            f"{expected_generation}, current generation {current_generation}"
        )
        self.scope = scope
        self.expected_generation = expected_generation
        self.current_state = current_state


def relevant_scopes_for_symbol(symbol: str) -> tuple[BreakerScope, ...]:
    asset_class = AssetClass.for_symbol(symbol)
    return (
        BreakerScope.operator_global(),
        BreakerScope.broker_drift(),
        BreakerScope.data(asset_class),
        BreakerScope.loss(asset_class),
        BreakerScope.drawdown(asset_class),
        BreakerScope.liquidity(symbol),
    )


def _now(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _state(row: CircuitBreakerState) -> BreakerState:
    return BreakerState(
        scope=BreakerScope(BreakerKind(row.kind), row.target),
        tripped=row.tripped,
        reason=row.reason,
        actor=row.actor,
        generation=row.generation,
        updated_at=row.updated_at,
    )


def _audit(
    session: Session,
    *,
    scope: BreakerScope,
    actor: str,
    action: str,
    reason: str,
    result_code: str,
    request_id: str,
    detail: Mapping[str, object] | None = None,
    now: datetime,
) -> None:
    session.add(
        AuditEvent(
            actor=actor,
            action=action,
            target_type="circuit_breaker",
            target_id=scope.key,
            request_id=request_id,
            reason=reason,
            result_code=result_code,
            detail_json=json.dumps(
                dict(detail or {}), sort_keys=True, default=str
            ),
            created_at=now,
        )
    )


def trip_in_session(
    session: Session,
    scope: BreakerScope,
    reason: str,
    actor: str,
    *,
    request_id: str,
    now: datetime | None = None,
    audit_reason: str | None = None,
) -> tuple[BreakerState, bool]:
    reason = reason.strip()
    actor = actor.strip()
    request_id = request_id.strip()
    if not reason or not actor or not request_id:
        raise ValueError(
            "breaker trip actor, reason, and request_id must be non-empty"
        )
    if audit_reason is not None and not audit_reason.strip():
        raise ValueError("breaker trip audit reason must be non-empty")
    timestamp = _now(now)
    prior_tripped = session.scalar(
        select(CircuitBreakerState.tripped).where(
            CircuitBreakerState.scope_key == scope.key
        )
    )
    statement = insert(CircuitBreakerState).values(
        scope_key=scope.key,
        kind=scope.kind.value,
        target=scope.target,
        tripped=True,
        reason=reason,
        actor=actor,
        generation=1,
        updated_at=timestamp,
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[CircuitBreakerState.scope_key],
            set_={
                "kind": scope.kind.value,
                "target": scope.target,
                "tripped": True,
                "reason": reason,
                "actor": actor,
                "generation": CircuitBreakerState.generation + 1,
                "updated_at": timestamp,
            },
        )
    )
    changed = prior_tripped is not True
    row = session.get(CircuitBreakerState, scope.key)
    assert row is not None
    _audit(
        session,
        scope=scope,
        actor=actor,
        action="circuit_breaker.trip",
        reason=(audit_reason or reason).strip(),
        result_code="tripped" if changed else "already_tripped",
        request_id=request_id,
        detail={"generation": row.generation},
        now=timestamp,
    )
    return _state(row), changed


def reset_in_session(
    session: Session,
    scope: BreakerScope,
    actor: str,
    reason: str,
    prior_health: Mapping[str, object],
    *,
    expected_generation: int,
    request_id: str,
    now: datetime | None = None,
) -> BreakerState:
    actor = actor.strip()
    reason = reason.strip()
    request_id = request_id.strip()
    if not actor:
        raise ValueError("breaker reset actor must be non-empty")
    if not reason:
        raise ValueError("breaker reset reason must be non-empty")
    if not request_id:
        raise ValueError("breaker reset request_id must be non-empty")
    if not prior_health:
        raise ValueError("breaker reset prior health must be non-empty")
    if type(expected_generation) is not int or expected_generation < 1:
        raise ValueError(
            "breaker reset expected generation must be a positive integer"
        )
    timestamp = _now(now)
    result = session.execute(
        update(CircuitBreakerState)
        .where(
            CircuitBreakerState.scope_key == scope.key,
            CircuitBreakerState.tripped.is_(True),
            CircuitBreakerState.generation == expected_generation,
        )
        .values(
            kind=scope.kind.value,
            target=scope.target,
            tripped=False,
            reason=reason,
            actor=actor,
            generation=CircuitBreakerState.generation + 1,
            updated_at=timestamp,
        )
    )
    if result.rowcount != 1:
        row = session.get(CircuitBreakerState, scope.key)
        current_state = _state(row) if row is not None else None
        _audit(
            session,
            scope=scope,
            actor=actor,
            action="circuit_breaker.reset",
            reason=reason,
            result_code="conflict",
            request_id=request_id,
            detail={
                "expected_generation": expected_generation,
                "current_generation": (
                    current_state.generation
                    if current_state is not None
                    else None
                ),
                "current_tripped": (
                    current_state.tripped
                    if current_state is not None
                    else None
                ),
                "prior_health": dict(prior_health),
            },
            now=timestamp,
        )
        raise BreakerResetConflict(
            scope,
            expected_generation,
            current_state,
        )
    row = session.get(CircuitBreakerState, scope.key)
    assert row is not None
    session.execute(
        delete(PanicReceipt).where(
            PanicReceipt.account_scope == "alpaca-paper",
            PanicReceipt.state == "completed",
        )
    )
    _audit(
        session,
        scope=scope,
        actor=actor,
        action="circuit_breaker.reset",
        reason=reason,
        result_code="reset",
        request_id=request_id,
        detail={
            "expected_generation": expected_generation,
            "generation": row.generation,
            "prior_health": dict(prior_health),
        },
        now=timestamp,
    )
    return _state(row)


class BreakerService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        self.submission_barrier = SubmissionBarrier(session_factory)

    def trip(
        self,
        scope: BreakerScope,
        reason: str,
        actor: str,
        *,
        request_id: str,
        now: datetime | None = None,
        audit_reason: str | None = None,
    ) -> BreakerState:
        with self.submission_barrier.hold_writer():
            with self.session_factory() as session:
                state, _changed = trip_in_session(
                    session,
                    scope,
                    reason,
                    actor,
                    now=now,
                    request_id=request_id,
                    audit_reason=audit_reason,
                )
                session.commit()
                return state

    def is_tripped(self, scope: BreakerScope) -> bool:
        with self.session_factory() as session:
            return bool(
                session.scalar(
                    select(CircuitBreakerState.tripped).where(
                        CircuitBreakerState.scope_key == scope.key
                    )
                )
            )

    def get(self, scope: BreakerScope) -> BreakerState | None:
        with self.session_factory() as session:
            row = session.get(CircuitBreakerState, scope.key)
            return _state(row) if row is not None else None

    def active_for_symbol(self, symbol: str) -> tuple[BreakerState, ...]:
        scope_keys = tuple(
            scope.key for scope in relevant_scopes_for_symbol(symbol)
        )
        with self.session_factory() as session:
            rows = session.scalars(
                select(CircuitBreakerState)
                .where(
                    CircuitBreakerState.scope_key.in_(scope_keys),
                    CircuitBreakerState.tripped.is_(True),
                )
                .order_by(CircuitBreakerState.scope_key)
            ).all()
            return tuple(_state(row) for row in rows)

    def reset(
        self,
        scope: BreakerScope,
        actor: str,
        reason: str,
        prior_health: Mapping[str, object],
        *,
        expected_generation: int,
        request_id: str,
        now: datetime | None = None,
    ) -> BreakerState:
        with self.submission_barrier.hold_writer():
            with self.session_factory() as session:
                try:
                    state = reset_in_session(
                        session,
                        scope,
                        actor,
                        reason,
                        prior_health,
                        expected_generation=expected_generation,
                        now=now,
                        request_id=request_id,
                    )
                except BreakerResetConflict:
                    session.commit()
                    raise
                else:
                    session.commit()
                    return state
