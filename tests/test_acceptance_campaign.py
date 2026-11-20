from datetime import datetime, timedelta, timezone

from trading_assistant.ops.acceptance_campaign import evaluate, start_campaign as real_start_campaign, CampaignCapture as RealCampaignCapture
from tests.conftest import TEST_SENSITIVE_CIPHER as TEST_CIPHER
from trading_assistant.ops.operational_state import StateStore
from trading_assistant.broker.alpaca import AlpacaClock
from datetime import date
from types import SimpleNamespace
import pytest

class FakeEvidence:
    def cycles_in_window(self, start, end, *, limit, scope=None):
        rows = self.cycles()
        return rows[:limit], len(rows) > limit


UTC = timezone.utc
START = datetime(2026, 11, 25, 12, tzinfo=UTC)
SCOPE = {"mode": "paper", "config_fingerprint": "a" * 64, "code_fingerprint": "b" * 64}


def start_campaign(store, now, scope, **kwargs):
    return real_start_campaign(store, now, scope, cipher=kwargs.get("cipher", TEST_CIPHER))


class CampaignCapture(RealCampaignCapture):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("cipher", TEST_CIPHER)
        super().__init__(*args, **kwargs)


def test_calendar_sessions_duplicate_blocked_and_missing_coverage(tmp_path):
    store = StateStore(tmp_path)
    campaign = start_campaign(store, START, SCOPE)
    # Thanksgiving is absent from this complete provider range; Friday closes early.
    calendar = {"start": "2026-11-25", "end": "2026-11-29", "source": "alpaca_calendar", "sessions": [
        {"date": "2026-11-25", "open": "2026-11-25T14:30:00+00:00", "close": "2026-11-25T21:00:00+00:00"},
        {"date": "2026-11-27", "open": "2026-11-27T14:30:00+00:00", "close": "2026-11-27T18:00:00+00:00"}]}
    cycle = {"session": "2026-11-25", "result": "blocked", "key": "same", **SCOPE}
    report = evaluate(campaign, START + timedelta(days=4), calendar, [cycle, cycle], evidence_complete=True, evidence_through=START + timedelta(days=4))
    assert [x["status"] for x in report["sessions"]] == ["blocked", "missed"]
    assert report["sessions"][0]["evidence_count"] == 1
    assert report["calendar_complete"] is False
    assert report["complete"] is False


def test_future_and_not_yet_due_and_unreadable_evidence(tmp_path):
    c = start_campaign(StateStore(tmp_path), START, SCOPE)
    cal = {"start": "2026-11-25", "end": "2026-11-25", "source": "alpaca_calendar", "sessions": [
        {"date": "2026-11-25", "open": "2026-11-25T14:30:00+00:00", "close": "2026-11-25T21:00:00+00:00"}]}
    assert evaluate(c, START, cal, [])["sessions"][0]["status"] == "not_yet_due"
    assert evaluate(c, datetime(2026, 11, 25, 17, tzinfo=UTC), cal, [], evidence_complete=False)["sessions"][0]["status"] == "unknown"


def test_receipts_survive_restart_and_mock_cannot_be_broker_acceptance(tmp_path):
    store = StateStore(tmp_path)
    c = start_campaign(store, START, SCOPE)
    assert start_campaign(StateStore(tmp_path), START + timedelta(days=1), SCOPE)["started_at"] == c["started_at"]
    report = evaluate(store.load("acceptance-campaign"), START + timedelta(days=1), None, [])
    assert report["checks"]["broker_reconciliation"] == "unknown"
    assert report["checks"]["safety_drill"] == "unknown"
    assert report["complete"] is False


def test_official_calendar_range_validates_boundaries_and_rejects_duplicates():
    day = date(2026, 11, 27)
    row = SimpleNamespace(date=day, open=datetime(2026, 11, 27, 9, 30), close=datetime(2026, 11, 27, 13))
    class Client:
        def get_calendar(self, request):
            return [row]
    clock = AlpacaClock(Client())
    result = clock.session_range(day, day)
    assert result["sessions"][0]["close"] == "2026-11-27T18:00:00+00:00"
    clock._trading.get_calendar = lambda request: [row, row]
    with pytest.raises(Exception):
        clock.session_range(day, day)
    with pytest.raises(ValueError):
        clock.session_range(day, day + timedelta(days=32))
    clock._trading.get_calendar = lambda request: []
    with pytest.raises(Exception):
        clock.session_range(day, day + timedelta(days=14))


def test_daemon_capture_persists_scoped_calendar_and_cycles_without_raw_detail(tmp_path):
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Guard:
        def ensure_owned(self): pass
    detail = {"session": "2026-11-25", "started_at": START.isoformat(), **SCOPE, "execution": "simulated", "secret": "must-not-persist"}
    class Evidence(FakeEvidence):
        def cycles(self):
            return [SimpleNamespace(detail=detail, result_code="blocked", idempotency_key="key", created_at=START)]
    store = StateStore(tmp_path)
    capture = CampaignCapture(store, Clock(), Evidence(), SCOPE, Guard(), now=lambda: START)
    capture.capture()
    saved = store.load("acceptance-campaign")
    assert saved["calendar"]["source"] == "alpaca_calendar"
    assert saved["cycles"][0]["result"] == "blocked"
    assert saved["cycles"][0]["execution"] == "simulated"
    assert "must-not-persist" not in str(saved)
    assert evaluate(saved, START, saved["calendar"], saved["cycles"])["complete"] is False


def test_fresh_persisted_reconciliation_receipt_has_truthful_method(tmp_path):
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence(FakeEvidence):
        def cycles(self): return []
    store = StateStore(tmp_path)
    store.update("health", lambda _: {"checked_at": START.isoformat(), "checks": [
        {"component": "reconciliation", "status": "pass", "code": "reconciliation_clean"}]})
    CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: START).capture()
    receipts = [r for r in store.load("acceptance-campaign")["receipts"] if r["check"] == "broker_reconciliation"]
    # Unsigned doctor labels cannot produce a trusted broker observation.
    assert not receipts


