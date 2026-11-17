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
    "lifecycle": {"real_local_runtime"},
    "recovery": {"real_local_runtime"},
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


def start_campaign(store, now, scope):
    at, scope = _time(now), _scope(scope)
    def initialize(old):
        if old:
            if old.get("schema_version") != 1:
                raise ValueError("unknown campaign schema")
            return old
        return {"schema_version": 1, "started_at": at.isoformat(), "ends_at": (at + timedelta(days=30)).isoformat(),
                "scope": scope, "receipts": [], "discarded_receipts": 0, "calendar": None}
    return store.update(NAME, initialize)


def bound_campaign_history(state, *, limit=220_000):
    """Keep the private store bounded and make every dropped record explicit."""
    value = {**state, "receipts": list(state["receipts"]), "observations": list(state["observations"]), "cycles": list(state["cycles"])}
    while len((json.dumps(value, allow_nan=False, sort_keys=True) + "\n").encode()) > limit:
        if value["receipts"]:
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
    from ..db.models import StartupReconciliationState
    with session_factory() as session:
        row = session.get(StartupReconciliationState, broker)
        outcome = reconciliation_outcome(row, now, max_age_seconds)
        evidence = {"status": row.status, "generation": row.generation, "completed_generation": row.completed_generation,
                    "started_at": str(row.started_at), "completed_at": str(row.completed_at), "updated_at": str(row.updated_at)} if row else {"status": "missing"}
    source_at = (row.completed_at if row.status == "current" else row.updated_at) if row else None
    return outcome, hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest(), source_at


def read_daemon_order_reconciliation(session_factory, now, max_age_seconds):
    """Read the latest daemon order-sync result, never an older successful result."""
    return _read_reconciliation_audit(session_factory, now, max_age_seconds, "orders.sync", ("daemon:monitor", "daemon:startup"), "reconciliation_required")


def read_position_reconciliation(session_factory, now, max_age_seconds):
    return _read_reconciliation_audit(session_factory, now, max_age_seconds, "positions.reconcile", None, "drift_detected")


def _read_reconciliation_audit(session_factory, now, max_age_seconds, action, actors, failure_code):
    from sqlalchemy import select
    from ..db.models import AuditEvent
    query = select(AuditEvent.id, AuditEvent.created_at, AuditEvent.result_code).where(AuditEvent.action == action)
    if actors is not None:
        query = query.where(AuditEvent.actor.in_(actors))
    with session_factory() as session:
        row = session.execute(query.order_by(AuditEvent.id.desc()).limit(1)).one_or_none()
    evidence = {"id": row.id, "created_at": str(row.created_at), "result_code": row.result_code} if row else {"status": "missing"}
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    if row is None:
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


def evaluate(campaign, now, calendar, cycles, *, evidence_complete=False, delay_minutes=30):
    now = _time(now)
    start, end = _time(campaign["started_at"]), _time(campaign["ends_at"])
    scope = _scope(campaign["scope"])
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
        elif scope["mode"] == "off" or opened + timedelta(minutes=delay_minutes) >= closed or opened + timedelta(minutes=delay_minutes) < start:
            status = "not_scheduled"
        elif now < opened + timedelta(minutes=delay_minutes):
            status = "not_yet_due"
        elif not evidence_complete:
            status = "unknown"
        elif now < closed:
            status = "due"
        else:
            status = "missed"
        sessions.append({"date": entry["date"], "status": status, "evidence_count": len(unique), "results": sorted(str(x) for x in results),
                         "execution_kinds": sorted({x.get("execution", "unknown") for x in matching})})
    checks, latest_receipts = {}, {}
    for name in CHECK_METHODS:
        matching = [r for r in campaign.get("receipts", []) if r.get("check") == name and r.get("scope") == scope and start <= _time(r.get("captured_at", r["observed_at"])) <= now]
        receipt = max(matching, key=lambda r: _time(r.get("captured_at", r["observed_at"]))) if matching else None
        latest_receipts[name] = receipt
        # Current status expires conservatively; history retains its original outcome.
        fresh = receipt is not None and start <= _time(receipt["observed_at"]) and timedelta(0) <= now - _time(receipt["observed_at"]) <= timedelta(minutes=3)
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
            "session_accounting_complete": bool(now >= end and calendar_ok and evidence_complete and sessions and all(s["status"] not in {"unknown", "due", "not_yet_due"} for s in sessions) and current_scope == scope),
            "scope_changed": current_scope != scope, "runtime_code": campaign.get("runtime_code", "unknown"), "code_versions": sorted({c.get("code_identity", "unknown") for c in cycles if all(c.get(k) == v for k, v in scope.items())}), "sessions": sessions,
            "calendar_complete": calendar_ok,
            "calendar_revisions": campaign.get("calendar_revisions", 0), "checks": checks, "latest_receipts": latest_receipts,
            "receipts": campaign.get("receipts", []), "discarded_receipts": campaign.get("discarded_receipts", 0), "coalesced_receipts": campaign.get("coalesced_receipts", 0),
            "evidence_complete": evidence_complete,
            "observation_coverage": coverage, "captured_at": campaign.get("captured_at"), "calendar_at": campaign.get("calendar_at"),
            "clock_anomalies": campaign.get("clock_anomalies", 0), "doctor_stale_observations": campaign.get("doctor_stale_observations", 0),
            "continuous_uptime": "unknown", "complete": False}


