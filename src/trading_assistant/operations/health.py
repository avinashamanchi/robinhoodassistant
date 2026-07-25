"""Minimal anonymous liveness and complete authenticated health reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from ..db.models import AuditEvent
from ..orders.safety_state import read_persisted_safety_truth


def database_reachable(session_factory) -> bool:
    try:
        with session_factory() as session:
            session.execute(select(1)).scalar_one()
        return True
    except Exception:
        return False


@dataclass(frozen=True)
class LivenessReport:
    alive: bool
    database_reachable: bool

    def as_dict(self) -> dict[str, bool]:
        return {
            "alive": self.alive,
            "database_reachable": self.database_reachable,
        }


@dataclass(frozen=True)
class OperationalHealthReport:
    payload: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return dict(self.payload)

    @property
    def mode(self) -> str:
        return str(self.payload["mode"])

    @property
    def broker(self) -> str:
        return str(self.payload["broker"])

    @property
    def database_reachable(self) -> bool:
        return self.payload.get("database_reachable") is True

    @property
    def heartbeat_age_seconds(self) -> float | None:
        value = self.payload.get("heartbeat_age_seconds")
        return float(value) if value is not None else None

    @property
    def reconciliation_age_seconds(self) -> float | None:
        value = self.payload.get("reconciliation_age_seconds")
        return float(value) if value is not None else None

    @property
    def last_confirmed_broker_contact(self) -> str | None:
        value = self.payload.get("last_confirmed_broker_contact")
        return str(value) if value is not None else None


def build_liveness(session_factory) -> LivenessReport:
    return LivenessReport(
        alive=True,
        database_reachable=database_reachable(session_factory),
    )


def build_operational_health(service) -> OperationalHealthReport:
    payload: dict[str, object] = {}
    last_contact = None
    try:
        with service.session_factory() as session:
            with session.begin():
                safety = read_persisted_safety_truth(session)
                payload = dict(service.health(safety=safety))
                last_contact = session.scalar(
                    select(AuditEvent.created_at)
                    .where(
                        AuditEvent.action.in_(
                            ("orders.sync", "positions.reconcile")
                        ),
                        AuditEvent.result_code.in_(
                            ("reconciled", "in_sync", "resolved")
                        ),
                    )
                    .order_by(
                        AuditEvent.created_at.desc(),
                        AuditEvent.id.desc(),
                    )
                    .limit(1)
                )
    except Exception:
        if not payload:
            payload = dict(service.health())
        payload["database_reachable"] = False
    else:
        payload["database_reachable"] = (
            payload.get("db_ok") is True
        )

    observed_at = datetime.fromisoformat(
        str(payload["observed_at"])
    )
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    payload["broker_contact_observed_at"] = (
        observed_at.isoformat()
    )
    payload["last_confirmed_broker_contact"] = (
        last_contact.isoformat() if last_contact is not None else None
    )
    reconciliation_age = None
    contact_valid = last_contact is None
    if last_contact is not None:
        if last_contact.tzinfo is None:
            last_contact = last_contact.replace(tzinfo=timezone.utc)
        contact_valid = last_contact <= observed_at
        if contact_valid:
            reconciliation_age = (
                observed_at - last_contact
            ).total_seconds()
    payload["broker_contact_evidence_valid"] = contact_valid
    payload["reconciliation_age_seconds"] = reconciliation_age
    return OperationalHealthReport(payload)