def test_report_marks_maintenance_and_unsampled_intervals_unknown(tmp_path):
    campaign = start_campaign(StateStore(tmp_path), START, SCOPE)
    campaign["observations"] = [
        {"at": START.isoformat(), "health": "pass", "maintenance": "maintenance_idle"},
        {"at": (START + timedelta(minutes=15)).isoformat(), "health": "unknown", "maintenance": "backup_running"},
        {"at": (START + timedelta(hours=1)).isoformat(), "health": "pass", "maintenance": "maintenance_idle"},
    ]
    report = evaluate(campaign, START + timedelta(hours=1), None, [])
    assert report["observation_coverage"]["planned_maintenance_samples"] == 1
    assert report["observation_coverage"]["gaps"] == 1
    assert report["continuous_uptime"] == "unknown"


def test_capture_cli_reports_unstarted_campaign_without_secrets(tmp_path, capsys):
    from trading_assistant.ops.acceptance_campaign import report_for_store
    result = report_for_store(StateStore(tmp_path), START)
    assert result == {"status": "not_started", "complete": False, "restart_status": "pending"}


def test_monitor_schedules_only_one_campaign_capture():
    import asyncio
    import threading
    from trading_assistant.daemon.monitor import Monitor
    started, release = threading.Event(), threading.Event()
    class Capture:
        calls = 0
        def capture(self):
            self.calls += 1
            started.set()
            release.wait(2)
    async def run():
        monitor = object.__new__(Monitor)
        monitor.campaign_capture = Capture()
        monitor._campaign_task = None
        monitor._schedule_campaign_capture()
        await asyncio.to_thread(started.wait, 1)
        monitor._schedule_campaign_capture()
        assert monitor.campaign_capture.calls == 1
        release.set()
        await monitor._campaign_task
    asyncio.run(run())


def test_daemon_builds_campaign_capture_even_when_autopilot_off(tmp_path):
    from trading_assistant.daemon.main import _build_campaign_capture
    from trading_assistant.risk.clock import FakeClock
    config = SimpleNamespace(autopilot=SimpleNamespace(mode="off", strategy="test"))
    service = SimpleNamespace(market_clock=lambda _: FakeClock(), session_factory=object())
    capture = _build_campaign_capture(config, service, None, tmp_path,
                                      config_fingerprint=lambda _: "a" * 64,
                                      code_fingerprint=lambda _: "b" * 64)
    assert capture.scope["mode"] == "off"


def test_changed_scope_is_visible_and_cannot_relabel_old_campaign(tmp_path):
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence(FakeEvidence):
        def cycles(self): return []
    store = StateStore(tmp_path)
    start_campaign(store, START, SCOPE)
    changed = {**SCOPE, "code_fingerprint": "c" * 64}
    CampaignCapture(store, Clock(), Evidence(), changed, None, now=lambda: START + timedelta(hours=1)).capture()
    from trading_assistant.ops.acceptance_campaign import report_for_store
    report = report_for_store(store, START + timedelta(hours=1), cipher=TEST_CIPHER)
    assert report["scope"] == SCOPE
    assert report["current_scope"] == changed
    assert report["scope_changed"] is True
    assert report["complete"] is False


def test_previous_scope_pass_is_not_a_current_scope_pass(tmp_path):
    campaign = start_campaign(StateStore(tmp_path), START, SCOPE)
    campaign["current_scope"] = {**SCOPE, "code_fingerprint": "c" * 64}
    campaign["receipts"] = [{"schema_version": 1, "check": "broker_order_reconciliation", "method": "persisted_broker_reconciliation",
        "source": "daemon_reconciliation", "outcome": "pass", "observed_at": START.isoformat(), "captured_at": START.isoformat(), "scope": SCOPE,
        "reference": "sha256:" + "a" * 64}]
    report = evaluate(campaign, START, None, [], receipt_verifier=lambda _: True)
    assert report["checks"]["broker_order_reconciliation"] == "unknown"
    assert report["latest_receipts"]["broker_order_reconciliation"]["outcome"] == "pass"


def test_fake_clock_requires_explicit_schedule_and_labels_it_mock():
    from trading_assistant.risk.clock import FakeClock
    day = date(2026, 11, 27)
    with pytest.raises(ValueError):
        FakeClock().session_range(day, day)
    clock = FakeClock(schedule=[{"date": day.isoformat(), "open": "2026-11-27T14:30:00+00:00", "close": "2026-11-27T18:00:00+00:00"}])
    assert clock.session_range(day, day)["source"] == "mock_calendar"


def test_reconciliation_evidence_requires_valid_fresh_completion():
    from trading_assistant.ops.acceptance_campaign import reconciliation_outcome
    row = SimpleNamespace(generation=1, completed_generation=1, status="current", started_at=START,
                          completed_at=START, updated_at=START)
    assert reconciliation_outcome(row, START + timedelta(seconds=20), 30) == "pass"
    assert reconciliation_outcome(row, START + timedelta(seconds=31), 30) == "unknown"
    row.updated_at = START + timedelta(seconds=60)
    assert reconciliation_outcome(row, START + timedelta(seconds=20), 30) == "unknown"


def test_daemon_order_reconciliation_uses_latest_real_audit_and_age():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from trading_assistant.db.models import AuditEvent
    from trading_assistant.ops.acceptance_campaign import read_daemon_order_reconciliation
    engine = create_engine("sqlite://")
    AuditEvent.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    def write(at, result, actor="daemon:monitor"):
        with factory.begin() as session:
            session.add(AuditEvent(actor=actor, action="orders.sync", target_type="broker_orders", target_id="all",
                                   request_id="fixture", reason="fixture", result_code=result, created_at=at))
    write(START, "reconciled")
    assert read_daemon_order_reconciliation(factory, START + timedelta(seconds=20), 30)[0] == "unknown"
    assert read_daemon_order_reconciliation(factory, START + timedelta(seconds=31), 30)[0] == "unknown"
    write(START + timedelta(seconds=21), "reconciliation_required")
    assert read_daemon_order_reconciliation(factory, START + timedelta(seconds=22), 30)[0] == "unknown"
    write(START + timedelta(seconds=40), "reconciled")
    assert read_daemon_order_reconciliation(factory, START + timedelta(seconds=22), 30)[0] == "unknown"


def test_stale_pass_receipt_is_historical_not_current(tmp_path):
    campaign = start_campaign(StateStore(tmp_path), START, SCOPE)
    campaign["receipts"] = [{"schema_version": 1, "check": "broker_order_reconciliation", "method": "persisted_broker_reconciliation",
        "source": "daemon_reconciliation", "outcome": "pass", "observed_at": START.isoformat(), "captured_at": START.isoformat(), "scope": SCOPE,
        "reference": "sha256:" + "a" * 64}]
    report = evaluate(campaign, START + timedelta(hours=2), None, [], receipt_verifier=lambda _: True)
    assert report["checks"]["broker_order_reconciliation"] == "unknown"
    assert report["latest_receipts"]["broker_order_reconciliation"]["outcome"] == "pass"


