"""Durable process-start broker-truth reconciliation latch."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Callable

from sqlalchemy.orm import Session, sessionmaker

from ..db.models import (
    AuditEvent,
    StartupReconciliationState,
)
from ..risk.submission_barrier import SubmissionBarrier
from ..security.sensitive_fields import persist_sensitive, sensitive_store


class StartupReconciliationFailed(RuntimeError):
    """The newest runtime generation could not prove broker/local agreement."""


_FAILURE_CODE = re.compile(r"[a-z][a-z0-9_]{2,63}\Z")


@dataclass(frozen=True, slots=True)
class StartupReconciliationValidation:
    """Pure validation result over non-narrative reconciliation columns."""

    valid: bool
    current: bool
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime | None


def _persisted_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_startup_reconciliation_snapshot(
    *,
    generation: object,
    completed_generation: object,
    status: object,
    started_at: object,
    completed_at: object,
    updated_at: object,
    observed_at: object,
    expected_generation: int | None = None,
) -> StartupReconciliationValidation:
    """Validate exactly the safe columns used by reconciliation authority."""

    observed = _persisted_utc(observed_at)
    started = _persisted_utc(started_at)
    completed = (
        _persisted_utc(completed_at)
        if completed_at is not None
        else None
    )
    updated = _persisted_utc(updated_at)
    invalid = StartupReconciliationValidation(
        valid=False,
        current=False,
        started_at=started,
        completed_at=completed,
        updated_at=updated,
    )
    if (
        observed is None
        or not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or type(generation) is not int
        or generation <= 0
        or type(completed_generation) is not int
        or completed_generation < 0
        or completed_generation > generation
        or status not in {"required", "current", "failed"}
        or started is None
        or updated is None
        or not started <= updated <= observed
        or (
            expected_generation is not None
            and (
                type(expected_generation) is not int
                or expected_generation <= 0
                or generation != expected_generation
            )
        )
    ):
        return invalid
    if status == "current":
        if (
            completed_generation != generation
            or completed is None
            or not started <= completed <= updated
        ):
            return invalid
        return StartupReconciliationValidation(
            valid=True,
            current=True,
            started_at=started,
            completed_at=completed,
            updated_at=updated,
        )
    if completed_generation >= generation or completed is not None:
        return invalid
    return StartupReconciliationValidation(
        valid=True,
        current=False,
        started_at=started,
        completed_at=None,
        updated_at=updated,
    )


def _context(
    actor: str,
    reason: str,
    request_id: str,
) -> tuple[str, str, str]:
    actor = actor.strip()
    reason = reason.strip()
    request_id = request_id.strip()
    if not actor or not reason or not request_id:
        raise ValueError(
            "startup reconciliation actor, reason, and request_id "
            "must be non-empty"
        )
    return actor, reason, request_id


class StartupReconciliationGate:
    """Generation-based latch shared by every process using one broker/DB."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker_key: str,
        *,
        enabled: bool,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.broker_key = broker_key
        self.enabled = enabled
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.submission_barrier = SubmissionBarrier(session_factory)

    def require(
        self,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> int:
        """Atomically invalidate prior proof and return the new generation."""
        actor, reason, request_id = _context(actor, reason, request_id)
        if not self.enabled:
            raise RuntimeError("startup reconciliation gate is disabled")
        now = self._clock()
        with self.submission_barrier.hold_writer():
            with self.session_factory() as session:
                state = session.get(
                    StartupReconciliationState,
                    self.broker_key,
                )
                if state is None:
                    generation = 1
                    state = StartupReconciliationState(
                        broker=self.broker_key,
                        generation=generation,
                        completed_generation=0,
                    )
                else:
                    generation = state.generation + 1
                    state.generation = generation
                state.status = "required"
                state.actor = actor
                state.request_id = request_id
                state.started_at = now
                state.completed_at = None
                state.updated_at = now
                store = sensitive_store(session, self.session_factory)
                store.write_many(
                    state,
                    {"reason": reason, "evidence_json": "{}"},
                )
                persist_sensitive(
                    session,
                    AuditEvent(
                        actor=actor,
                        action="startup_reconciliation.require",
                        target_type="broker",
                        target_id=self.broker_key,
                        request_id=request_id,
                        result_code="required",
                    ),
                    {
                        "reason": reason,
                        "detail_json": json.dumps(
                            {"generation": generation},
                            sort_keys=True,
                        ),
                    },
                    session_factory=self.session_factory,
                )
                session.commit()
                return generation

    def complete(
        self,
        generation: int,
        *,
        evidence: dict,
        actor: str,
        reason: str,
        request_id: str,
    ) -> bool:
        """Mark only the still-current generation complete."""
        actor, reason, request_id = _context(actor, reason, request_id)
        if generation <= 0:
            raise ValueError("startup reconciliation generation must be positive")
        if not self.enabled:
            return True
        now = self._clock()
        encoded_evidence = json.dumps(evidence, sort_keys=True)
        with self.submission_barrier.hold_writer():
            with self.session_factory() as session:
                state = session.get(
                    StartupReconciliationState,
                    self.broker_key,
                )
                if state is None or state.generation != generation:
                    return False
                if (
                    state.status == "current"
                    and state.completed_generation == generation
                ):
                    return True
                state.completed_generation = generation
                state.status = "current"
                state.actor = actor
                state.request_id = request_id
                state.completed_at = now
                state.updated_at = now
                sensitive_store(
                    session,
                    self.session_factory,
                ).write_many(
                    state,
                    {
                        "reason": reason,
                        "evidence_json": encoded_evidence,
                    },
                )
                persist_sensitive(
                    session,
                    AuditEvent(
                        actor=actor,
                        action="startup_reconciliation.complete",
                        target_type="broker",
                        target_id=self.broker_key,
                        request_id=request_id,
                        result_code="current",
                    ),
                    {
                        "reason": reason,
                        "detail_json": encoded_evidence,
                    },
                    session_factory=self.session_factory,
                )
                session.commit()
                return True

    def fail(
        self,
        generation: int,
        failure_code: str,
        *,
        evidence: dict,
        actor: str,
        reason: str,
        request_id: str,
    ) -> bool:
        """Persist a failure without allowing stale work to relock newer proof."""
        actor, reason, request_id = _context(actor, reason, request_id)
        failure_code = failure_code.strip()
        if _FAILURE_CODE.fullmatch(failure_code) is None:
            raise ValueError("startup reconciliation failure code is invalid")
        if not self.enabled:
            return False
        now = self._clock()
        detail = {**evidence, "failure_code": failure_code}
        encoded_evidence = json.dumps(detail, sort_keys=True)
        with self.submission_barrier.hold_writer():
            with self.session_factory() as session:
                state = session.get(
                    StartupReconciliationState,
                    self.broker_key,
                )
                if (
                    state is None
                    or state.generation != generation
                    or state.completed_generation >= generation
                ):
                    session.rollback()
                    return False
                state.status = "failed"
                state.actor = actor
                state.request_id = request_id
                state.completed_at = None
                state.updated_at = now
                sensitive_store(
                    session,
                    self.session_factory,
                ).write_many(
                    state,
                    {
                        "reason": failure_code,
                        "evidence_json": encoded_evidence,
                    },
                )
                persist_sensitive(
                    session,
                    AuditEvent(
                        actor=actor,
                        action="startup_reconciliation.fail",
                        target_type="broker",
                        target_id=self.broker_key,
                        request_id=request_id,
                        result_code="failed",
                    ),
                    {
                        "reason": reason,
                        "detail_json": encoded_evidence,
                    },
                    session_factory=self.session_factory,
                )
                session.commit()
                return True

    def posture(self) -> dict[str, object]:
        """Return fixed safety state without exposing broker/provider detail."""
        if not self.enabled:
            return {
                "status": "not_required",
                "generation": 0,
                "completed_generation": 0,
                "failure_code": None,
            }
        with self.session_factory() as session:
            state = session.get(StartupReconciliationState, self.broker_key)
            if state is None:
                return {
                    "status": "required",
                    "generation": 0,
                    "completed_generation": 0,
                    "failure_code": None,
                }
            return {
                "status": state.status,
                "generation": state.generation,
                "completed_generation": state.completed_generation,
                "failure_code": (
                    sensitive_store(
                        session,
                        self.session_factory,
                    ).read(state, "reason")
                    if state.status == "failed"
                    else None
                ),
            }

    def current_generation(self) -> int:
        if not self.enabled:
            return 0
        with self.session_factory() as session:
            state = session.get(
                StartupReconciliationState,
                self.broker_key,
            )
            return state.generation if state is not None else 0

    def is_current(self, generation: int | None = None) -> bool:
        if not self.enabled:
            return True
        try:
            with self.session_factory() as session:
                row = session.query(
                    StartupReconciliationState.generation,
                    StartupReconciliationState.completed_generation,
                    StartupReconciliationState.status,
                    StartupReconciliationState.started_at,
                    StartupReconciliationState.completed_at,
                    StartupReconciliationState.updated_at,
                ).filter(
                    StartupReconciliationState.broker
                    == self.broker_key
                ).one_or_none()
            if row is None:
                return False
            validation = validate_startup_reconciliation_snapshot(
                generation=row.generation,
                completed_generation=row.completed_generation,
                status=row.status,
                started_at=row.started_at,
                completed_at=row.completed_at,
                updated_at=row.updated_at,
                observed_at=self._clock(),
                expected_generation=generation,
            )
            return validation.valid and validation.current
        except Exception:
            return False
