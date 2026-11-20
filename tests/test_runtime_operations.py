"""Read-only doctor and long-running awake guard, without broker credentials."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3


def module():
    spec = importlib.util.spec_from_file_location("runtime_operations", Path("scripts/runtime_operations.py"))
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result


NOW = datetime(2026, 11, 17, tzinfo=timezone.utc)


def test_stale_and_duplicate_health_observations_never_clear_new_failure(tmp_path):
    m = module()
    from trading_assistant.ops.operational_state import StateStore
    store = StateStore(tmp_path)
    newer = NOW + timedelta(seconds=30)
    failure = [{"component": "daemon", "status": "fail", "code": "heartbeat_stale"}]
    passing = [{"component": "daemon", "status": "pass", "code": "heartbeat_fresh"}]
    first = m.record_checks(store, failure, newer)
    for at in (NOW, newer):
        result = m.record_checks(store, passing, at)
        assert result["checked_at"] == newer.isoformat()
        assert result["incidents"]["daemon"] == first["incidents"]["daemon"]
    assert store.load("health")["stale_observations"] == 2


def test_doctor_single_owner_rejects_overlapping_capture(tmp_path):
    import pytest
    m = module()
    from trading_assistant.ops.operational_state import StateError, StateStore
    store = StateStore(tmp_path)
    with store.lease("doctor-owner"):
        with pytest.raises(StateError):
            m.doctor(tmp_path, store, NOW)
    assert store.load("health") == {}


def test_stale_doctor_does_not_clear_newer_maintenance_state(tmp_path):
    m = module()
    from trading_assistant.ops.operational_state import StateStore
    store = StateStore(tmp_path)
    store.update("maintenance", lambda _: {"active_since": NOW.isoformat()})
    m.record_checks(store, [{"component": "daemon", "status": "fail", "code": "heartbeat_stale"}], NOW + timedelta(seconds=30))
    m.doctor(tmp_path, store, NOW)
    assert store.load("maintenance")["active_since"] == NOW.isoformat()
    assert store.load("health")["incidents"]["daemon"]["state"] == "open"


def test_doctor_publication_authenticates_new_checks_and_rejects_tamper(tmp_path, sensitive_cipher):
    from trading_assistant.ops.operational_state import StateStore
    from trading_assistant.ops.state_attestation import verify_doctor_health
    m = module()
    store = StateStore(tmp_path)
    # A legacy edited pass is not authenticated by a later actual failing check.
    store.update("health", lambda _: {"schema_version": 1, "checked_at": NOW.isoformat(),
        "checks": [{"component": "daemon", "code": "heartbeat_fresh", "status": "pass"}], "incidents": {}, "events": []})
    observed = m.record_checks(store, [{"component": "daemon", "code": "heartbeat_stale", "status": "fail"}], NOW + timedelta(seconds=60), cipher=sensitive_cipher)
    assert verify_doctor_health(observed, sensitive_cipher)
    assert observed["doctor_publication"]["previous_verified"] is False
    assert next(c for c in observed["checks"] if c["component"] == "daemon")["status"] == "fail"
    assert next(c for c in observed["checks"] if c["component"] == "monitor")["status"] == "unknown"
    edited = {**observed, "checks": [{"component": "daemon", "code": "heartbeat_fresh", "status": "pass"}]}
    assert not verify_doctor_health(edited, sensitive_cipher)
    assert not verify_doctor_health(observed, None)


def test_doctor_publication_preserves_trusted_state_on_rollback_and_key_rotation(tmp_path, sensitive_cipher):
    from trading_assistant.ops.operational_state import StateStore
    from trading_assistant.ops.state_attestation import verify_doctor_health
    from trading_assistant.security.crypto import SensitiveDataCipher
    m = module()
    store = StateStore(tmp_path)
    fail = [{"component": "daemon", "code": "heartbeat_stale", "status": "fail"}]
    m.record_checks(store, fail, NOW + timedelta(seconds=30), cipher=sensitive_cipher)
    result = m.record_checks(store, [{"component": "daemon", "code": "heartbeat_fresh", "status": "pass"}], NOW, cipher=sensitive_cipher)
    assert result["incidents"]["daemon"]["state"] == "open"
    rotated = SensitiveDataCipher({"pytest-field-key-2026": b"t" * 32, "rotated-key-2026": b"r" * 32}, active_key_id="rotated-key-2026")
    assert verify_doctor_health(result, rotated)
    missing = SensitiveDataCipher({"rotated-key-2026": b"r" * 32}, active_key_id="rotated-key-2026")
    assert not verify_doctor_health(result, missing)


def test_doctor_key_failure_preserves_conservative_failure_without_attestation(tmp_path):
    from trading_assistant.ops.operational_state import StateStore
    from trading_assistant.ops.state_attestation import verify_doctor_health
    class BrokenCipher:
        def encrypt(self, *_): raise ValueError("unavailable")
        def decrypt(self, *_): raise ValueError("unavailable")
    m = module()
    store = StateStore(tmp_path)
    result = m.record_checks(store, [{"component": "daemon", "code": "heartbeat_stale", "status": "fail"}], NOW, cipher=BrokenCipher())
    assert result["incidents"]["daemon"]["state"] == "open"
    assert not verify_doctor_health(result, BrokenCipher())


def test_incidents_persist_deduplicate_and_unknown_does_not_resolve(tmp_path):
    m = module()
    from trading_assistant.ops.operational_state import StateStore
    store = StateStore(tmp_path)
    failed = [{"component":"daemon", "status":"fail", "code":"heartbeat_stale"}]
    first = m.record_checks(store, failed, NOW)
    incident = first["incidents"]["daemon"]
    assert incident["state"] == "open"
    identity = incident["id"]
    second = m.record_checks(store, failed, NOW + timedelta(seconds=60))
    assert second["incidents"]["daemon"]["id"] == identity
    assert len(second["events"]) == 1
    unknown = m.record_checks(store, [{"component":"daemon", "status":"unknown", "code":"database_unreadable"}], NOW + timedelta(seconds=120))
    assert unknown["incidents"]["daemon"]["state"] == "open"
    resolved = m.record_checks(store, [{"component":"daemon", "status":"pass", "code":"heartbeat_fresh"}], NOW + timedelta(seconds=180))
    assert resolved["incidents"]["daemon"]["state"] == "resolved"
    assert len(resolved["events"]) == 2


def test_monitoring_gap_is_evidence_of_gap_and_history_is_bounded(tmp_path):
    m = module()
    from trading_assistant.ops.operational_state import StateStore
    store = StateStore(tmp_path)
    m.record_checks(store, [], NOW)
    result = m.record_checks(store, [], NOW + timedelta(minutes=10))
    assert result["incidents"]["monitor"]["code"] == "monitoring_gap"
    assert result["incidents"]["monitor"]["state"] == "open"
    for i in range(210):
        m.record_checks(store, [{"component":"daemon", "status":"fail" if i%2 else "pass", "code":"heartbeat_stale" if i%2 else "heartbeat_fresh"}], NOW + timedelta(minutes=11, seconds=i*60))
    assert len(store.load("health")["events"]) <= 100


def test_read_only_database_snapshot_reports_safety_without_decrypting_or_writing(tmp_path):
    m = module()
    db = tmp_path / "trading_assistant.db"
    with sqlite3.connect(db) as connection:
        connection.executescript("CREATE TABLE heartbeats(source TEXT, at TEXT); CREATE TABLE circuit_breaker_state(scope_key TEXT,tripped INTEGER); CREATE TABLE startup_reconciliation_state(status TEXT, generation INTEGER, completed_generation INTEGER);")
        connection.execute("INSERT INTO heartbeats VALUES ('daemon',?)", (NOW.replace(tzinfo=None).isoformat(),))
        connection.execute("INSERT INTO circuit_breaker_state VALUES ('operator_global',1)")
        connection.execute("INSERT INTO startup_reconciliation_state VALUES ('current',1,1)")
    before = db.read_bytes()
    checks = {c["component"]:c for c in m.database_checks(tmp_path,NOW)}
    assert checks["database"]["status"] == "pass"
    assert checks["daemon"]["status"] == "pass"
    assert checks["safety"]["status"] == "fail"
    assert checks["reconciliation"]["status"] == "pass"
    assert db.read_bytes() == before


def test_unsafe_or_missing_database_is_unknown_and_does_not_create_one(tmp_path):
    m = module()
    checks = m.database_checks(tmp_path,NOW)
    assert all(c["status"] == "unknown" for c in checks)
    assert not (tmp_path / "trading_assistant.db").exists()


def test_alert_records_only_use_fixed_known_components_and_codes(tmp_path):
    import pytest
    m = module()
    from trading_assistant.ops.operational_state import StateStore
    store = StateStore(tmp_path)
    with pytest.raises(ValueError):
        m.record_checks(store,[{"component":"daemon", "status":"fail", "code":"secret payload"}],NOW)
    assert store.load("health") == {}


def test_doctor_uses_shared_release_validator_with_root(tmp_path,monkeypatch):
    m = module()
    from trading_assistant.ops import release_evidence
    from trading_assistant.ops.operational_state import StateStore
    monkeypatch.setattr(release_evidence,"clean_commit",lambda root:"a"*40)
    monkeypatch.setattr(release_evidence,"read_report",lambda path:{"fixture":True})
    def validate(payload,*,expected_commit,root):
        assert root == tmp_path and expected_commit == "a"*40
    monkeypatch.setattr(release_evidence,"validate_report",validate)
    result = m.doctor(tmp_path,StateStore(tmp_path),NOW)
    assert next(c for c in result["checks"] if c["component"] == "release")["status"] == "pass"


def test_job_check_distinguishes_missing_jobs_and_failed_inspection():
    import subprocess
    m=module()
    def good(args,**kwargs):return subprocess.CompletedProcess(args,0)
    def missing(args,**kwargs):return subprocess.CompletedProcess(args,113)
    def broken(args,**kwargs):raise subprocess.TimeoutExpired(args,3)
    assert m.job_check(runner=good)["status"]=="pass"
    assert m.job_check(runner=missing)["code"]=="jobs_missing"
    assert m.job_check(runner=broken)["status"]=="unknown"


def test_backup_check_uses_committed_whole_database_header_and_age(tmp_path,monkeypatch):
    m=module()
    from trading_assistant.ops import backup
    monkeypatch.setattr(backup,"list_committed_backups",lambda path,*,artifact_label: (tmp_path/"fixture",) if artifact_label=="whole-database-v1" else ())
    monkeypatch.setattr(backup,"read_encrypted_backup_header",lambda path:{"created_at":NOW.isoformat()})
    assert m.backup_check(tmp_path,NOW)["status"]=="pass"
    assert m.backup_check(tmp_path,NOW+timedelta(hours=37))["code"]=="backup_missing"


def test_doctor_does_not_treat_planned_backup_pause_as_runtime_failure(tmp_path,monkeypatch):
    import fcntl
    import os
    m=module()
    from trading_assistant.ops.operational_state import StateStore
    store=StateStore(tmp_path);store.load("health")
    fd=os.open(tmp_path/".local/scheduled-backup.lock",os.O_RDWR|os.O_CREAT,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        def forbidden(*args,**kwargs):raise AssertionError("backup pause is expected")
        monkeypatch.setattr(m,"database_checks",forbidden)
        monkeypatch.setattr(m,"job_check",forbidden)
        result=m.doctor(tmp_path,store,NOW)
        assert next(c for c in result["checks"] if c["component"]=="maintenance")["code"]=="backup_running"
        assert not any(i["state"]=="open" for c,i in result["incidents"].items() if c in {"app","daemon","jobs","database"})
    finally:os.close(fd)


def test_unknown_maintenance_still_observes_runtime_and_opens_incident(tmp_path,monkeypatch):
    m=module()
    from trading_assistant.ops.operational_state import StateStore
    store=StateStore(tmp_path);store.load("health")
    lock=tmp_path/".local/scheduled-backup.lock";lock.touch();lock.chmod(0o644)
    observed=[]
    def database(root,now):
        observed.append(True)
        return [{"component":"daemon","status":"fail","code":"heartbeat_stale"}]
    monkeypatch.setattr(m,"database_checks",database)
    result=m.doctor(tmp_path,store,NOW)
    assert observed
    assert result["incidents"]["maintenance"]["state"]=="open"
    assert result["incidents"]["daemon"]["state"]=="open"


def test_stalled_verified_backup_becomes_actionable_incident(tmp_path,monkeypatch):
    m=module()
    from trading_assistant.ops.operational_state import StateStore
    store=StateStore(tmp_path)
    monkeypatch.setattr(m,"maintenance_state",lambda _:"active")
    m.doctor(tmp_path,store,NOW)
    result=m.doctor(tmp_path,store,NOW+timedelta(minutes=16))
    assert result["incidents"]["maintenance"]["code"]=="backup_stalled"


def test_notification_check_needs_pairing_confirmed_test_and_fresh_delivery_job(tmp_path):
    m=module()
    from trading_assistant.ops.operational_state import StateStore
    store=StateStore(tmp_path)
    assert m.notification_check(store,NOW)["code"]=="telegram_unpaired"
    store.update("telegram",lambda _:{"chat_id":123,"bot_id":42,"test_delivered_at":NOW.isoformat()})
    assert m.notification_check(store,NOW)["status"]=="unknown"
    store.update("telegram-status",lambda _:{"checked_at":NOW.isoformat(),"status":"available"})
    assert m.notification_check(store,NOW)["status"]=="pass"
    store.update("telegram-delivery",lambda _:{"events":{"receipt":{"state":"unknown"}}})
    assert m.notification_check(store,NOW)["code"]=="telegram_delivery_uncertain"
    store.update("telegram-delivery",lambda _:{"events":{}})
    store.update("telegram-status",lambda _:{"checked_at":NOW.isoformat(),"status":"unavailable"})
    assert m.notification_check(store,NOW)["code"]=="telegram_unavailable"
    assert m.notification_check(store,NOW+timedelta(minutes=4))["status"]=="unknown"