def test_maintenance_only_doctor_check_does_not_claim_runtime_healthy(tmp_path):
    import importlib.util
    from pathlib import Path
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence(FakeEvidence):
        def cycles(self): return []
    store = StateStore(tmp_path)
    spec = importlib.util.spec_from_file_location("runtime_operations", Path("scripts/runtime_operations.py"))
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)
    doctor.record_checks(store, [{"component": "maintenance", "status": "pass", "code": "backup_running"}], START, cipher=TEST_CIPHER)
    CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: START).capture()
    sample = store.load("acceptance-campaign")["observations"][0]
    assert sample["h"] == "unknown"
    assert sample["m"] == "backup_running"


def test_receipt_failure_then_recovery_retains_both_transitions(tmp_path):
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence(FakeEvidence):
        def cycles(self): return []
    current = [START]
    outcome = ["fail"]
    store = StateStore(tmp_path)
    capture = CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: current[0],
                              order_reconciliation_reader=lambda at: (outcome[0], "a" * 64, at))
    capture.capture()
    current[0] += timedelta(minutes=31)
    outcome[0] = "pass"
    capture.capture()
    assert [r["outcome"] for r in store.load("acceptance-campaign")["receipts"] if r["check"] == "broker_order_reconciliation"] == ["fail", "pass"]


def test_runtime_safety_failure_is_visible_as_a_receipt(tmp_path):
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence(FakeEvidence):
        def cycles(self): return []
    store = StateStore(tmp_path)
    store.update("health", lambda _: {"checked_at": START.isoformat(), "checks": [
        {"component": "safety", "status": "fail", "code": "safety_latched"}]})
    CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: START).capture()
    from trading_assistant.ops.acceptance_campaign import report_for_store
    report = report_for_store(store, START, cipher=TEST_CIPHER)
    assert report["checks"]["runtime_health"] == "fail"
    assert report["latest_receipts"]["runtime_health"]["method"] == "real_local_runtime"


def test_completed_window_does_not_displace_campaign_observations(tmp_path):
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence(FakeEvidence):
        def cycles(self): return []
    store = StateStore(tmp_path)
    start_campaign(store, START, SCOPE)
    capture = CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: START + timedelta(days=31))
    capture.capture()
    assert store.load("acceptance-campaign").get("observations", []) == []


def test_missing_first_and_latest_samples_are_explicit_gaps(tmp_path):
    campaign = start_campaign(StateStore(tmp_path), START, SCOPE)
    campaign["observations"] = [{"t": (START + timedelta(hours=1)).isoformat(), "h": "pass", "m": "maintenance_idle"}]
    coverage = evaluate(campaign, START + timedelta(hours=2), None, [])["observation_coverage"]
    assert coverage["gaps"] == 2


def test_mock_calendar_cannot_prove_real_session_was_missed(tmp_path):
    campaign = start_campaign(StateStore(tmp_path), START, SCOPE)
    calendar = {"start": "2026-11-25", "end": "2026-11-25", "source": "mock_calendar", "sessions": [
        {"date": "2026-11-25", "open": "2026-11-25T14:30:00+00:00", "close": "2026-11-25T21:00:00+00:00"}]}
    report = evaluate(campaign, START + timedelta(days=1), calendar, [])
    assert report["sessions"][0]["status"] == "unknown"
    assert report["calendar_complete"] is False


def test_unspecified_evidence_coverage_cannot_prove_a_missed_session(tmp_path):
    campaign = start_campaign(StateStore(tmp_path), START, SCOPE)
    calendar = {"start": "2026-11-25", "end": "2026-11-25", "source": "alpaca_calendar", "sessions": [
        {"date": "2026-11-25", "open": "2026-11-25T14:30:00+00:00", "close": "2026-11-25T21:00:00+00:00"}]}
    assert evaluate(campaign, START + timedelta(days=1), calendar, [])["sessions"][0]["status"] == "unknown"


def test_changed_calendar_is_exposed_and_does_not_assert_session_coverage(tmp_path):
    campaign = start_campaign(StateStore(tmp_path), START, SCOPE)
    campaign["calendar_revisions"] = 1
    calendar = {"start": "2026-11-25", "end": "2026-12-25", "source": "alpaca_calendar", "sessions": [
        {"date": "2026-11-25", "open": "2026-11-25T14:30:00+00:00", "close": "2026-11-25T21:00:00+00:00"}]}
    report = evaluate(campaign, START + timedelta(days=1), calendar, [])
    assert report["calendar_complete"] is False
    assert report["sessions"][0]["status"] == "unknown"
    assert report["calendar_revisions"] == 1


def test_local_receipts_refresh_each_minute_without_requerying_calendar(tmp_path):
    class Clock:
        calls = 0
        def session_range(self, start, end):
            self.calls += 1
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence(FakeEvidence):
        def cycles(self): return []
    now = [START]
    store, clock = StateStore(tmp_path), Clock()
    capture = CampaignCapture(store, clock, Evidence(), SCOPE, None, now=lambda: now[0],
                              order_reconciliation_reader=lambda at: ("pass", "a" * 64, at))
    capture.capture()
    now[0] += timedelta(minutes=1)
    capture.capture()
    saved = store.load("acceptance-campaign")
    assert saved["captured_at"] == now[0].isoformat()
    assert len(saved["observations"]) == 1
    assert clock.calls == 1
    assert evaluate(saved, now[0] + timedelta(minutes=2), saved["calendar"], [], receipt_verifier=lambda _: True)["checks"]["broker_order_reconciliation"] == "pass"


def test_versions_exclude_incompatible_evidence(tmp_path):
    campaign = start_campaign(StateStore(tmp_path), START, SCOPE)
    cycles = [{"session": "2026-11-25", "result": "blocked", "key": "one", **SCOPE, "code_identity": "a" * 40},
              {"session": "2026-11-25", "result": "blocked", "key": "two", **SCOPE, "code_fingerprint": "c" * 64, "code_identity": "b" * 40}]
    assert evaluate(campaign, START, None, cycles)["code_versions"] == ["a" * 40]


