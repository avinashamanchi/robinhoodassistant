"""Repository-produced, encrypted lifecycle proofs for replay validation.

The proof payload contains only ordinary lifecycle columns plus SHA-256
digests of encrypted sensitive-field envelopes.  It never contains decrypted
approval or proposal text.  Because the payload is itself stored in a
row-bound AES-GCM audit field, direct target-row edits cannot manufacture a
matching proof through ordinary ORM writes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trading_assistant.db.models import (
    AuditEvent,
    Fill,
    Order,
    Proposal,
    Rule,
    RuleGroup,
)
from trading_assistant.security.sensitive_fields import sensitive_store


LIFECYCLE_PROOF_SCHEMA = 1
_SUPPORTED_TARGETS = frozenset({"order", "rule", "rule_group"})


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if not value.is_finite():
        return "invalid"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _envelope_hash(value: object) -> str:
    if not isinstance(value, str):
        return "invalid"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _proposal_snapshot(proposal: Proposal) -> dict[str, Any]:
    return {
        "id": proposal.id,
        "order_id": proposal.order_id,
        "source_rule_group_id": proposal.source_rule_group_id,
        "source_rule_id": proposal.source_rule_id,
        "plan_generation": proposal.plan_generation,
        "ttl_minutes": proposal.ttl_minutes,
        "created_at": _timestamp(proposal.created_at),
        "expires_at": _timestamp(proposal.expires_at),
        "reasoning_envelope_hash": _envelope_hash(
            proposal.reasoning
        ),
    }


def _fill_snapshot(fill: Fill) -> dict[str, Any]:
    return {
        "id": fill.id,
        "order_id": fill.order_id,
        "ticker": fill.ticker,
        "side": fill.side,
        "qty": _decimal(fill.qty),
        "price": _decimal(fill.price),
        "broker_fill_id": fill.broker_fill_id,
        "reconciliation_state": fill.reconciliation_state,
        "filled_at": _timestamp(fill.filled_at),
    }


def order_lifecycle_snapshot(
    session: Session,
    order_id: int,
) -> dict[str, Any] | None:
    session.flush()
    order = session.get(Order, order_id, populate_existing=True)
    if order is None:
        return None
    proposals = list(
        session.scalars(
            select(Proposal)
            .where(Proposal.order_id == order.id)
            .order_by(Proposal.id)
        )
    )
    fills = list(
        session.scalars(
            select(Fill)
            .where(Fill.order_id == order.id)
            .order_by(Fill.id)
        )
    )
    return {
        "order": {
            "id": order.id,
            "idempotency_key": order.idempotency_key,
            "ticker": order.ticker,
            "side": order.side,
            "order_type": order.order_type,
            "qty": _decimal(order.qty),
            "notional": _decimal(order.notional),
            "limit_price": _decimal(order.limit_price),
            "status": order.status,
            "broker_order_id": order.broker_order_id,
            "approval_actor": order.approval_actor,
            "approval_reason_envelope_hash": _envelope_hash(
                order.approval_reason
            ),
            "approved_at": _timestamp(order.approved_at),
            "submission_kind": order.submission_kind,
            "submission_payload_json": order.submission_payload_json,
            "submission_attempt": order.submission_attempt,
            "submission_started_at": _timestamp(
                order.submission_started_at
            ),
            "acceptance_state": order.acceptance_state,
            "last_reconciled_at": _timestamp(
                order.last_reconciled_at
            ),
            "last_error_code": order.last_error_code,
            "plan_cancel_state": order.plan_cancel_state,
            "version": order.version,
            "created_at": _timestamp(order.created_at),
            "updated_at": _timestamp(order.updated_at),
        },
        "proposals": [
            _proposal_snapshot(proposal) for proposal in proposals
        ],
        "fills": [_fill_snapshot(fill) for fill in fills],
    }


def rule_group_lifecycle_snapshot(
    session: Session,
    group_id: int,
) -> dict[str, Any] | None:
    session.flush()
    group = session.get(
        RuleGroup,
        group_id,
        populate_existing=True,
    )
    if group is None:
        return None
    return {
        "id": group.id,
        "group_key": group.group_key,
        "state": group.state,
        "lease_owner": group.lease_owner,
        "lease_expires_at": _timestamp(group.lease_expires_at),
        "terminal_rule_id": group.terminal_rule_id,
        "version": group.version,
        "reconciliation_required": group.reconciliation_required,
        "created_at": _timestamp(group.created_at),
        "updated_at": _timestamp(group.updated_at),
    }


def rule_lifecycle_snapshot(
    session: Session,
    rule_id: int,
) -> dict[str, Any] | None:
    session.flush()
    rule = session.get(Rule, rule_id, populate_existing=True)
    if rule is None:
        return None
    return {
        "id": rule.id,
        "group_id": rule.group_id,
        "payload_version": rule.payload_version,
        "ticker": rule.ticker,
        "condition_json": rule.condition_json,
        "action_json": rule.action_json,
        "state": rule.state,
        "created_at": _timestamp(rule.created_at),
        "plan_id": rule.plan_id,
        "kind": rule.kind,
        "fraction": _decimal(rule.fraction),
        "hwm": _decimal(rule.hwm),
        "deadline": _timestamp(rule.deadline),
        "pre_approved": rule.pre_approved,
        "activation": rule.activation,
        "terminal_on_trigger": rule.terminal_on_trigger,
    }


def lifecycle_snapshot(
    session: Session,
    target_type: str,
    target_id: int,
) -> dict[str, Any] | None:
    if target_type == "order":
        return order_lifecycle_snapshot(session, target_id)
    if target_type == "rule_group":
        return rule_group_lifecycle_snapshot(session, target_id)
    if target_type == "rule":
        return rule_lifecycle_snapshot(session, target_id)
    return None


def augment_lifecycle_detail(
    session: Session,
    *,
    target_type: str,
    target_id: int | str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged = dict(detail or {})
    if target_type not in _SUPPORTED_TARGETS:
        return merged
    try:
        normalized_target_id = int(target_id)
    except (TypeError, ValueError):
        return merged
    snapshot = lifecycle_snapshot(
        session,
        target_type,
        normalized_target_id,
    )
    if snapshot is not None:
        merged["lifecycle_proof"] = {
            "schema": LIFECYCLE_PROOF_SCHEMA,
            "target_type": target_type,
            "target_id": normalized_target_id,
            "snapshot": snapshot,
        }
    return merged


def augment_lifecycle_detail_json(
    session: Session,
    *,
    target_type: str,
    target_id: int | str,
    detail_json: str,
) -> str:
    if target_type not in _SUPPORTED_TARGETS:
        return detail_json
    try:
        detail = json.loads(detail_json)
    except (TypeError, json.JSONDecodeError):
        detail = None
    if not isinstance(detail, dict):
        detail = {}
    return json.dumps(
        augment_lifecycle_detail(
            session,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
        ),
        separators=(",", ":"),
        sort_keys=True,
    )


def read_lifecycle_detail(
    session: Session,
    session_factory: sessionmaker[Session],
    event: AuditEvent,
) -> dict[str, Any] | None:
    try:
        detail = json.loads(
            sensitive_store(
                session,
                session_factory,
            ).read(event, "detail_json")
        )
    except Exception:
        return None
    return detail if isinstance(detail, dict) else None


def latest_lifecycle_event(
    session: Session,
    session_factory: sessionmaker[Session],
    *,
    target_type: str,
    target_id: int,
    action: str | None = None,
) -> tuple[AuditEvent, dict[str, Any]] | None:
    statement = select(AuditEvent).where(
        AuditEvent.target_type == target_type,
        AuditEvent.target_id == str(target_id),
    )
    if action is not None:
        statement = statement.where(AuditEvent.action == action)
    events = list(
        session.scalars(statement.order_by(AuditEvent.id.desc()))
    )
    for event in events:
        detail = read_lifecycle_detail(
            session,
            session_factory,
            event,
        )
        proof = (
            detail.get("lifecycle_proof")
            if isinstance(detail, dict)
            else None
        )
        if (
            isinstance(proof, dict)
            and proof.get("schema") == LIFECYCLE_PROOF_SCHEMA
            and proof.get("target_type") == target_type
            and proof.get("target_id") == target_id
        ):
            return event, detail
        if action is not None:
            return None
    return None


def lifecycle_proof_matches(
    session: Session,
    session_factory: sessionmaker[Session],
    *,
    target_type: str,
    target_id: int,
) -> bool:
    resolved = latest_lifecycle_event(
        session,
        session_factory,
        target_type=target_type,
        target_id=target_id,
    )
    if resolved is None:
        return False
    _event, detail = resolved
    proof = detail.get("lifecycle_proof")
    current = lifecycle_snapshot(
        session,
        target_type,
        target_id,
    )
    return bool(
        isinstance(proof, dict)
        and proof.get("schema") == LIFECYCLE_PROOF_SCHEMA
        and proof.get("target_type") == target_type
        and proof.get("target_id") == target_id
        and current is not None
        and proof.get("snapshot") == current
    )
