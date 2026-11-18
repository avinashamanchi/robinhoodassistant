"""Read verified campaign transitions from existing encrypted evidence only."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import re

from sqlalchemy import select


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.utcoffset() is None:
            raise ValueError("naive source timestamp")
    if not isinstance(value, datetime):
        raise ValueError("invalid timestamp")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _unknown():
    return {"outcome": "unknown"}


def _proof(transition, observed_at, reference):
    return {"outcome": "pass", "transition": transition, "observed_at": _utc(observed_at).isoformat(),
            "reference": "sha256:" + hashlib.sha256(reference.encode()).hexdigest()}


def read_lifecycle(factory, cycles, scope, start, now):
    """Verify a scoped paper cycle's filled order against its encrypted proof."""
    from ..db.lifecycle_proofs import latest_lifecycle_event, lifecycle_proof_matches
    from ..db.models import AuditEvent, Fill, Order, fill_has_trusted_identity
    from ..autopilot.evidence import cycle_key
    from ..autopilot.identity import evidence_fingerprint
    if scope.get("mode") != "paper":
        return _unknown()
    try:
        candidates = []
        for cycle in cycles:
            detail = cycle.detail
            if cycle.result_code != "executed" or detail.get("execution") != "paper" or any(detail.get(k) != v for k, v in scope.items()):
                continue
            if _utc(cycle.created_at) > _utc(now):
                return _unknown()
            session_date = date.fromisoformat(detail["session"])
            if cycle.idempotency_key != cycle_key(scope["mode"], session_date, evidence_fingerprint(scope["config_fingerprint"], scope["code_fingerprint"])):
                continue
            version = detail.get("code_identity")
            if not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{40}", version):
                continue
            started, finished = _utc(detail["started_at"]), _utc(detail["finished_at"])
            if not _utc(start) <= started <= finished <= _utc(cycle.created_at) <= _utc(now):
                continue
            orders = detail.get("orders")
            if not isinstance(orders, list) or len(orders) > 100:
                continue
            candidates.extend((item.get("order_id"), started, version) for item in orders if isinstance(item, dict) and type(item.get("order_id")) is int and item["order_id"] > 0)
        if len(candidates) > 100:
            return _unknown()
        with factory() as session:
            for order_id, started, version in candidates[-1:]:
                order = session.get(Order, order_id)
                if order is None or order.status != "filled" or order.acceptance_state != "accepted" or not order.broker_order_id:
                    continue
                resolved = latest_lifecycle_event(session, factory, target_type="order", target_id=order_id)
                if resolved is None:
                    continue
                event, _ = resolved
                latest_id = session.scalar(select(AuditEvent.id).where(AuditEvent.target_type == "order", AuditEvent.target_id == str(order_id)).order_by(AuditEvent.id.desc()).limit(1))
                if event.id != latest_id or not started <= _utc(event.created_at) <= _utc(now) or not lifecycle_proof_matches(session, factory, target_type="order", target_id=order_id):
                    continue
                fills = list(session.scalars(select(Fill).where(Fill.order_id == order_id)))
                if not fills or len(fills) > 100 or any(not fill_has_trusted_identity(f) or f.ticker != order.ticker or f.side != order.side or not _utc(started) <= _utc(f.filled_at) <= _utc(event.created_at) or f.qty <= 0 or f.price <= 0 for f in fills):
                    continue
                total = sum((f.qty for f in fills), Decimal(0))
                if order.qty is not None and total != order.qty:
                    continue
                result = _proof("paper_order_filled_exact", event.created_at, f"order-lifecycle:{event.id}:{order_id}:{event.detail_json}")
                result["code_version"] = version
                return result
    except Exception:
        return _unknown()
    return _unknown()


def read_recovery(factory, start, now):
    """Verify a guarded global/drift reset and current exact breaker generation."""
    from ..db.lifecycle_proofs import read_lifecycle_detail
    from ..db.models import AuditEvent, CircuitBreakerState
    try:
        with factory() as session:
            if session.scalar(select(CircuitBreakerState.scope_key).where(CircuitBreakerState.tripped.is_(True)).limit(1)) is not None:
                return _unknown()
            events = session.scalars(select(AuditEvent).where(AuditEvent.target_type == "circuit_breaker",
                AuditEvent.target_id.in_(("operator_global", "broker_drift")), AuditEvent.action == "circuit_breaker.reset")
                .order_by(AuditEvent.id.desc()).limit(50)).all()
            if events and _utc(events[0].created_at) > _utc(now):
                return _unknown()
            for event in events:
                when = _utc(event.created_at)
                if not _utc(start) <= when <= _utc(now) or event.result_code != "reset":
                    continue
                latest = session.scalar(select(AuditEvent.id).where(AuditEvent.target_type == "circuit_breaker", AuditEvent.target_id == event.target_id,
                    AuditEvent.action.in_(("circuit_breaker.reset", "circuit_breaker.trip"))).order_by(AuditEvent.id.desc()).limit(1))
                row = session.get(CircuitBreakerState, event.target_id)
                detail = read_lifecycle_detail(session, factory, event)
                if latest != event.id or row is None or row.tripped or not isinstance(detail, dict):
                    continue
                expected, generation, health = detail.get("expected_generation"), detail.get("generation"), detail.get("prior_health")
                if type(expected) is not int or expected < 1 or type(generation) is not int or generation != expected + 1 or row.generation != generation or row.kind != event.target_id or row.target != "" or _utc(row.updated_at) != when or not isinstance(health, dict):
                    continue
                health_at = _utc(health.get("captured_at"))
                if not 0 <= (when - health_at).total_seconds() <= 300 or health.get("order_reconciliation") != "clean" or health.get("position_reconciliation") != "clean":
                    continue
                if event.target_id == "operator_global" and (type(health.get("remote_open_orders")) is not int or health["remote_open_orders"] != 0 or health.get("local_enumeration") != "confirmed" or health.get("other_active_breakers") != []):
                    continue
                return _proof(event.target_id + "_reset", when, f"breaker-reset:{event.id}:{generation}:{event.detail_json}")
    except Exception:
        return _unknown()
    return _unknown()


def read_mock_drill(root, commit, now):
    """Attest the importer-validated CI mock step, not a local execution."""
    from .release_evidence import read_report_bytes, decode_report, validate_report
    try:
        directory = root / ".local/verification"
        raw = read_report_bytes(directory / "release-results.json")
        report = validate_report(decode_report(raw), expected_commit=commit, root=root)
        digest = hashlib.sha256(raw).hexdigest()
        provenance = decode_report(read_report_bytes(directory / f"provenance-{digest}.json"))
        run = provenance.get("github_run_id")
        repo = "avinashamanchi/robinhoodassistant"
        if not (provenance.get("source") == "github_actions_artifact" and provenance.get("report_sha256") == digest
                and provenance.get("repository") == repo and provenance.get("workflow_path") == ".github/workflows/ci.yml"
                and type(run) is int and run > 0 and type(provenance.get("github_run_attempt")) is int and provenance["github_run_attempt"] > 0
                and type(provenance.get("artifact_id")) is int and provenance["artifact_id"] > 0
                and re.fullmatch(r"sha256:[a-f0-9]{64}", str(provenance.get("artifact_digest")))
                and provenance.get("run_url") == f"https://github.com/{repo}/actions/runs/{run}"):
            return _unknown()
        at = _utc(report["finished_at"])
        if at > _utc(now):
            return _unknown()
        result = _proof("ci_isolated_mock_drill", at, "release-report:" + digest)
        result["reference"] = "sha256:" + digest
        return result
    except Exception:
        return _unknown()