def test_audit_receipt_expires_from_source_time_not_capture_time(tmp_path):
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence(FakeEvidence):
        def cycles(self): return []
    at = START + timedelta(minutes=2)
    store = StateStore(tmp_path)
    start_campaign(store, START, SCOPE)
    capture = CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: at,
                              order_reconciliation_reader=lambda _: ("pass", "a" * 64, START))
    capture.capture()
    report = evaluate(store.load("acceptance-campaign"), START + timedelta(minutes=4), None, [], receipt_verifier=lambda _: True)
    receipt = report["latest_receipts"]["broker_order_reconciliation"]
    assert receipt["captured_at"] == at.isoformat()
    assert receipt["observed_at"] == START.isoformat()
    assert report["checks"]["broker_order_reconciliation"] == "unknown"


def test_report_displays_running_code_identity_even_without_cycles(tmp_path):
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence(FakeEvidence):
        def cycles(self): return []
    store = StateStore(tmp_path)
    CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: START, runtime_code="d" * 40).capture()
    from trading_assistant.ops.acceptance_campaign import report_for_store
    assert report_for_store(store, START, cipher=TEST_CIPHER)["runtime_code"] == "d" * 40


def test_failed_audit_read_does_not_leave_prior_pass_as_current(tmp_path):
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence(FakeEvidence):
        def cycles(self): return []
    now = [START]
    def reader(_):
        if now[0] == START: return "pass", "a" * 64, now[0]
        raise OSError("sensitive error should not persist")
    store = StateStore(tmp_path)
    capture = CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: now[0], order_reconciliation_reader=reader)
    capture.capture()
    now[0] += timedelta(minutes=1)
    capture.capture()
    from trading_assistant.ops.acceptance_campaign import report_for_store
    assert report_for_store(store, now[0], cipher=TEST_CIPHER)["checks"]["broker_order_reconciliation"] == "unknown"
    assert "sensitive error" not in str(store.load("acceptance-campaign"))


def test_period_progress_ends_after_thirty_calendar_days(tmp_path):
    campaign = start_campaign(StateStore(tmp_path), START, SCOPE)
    assert evaluate(campaign, START + timedelta(days=29), None, [])["period_elapsed"] is False
    result = evaluate(campaign, START + timedelta(days=30), None, [])
    assert result["period_elapsed"] is True
    assert result["complete"] is False


def test_official_calendar_preserves_dst_boundaries():
    before = SimpleNamespace(date=date(2026, 3, 6), open=datetime(2026, 3, 6, 9, 30), close=datetime(2026, 3, 6, 16))
    after = SimpleNamespace(date=date(2026, 3, 9), open=datetime(2026, 3, 9, 9, 30), close=datetime(2026, 3, 9, 16))
    class Client:
        def get_calendar(self, request): return [before, after]
    sessions = AlpacaClock(Client()).session_range(before.date, after.date)["sessions"]
    assert sessions[0]["open"] == "2026-03-06T14:30:00+00:00"
    assert sessions[1]["open"] == "2026-03-09T13:30:00+00:00"


def test_partial_first_session_is_not_retroactively_missed(tmp_path):
    start = datetime(2026, 11, 25, 17, tzinfo=UTC)
    campaign = start_campaign(StateStore(tmp_path), start, SCOPE)
    calendar = {"start": "2026-11-25", "end": "2026-11-27", "source": "alpaca_calendar", "sessions": [
        {"date": "2026-11-25", "open": "2026-11-25T14:30:00+00:00", "close": "2026-11-25T21:00:00+00:00"},
        {"date": "2026-11-27", "open": "2026-11-27T14:30:00+00:00", "close": "2026-11-27T18:00:00+00:00"}]}
    result = evaluate(campaign, datetime(2026, 11, 27, 19, tzinfo=UTC), calendar, [], evidence_complete=True, evidence_through=datetime(2026, 11, 27, 19, tzinfo=UTC))
    assert [s["status"] for s in result["sessions"]] == ["not_scheduled", "missed"]


def test_scan_horizon_prevents_stale_collector_from_inventing_month_coverage(tmp_path):
    campaign = start_campaign(StateStore(tmp_path), START, SCOPE)
    calendar = {"start": "2026-11-25", "end": "2026-12-25", "source": "alpaca_calendar", "sessions": [
        {"date": "2026-11-27", "open": "2026-11-27T14:30:00+00:00", "close": "2026-11-27T18:00:00+00:00"}]}
    report = evaluate(campaign, START + timedelta(days=31), calendar, [], evidence_complete=True,
                      evidence_through=START)
    assert report["sessions"][0]["status"] == "unknown"
    assert report["session_accounting_complete"] is False
    report = evaluate(campaign, START + timedelta(days=31), calendar, [], evidence_complete=True,
                      evidence_through=START + timedelta(days=31))
    assert report["sessions"][0]["status"] == "missed"


def test_session_becoming_due_after_campaign_end_is_not_scheduled(tmp_path):
    campaign = start_campaign(StateStore(tmp_path), START, SCOPE)
    end = START + timedelta(days=30)
    opened = end - timedelta(minutes=15)
    close = end + timedelta(hours=5)
    calendar = {"start": "2026-11-25", "end": "2026-12-25", "source": "alpaca_calendar", "sessions": [
        {"date": opened.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).date().isoformat(),
         "open": opened.isoformat(), "close": close.isoformat()}]}
    result = evaluate(campaign, end + timedelta(days=1), calendar, [], evidence_complete=True,
                      evidence_through=end + timedelta(days=1))
    assert result["sessions"][0]["status"] == "not_scheduled"


def test_cycle_window_limits_rows_before_decryption(session_factory, monkeypatch):
    from trading_assistant.autopilot.evidence import EvidenceStore
    from trading_assistant.security.sensitive_fields import sensitive_store
    from trading_assistant.db.models import AuditEvent
    from sqlalchemy import event
    store = EvidenceStore(session_factory, actor="fixture")
    from zoneinfo import ZoneInfo
    day = datetime.now(UTC).astimezone(ZoneInfo("America/New_York")).date()
    for i in range(4):
        store.record_cycle(key=f"fixture-{i}", session=day, result_code="observed", detail={"started_at": START.isoformat()})
    captured = []
    def before_execute(state):
        if state.is_select and "audit_events" in str(state.statement):
            captured.append((str(state.statement), state.session.connection().connection.driver_connection.in_transaction))
    from sqlalchemy.orm import Session
    event.listen(Session, "do_orm_execute", before_execute)
    try:
        rows, overflow = store.cycles_in_window(START - timedelta(days=1000), datetime.now(UTC) + timedelta(days=1), limit=2)
    finally:
        event.remove(Session, "do_orm_execute", before_execute)
    assert len(rows) == 2 and overflow is True
    assert any("LIMIT" in statement.upper() and "created_at" in statement and in_transaction for statement, in_transaction in captured)


