"""Durable process-start broker-truth reconciliation latch."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from ..db.models import (
    AuditEvent,
    StartupReconciliationState,
)
from ..risk.submission_barrier import SubmissionBarrier


class StartupReconciliationFailed(RuntimeError):
    """The newest runtime generation could not prove broker/local agreement."""


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
    ) -> None:
        self.session_factory = session_factory
        self.broker_key = broker_key
        self.enabled = enabled
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
        now = datetime.now(timezone.utc)
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
                    session.add(state)
                else:
                    generation = state.generation + 1
                    state.generation = generation
                state.status = "required"
                state.actor = actor
                state.reason = reason
                state.request_id = request_id
                state.evidence_json = "{}"
                state.started_at = now
                state.completed_at = None
                state.updated_at = now
                session.add(
                    AuditEvent(
                        actor=actor,
                        action="startup_reconciliation.require",
                        target_type="broker",
                        target_id=self.broker_key,
                        request_id=request_id,
                        reason=reason,
                        result_code="required",
                        detail_json=json.dumps(
                            {"generation": generation},
                            sort_keys=True,
                        ),
                    )
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
        now = datetime.now(timezone.utc)
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
                result = session.execute(
                    update(StartupReconciliationState)
                    .where(
                        StartupReconciliationState.broker
                        == self.broker_key,
                        StartupReconciliationState.generation
                        == generation,
                    )
                    .values(
                        completed_generation=generation,
                        status="current",
                        actor=actor,
                        reason=reason,
                        request_id=request_id,
                        evidence_json=encoded_evidence,
                        completed_at=now,
                        updated_at=now,
                    )
                )
                if result.rowcount != 1:
                    session.rollback()
                    return False
                session.add(
                    AuditEvent(
                        actor=actor,
                        action="startup_reconciliation.complete",
                        target_type="broker",
                        target_id=self.broker_key,
                        request_id=request_id,
                        reason=reason,
                        result_code="current",
                        detail_json=encoded_evidence,
                    )
                )
                session.commit()
                return True

    def fail(
        self,
        generation: int,
        failure: str,
        *,
        evidence: dict,
        actor: str,
        reason: str,
        request_id: str,
    ) -> bool:
        """Persist a failure without allowing stale work to relock newer proof."""
        actor, reason, request_id = _context(actor, reason, request_id)
        failure = failure.strip()
        if not failure:
            raise ValueError("startup reconciliation failure must be non-empty")
        if not self.enabled:
            return False
        now = datetime.now(timezone.utc)
        detail = {**evidence, "failure": failure}
        encoded_evidence = json.dumps(detail, sort_keys=True)
        with self.submission_barrier.hold_writer():
            with self.session_factory() as session:
                result = session.execute(
                    update(StartupReconciliationState)
                    .where(
                        StartupReconciliationState.broker
                        == self.broker_key,
                        StartupReconciliationState.generation
                        == generation,
                        StartupReconciliationState.completed_generation
                        < generation,
                    )
                    .values(
                        status="failed",
                        actor=actor,
                        reason=failure,
                        request_id=request_id,
                        evidence_json=encoded_evidence,
                        completed_at=None,
                        updated_at=now,
                    )
                )
                if result.rowcount != 1:
                    session.rollback()
                    return False
                session.add(
                    AuditEvent(
                        actor=actor,
                        action="startup_reconciliation.fail",
                        target_type="broker",
                        target_id=self.broker_key,
                        request_id=request_id,
                        reason=reason,
                        result_code="failed",
                        detail_json=encoded_evidence,
                    )
                )
                session.commit()
                return True

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
        with self.session_factory() as session:
            state = session.get(
                StartupReconciliationState,
                self.broker_key,
            )
            return (
                state is not None
                and state.generation > 0
                and state.status == "current"
                and state.completed_generation == state.generation
                and (
                    generation is None
                    or state.generation == generation
                )
            )
