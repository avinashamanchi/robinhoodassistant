"""Private operational campaign evidence; this module has no trade authority."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re

NAME = "acceptance-campaign"
MAX_RECEIPTS = 192
CHECK_METHODS = {
    "broker_reconciliation": {"persisted_broker_reconciliation", "real_broker_read"},
    "broker_order_reconciliation": {"persisted_broker_reconciliation"},
    "broker_position_reconciliation": {"persisted_broker_reconciliation"},
    "runtime_health": {"real_local_runtime"},
    "backup": {"real_local_backup"},
    "safety_drill": {"isolated_mock_drill"},
    "lifecycle": {"persisted_broker_lifecycle"},
    "recovery": {"real_local_runtime"},
    "restart": {"real_local_runtime"},
}
SOURCES = {"daemon_reconciliation", "audit_reconciliation", "runtime_doctor", "backup", "safety_drill", "operator", "broker"}


def _time(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.utcoffset() is None:
        raise ValueError("aware timestamp required")
    return result.astimezone(timezone.utc)


def _scope(scope):
    if set(scope) != {"mode", "config_fingerprint", "code_fingerprint"} or scope["mode"] not in {"off", "observe", "paper", "propose"}:
        raise ValueError("invalid campaign scope")
    for field in ("config_fingerprint", "code_fingerprint"):
        if not re.fullmatch(r"[a-f0-9]{64}", scope[field]):
            raise ValueError("invalid fingerprint")
    return dict(scope)


def _state_ref(state):
    from ..security.crypto import SensitiveFieldRef
    identity = json.dumps({"started_at": state["started_at"], "ends_at": state["ends_at"], "scope": _scope(state["scope"])}, sort_keys=True, separators=(",", ":"))
    return SensitiveFieldRef("operational_campaign", hashlib.sha256(identity.encode()).hexdigest(), "state_attestation", 1)


def seal_campaign(state, cipher):
    from .state_attestation import seal_mapping
    return seal_mapping(state, cipher, _state_ref(state))


def verify_campaign(state, cipher):
    try:
        from .state_attestation import verify_mapping
        return verify_mapping(state, cipher, _state_ref(state))
    except Exception:
        return False


RECEIPT_SOURCES = {
    "broker_reconciliation": ("persisted_broker_reconciliation", "daemon_reconciliation"),
    "broker_order_reconciliation": ("persisted_broker_reconciliation", "daemon_reconciliation"),
    "broker_position_reconciliation": ("persisted_broker_reconciliation", "audit_reconciliation"),
    "runtime_health": ("real_local_runtime", "runtime_doctor"), "backup": ("real_local_backup", "backup"),
    "safety_drill": ("isolated_mock_drill", "validated_ci_handoff"),
    "lifecycle": ("persisted_broker_lifecycle", "encrypted_lifecycle"),
    "recovery": ("real_local_runtime", "encrypted_breaker_audit"), "restart": ("real_local_runtime", "validated_restart"),
}


def validated_receipt(value, scope):
    """Return only the fixed bounded receipt schema, or no evidence."""
    try:
        required = {"schema_version", "check", "method", "source", "outcome", "observed_at", "captured_at", "scope", "reference"}
        optional = {"details", "transition", "time_basis", "code_version", "orders", "observation_auth", "code"}
        if not isinstance(value, dict) or not required <= set(value) or set(value) - required - optional or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            return None
        name = value["check"]
        if name not in RECEIPT_SOURCES or (value["method"], value["source"]) != RECEIPT_SOURCES[name] or value["scope"] != scope or value["outcome"] not in {"pass", "fail", "unknown"}:
            return None
        if "observation_auth" in value and (name != "runtime_health" or value["observation_auth"] not in {"doctor_attested", "conservative_unverified"}):
            return None
        if name == "runtime_health" and value["outcome"] == "pass" and value.get("observation_auth") != "doctor_attested":
            return None
        if "code" in value:
            backup_codes = {"backup_recent": "pass", "backup_missing": "fail", "backup_size_unsupported": "unknown", "backup_scan_limit": "unknown", "backup_read_unknown": "unknown", "backup_clock_unknown": "unknown"}
            if name != "backup" or backup_codes.get(value["code"]) != value["outcome"]:
                return None
        if not isinstance(value["reference"], str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", value["reference"]):
            return None
        if _time(value["observed_at"]) > _time(value["captured_at"]):
            return None
        transitions = {"lifecycle": {"paper_order_filled_exact"}, "recovery": {"operator_global_reset", "broker_drift_reset"}, "safety_drill": {"ci_isolated_mock_drill"}, "restart": set()}
        if value["outcome"] == "pass" and name in transitions and value.get("transition") not in transitions[name]:
            return None
        if "transition" in value and value["transition"] not in transitions.get(name, set()) | {"unknown"}:
            return None
        if "time_basis" in value and value["time_basis"] not in {"event_time", "capture_time", "verifier_finish_before_ci_drill"}:
            return None
        if value["outcome"] == "pass" and name in transitions:
            expected_basis = "verifier_finish_before_ci_drill" if name == "safety_drill" else "event_time"
            if value.get("time_basis") != expected_basis:
                return None
        if "code_version" in value and value["code_version"] != "unknown" and not re.fullmatch(r"[a-f0-9]{40}", value["code_version"]):
            return None
        if "details" in value:
            if name != "runtime_health":
                return None
            from .incident_catalog import COMPONENTS
            details = value["details"]
            if not isinstance(details, list) or len(details) > 32 or any(not isinstance(x, dict) or set(x) != {"component", "code", "status"} or x["component"] not in COMPONENTS or x["code"] not in COMPONENTS[x["component"]] or x["status"] not in {"pass", "warn", "fail", "unknown"} for x in details):
                return None
        if "orders" in value:
            if name != "lifecycle":
                return None
            orders = value["orders"]
            if not isinstance(orders, list) or len(orders) > 100:
                return None
            for x in orders:
                if not isinstance(x, dict) or set(x) - {"order_digest", "outcome", "transition", "observed_at", "reference", "code_version"} or not isinstance(x.get("order_digest"), str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", x["order_digest"]) or x.get("outcome") not in {"pass", "unknown"}:
                    return None
                if x["outcome"] == "unknown" and set(x) != {"order_digest", "outcome"}:
                    return None
                if x["outcome"] == "pass" and set(x) != {"order_digest", "outcome", "transition", "observed_at", "reference", "code_version"}:
                    return None
                if x["outcome"] == "pass" and (x.get("transition") != "paper_order_filled_exact" or not isinstance(x.get("reference"), str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", x["reference"]) or not isinstance(x.get("code_version"), str) or not re.fullmatch(r"[a-f0-9]{40}", x["code_version"]) or _time(x.get("observed_at")) > _time(value["captured_at"])):
                    return None
        if len(json.dumps(value, allow_nan=False).encode()) > 32_000:
            return None
        return dict(value)
    except (TypeError, ValueError, KeyError):
        return None


def start_campaign(store, now, scope, *, cipher=None):
    at, scope = _time(now), _scope(scope)
    def initialize(old):
        if old:
            if old.get("schema_version") != 1:
                raise ValueError("unknown campaign schema")
            if cipher is not None and not verify_campaign(old, cipher):
                raise ValueError("unverified campaign state")
            return old
        result = {"schema_version": 1, "started_at": at.isoformat(), "ends_at": (at + timedelta(days=30)).isoformat(),
                "scope": scope, "receipts": [], "discarded_receipts": 0, "calendar": None}
        return seal_campaign(result, cipher) if cipher is not None else result
    return store.update(NAME, initialize)


def bound_campaign_history(state, *, limit=220_000):
    """Keep the private store bounded and make every dropped record explicit."""
    value = {**state, "receipts": list(state["receipts"]), "observations": list(state["observations"]), "cycles": list(state["cycles"]),
             "post_window_receipts": list(state.get("post_window_receipts", []))}
    while len((json.dumps(value, allow_nan=False, sort_keys=True) + "\n").encode()) > limit:
        if value["post_window_receipts"]:
            value["post_window_receipts"].pop(0)
            value["discarded_post_window_receipts"] = value.get("discarded_post_window_receipts", 0) + 1
        elif value["receipts"]:
            value["receipts"].pop(0)
            value["discarded_receipts"] += 1
        elif value["observations"]:
            value["observations"].pop(0)
            value["discarded_observations"] += 1
        elif value["cycles"]:
            value["cycles"].pop(0)
            value["evidence_complete"] = False
            value["discarded_cycles"] = value.get("discarded_cycles", 0) + 1
        else:
            raise ValueError("campaign metadata exceeds bound")
    return value


def validate_calendar(calendar):
    if not isinstance(calendar, dict) or set(calendar) != {"start", "end", "source", "sessions"} or calendar["source"] not in {"alpaca_calendar", "mock_calendar"}:
        raise ValueError("invalid calendar")
    first, last = date.fromisoformat(calendar["start"]), date.fromisoformat(calendar["end"])
    if first > last or (last-first).days > 31 or not isinstance(calendar["sessions"], list):
        raise ValueError("invalid calendar range")
    seen = set()
    for item in calendar["sessions"]:
        if set(item) != {"date", "open", "close"}:
            raise ValueError("invalid session")
        day = date.fromisoformat(item["date"])
        opened, closed = _time(item["open"]), _time(item["close"])
        from zoneinfo import ZoneInfo
        if day in seen or not first <= day <= last or opened >= closed or opened.astimezone(ZoneInfo("America/New_York")).date() != day or closed.astimezone(ZoneInfo("America/New_York")).date() != day:
            raise ValueError("invalid session")
        seen.add(day)
    return calendar


def reconciliation_outcome(row, now, max_age_seconds):
    from ..orders.startup import validate_startup_reconciliation_snapshot
    if row is None:
        return "unknown"
    validation = validate_startup_reconciliation_snapshot(
        generation=row.generation, completed_generation=row.completed_generation, status=row.status,
        started_at=row.started_at, completed_at=row.completed_at, updated_at=row.updated_at, observed_at=_time(now))
    if not validation.valid:
        return "unknown"
    if not validation.current:
        return "fail"
    return "pass" if validation.completed_at is not None and 0 <= (_time(now) - validation.completed_at).total_seconds() <= max_age_seconds else "unknown"


def read_reconciliation(session_factory, broker, now, max_age_seconds):
    from sqlalchemy import select
    from ..db.models import StartupReconciliationState
    with session_factory() as session:
        row = session.execute(select(StartupReconciliationState.status, StartupReconciliationState.generation,
            StartupReconciliationState.completed_generation, StartupReconciliationState.started_at,
            StartupReconciliationState.completed_at, StartupReconciliationState.updated_at)
            .where(StartupReconciliationState.broker == broker).limit(1)).one_or_none()
        outcome = reconciliation_outcome(row, now, max_age_seconds)
        # Legacy startup rows do not bind the broker observation to the producer contract.
        # Individual encrypted order/position observations below are the supported proof.
        if outcome == "pass":
            outcome = "unknown"
        evidence = {"status": row.status, "generation": row.generation, "completed_generation": row.completed_generation,
                    "started_at": str(row.started_at), "completed_at": str(row.completed_at), "updated_at": str(row.updated_at)} if row else {"status": "missing"}
    source_at = (row.completed_at if row.status == "current" else row.updated_at) if row else None
    return outcome, hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest(), source_at


def read_daemon_order_reconciliation(session_factory, now, max_age_seconds):
    """Read the latest daemon order-sync result, never an older successful result."""
    return _read_reconciliation_audit(session_factory, now, max_age_seconds, "orders.sync", ("daemon:monitor", "daemon:startup"), "reconciliation_required")


def read_position_reconciliation(session_factory, now, max_age_seconds):
    return _read_reconciliation_audit(session_factory, now, max_age_seconds, "positions.reconcile", None, "drift_detected")


def reconciliation_receipt(name, reader, at, scope):
    specs = {"broker_reconciliation": ("persisted_broker_reconciliation", "daemon_reconciliation", "reconciliation_unavailable"),
             "broker_order_reconciliation": ("persisted_broker_reconciliation", "daemon_reconciliation", "order_reconciliation_unavailable"),
             "broker_position_reconciliation": ("persisted_broker_reconciliation", "audit_reconciliation", "position_reconciliation_unavailable"),
             "backup": ("real_local_backup", "backup", "backup_unavailable")}
    method, source, failure = specs[name]
    result = None
    try:
        result = reader(at)
        outcome, digest, source_at = result[:3]
        if source_at is None or outcome not in {"pass", "fail", "unknown"} or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("invalid reconciliation observation")
        source_at = _time(source_at.replace(tzinfo=timezone.utc) if source_at.tzinfo is None else source_at)
        if source_at > at:
            raise ValueError("future reconciliation observation")
    except Exception:
        outcome, source_at = "unknown", at
        digest = hashlib.sha256(failure.encode()).hexdigest()
    receipt = {"check": name, "method": method, "source": source, "outcome": outcome,
               "observed_at": source_at.isoformat(), "captured_at": at.isoformat(), "scope": scope, "reference": "sha256:" + digest}
    if name == "backup":
        allowed = {"pass": {"backup_recent"}, "fail": {"backup_missing"}, "unknown": {"backup_size_unsupported", "backup_scan_limit", "backup_read_unknown", "backup_clock_unknown"}}
        code = result[3] if isinstance(result, (tuple, list)) and len(result) > 3 else {"pass": "backup_recent", "fail": "backup_missing", "unknown": "backup_read_unknown"}[outcome]
        valid_code = isinstance(code, str) and code in allowed[outcome]
        receipt["code"] = code if valid_code else "backup_read_unknown"
        if not valid_code:
            receipt["outcome"] = "unknown"
    return receipt


def read_backup_artifacts(root, at, *, max_total_bytes=512 * 1024 * 1024):
    """Inspect bounded committed backup headers; this is not a restore check."""
    import os
    from .backup import read_encrypted_backup_header
    directory = root / ".local/encrypted-backups"
    outcome, instants, code = "unknown", [], "backup_read_unknown"
    try:
        if directory.is_symlink():
            outcome = "unknown"
        elif not directory.exists():
            outcome, code = "fail", "backup_missing"
        elif not directory.is_dir():
            outcome = "unknown"
        else:
            candidates = []
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= 512:
                        code = "backup_scan_limit"
                        raise ValueError("backup directory exceeds inspection bound")
                    if entry.name.endswith("-whole-database-v1.sqlite3.aesgcm"):
                        candidates.append(directory / entry.name)
            if len(candidates) > 128:
                code = "backup_scan_limit"
                raise ValueError("backup candidates exceed inspection bound")
            total = 0
            for path in candidates:
                size = path.lstat().st_size
                total += size
                if total > max_total_bytes:
                    code = "backup_size_unsupported"
                    raise ValueError("backup bytes exceed inspection bound")
                header = read_encrypted_backup_header(path, max_artifact_bytes=max_total_bytes - (total - size))
                instants.append(_time(header["created_at"]))
            outcome = "unknown" if any(stamp > at for stamp in instants) else "pass" if any(at - stamp <= timedelta(hours=36) for stamp in instants) else "fail"
            code = {"unknown": "backup_clock_unknown", "pass": "backup_recent", "fail": "backup_missing"}[outcome]
    except Exception:
        outcome = "unknown"
    evidence = {"check": "committed_backup_headers", "outcome": outcome, "code": code,
                "created_at": sorted(t.isoformat() for t in instants)}
    return outcome, hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest(), at, code


def _read_reconciliation_audit(session_factory, now, max_age_seconds, action, actors, failure_code):
    from sqlalchemy import select, func
    from ..db.models import AuditEvent
    from ..security.sensitive_fields import sensitive_store
    query = select(AuditEvent.id, func.length(AuditEvent.detail_json)).where(AuditEvent.action == action)
    if actors is not None:
        query = query.where(AuditEvent.actor.in_(actors))
    with session_factory() as session:
        if session.bind.dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN")
        latest = session.execute(query.order_by(AuditEvent.id.desc()).limit(1)).one_or_none()
        row = session.get(AuditEvent, latest[0]) if latest is not None and type(latest[1]) is int and latest[1] <= 65536 else None
        try:
            detail = json.loads(sensitive_store(session, session_factory).read(row, "detail_json")) if row else {}
            metadata = detail.get("campaign_observation")
            target = "broker_orders:all" if action == "orders.sync" else "portfolio:all"
            actual_target = f"{row.target_type}:{row.target_id}"
            event_at = row.created_at.replace(tzinfo=timezone.utc) if row.created_at.tzinfo is None else row.created_at
            expected = {"schema": 1, "action": action, "target": target, "broker": "alpaca", "configured_broker": "alpaca",
                        "mode": "paper", "result": row.result_code, "request_id": row.request_id, "observed_at": event_at.isoformat()}
            valid = (actual_target == target and type(metadata) is dict and type(metadata.get("schema")) is int
                     and isinstance(row.request_id, str) and bool(row.request_id.strip()) and metadata == expected)
            if action == "orders.sync":
                unresolved, drift = detail.get("unresolved_unknown"), detail.get("broker_drift")
                failed, cancellations = detail.get("campaign_failed_count"), detail.get("campaign_plan_cancel_failures")
                valid = valid and isinstance(unresolved, list) and isinstance(drift, list) and type(failed) is int and type(cancellations) is int and failed >= 0 and cancellations >= 0 and failed == len(unresolved) + len(drift) + cancellations and row.result_code == ("reconciled" if failed == 0 else "reconciliation_required")
            else:
                drift = detail.get("drift")
                valid = valid and isinstance(drift, dict) and row.result_code == ("drift_detected" if drift else "reconciled")
        except Exception:
            valid = False
    evidence = {"id": row.id, "created_at": str(row.created_at), "result_code": row.result_code,
                "envelope_digest": hashlib.sha256(row.detail_json.encode()).hexdigest()} if row else {"status": "missing"}
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    if row is None or not valid:
        return "unknown", digest, None
    recorded = row.created_at.replace(tzinfo=timezone.utc) if row.created_at.tzinfo is None else row.created_at
    age = (_time(now) - _time(recorded)).total_seconds()
    if not 0 <= age <= max_age_seconds:
        return "unknown", digest, recorded
    if row.result_code == "reconciled":
        return "pass", digest, recorded
    if row.result_code == failure_code:
        return "fail", digest, recorded
    return "unknown", digest, recorded


def evaluate(campaign, now, calendar, cycles, *, evidence_complete=False, evidence_through=None, delay_minutes=30, receipt_verifier=None):
    now = _time(now)
    start, end = _time(campaign["started_at"]), _time(campaign["ends_at"])
    scope = _scope(campaign["scope"])
    try:
        through = _time(evidence_through) if evidence_through else None
        if through and (through < start or through > now):
            through = None
    except (TypeError, ValueError):
        through = None
    from zoneinfo import ZoneInfo
    zone = ZoneInfo("America/New_York")
    first, last = start.astimezone(zone).date(), (end - timedelta(microseconds=1)).astimezone(zone).date()
    sessions = []
    try:
        cal = validate_calendar(calendar)
        calendar_ok = date.fromisoformat(cal["start"]) <= first and date.fromisoformat(cal["end"]) >= last and cal["source"] == "alpaca_calendar" and campaign.get("calendar_revisions", 0) == 0
    except (ValueError, TypeError, KeyError):
        cal, calendar_ok = None, False
    for entry in sorted((cal or {}).get("sessions", []), key=lambda x: x["date"]):
        opened, closed = _time(entry["open"]), _time(entry["close"])
        if not first <= date.fromisoformat(entry["date"]) <= last or closed <= start or opened >= end:
            continue
        matching = [x for x in cycles if x.get("session") == entry["date"] and all(x.get(k) == v for k, v in scope.items())]
        unique = {x.get("key"): x for x in matching if x.get("key")}
        results = {x.get("result") for x in matching if x.get("key")}
        if cal["source"] != "alpaca_calendar" or campaign.get("calendar_revisions", 0):
            status = "unknown"
        elif len(results) > 1:
            status = "unknown"
        elif results:
            result = next(iter(results))
            status = {"blocked": "blocked", "failed": "failed", "observed": "completed", "executed": "completed", "proposed": "completed", "degraded": "completed"}.get(result, "unknown")
        elif scope["mode"] == "off" or opened + timedelta(minutes=delay_minutes) >= min(closed, end) or opened + timedelta(minutes=delay_minutes) < start:
            status = "not_scheduled"
        elif now < opened + timedelta(minutes=delay_minutes):
            status = "not_yet_due"
        elif not evidence_complete or through is None:
            status = "unknown"
        elif now < min(closed, end):
            status = "due"
        elif through < min(closed, end):
            status = "unknown"
        else:
            status = "missed"
        sessions.append({"date": entry["date"], "status": status, "evidence_count": len(unique), "results": sorted(str(x) for x in results),
                         "execution_kinds": sorted({x.get("execution", "unknown") for x in matching})})
    checks, latest_receipts, safe_receipts = {}, {}, []
    original = campaign.get("receipts", [])
    post_window = campaign.get("post_window_receipts", [])
    raw_receipts = original + post_window if isinstance(original, list) and isinstance(post_window, list) and len(original) + len(post_window) <= 256 else []
    try:
        rollback = bool(campaign.get("captured_at") and now < _time(campaign["captured_at"]))
    except (TypeError, ValueError):
        rollback = True
    for raw in raw_receipts:
        candidate = validated_receipt(raw, scope)
        if candidate is not None and receipt_verifier is not None and receipt_verifier(candidate):
            source_time = _time(candidate["observed_at"])
            safe_receipts.append({**candidate, "period_scope": "post_window" if _time(candidate["captured_at"]) >= end else "campaign",
                                  "event_scope": "historical" if source_time < start else "post_window" if source_time >= end else "campaign"})
            if _time(candidate["captured_at"]) > now:
                rollback = True
    for name in CHECK_METHODS:
        matching = [r for r in raw_receipts if isinstance(r, dict) and r.get("check") == name]
        receipt = validated_receipt(matching[-1], scope) if matching else None
        if receipt is not None and (receipt_verifier is None or not receipt_verifier(receipt)):
            receipt = None
        if receipt:
            source_time = _time(receipt["observed_at"])
            latest_receipts[name] = {**receipt, "period_scope": "post_window" if _time(receipt["captured_at"]) >= end else "campaign",
                                     "event_scope": "historical" if source_time < start else "post_window" if source_time >= end else "campaign"}
        else:
            latest_receipts[name] = None
        # Current status expires conservatively; history retains its original outcome.
        freshness_time = receipt.get("captured_at", receipt["observed_at"]) if receipt and name in {"lifecycle", "recovery", "safety_drill"} else receipt["observed_at"] if receipt else None
        fresh = receipt is not None and not rollback and not (now >= end and campaign.get("discarded_post_window_receipts", 0)) and start <= _time(receipt["observed_at"]) < end and timedelta(0) <= now - _time(freshness_time) <= timedelta(minutes=3)
        if fresh and name == "safety_drill" and _time(receipt["captured_at"]) >= end:
            fresh = any(r["check"] == name and r["reference"] == receipt["reference"] and _time(r["captured_at"]) < end for r in safe_receipts)
        checks[name] = receipt["outcome"] if fresh and campaign.get("current_scope", scope) == scope else "unknown"
    observations = campaign.get("observations", [])
    times = [_time(o.get("t", o.get("at"))) for o in observations]
    gaps = sum(b <= a or b-a > timedelta(minutes=40) for a, b in zip(times, times[1:]))
    horizon = min(max(now, start), end)
    if times:
        gaps += int(times[0] - start > timedelta(minutes=40))
        gaps += int(horizon - times[-1] > timedelta(minutes=40))
    elif horizon - start > timedelta(minutes=40):
        gaps += 1
    coverage = {"samples": len(observations), "observed_healthy_samples": sum(o.get("h", o.get("health")) == "pass" for o in observations),
                "planned_maintenance_samples": sum(o.get("m", o.get("maintenance")) == "backup_running" for o in observations),
                "unknown_samples": sum(o.get("h", o.get("health")) == "unknown" for o in observations), "gaps": gaps,
                "discarded": campaign.get("discarded_observations", 0)}
    # Samples and isolated checks do not establish uninterrupted uptime.
    current_scope = campaign.get("current_scope", scope)
    return {"started_at": start.isoformat(), "ends_at": end.isoformat(), "scope": scope, "current_scope": current_scope,
            "period_elapsed": now >= end, "remaining_seconds": max(0, int((end-now).total_seconds())),
            "restart_status": "pending",
            "session_accounting_complete": bool(not rollback and now >= end and calendar_ok and evidence_complete and through is not None and through >= end and sessions and all(s["status"] not in {"unknown", "due", "not_yet_due"} for s in sessions) and current_scope == scope),
            "scope_changed": current_scope != scope, "runtime_code": campaign.get("runtime_code", "unknown"), "code_versions": sorted({c.get("code_identity", "unknown") for c in cycles if all(c.get(k) == v for k, v in scope.items())}), "sessions": sessions,
            "calendar_complete": calendar_ok,
            "calendar_revisions": campaign.get("calendar_revisions", 0), "checks": checks, "latest_receipts": latest_receipts,
            "receipts": safe_receipts, "discarded_receipts": campaign.get("discarded_receipts", 0), "discarded_post_window_receipts": campaign.get("discarded_post_window_receipts", 0), "coalesced_receipts": campaign.get("coalesced_receipts", 0),
            "evidence_complete": evidence_complete, "evidence_through": through.isoformat() if through else None,
            "observation_coverage": coverage, "captured_at": campaign.get("captured_at"), "calendar_at": campaign.get("calendar_at"),
            "clock_anomalies": campaign.get("clock_anomalies", 0), "report_clock_rollback": rollback,
            "doctor_stale_observations": campaign.get("doctor_stale_observations"),
            "doctor_discarded_events": campaign.get("doctor_discarded_events"),
            "doctor_legacy_history_unverified": campaign.get("doctor_legacy_history_unverified"),
            "continuous_uptime": "unknown", "complete": False}


def report_for_store(store, now, *, cipher=None):
    try:
        campaign = store.load(NAME)
    except Exception:
        return {"status": "unverified", "complete": False, "restart_status": "pending", "checks": {name: "unknown" for name in CHECK_METHODS}}
    if not campaign:
        return {"status": "not_started", "complete": False, "restart_status": "pending"}
    if not verify_campaign(campaign, cipher):
        return {"status": "unverified", "complete": False, "restart_status": "pending", "checks": {name: "unknown" for name in CHECK_METHODS}}
    return evaluate(campaign, now, campaign.get("calendar"), campaign.get("cycles", []),
                    evidence_complete=campaign.get("evidence_complete", False),
                    evidence_through=campaign.get("evidence_through"),
                    delay_minutes=campaign.get("delay_minutes", 30), receipt_verifier=lambda _: True)


def main():
    from pathlib import Path
    from ..installation import require_designated
    from .operational_state import StateStore
    root = Path(__file__).resolve().parents[3]
    require_designated(root)
    try:
        from ..config import load_config
        from ..security.secrets import load_role_secrets
        from ..security.crypto import build_sensitive_data_cipher
        config = load_config()
        cipher = build_sensitive_data_cipher(config.encryption, load_role_secrets("backup", config=config))
    except Exception:
        cipher = None
    print(json.dumps(report_for_store(StateStore(root), datetime.now(timezone.utc), cipher=cipher), sort_keys=True))


if __name__ == "__main__":
    main()


class CampaignCapture:
    """Runs only in the existing daemon; never calls an order or broker mutation."""
    def __init__(self, store, clock, evidence, scope, guard, *, now=None, delay_minutes=30, reconciliation_reader=None, order_reconciliation_reader=None, position_reconciliation_reader=None, backup_reader=None, runtime_code=None, transition_reader=None, cipher=None):
        self.store, self.clock, self.evidence = store, clock, evidence
        self.scope, self.guard = _scope(scope), guard
        self.now = now or (lambda: datetime.now(timezone.utc))
        if not isinstance(delay_minutes, int) or not 0 <= delay_minutes <= 360:
            raise ValueError("invalid delay")
        self.delay_minutes = delay_minutes
        self.reconciliation_reader = reconciliation_reader
        self.order_reconciliation_reader = order_reconciliation_reader
        self.position_reconciliation_reader = position_reconciliation_reader
        self.backup_reader = backup_reader
        self.runtime_code = runtime_code if isinstance(runtime_code, str) and re.fullmatch(r"[a-f0-9]{40}", runtime_code) else "unknown"
        self.transition_reader = transition_reader
        self.cipher = cipher
        self._last = None

    def capture(self):
        at = _time(self.now())
        if self._last is not None and timedelta(0) <= at - self._last < timedelta(minutes=1):
            return False
        with self.store.lease("campaign-owner"):
            if self.guard is not None:
                self.guard.ensure_owned()
            campaign = start_campaign(self.store, at, self.scope, cipher=self.cipher)
            if campaign["scope"] != self.scope:
                if self.guard is not None:
                    self.guard.ensure_owned()
                def change_scope(old):
                    if self.cipher is not None and not verify_campaign(old, self.cipher):
                        raise ValueError("unverified campaign state")
                    value = {**old, "current_scope": self.scope, "runtime_code": self.runtime_code, "scope_changed_at": at.isoformat()}
                    return seal_campaign(value, self.cipher) if self.cipher is not None else value
                self.store.update(NAME, change_scope)
                self._last = at
                return False
            if campaign.get("captured_at"):
                last = _time(campaign["captured_at"])
                if timedelta(0) <= at - last < timedelta(minutes=1):
                    return False
            start, end = _time(campaign["started_at"]), _time(campaign["ends_at"])
            from zoneinfo import ZoneInfo
            zone = ZoneInfo("America/New_York")
            calendar_at = campaign.get("calendar_at")
            refresh_calendar = calendar_at is None or not timedelta(0) <= at - _time(calendar_at) < timedelta(minutes=30)
            if refresh_calendar:
                try:
                    calendar = validate_calendar(self.clock.session_range(start.astimezone(zone).date(), (end - timedelta(microseconds=1)).astimezone(zone).date()))
                except Exception:
                    calendar = None
                calendar_at = at.isoformat()
            else:
                calendar = campaign.get("calendar")
            rows, unreadable, raw_cycles, overflow = [], False, [], False
            try:
                raw_cycles, overflow = self.evidence.cycles_in_window(start, min(at, end), limit=100, scope=self.scope)
                for record in raw_cycles:
                    created = _time(record.created_at.replace(tzinfo=timezone.utc) if record.created_at.tzinfo is None else record.created_at)
                    if not start <= created <= min(at, end):
                        continue
                    detail = record.detail
                    if detail.get("unreadable"):
                        unreadable = True
                        continue
                    try:
                        cycle_started = _time(detail["started_at"])
                    except (KeyError, TypeError, ValueError):
                        unreadable = True
                        continue
                    if cycle_started < start:
                        continue
                    if cycle_started > created or cycle_started >= end:
                        unreadable = True
                        continue
                    candidate = {k: detail.get(k) for k in ("session", "mode", "config_fingerprint", "code_fingerprint")}
                    try:
                        date.fromisoformat(candidate["session"])
                        _scope({k: candidate[k] for k in self.scope})
                    except (TypeError, ValueError, KeyError):
                        unreadable = True
                        continue
                    if record.result_code not in {"observed", "executed", "blocked", "failed", "degraded", "proposed"}:
                        unreadable = True
                        continue
                    code = detail.get("code_identity")
                    candidate.update(result=record.result_code, key=hashlib.sha256(record.idempotency_key.encode()).hexdigest(),
                                     execution=detail.get("execution") if detail.get("execution") in {"simulated", "paper", "proposals"} else "unknown",
                                     code_identity=code if isinstance(code, str) and re.fullmatch(r"[a-f0-9]{40}", code) else "unknown")
                    rows.append(candidate)
            except Exception:
                unreadable = True
                rows = []
            truncated = overflow or len(rows) > 100
            rows = rows[-100:]
            try:
                health = self.store.load("health")
            except Exception:
                health = {}
            if not isinstance(health, dict):
                health = {}
            # Capture only typed health evidence; preserve unknown when health is missing or stale.
            try:
                checked = _time(health["checked_at"])
                fresh = timedelta(0) <= at - checked <= timedelta(seconds=180)
            except (KeyError, TypeError, ValueError):
                fresh = False
            health_status = "unknown"
            from .incident_catalog import COMPONENTS
            from .state_attestation import verify_doctor_health
            health_trusted = verify_doctor_health(health, self.cipher)
            raw_checks = health.get("checks")
            raw_checks = raw_checks if isinstance(raw_checks, list) else []
            safe_checks = [dict(c) for c in raw_checks if isinstance(c, dict)
                           and set(c) == {"component", "code", "status"}
                           and all(isinstance(c[k], str) for k in ("component", "code", "status"))
                           and c["component"] in COMPONENTS and c["code"] in COMPONENTS[c["component"]]
                           and c["status"] in {"pass", "warn", "fail", "unknown"}]
            if not health_trusted:
                safe_checks = [c for c in safe_checks if c["status"] != "pass"]
            if fresh:
                states = {c["status"] for c in safe_checks}
                components = {c["component"] for c in safe_checks}
                required = {"database", "daemon", "safety", "reconciliation", "maintenance", "jobs", "backup", "notifications", "power", "app", "release", "monitor"}
                health_status = "fail" if "fail" in states else "pass" if required <= components and states <= {"pass"} else "unknown"
            observation = {"t": at.isoformat(), "h": health_status,
                           "m": next((c["code"] for c in safe_checks if c["component"] == "maintenance"), "unknown") if fresh else "unknown"}
            receipts = []
            usable_health = fresh and checked >= start and health_trusted
            health_observed = checked if usable_health else at
            if not usable_health:
                health_status = "fail" if fresh and any(c["status"] == "fail" for c in safe_checks) else "unknown"
            observation["h"] = health_status
            digest = hashlib.sha256(json.dumps({"checked_at": health_observed.isoformat(), "checks": safe_checks}, sort_keys=True).encode()).hexdigest()
            receipts.append({"check": "runtime_health", "method": "real_local_runtime", "source": "runtime_doctor",
                             "outcome": health_status, "observed_at": health_observed.isoformat(), "captured_at": at.isoformat(), "scope": self.scope,
                             "reference": "sha256:" + digest, "observation_auth": "doctor_attested" if usable_health else "conservative_unverified",
                             "details": [c for c in safe_checks if c["status"] != "pass"]})
            if fresh and checked >= start:
                for check in safe_checks:
                    mapping = {("reconciliation", "reconciliation_clean", "pass"): ("broker_reconciliation", "persisted_broker_reconciliation", "daemon_reconciliation", "unknown"),
                               ("reconciliation", "reconciliation_required", "fail"): ("broker_reconciliation", "persisted_broker_reconciliation", "daemon_reconciliation", "fail"),
                               ("backup", "backup_recent", "pass"): ("backup", "real_local_backup", "backup", "unknown"),
                               ("backup", "backup_missing", "fail"): ("backup", "real_local_backup", "backup", "fail"),
                               ("backup", "backup_unknown", "unknown"): ("backup", "real_local_backup", "backup", "unknown")}
                    spec = mapping.get((check.get("component"), check.get("code"), check.get("status")))
                    if spec:
                        name, method, source, outcome = spec
                        digest = hashlib.sha256(json.dumps({"checked_at": checked.isoformat(), "check": check}, sort_keys=True).encode()).hexdigest()
                        receipts.append({"check": name, "method": method, "source": source, "outcome": outcome,
                                         "observed_at": checked.isoformat(), "captured_at": at.isoformat(), "scope": self.scope, "reference": "sha256:" + digest})
                if not any(r["check"] == "backup" for r in receipts):
                    receipts.append({"check": "backup", "method": "real_local_backup", "source": "backup", "outcome": "unknown",
                                     "observed_at": checked.isoformat(), "captured_at": at.isoformat(), "scope": self.scope,
                                     "reference": "sha256:" + hashlib.sha256(b"backup_not_observed").hexdigest()})
            for name, reader in (("broker_reconciliation", self.reconciliation_reader),
                                 ("broker_order_reconciliation", self.order_reconciliation_reader),
                                 ("broker_position_reconciliation", self.position_reconciliation_reader),
                                 ("backup", self.backup_reader)):
                if reader is not None:
                    receipts = [r for r in receipts if r["check"] != name]
                    receipts.append(reconciliation_receipt(name, reader, at, self.scope))
            if self.transition_reader is not None:
                specs = {"lifecycle": ("persisted_broker_lifecycle", "encrypted_lifecycle", {"paper_order_filled_exact"}),
                         "recovery": ("real_local_runtime", "encrypted_breaker_audit", {"operator_global_reset", "broker_drift_reset"}),
                         "safety_drill": ("isolated_mock_drill", "validated_ci_handoff", {"ci_isolated_mock_drill"})}
                try:
                    transitions = self.transition_reader(raw_cycles, start, at)
                    if not isinstance(transitions, dict):
                        raise ValueError("invalid transition observations")
                except Exception:
                    transitions = {}
                for name, (method, source, allowed) in specs.items():
                    result = transitions.get(name, {})
                    try:
                        outcome = result.get("outcome")
                        observed = _time(result["observed_at"])
                        transition = result.get("transition")
                        reference = result.get("reference")
                        if outcome != "pass" or transition not in allowed or observed > at or not isinstance(reference, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", reference):
                            raise ValueError("invalid transition")
                    except (KeyError, TypeError, ValueError):
                        outcome, observed, transition = "unknown", at, "unknown"
                        reference = "sha256:" + hashlib.sha256((name + "_unavailable").encode()).hexdigest()
                    transition_receipt = {"check": name, "method": method, "source": source, "outcome": outcome,
                                     "transition": transition, "observed_at": observed.isoformat(), "captured_at": at.isoformat(),
                                     "time_basis": "verifier_finish_before_ci_drill" if name == "safety_drill" and outcome == "pass" else "event_time" if outcome == "pass" else "capture_time",
                                     "code_version": result.get("code_version") if outcome == "pass" and isinstance(result.get("code_version"), str) and re.fullmatch(r"[a-f0-9]{40}", result["code_version"]) else self.runtime_code,
                                     "scope": self.scope, "reference": reference}
                    if name == "lifecycle" and outcome == "pass" and isinstance(result.get("orders"), list):
                        transition_receipt["orders"] = result["orders"]
                    receipts.append(transition_receipt)
            if self.guard is not None:
                self.guard.ensure_owned()
            def save(old):
                if self.cipher is not None and not verify_campaign(old, self.cipher):
                    raise ValueError("unverified campaign state")
                if old.get("scope") != self.scope or _time(old["started_at"]) != start:
                    raise ValueError("campaign changed during capture")
                if old.get("captured_at") and at <= _time(old["captured_at"]):
                    value = {**old, "clock_anomalies": min(old.get("clock_anomalies", 0) + 1, 1000000000)}
                    return seal_campaign(value, self.cipher) if self.cipher is not None else value
                observations = old.get("observations", [])
                last_sample = _time(observations[-1].get("t", observations[-1].get("at"))) if observations else None
                if at < end and (last_sample is None or at - last_sample >= timedelta(minutes=30)):
                    observations = observations + [observation]
                # Compress repeated same-day outcomes, never erase a transition.
                all_receipts = list(old["receipts"])
                coalesced = old.get("coalesced_receipts", 0)
                post_receipts = list(old.get("post_window_receipts", []))
                for receipt in receipts:
                    receipt = {**receipt, "schema_version": 1}
                    if at >= end:
                        post_receipts = [r for r in post_receipts if r.get("check") != receipt["check"]] + [receipt]
                        continue
                    prior = next((i for i in range(len(all_receipts)-1, -1, -1) if all_receipts[i]["check"] == receipt["check"]), None)
                    if prior is not None and all_receipts[prior]["outcome"] == receipt["outcome"] and all_receipts[prior].get("transition") == receipt.get("transition") and all_receipts[prior].get("orders") == receipt.get("orders") and all_receipts[prior]["observed_at"][:10] == receipt["observed_at"][:10] and ("transition" not in receipt or all_receipts[prior]["reference"] == receipt["reference"]):
                        all_receipts.pop(prior)
                        coalesced += 1
                    all_receipts.append(receipt)
                previous_calendar = old.get("last_valid_calendar")
                revisions = old.get("calendar_revisions", 0) + int(calendar is not None and previous_calendar is not None and calendar != previous_calendar)
                value = {**old, "current_scope": self.scope, "runtime_code": self.runtime_code, "calendar": calendar, "calendar_at": calendar_at,
                        "doctor_stale_observations": health.get("stale_observations") if health_trusted else None,
                        "doctor_discarded_events": health.get("discarded_events") if health_trusted else None,
                        "doctor_legacy_history_unverified": health.get("legacy_history_unverified") if health_trusted else None,
                        "last_valid_calendar": calendar if calendar is not None else previous_calendar, "calendar_revisions": revisions,
                        "cycles": rows, "evidence_complete": not (unreadable or truncated), "evidence_through": at.isoformat() if not (unreadable or truncated) else None, "delay_minutes": self.delay_minutes,
                        "captured_at": at.isoformat(), "observations": observations[-1500:],
                        "coalesced_receipts": coalesced,
                        "receipts": all_receipts[-MAX_RECEIPTS:], "post_window_receipts": post_receipts[-32:], "discarded_receipts": old["discarded_receipts"] + max(0, len(all_receipts)-MAX_RECEIPTS),
                        "discarded_observations": old.get("discarded_observations", 0) + max(0, len(observations)-1500)}
                value = bound_campaign_history(value)
                return seal_campaign(value, self.cipher) if self.cipher is not None else value
            self.store.update(NAME, save)
            self._last = at
            return True