def test_cycle_window_selects_scope_key_before_loading(session_factory):
    from trading_assistant.autopilot.evidence import EvidenceStore, cycle_key
    from trading_assistant.autopilot.identity import evidence_fingerprint
    store = EvidenceStore(session_factory, actor="fixture")
    now = datetime.now(UTC)
    from zoneinfo import ZoneInfo
    day = now.astimezone(ZoneInfo("America/New_York")).date()
    valid = cycle_key(SCOPE["mode"], day, evidence_fingerprint(SCOPE["config_fingerprint"], SCOPE["code_fingerprint"]))
    store.record_cycle(key=valid, session=day, result_code="observed", detail={**SCOPE, "marker": "scoped", "session": day.isoformat()})
    for i in range(5):
        store.record_cycle(key=f"different-{i}", session=day, result_code="observed", detail={"marker": "other"})
    rows, overflow = store.cycles_in_window(now - timedelta(minutes=1), now + timedelta(minutes=1), limit=2, scope=SCOPE)
    assert not overflow
    assert [r.detail["marker"] for r in rows] == ["scoped"]


def test_cycle_window_rejects_encrypted_session_and_target_mismatch(session_factory):
    from sqlalchemy import select
    from trading_assistant.autopilot.evidence import EvidenceStore, cycle_key
    from trading_assistant.autopilot.identity import evidence_fingerprint
    from trading_assistant.db.models import AuditEvent
    from zoneinfo import ZoneInfo
    store = EvidenceStore(session_factory, actor="fixture")
    now = datetime.now(UTC)
    day = now.astimezone(ZoneInfo("America/New_York")).date()
    key = cycle_key(SCOPE["mode"], day, evidence_fingerprint(SCOPE["config_fingerprint"], SCOPE["code_fingerprint"]))
    store.record_cycle(key=key, session=day, result_code="observed", detail={**SCOPE, "session": day.isoformat()})
    with session_factory.begin() as session:
        event = session.scalar(select(AuditEvent).where(AuditEvent.action == "autopilot.cycle"))
        event.target_id = (day - timedelta(days=1)).isoformat()
    rows, incomplete = store.cycles_in_window(now - timedelta(days=1), now + timedelta(minutes=1), scope=SCOPE)
    assert rows == []
    assert incomplete is True


def test_reconciliation_requires_encrypted_bound_producer_metadata(make_service, session_factory):
    from trading_assistant.config import BrokerKind
    from trading_assistant.ops.acceptance_campaign import read_daemon_order_reconciliation
    svc = make_service()
    svc.broker.reconciliation_key = "alpaca"
    svc.config = svc.config.model_copy(update={"trading": svc.config.trading.model_copy(update={"broker": BrokerKind.ALPACA})})
    svc.sync_open_orders(actor="daemon:monitor", reason="fixture", request_id="fixture")
    now = datetime.now(UTC) + timedelta(seconds=1)
    assert read_daemon_order_reconciliation(session_factory, now, 300)[0] == "pass"
    from sqlalchemy import select
    from trading_assistant.db.models import AuditEvent
    from trading_assistant.security.sensitive_fields import sensitive_store
    import json
    with session_factory.begin() as session:
        event = session.scalar(select(AuditEvent).where(AuditEvent.action == "orders.sync").order_by(AuditEvent.id.desc()))
        detail = json.loads(sensitive_store(session).read(event, "detail_json"))
        detail["campaign_observation"]["broker"] = "mock"
        sensitive_store(session).write_many(event, {"detail_json": json.dumps(detail)})
    assert read_daemon_order_reconciliation(session_factory, now, 300)[0] == "unknown"


@pytest.mark.parametrize("damage", ["corrupt", "target", "source", "contradictory_detail"])
def test_position_reconciliation_producer_proof_rejects_tamper(make_service, session_factory, damage):
    import sqlite3
    from sqlalchemy import select
    from trading_assistant.config import BrokerKind
    from trading_assistant.db.models import AuditEvent
    from trading_assistant.ops.acceptance_campaign import read_position_reconciliation
    from trading_assistant.security.sensitive_fields import sensitive_store
    import json
    svc = make_service()
    svc.broker.reconciliation_key = "alpaca"
    svc.config = svc.config.model_copy(update={"trading": svc.config.trading.model_copy(update={"broker": BrokerKind.ALPACA})})
    svc.reconcile_positions(actor="daemon:startup", reason="fixture", request_id="fixture")
    now = datetime.now(UTC) + timedelta(seconds=1)
    assert read_position_reconciliation(session_factory, now, 300)[0] == "pass"
    if damage == "corrupt":
        with sqlite3.connect(session_factory.kw["bind"].url.database) as connection:
            connection.execute("UPDATE audit_events SET detail_json='bad' WHERE action='positions.reconcile'")
    else:
        with session_factory.begin() as session:
            event = session.scalar(select(AuditEvent).where(AuditEvent.action == "positions.reconcile"))
            if damage == "target":
                event.target_type = "other"
            else:
                detail = json.loads(sensitive_store(session).read(event, "detail_json"))
                if damage == "source":
                    detail["campaign_observation"]["configured_broker"] = "mock"
                else:
                    detail["drift"] = {"AAPL": {"broker": "1", "local": "0"}}
                sensitive_store(session).write_many(event, {"detail_json": json.dumps(detail)})
    assert read_position_reconciliation(session_factory, now, 300)[0] == "unknown"


def test_reconciliation_receipt_builder_rejects_missing_source_time():
    from trading_assistant.ops.acceptance_campaign import reconciliation_receipt
    receipt = reconciliation_receipt("broker_order_reconciliation", lambda _: ("pass", "a" * 64, None), START, SCOPE)
    assert receipt["outcome"] == "unknown"
    assert receipt["source"] == "daemon_reconciliation"


