from datetime import datetime, timedelta, timezone

from trading_assistant.ops.acceptance_campaign import evaluate, start_campaign, CampaignCapture
from trading_assistant.ops.operational_state import StateStore
from trading_assistant.broker.alpaca import AlpacaClock
from datetime import date
from types import SimpleNamespace
import pytest

UTC = timezone.utc
START = datetime(2026, 11, 25, 12, tzinfo=UTC)
SCOPE = {"mode": "paper", "config_fingerprint": "a" * 64, "code_fingerprint": "b" * 64}


def test_calendar_sessions_duplicate_blocked_and_missing_coverage(tmp_path):
    store = StateStore(tmp_path)
    campaign = start_campaign(store, START, SCOPE)
    # Thanksgiving is absent from this complete provider range; Friday closes early.
    calendar = {"start": "2026-11-25", "end": "2026-11-29", "source": "alpaca_calendar", "sessions": [
        {"date": "2026-11-25", "open": "2026-11-25T14:30:00+00:00", "close": "2026-11-25T21:00:00+00:00"},
        {"date": "2026-11-27", "open": "2026-11-27T14:30:00+00:00", "close": "2026-11-27T18:00:00+00:00"}]}
    cycle = {"session": "2026-11-25", "result": "blocked", "key": "same", **SCOPE}
    report = evaluate(campaign, START + timedelta(days=4), calendar, [cycle, cycle], evidence_complete=True)
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
    class Evidence:
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
    class Evidence:
        def cycles(self): return []
    store = StateStore(tmp_path)
    store.update("health", lambda _: {"checked_at": START.isoformat(), "checks": [
        {"component": "reconciliation", "status": "pass", "code": "reconciliation_clean"}]})
    CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: START).capture()
    receipt = next(r for r in store.load("acceptance-campaign")["receipts"] if r["check"] == "broker_reconciliation")
    assert receipt["method"] == "persisted_broker_reconciliation"
    # Doctor checks generations but not reconciliation age: it cannot prove current broker truth.
    assert receipt["outcome"] == "unknown"
    assert receipt["reference"].startswith("sha256:")


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
    assert result == {"status": "not_started", "complete": False}


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
    class Evidence:
        def cycles(self): return []
    store = StateStore(tmp_path)
    start_campaign(store, START, SCOPE)
    changed = {**SCOPE, "code_fingerprint": "c" * 64}
    CampaignCapture(store, Clock(), Evidence(), changed, None, now=lambda: START + timedelta(hours=1)).capture()
    from trading_assistant.ops.acceptance_campaign import report_for_store
    report = report_for_store(store, START + timedelta(hours=1))
    assert report["scope"] == SCOPE
    assert report["current_scope"] == changed
    assert report["scope_changed"] is True
    assert report["complete"] is False


def test_previous_scope_pass_is_not_a_current_scope_pass(tmp_path):
    campaign = start_campaign(StateStore(tmp_path), START, SCOPE)
    campaign["current_scope"] = {**SCOPE, "code_fingerprint": "c" * 64}
    campaign["receipts"] = [{"check": "broker_order_reconciliation", "method": "persisted_broker_reconciliation",
        "source": "daemon_reconciliation", "outcome": "pass", "observed_at": START.isoformat(), "scope": SCOPE,
        "reference": "sha256:" + "a" * 64}]
    report = evaluate(campaign, START, None, [])
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
    assert read_daemon_order_reconciliation(factory, START + timedelta(seconds=20), 30)[0] == "pass"
    assert read_daemon_order_reconciliation(factory, START + timedelta(seconds=31), 30)[0] == "unknown"
    write(START + timedelta(seconds=21), "reconciliation_required")
    assert read_daemon_order_reconciliation(factory, START + timedelta(seconds=22), 30)[0] == "fail"
    write(START + timedelta(seconds=40), "reconciled")
    assert read_daemon_order_reconciliation(factory, START + timedelta(seconds=22), 30)[0] == "unknown"


def test_stale_pass_receipt_is_historical_not_current(tmp_path):
    campaign = start_campaign(StateStore(tmp_path), START, SCOPE)
    campaign["receipts"] = [{"check": "broker_order_reconciliation", "method": "persisted_broker_reconciliation",
        "source": "daemon_reconciliation", "outcome": "pass", "observed_at": START.isoformat(), "scope": SCOPE,
        "reference": "sha256:" + "a" * 64}]
    report = evaluate(campaign, START + timedelta(hours=2), None, [])
    assert report["checks"]["broker_order_reconciliation"] == "unknown"
    assert report["latest_receipts"]["broker_order_reconciliation"]["outcome"] == "pass"


def test_maintenance_only_doctor_check_does_not_claim_runtime_healthy(tmp_path):
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence:
        def cycles(self): return []
    store = StateStore(tmp_path)
    store.update("health", lambda _: {"checked_at": START.isoformat(), "checks": [
        {"component": "maintenance", "status": "pass", "code": "backup_running"},
        {"component": "monitor", "status": "pass", "code": "monitoring_current"}]})
    CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: START).capture()
    sample = store.load("acceptance-campaign")["observations"][0]
    assert sample["h"] == "unknown"
    assert sample["m"] == "backup_running"


