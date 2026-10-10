"""Durable readiness evidence: observation cycles and backtest runs.

Evidence is stored as audit events (``audit_events``), whose ``detail_json``
is encrypted at rest by the sensitive-field store, so no schema change is
needed and the records sit beside every other operator-visible receipt.

* ``autopilot.cycle``: one per daemon cycle. ``result_code`` is
  ``observed`` (decided, nothing placed), ``executed`` (paper orders
  placed), ``blocked`` (paper mode, readiness gate failed, nothing placed),
  ``degraded`` (data or broker truth unavailable for part of the universe)
  or ``failed`` (the cycle raised). The detail records the session,
  timestamps, mode, execution kind (``simulated`` or ``paper``),
  fingerprints, code identity, every per-symbol decision with data age and
  intended action, risk rejections, and the readiness verdict.
* ``autopilot.backtest``: one per real-data backtest of the configured
  strategy, with its data window, symbols, code fingerprint and results.

The idempotency key of a cycle event names (mode, session, approval
fingerprint), so the daemon can tell whether a session's cycle already ran.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import select

from ..db.models import AuditEvent
from ..operations.audit import AuditRecorder, MutationContext
from ..security.sensitive_fields import sensitive_store

CYCLE_ACTION = "autopilot.cycle"
BACKTEST_ACTION = "autopilot.backtest"
COMPLETED_RESULTS = frozenset({"observed", "executed", "blocked", "degraded"})
CLEAN_RESULTS = frozenset({"observed", "executed", "blocked"})
EVIDENCE_SCHEMA = 1


def cycle_key(mode: str, session: date, evidence_fp: str) -> str:
    material = f"{mode}|{session.isoformat()}|{evidence_fp}"
    return "ap-cycle-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


@dataclass(frozen=True)
class EvidenceRecord:
    result_code: str
    created_at: datetime
    idempotency_key: str
    detail: dict[str, Any]


class EvidenceStore:
    def __init__(self, session_factory, *, actor: str) -> None:
        self.session_factory = session_factory
        self.actor = actor
        self._recorder = AuditRecorder(session_factory)

    def _record(
        self,
        action: str,
        *,
        target_id: str,
        result_code: str,
        idempotency_key: str,
        reason: str,
        detail: dict[str, Any],
    ) -> None:
        self._recorder.record(
            MutationContext(
                actor=self.actor,
                request_id=uuid4().hex,
                reason=reason,
                idempotency_key=idempotency_key,
            ),
            action,
            "autopilot",
            target_id,
            result_code,
            {"schema": EVIDENCE_SCHEMA, **detail},
        )

    def record_cycle(
        self,
        *,
        key: str,
        session: date,
        result_code: str,
        detail: dict[str, Any],
    ) -> None:
        self._record(
            CYCLE_ACTION,
            target_id=session.isoformat(),
            result_code=result_code,
            idempotency_key=key,
            reason="autopilot cycle evidence",
            detail=detail,
        )

    def record_backtest(self, *, result_code: str, detail: dict[str, Any]) -> None:
        self._record(
            BACKTEST_ACTION,
            target_id=str(detail.get("strategy", "unknown"))[:64],
            result_code=result_code,
            idempotency_key="",
            reason="autopilot backtest evidence",
            detail=detail,
        )

    def _load(self, action: str, *, key: str | None = None) -> list[EvidenceRecord]:
        records: list[EvidenceRecord] = []
        with self.session_factory() as session:
            query = select(AuditEvent).where(AuditEvent.action == action)
            if key is not None:
                query = query.where(AuditEvent.idempotency_key == key)
            store = sensitive_store(session, self.session_factory)
            for event in session.execute(query.order_by(AuditEvent.id)).scalars():
                try:
                    detail = json.loads(store.read(event, "detail_json"))
                except Exception:
                    detail = {"unreadable": True}
                records.append(
                    EvidenceRecord(
                        result_code=event.result_code,
                        created_at=event.created_at,
                        idempotency_key=event.idempotency_key,
                        detail=detail if isinstance(detail, dict) else {"unreadable": True},
                    )
                )
        return records

    def cycles(self, *, key: str | None = None) -> list[EvidenceRecord]:
        return self._load(CYCLE_ACTION, key=key)

    def backtests(self) -> list[EvidenceRecord]:
        return self._load(BACKTEST_ACTION)

    def session_completed(self, key: str) -> bool:
        return any(r.result_code in COMPLETED_RESULTS for r in self.cycles(key=key))

    def failed_attempts(self, key: str) -> int:
        return sum(1 for r in self.cycles(key=key) if r.result_code == "failed")


def decision_counts(decisions: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for decision in decisions:
        reason = str(decision.get("reason"))
        counts[reason] = counts.get(reason, 0) + 1
    return counts