def test_unknown_backup_observation_replaces_prior_pass(tmp_path):
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence(FakeEvidence):
        def cycles(self): return []
    now = [START]
    store = StateStore(tmp_path)
    def health(code, status):
        store.update("health", lambda _: {"checked_at": now[0].isoformat(), "checks": [{"component": "backup", "code": code, "status": status}]})
    backup_outcome = ["pass"]
    capture = CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: now[0],
                              backup_reader=lambda at: (backup_outcome[0], "a" * 64, at))
    health("backup_recent", "pass")
    capture.capture()
    now[0] += timedelta(minutes=1)
    backup_outcome[0] = "unknown"
    health("backup_unknown", "unknown")
    capture.capture()
    backup = [r for r in store.load("acceptance-campaign")["receipts"] if r["check"] == "backup"]
    assert [r["outcome"] for r in backup] == ["pass", "unknown"]


def test_campaign_accepts_signed_doctor_only_and_rejects_edited_pass(tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("runtime_operations", Path("scripts/runtime_operations.py"))
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence(FakeEvidence):
        def cycles(self): return []
    checks = [{"component": component, "code": code, "status": "pass"} for component, code in {
        "database": "database_readable", "daemon": "heartbeat_fresh", "safety": "safety_clear",
        "reconciliation": "reconciliation_clean", "maintenance": "maintenance_idle", "jobs": "jobs_loaded",
        "backup": "backup_recent", "notifications": "telegram_current", "power": "ac_awake",
        "app": "app_live", "release": "release_verified"}.items()]
    store = StateStore(tmp_path)
    start_campaign(store, START, SCOPE)
    doctor.record_checks(store, checks, START, cipher=TEST_CIPHER)
    doctor.record_checks(store, checks, START + timedelta(minutes=1), cipher=TEST_CIPHER)
    now = [START + timedelta(minutes=1, seconds=1)]
    capture = CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: now[0])
    capture.capture()
    from trading_assistant.ops.acceptance_campaign import report_for_store
    current = report_for_store(store, now[0], cipher=TEST_CIPHER)
    assert current["checks"]["runtime_health"] == "pass"
    assert current["latest_receipts"]["runtime_health"]["observation_auth"] == "doctor_attested"
    def tamper(old):
        changed = [dict(c) for c in old["checks"]]
        changed[0]["code"] = "database_unreadable"
        return {**old, "checks": changed}
    store.update("health", tamper)
    now[0] += timedelta(minutes=1)
    capture.capture()
    assert report_for_store(store, now[0], cipher=TEST_CIPHER)["checks"]["runtime_health"] == "unknown"


def test_backup_reader_checks_actual_committed_header_and_age(tmp_path):
    import sqlite3
    from trading_assistant.ops.backup import create_encrypted_database_backup
    from trading_assistant.ops.acceptance_campaign import read_backup_artifacts
    db = tmp_path / "fixture.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE fixture (id INTEGER)")
    db.chmod(0o600)
    destination = tmp_path / ".local/encrypted-backups"
    create_encrypted_database_backup(db, destination, backup_key=b"b" * 32, backup_key_id="test-backup-key",
        schema_head="20260730_0018", artifact_label="whole-database-v1", now=lambda: START)
    assert read_backup_artifacts(tmp_path, START)[0] == "pass"
    assert read_backup_artifacts(tmp_path, START + timedelta(hours=37))[0] == "fail"
    assert read_backup_artifacts(tmp_path, START - timedelta(seconds=1))[0] == "unknown"
    assert read_backup_artifacts(tmp_path, START, max_total_bytes=1)[0] == "unknown"
    from trading_assistant.ops.acceptance_campaign import reconciliation_receipt
    receipt = reconciliation_receipt("backup", lambda at: read_backup_artifacts(tmp_path, at, max_total_bytes=1), START, SCOPE)
    assert receipt["outcome"] == "unknown" and receipt["code"] == "backup_size_unsupported"


def test_authenticated_campaign_rejects_tamper_and_fits_private_store(tmp_path, sensitive_cipher):
    from trading_assistant.ops.acceptance_campaign import seal_campaign, report_for_store, bound_campaign_history
    store = StateStore(tmp_path)
    state = start_campaign(store, START, SCOPE)
    state["receipts"] = [{"schema_version": 1, "check": "backup", "method": "real_local_backup", "source": "backup", "outcome": "pass",
                          "observed_at": START.isoformat(), "captured_at": START.isoformat(), "scope": SCOPE, "reference": "sha256:" + "a" * 64}]
    template = state["receipts"][0]
    state["receipts"] = [{**template, "check": "runtime_health", "method": "real_local_runtime", "source": "runtime_doctor", "outcome": "unknown",
                           "details": [{"component": "backup", "code": "backup_unknown", "status": "unknown"}] * 20} for _ in range(191)] + [template]
    state["observations"] = [{"t": (START + timedelta(minutes=i)).isoformat(), "h": "unknown", "m": "unknown"} for i in range(1500)]
    state["cycles"] = []
    state["discarded_observations"] = 0
    state = bound_campaign_history(state)
    sealed = seal_campaign(state, sensitive_cipher)
    store.update("acceptance-campaign", lambda _: sealed)
    assert len(__import__("json").dumps(sealed).encode()) < 256 * 1024
    assert (tmp_path / ".local/operations/acceptance-campaign.json").stat().st_size < 256 * 1024
    assert state["discarded_receipts"] > 0
    assert report_for_store(store, START, cipher=sensitive_cipher)["checks"]["backup"] == "pass"
    store.update("acceptance-campaign", lambda old: {**old, "receipts": [{**old["receipts"][-1], "outcome": "fail"}]})
    assert report_for_store(store, START, cipher=sensitive_cipher)["checks"]["backup"] == "unknown"


def test_attestation_is_bound_to_campaign_and_fails_without_keys(tmp_path, sensitive_cipher):
    from trading_assistant.ops.acceptance_campaign import seal_campaign, verify_campaign
    state = start_campaign(StateStore(tmp_path), START, SCOPE)
    sealed = seal_campaign(state, sensitive_cipher)
    assert verify_campaign(sealed, sensitive_cipher)
    assert not verify_campaign({**sealed, "scope": {**SCOPE, "code_fingerprint": "c" * 64}}, sensitive_cipher)
    assert not verify_campaign({**sealed, "started_at": (START + timedelta(days=1)).isoformat()}, sensitive_cipher)
    assert not verify_campaign(sealed, None)