def test_receipt_failure_then_recovery_retains_both_transitions(tmp_path):
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence:
        def cycles(self): return []
    current = [START]
    outcome = ["fail"]
    store = StateStore(tmp_path)
    capture = CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: current[0],
                              order_reconciliation_reader=lambda _: (outcome[0], "a" * 64))
    capture.capture()
    current[0] += timedelta(minutes=31)
    outcome[0] = "pass"
    capture.capture()
    assert [r["outcome"] for r in store.load("acceptance-campaign")["receipts"]] == ["fail", "pass"]


def test_runtime_safety_failure_is_visible_as_a_receipt(tmp_path):
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence:
        def cycles(self): return []
    store = StateStore(tmp_path)
    store.update("health", lambda _: {"checked_at": START.isoformat(), "checks": [
        {"component": "safety", "status": "fail", "code": "safety_latched"}]})
    CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: START).capture()
    from trading_assistant.ops.acceptance_campaign import report_for_store
    report = report_for_store(store, START)
    assert report["checks"]["runtime_health"] == "fail"
    assert report["latest_receipts"]["runtime_health"]["method"] == "real_local_runtime"


def test_completed_window_does_not_displace_campaign_observations(tmp_path):
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence:
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
    class Evidence:
        def cycles(self): return []
    now = [START]
    store, clock = StateStore(tmp_path), Clock()
    capture = CampaignCapture(store, clock, Evidence(), SCOPE, None, now=lambda: now[0],
                              order_reconciliation_reader=lambda _: ("pass", "a" * 64))
    capture.capture()
    now[0] += timedelta(minutes=1)
    capture.capture()
    saved = store.load("acceptance-campaign")
    assert saved["captured_at"] == now[0].isoformat()
    assert len(saved["observations"]) == 1
    assert clock.calls == 1
    assert evaluate(saved, now[0] + timedelta(minutes=2), saved["calendar"], [])["checks"]["broker_order_reconciliation"] == "pass"


def test_versions_exclude_incompatible_evidence(tmp_path):
    campaign = start_campaign(StateStore(tmp_path), START, SCOPE)
    cycles = [{"session": "2026-11-25", "result": "blocked", "key": "one", **SCOPE, "code_identity": "a" * 40},
              {"session": "2026-11-25", "result": "blocked", "key": "two", **SCOPE, "code_fingerprint": "c" * 64, "code_identity": "b" * 40}]
    assert evaluate(campaign, START, None, cycles)["code_versions"] == ["a" * 40]


def test_audit_receipt_expires_from_source_time_not_capture_time(tmp_path):
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence:
        def cycles(self): return []
    at = START + timedelta(minutes=2)
    store = StateStore(tmp_path)
    start_campaign(store, START, SCOPE)
    capture = CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: at,
                              order_reconciliation_reader=lambda _: ("pass", "a" * 64, START))
    capture.capture()
    report = evaluate(store.load("acceptance-campaign"), START + timedelta(minutes=4), None, [])
    receipt = report["latest_receipts"]["broker_order_reconciliation"]
    assert receipt["captured_at"] == at.isoformat()
    assert receipt["observed_at"] == START.isoformat()
    assert report["checks"]["broker_order_reconciliation"] == "unknown"


def test_report_displays_running_code_identity_even_without_cycles(tmp_path):
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence:
        def cycles(self): return []
    store = StateStore(tmp_path)
    CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: START, runtime_code="d" * 40).capture()
    from trading_assistant.ops.acceptance_campaign import report_for_store
    assert report_for_store(store, START)["runtime_code"] == "d" * 40


def test_failed_audit_read_does_not_leave_prior_pass_as_current(tmp_path):
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence:
        def cycles(self): return []
    now = [START]
    def reader(_):
        if now[0] == START: return "pass", "a" * 64
        raise OSError("sensitive error should not persist")
    store = StateStore(tmp_path)
    capture = CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: now[0], order_reconciliation_reader=reader)
    capture.capture()
    now[0] += timedelta(minutes=1)
    capture.capture()
    from trading_assistant.ops.acceptance_campaign import report_for_store
    assert report_for_store(store, now[0])["checks"]["broker_order_reconciliation"] == "unknown"
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
    result = evaluate(campaign, datetime(2026, 11, 27, 19, tzinfo=UTC), calendar, [], evidence_complete=True)
    assert [s["status"] for s in result["sessions"]] == ["not_scheduled", "missed"]


def test_pre_campaign_cycle_finishing_after_start_remains_historical(tmp_path):
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence:
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
    assert read_position_reconciliation(factory, START + timedelta(seconds=20), 30)[0] == "pass"
    write(START + timedelta(seconds=21), "drift_detected")
    assert read_position_reconciliation(factory, START + timedelta(seconds=22), 30)[0] == "fail"
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
    report = evaluate(campaign, START + timedelta(days=30), calendar, [cycle], evidence_complete=True)
    assert report["session_accounting_complete"] is True
    assert report["continuous_uptime"] == "unknown"
    assert report["complete"] is False
