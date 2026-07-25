"""Boundary mutation provenance without replacing atomic domain audit rows."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Mapping

from ..db.models import AuditEvent
from ..logging import redact

_IDENTITY_LIMIT = 64
_ACTOR_LIMIT = 128


def _bounded(value: str, name: str, limit: int, *, optional: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if optional and value == "":
        return ""
    if not value or value != value.strip() or len(value) > limit:
        raise ValueError(f"{name} is invalid")
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in value):
        raise ValueError(f"{name} is invalid")
    return value


@dataclass(frozen=True)
class MutationContext:
    actor: str
    request_id: str
    reason: str = ""
    idempotency_key: str = ""
    started_at: float = field(default_factory=time.perf_counter)

    def __post_init__(self) -> None:
        _bounded(self.actor, "actor", _ACTOR_LIMIT)
        _bounded(self.request_id, "request_id", _IDENTITY_LIMIT)
        _bounded(
            self.idempotency_key,
            "idempotency_key",
            _IDENTITY_LIMIT,
            optional=True,
        )
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be non-empty")


class AuditRecorder:
    """Persist an additional boundary receipt using fixed, redacted detail."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def record(
        self,
        context: MutationContext,
        action: str,
        target_type: str,
        target_id: str,
        result_code: str,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        elapsed_ms = max(
            0,
            round((time.perf_counter() - context.started_at) * 1000),
        )
        detail_json = redact(
            json.dumps(detail or {}, sort_keys=True, default=str)
        )
        with self.session_factory() as session:
            session.add(
                AuditEvent(
                    actor=context.actor,
                    action=action[:64],
                    target_type=target_type[:32],
                    target_id=target_id[:64],
                    request_id=context.request_id,
                    idempotency_key=context.idempotency_key,
                    reason=redact(context.reason),
                    result_code=result_code[:64],
                    latency_ms=elapsed_ms,
                    detail_json=detail_json,
                )
            )
            session.commit()


def mark_http_mutation(
    request,
    *,
    actor: str,
    reason: str,
    action: str,
    target_type: str,
    target_id: str,
) -> MutationContext:
    context = MutationContext(
        actor=actor,
        request_id=request.state.request_id,
        reason=reason,
        idempotency_key=getattr(
            request.state,
            "idempotency_key",
            "",
        ),
    )
    request.state.mutation_receipt = (
        context,
        action,
        target_type,
        str(target_id),
    )
    return context