def test_malformed_receipt_is_unknown_and_not_reflected(tmp_path):
    campaign = start_campaign(StateStore(tmp_path), START, SCOPE)
    campaign["receipts"] = [{"check": "broker_order_reconciliation", "method": "isolated_mock_drill", "outcome": "pass", "secret": "do-not-print"}]
    report = evaluate(campaign, START, None, [], receipt_verifier=lambda _: True)
    assert report["checks"]["broker_order_reconciliation"] == "unknown"
    assert "do-not-print" not in str(report)


def test_lifecycle_receipt_does_not_echo_untyped_nested_metadata(tmp_path):
    campaign = start_campaign(StateStore(tmp_path), START, SCOPE)
    campaign["receipts"] = [{"schema_version": 1, "check": "lifecycle", "method": "persisted_broker_lifecycle", "source": "encrypted_lifecycle",
        "outcome": "pass", "observed_at": START.isoformat(), "captured_at": START.isoformat(), "scope": SCOPE,
        "reference": "sha256:" + "a" * 64, "transition": "paper_order_filled_exact", "time_basis": "event_time",
        "orders": [{"outcome": "unknown", "order_digest": "sha256:" + "b" * 64, "reference": "do-not-print"}]}]
    report = evaluate(campaign, START, None, [], receipt_verifier=lambda _: True)
    assert report["checks"]["lifecycle"] == "unknown"
    assert "do-not-print" not in str(report)


def test_report_clock_rollback_does_not_restore_prior_pass(tmp_path):
    campaign = start_campaign(StateStore(tmp_path), START, SCOPE)
    def receipt(at, outcome):
        return {"schema_version": 1, "check": "backup", "method": "real_local_backup", "source": "backup", "outcome": outcome,
                "observed_at": at.isoformat(), "captured_at": at.isoformat(), "scope": SCOPE, "reference": "sha256:" + "a" * 64}
    campaign["receipts"] = [receipt(START, "pass"), receipt(START + timedelta(minutes=1), "fail")]
    campaign["captured_at"] = (START + timedelta(minutes=1)).isoformat()
    report = evaluate(campaign, START + timedelta(seconds=30), None, [], receipt_verifier=lambda _: True)
    assert report["checks"]["backup"] == "unknown"
    assert report["latest_receipts"]["backup"]["outcome"] == "fail"
    assert report["report_clock_rollback"] is True
    campaign.pop("captured_at")
    report = evaluate(campaign, START + timedelta(seconds=30), None, [], receipt_verifier=lambda _: True)
    assert report["checks"]["backup"] == "unknown"
    assert report["report_clock_rollback"] is True


def test_day_31_recovery_is_not_campaign_credit(tmp_path):
    campaign = start_campaign(StateStore(tmp_path), START, SCOPE)
    at = START + timedelta(days=31)
    campaign["post_window_receipts"] = [{"schema_version": 1, "check": "recovery", "method": "real_local_runtime", "source": "encrypted_breaker_audit",
        "outcome": "pass", "observed_at": at.isoformat(), "captured_at": at.isoformat(), "scope": SCOPE,
        "transition": "operator_global_reset", "time_basis": "event_time", "reference": "sha256:" + "a" * 64}]
    report = evaluate(campaign, at, None, [], receipt_verifier=lambda _: True)
    assert report["checks"]["recovery"] == "unknown"
    assert report["latest_receipts"]["recovery"]["outcome"] == "pass"
    assert report["latest_receipts"]["recovery"]["event_scope"] == "post_window"


def test_controlled_restart_is_explicitly_pending_without_proof(tmp_path):
    campaign = start_campaign(StateStore(tmp_path), START, SCOPE)
    report = evaluate(campaign, START, None, [])
    assert report["checks"]["restart"] == "unknown"
    assert report["restart_status"] == "pending"


def test_post_window_ci_handoff_cannot_backdate_undated_mock_drill(tmp_path):
    campaign = start_campaign(StateStore(tmp_path), START, SCOPE)
    end = START + timedelta(days=30)
    receipt = {"schema_version": 1, "check": "safety_drill", "method": "isolated_mock_drill", "source": "validated_ci_handoff",
               "outcome": "pass", "observed_at": (end - timedelta(minutes=1)).isoformat(), "captured_at": (end + timedelta(minutes=1)).isoformat(),
               "scope": SCOPE, "transition": "ci_isolated_mock_drill", "time_basis": "verifier_finish_before_ci_drill", "reference": "sha256:" + "a" * 64}
    campaign["post_window_receipts"] = [receipt]
    report = evaluate(campaign, end + timedelta(minutes=1), None, [], receipt_verifier=lambda _: True)
    assert report["checks"]["safety_drill"] == "unknown"


def test_post_window_recaptures_do_not_evict_in_window_receipts(tmp_path):
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence(FakeEvidence):
        def cycles(self): return []
    now = [START]
    store = StateStore(tmp_path)
    capture = CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: now[0],
                              order_reconciliation_reader=lambda at: ("pass", "a" * 64, at))
    capture.capture()
    original = list(store.load("acceptance-campaign")["receipts"])
    now[0] += timedelta(days=31)
    capture.capture()
    state = store.load("acceptance-campaign")
    assert state["receipts"] == original
    assert any(r["check"] == "broker_order_reconciliation" for r in state["post_window_receipts"])


def test_pre_campaign_cycle_finishing_after_start_remains_historical(tmp_path):
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence(FakeEvidence):
        def cycles(self):
            return [SimpleNamespace(detail={"session": "2026-11-25", "started_at": (START - timedelta(minutes=1)).isoformat(),
                                           "execution": "simulated", **SCOPE}, result_code="blocked", idempotency_key="prestart", created_at=START)]
    store = StateStore(tmp_path)
    CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: START).capture()
    assert store.load("acceptance-campaign")["cycles"] == []