def report_for_store(store, now):
    campaign = store.load(NAME)
    if not campaign:
        return {"status": "not_started", "complete": False}
    return evaluate(campaign, now, campaign.get("calendar"), campaign.get("cycles", []),
                    evidence_complete=campaign.get("evidence_complete", False),
                    delay_minutes=campaign.get("delay_minutes", 30))


def main():
    from pathlib import Path
    from ..installation import require_designated
    from .operational_state import StateStore
    root = Path(__file__).resolve().parents[3]
    require_designated(root)
    print(json.dumps(report_for_store(StateStore(root), datetime.now(timezone.utc)), sort_keys=True))


if __name__ == "__main__":
    main()


class CampaignCapture:
    """Runs only in the existing daemon; never calls an order or broker mutation."""
    def __init__(self, store, clock, evidence, scope, guard, *, now=None, delay_minutes=30, reconciliation_reader=None, order_reconciliation_reader=None, position_reconciliation_reader=None, runtime_code=None):
        self.store, self.clock, self.evidence = store, clock, evidence
        self.scope, self.guard = _scope(scope), guard
        self.now = now or (lambda: datetime.now(timezone.utc))
        if not isinstance(delay_minutes, int) or not 0 <= delay_minutes <= 360:
            raise ValueError("invalid delay")
        self.delay_minutes = delay_minutes
        self.reconciliation_reader = reconciliation_reader
        self.order_reconciliation_reader = order_reconciliation_reader
        self.position_reconciliation_reader = position_reconciliation_reader
        self.runtime_code = runtime_code if isinstance(runtime_code, str) and re.fullmatch(r"[a-f0-9]{40}", runtime_code) else "unknown"
        self._last = None

    def capture(self):
        at = _time(self.now())
        if self._last is not None and timedelta(0) <= at - self._last < timedelta(minutes=1):
            return False
        with self.store.lease("campaign-owner"):
            if self.guard is not None:
                self.guard.ensure_owned()
            campaign = start_campaign(self.store, at, self.scope)
            if campaign["scope"] != self.scope:
                if self.guard is not None:
                    self.guard.ensure_owned()
                self.store.update(NAME, lambda old: {**old, "current_scope": self.scope, "runtime_code": self.runtime_code, "scope_changed_at": at.isoformat()})
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
            rows, unreadable = [], False
            try:
                for record in self.evidence.cycles():
                    created = _time(record.created_at.replace(tzinfo=timezone.utc) if record.created_at.tzinfo is None else record.created_at)
                    if not start <= created <= at:
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
            truncated = len(rows) > 100
            rows = rows[-100:]
            health = self.store.load("health")
            # Capture only typed health evidence; preserve unknown when health is missing or stale.
            try:
                checked = _time(health["checked_at"])
                fresh = timedelta(0) <= at - checked <= timedelta(seconds=180)
            except (KeyError, TypeError, ValueError):
                fresh = False
            health_status = "unknown"
            from .incident_catalog import COMPONENTS
            safe_checks = [dict(c) for c in health.get("checks", []) if isinstance(c, dict)
                           and set(c) == {"component", "code", "status"}
                           and c["component"] in COMPONENTS and c["code"] in COMPONENTS[c["component"]]
                           and c["status"] in {"pass", "warn", "fail", "unknown"}]
            if fresh:
                states = {c["status"] for c in safe_checks}
                components = {c["component"] for c in safe_checks}
                required = {"database", "daemon", "safety", "reconciliation", "maintenance", "jobs", "backup", "notifications", "power", "app", "release", "monitor"}
                health_status = "fail" if "fail" in states else "pass" if required <= components and states <= {"pass"} else "unknown"
            observation = {"t": at.isoformat(), "h": health_status,
                           "m": next((c["code"] for c in safe_checks if c["component"] == "maintenance"), "unknown") if fresh else "unknown"}
            receipts = []
            if fresh and checked >= start:
                digest = hashlib.sha256(json.dumps({"checked_at": checked.isoformat(), "checks": safe_checks}, sort_keys=True).encode()).hexdigest()
                receipts.append({"check": "runtime_health", "method": "real_local_runtime", "source": "runtime_doctor",
                                 "outcome": health_status, "observed_at": checked.isoformat(), "captured_at": at.isoformat(), "scope": self.scope,
                                 "reference": "sha256:" + digest, "details": [c for c in safe_checks if c["status"] != "pass"]})
                for check in safe_checks:
                    mapping = {("reconciliation", "reconciliation_clean", "pass"): ("broker_reconciliation", "persisted_broker_reconciliation", "daemon_reconciliation", "unknown"),
                               ("reconciliation", "reconciliation_required", "fail"): ("broker_reconciliation", "persisted_broker_reconciliation", "daemon_reconciliation", "fail"),
                               ("backup", "backup_recent", "pass"): ("backup", "real_local_backup", "backup", "pass"),
                               ("backup", "backup_missing", "fail"): ("backup", "real_local_backup", "backup", "fail")}
                    spec = mapping.get((check.get("component"), check.get("code"), check.get("status")))
                    if spec:
                        name, method, source, outcome = spec
                        digest = hashlib.sha256(json.dumps({"checked_at": checked.isoformat(), "check": check}, sort_keys=True).encode()).hexdigest()
                        receipts.append({"check": name, "method": method, "source": source, "outcome": outcome,
                                         "observed_at": checked.isoformat(), "captured_at": at.isoformat(), "scope": self.scope, "reference": "sha256:" + digest})
            if self.reconciliation_reader is not None:
                try:
                    result = self.reconciliation_reader(at)
                    outcome, digest = result[:2]
                    source_at = result[2] if len(result) > 2 else at
                    source_at = _time(source_at.replace(tzinfo=timezone.utc) if source_at is not None and source_at.tzinfo is None else source_at) if source_at is not None else at
                    if source_at > at:
                        raise ValueError("future reconciliation observation")
                    if outcome not in {"pass", "fail", "unknown"} or not re.fullmatch(r"[a-f0-9]{64}", digest):
                        raise ValueError("invalid reconciliation observation")
                    receipts = [r for r in receipts if r["check"] != "broker_reconciliation"]
                    receipts.append({"check": "broker_reconciliation", "method": "persisted_broker_reconciliation",
                                     "source": "daemon_reconciliation", "outcome": outcome, "observed_at": source_at.isoformat(), "captured_at": at.isoformat(),
                                     "scope": self.scope, "reference": "sha256:" + digest})
                except Exception:
                    receipts = [r for r in receipts if r["check"] != "broker_reconciliation"]
                    receipts.append({"check": "broker_reconciliation", "method": "persisted_broker_reconciliation", "source": "daemon_reconciliation",
                                     "outcome": "unknown", "observed_at": at.isoformat(), "captured_at": at.isoformat(), "scope": self.scope,
                                     "reference": "sha256:" + hashlib.sha256(b"reconciliation_unavailable").hexdigest()})
            if self.order_reconciliation_reader is not None:
                try:
                    result = self.order_reconciliation_reader(at)
                    outcome, digest = result[:2]
                    source_at = result[2] if len(result) > 2 else at
                    source_at = _time(source_at.replace(tzinfo=timezone.utc) if source_at is not None and source_at.tzinfo is None else source_at) if source_at is not None else at
                    if source_at > at:
                        raise ValueError("future reconciliation observation")
                    if outcome not in {"pass", "fail", "unknown"} or not re.fullmatch(r"[a-f0-9]{64}", digest):
                        raise ValueError("invalid order reconciliation observation")
                    receipts.append({"check": "broker_order_reconciliation", "method": "persisted_broker_reconciliation",
                                     "source": "daemon_reconciliation", "outcome": outcome, "observed_at": source_at.isoformat(), "captured_at": at.isoformat(),
                                     "scope": self.scope, "reference": "sha256:" + digest})
                except Exception:
                    receipts.append({"check": "broker_order_reconciliation", "method": "persisted_broker_reconciliation", "source": "daemon_reconciliation",
                                     "outcome": "unknown", "observed_at": at.isoformat(), "captured_at": at.isoformat(), "scope": self.scope,
                                     "reference": "sha256:" + hashlib.sha256(b"order_reconciliation_unavailable").hexdigest()})
            if self.position_reconciliation_reader is not None:
                try:
                    result = self.position_reconciliation_reader(at)
                    outcome, digest = result[:2]
                    source_at = result[2] if len(result) > 2 else at
                    source_at = _time(source_at.replace(tzinfo=timezone.utc) if source_at is not None and source_at.tzinfo is None else source_at) if source_at is not None else at
                    if source_at > at or outcome not in {"pass", "fail", "unknown"} or not re.fullmatch(r"[a-f0-9]{64}", digest):
                        raise ValueError("invalid position reconciliation observation")
                except Exception:
                    outcome, source_at = "unknown", at
                    digest = hashlib.sha256(b"position_reconciliation_unavailable").hexdigest()
                receipts.append({"check": "broker_position_reconciliation", "method": "persisted_broker_reconciliation", "source": "audit_reconciliation",
                                 "outcome": outcome, "observed_at": source_at.isoformat(), "captured_at": at.isoformat(), "scope": self.scope,
                                 "reference": "sha256:" + digest})
            if self.guard is not None:
                self.guard.ensure_owned()
            def save(old):
                if old.get("scope") != self.scope or _time(old["started_at"]) != start:
                    raise ValueError("campaign changed during capture")
                if old.get("captured_at") and at <= _time(old["captured_at"]):
                    return {**old, "clock_anomalies": min(old.get("clock_anomalies", 0) + 1, 1000000000)}
                observations = old.get("observations", [])
                last_sample = _time(observations[-1].get("t", observations[-1].get("at"))) if observations else None
                if at < end and (last_sample is None or at - last_sample >= timedelta(minutes=30)):
                    observations = observations + [observation]
                # Compress repeated same-day outcomes, never erase a transition.
                all_receipts = list(old["receipts"])
                coalesced = old.get("coalesced_receipts", 0)
                for receipt in receipts:
                    prior = next((i for i in range(len(all_receipts)-1, -1, -1) if all_receipts[i]["check"] == receipt["check"]), None)
                    if prior is not None and all_receipts[prior]["outcome"] == receipt["outcome"] and all_receipts[prior]["observed_at"][:10] == receipt["observed_at"][:10]:
                        all_receipts.pop(prior)
                        coalesced += 1
                    all_receipts.append(receipt)
                previous_calendar = old.get("last_valid_calendar")
                revisions = old.get("calendar_revisions", 0) + int(calendar is not None and previous_calendar is not None and calendar != previous_calendar)
                value = {**old, "current_scope": self.scope, "runtime_code": self.runtime_code, "calendar": calendar, "calendar_at": calendar_at,
                        "doctor_stale_observations": health.get("stale_observations", 0) if type(health.get("stale_observations", 0)) is int and 0 <= health.get("stale_observations", 0) <= 1_000_000_000 else 0,
                        "last_valid_calendar": calendar if calendar is not None else previous_calendar, "calendar_revisions": revisions,
                        "cycles": rows, "evidence_complete": not (unreadable or truncated), "delay_minutes": self.delay_minutes,
                        "captured_at": at.isoformat(), "observations": observations[-1500:],
                        "coalesced_receipts": coalesced,
                        "receipts": all_receipts[-MAX_RECEIPTS:], "discarded_receipts": old["discarded_receipts"] + max(0, len(all_receipts)-MAX_RECEIPTS),
                        "discarded_observations": old.get("discarded_observations", 0) + max(0, len(observations)-1500)}
                return bound_campaign_history(value)
            self.store.update(NAME, save)
            self._last = at
            return True
