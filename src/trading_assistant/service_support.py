"""Mutation-context validation and audit persistence shared by the service
and its order-cancellation mixin (moved verbatim from ``service``)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .db.models import AuditEvent, utcnow
from .db.lifecycle_proofs import augment_lifecycle_detail_json
from .security.sensitive_fields import persist_sensitive


def _require_mutation_context(
    actor: str,
    reason: str,
    request_id: str,
) -> tuple[str, str, str]:
    actor = actor.strip()
    reason = reason.strip()
    request_id = request_id.strip()
    if not actor or not reason or not request_id:
        raise ValueError(
            "mutation actor, reason, and request_id must be non-empty"
        )
    return actor, reason, request_id


def _persist_audit(
    session: Session,
    *,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    request_id: str,
    reason: str,
    result_code: str,
    idempotency_key: str = "",
    detail_json: str = "{}",
    created_at=None,
    lifecycle_proof: bool = True,
) -> AuditEvent:
    if lifecycle_proof:
        detail_json = augment_lifecycle_detail_json(
            session,
            target_type=target_type,
            target_id=target_id,
            detail_json=detail_json,
        )
    event = AuditEvent(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        request_id=request_id,
        idempotency_key=idempotency_key,
        result_code=result_code,
        created_at=created_at or utcnow(),
    )
    persist_sensitive(
        session,
        event,
        {"reason": reason, "detail_json": detail_json},
    )
    return event