def test_position_reconciliation_reads_latest_audit_and_drift():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from trading_assistant.db.models import AuditEvent
    from trading_assistant.ops.acceptance_campaign import read_position_reconciliation
    engine = create_engine("sqlite://")
    AuditEvent.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    def write(at, result):
        with factory.begin() as session:
            session.add(AuditEvent(actor="operator", action="positions.reconcile", target_type="portfolio", target_id="all",
                                   request_id="fixture", reason="fixture", result_code=result, created_at=at))
    write(START, "reconciled")
    assert read_position_reconciliation(factory, START + timedelta(seconds=20), 30)[0] == "unknown"
    write(START + timedelta(seconds=21), "drift_detected")
    assert read_position_reconciliation(factory, START + timedelta(seconds=22), 30)[0] == "unknown"
    assert read_position_reconciliation(factory, START + timedelta(seconds=60), 30)[0] == "unknown"


def test_size_bounding_records_discarded_history():
    from trading_assistant.ops.acceptance_campaign import bound_campaign_history
    state = {"receipts": [{"detail": "x" * 1000} for _ in range(200)], "observations": [{"t": "x" * 100} for _ in range(500)],
             "cycles": [], "discarded_receipts": 0, "discarded_observations": 0, "evidence_complete": True}
    bounded = bound_campaign_history(state, limit=100_000)
    assert bounded["discarded_receipts"] > 0
    import json
    assert len(json.dumps(bounded).encode()) <= 100_000


def test_elapsed_calendar_session_accounting_can_finish_without_claiming_uptime(tmp_path):
    campaign = start_campaign(StateStore(tmp_path), START, SCOPE)
    calendar = {"start": "2026-11-25", "end": "2026-12-25", "source": "alpaca_calendar", "sessions": [
        {"date": "2026-11-25", "open": "2026-11-25T14:30:00+00:00", "close": "2026-11-25T21:00:00+00:00"}]}
    cycle = {"session": "2026-11-25", "result": "blocked", "key": "one", **SCOPE}
    report = evaluate(campaign, START + timedelta(days=30), calendar, [cycle], evidence_complete=True, evidence_through=START + timedelta(days=30))
    assert report["session_accounting_complete"] is True
    assert report["continuous_uptime"] == "unknown"
    assert report["complete"] is False


@pytest.mark.parametrize("damage", ["missing", "unreadable", "undated", "unverified", "malformed"])
def test_missing_current_doctor_replaces_recent_pass_with_signed_unknown(tmp_path, monkeypatch, damage):
    import importlib.util
    from pathlib import Path
    from trading_assistant.ops.acceptance_campaign import report_for_store
    spec = importlib.util.spec_from_file_location("runtime_operations", Path("scripts/runtime_operations.py"))
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence(FakeEvidence):
        def cycles(self): return []
    checks = [{"component": component, "code": code, "status": "pass"} for component, code in {
        "database": "database_readable", "daemon": "heartbeat_fresh", "safety": "safety_clear",
        "reconciliation": "reconciliation_clean", "maintenance": "maintenance_idle", "jobs": "jobs_loaded",
        "backup": "backup_recent", "notifications": "telegram_current", "power": "ac_awake",
        "app": "app_live", "release": "release_verified"}.items()]
    store = StateStore(tmp_path)
    start_campaign(store, START, SCOPE)
    doctor.record_checks(store, checks, START, cipher=TEST_CIPHER)
    doctor.record_checks(store, checks, START + timedelta(minutes=1), cipher=TEST_CIPHER)
    now = [START + timedelta(minutes=1, seconds=1)]
    capture = CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: now[0])
    capture.capture()
    assert report_for_store(store, now[0], cipher=TEST_CIPHER)["checks"]["runtime_health"] == "pass"
    original_load = store.load
    if damage == "missing":
        store.update("health", lambda _: {})
    elif damage == "undated":
        store.update("health", lambda old: {**old, "checked_at": "invalid"})
    elif damage == "unverified":
        store.update("health", lambda old: {**old, "checks": old["checks"][:-1]})
    elif damage == "malformed":
        store.update("health", lambda old: {**old, "checks": [{"component": [], "code": "x", "status": "pass"}]})
    else:
        def load(name):
            if name == "health": raise ValueError("unreadable fixture")
            return original_load(name)
        monkeypatch.setattr(store, "load", load)
    now[0] += timedelta(minutes=1)
    capture.capture()
    history = [r for r in original_load("acceptance-campaign")["receipts"] if r["check"] == "runtime_health"]
    assert [r["outcome"] for r in history] == ["pass", "unknown"]
    assert history[-1]["observed_at"] == now[0].isoformat()
    assert report_for_store(store, now[0], cipher=TEST_CIPHER)["checks"]["runtime_health"] == "unknown"


@pytest.mark.parametrize("field,value", [("mode", "observe"), ("config_fingerprint", "c" * 64), ("code_fingerprint", "d" * 64)])
def test_scoped_cycle_disagreement_makes_signed_capture_incomplete(session_factory, tmp_path, field, value):
    from sqlalchemy import select
    from trading_assistant.autopilot.evidence import EvidenceStore, cycle_key
    from trading_assistant.autopilot.identity import evidence_fingerprint
    from trading_assistant.db.models import AuditEvent
    from trading_assistant.ops.acceptance_campaign import report_for_store
    day = date(2026, 11, 27)
    at = datetime(2026, 11, 27, 19, tzinfo=UTC)
    evidence = EvidenceStore(session_factory, actor="fixture")
    key = cycle_key(SCOPE["mode"], day, evidence_fingerprint(SCOPE["config_fingerprint"], SCOPE["code_fingerprint"]))
    detail = {**SCOPE, "session": day.isoformat(), "started_at": "2026-11-27T15:00:00+00:00", field: value}
    evidence.record_cycle(key=key, session=day, result_code="blocked", detail=detail)
    with session_factory.begin() as session:
        session.scalar(select(AuditEvent).where(AuditEvent.action == "autopilot.cycle")).created_at = datetime(2026, 11, 27, 16, tzinfo=UTC)
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": [
                {"date": day.isoformat(), "open": "2026-11-27T14:30:00+00:00", "close": "2026-11-27T18:00:00+00:00"}]}
    rows, incomplete = evidence.cycles_in_window(START, at, scope=SCOPE)
    assert rows == [] and incomplete is True
    state = StateStore(tmp_path)
    start_campaign(state, START, SCOPE)
    CampaignCapture(state, Clock(), evidence, SCOPE, None, now=lambda: at).capture()
    saved = state.load("acceptance-campaign")
    assert saved["evidence_complete"] is False and saved["evidence_through"] is None
    assert report_for_store(state, at, cipher=TEST_CIPHER)["sessions"][0]["status"] == "unknown"
