from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import pytest

from trading_assistant.autopilot.evidence import EvidenceRecord
from trading_assistant.db.models import Fill, Order
from trading_assistant.security.sensitive_fields import persist_sensitive
from trading_assistant.service_support import _persist_audit
from trading_assistant.ops.campaign_transitions import read_lifecycle, read_recovery, read_mock_drill

NOW = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
SCOPE = {"mode": "paper", "config_fingerprint": "a" * 64, "code_fingerprint": "b" * 64}


def make_order(factory):
    with factory.begin() as session:
        order = Order(idempotency_key="test-order", ticker="AAPL", side="buy", order_type="market", qty=Decimal("1"),
                      status="filled", broker_order_id="broker-order", acceptance_state="accepted", created_at=NOW,
                      updated_at=NOW + timedelta(seconds=1))
        persist_sensitive(session, order, {"approval_reason": "fixture"})
        session.add(Fill(order_id=order.id, ticker="AAPL", side="buy", qty=Decimal("1"), price=Decimal("100"),
                         broker_fill_id="fill-id", reconciliation_state="trusted", filled_at=NOW + timedelta(seconds=1)))
        session.flush()
        _persist_audit(session, actor="daemon:test", action="order.fill_reconcile", target_type="order", target_id=str(order.id),
                       request_id="fixture", reason="fixture", result_code="filled", created_at=NOW + timedelta(seconds=2))
        return order.id


def cycle(order_id, scope=SCOPE):
    from trading_assistant.autopilot.evidence import cycle_key
    from trading_assistant.autopilot.identity import evidence_fingerprint
    key = cycle_key(scope["mode"], NOW.date(), evidence_fingerprint(scope["config_fingerprint"], scope["code_fingerprint"]))
    return EvidenceRecord("executed", NOW + timedelta(seconds=3), key, {**scope, "code_identity": "d" * 40, "session": NOW.date().isoformat(), "execution": "paper", "started_at": NOW.isoformat(),
        "finished_at": (NOW + timedelta(seconds=3)).isoformat(), "orders": [{"order_id": order_id}]})


def test_terminal_fill_receipt_uses_encrypted_proof_and_scoped_cycle(session_factory):
    order_id = make_order(session_factory)
    found = read_lifecycle(session_factory, [cycle(order_id)], SCOPE, NOW, NOW + timedelta(minutes=1))
    assert found["outcome"] == "pass"
    assert found["transition"] == "paper_order_filled_exact"
    assert found["code_version"] == "d" * 40
    assert "AAPL" not in str(found) and "broker-order" not in str(found)
    assert read_lifecycle(session_factory, [cycle(order_id, {**SCOPE, "code_fingerprint": "c" * 64})], SCOPE, NOW, NOW + timedelta(minutes=1))["outcome"] == "unknown"
    from dataclasses import replace
    forged = replace(cycle(order_id), idempotency_key="forged")
    assert read_lifecycle(session_factory, [forged], SCOPE, NOW, NOW + timedelta(minutes=1))["outcome"] == "unknown"
    with session_factory.begin() as session:
        session.get(Order, order_id).status = "canceled"
    assert read_lifecycle(session_factory, [cycle(order_id)], SCOPE, NOW, NOW + timedelta(minutes=1))["outcome"] == "unknown"


@pytest.mark.parametrize("damage", ["unreadable_proof", "untrusted_fill", "mismatched_quantity"])
def test_lifecycle_tampered_or_unreadable_evidence_is_unknown(session_factory, damage):
    import sqlite3
    order_id = make_order(session_factory)
    if damage == "unreadable_proof":
        database = session_factory.kw["bind"].url.database
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE audit_events SET detail_json = 'tampered' WHERE action = 'order.fill_reconcile'")
    elif damage == "untrusted_fill":
        with session_factory.begin() as session:
            session.query(Fill).filter_by(order_id=order_id).one().reconciliation_state = "fill_reconcile_required"
    else:
        with session_factory.begin() as session:
            session.get(Order, order_id).qty = Decimal("2")
    assert read_lifecycle(session_factory, [cycle(order_id)], SCOPE, NOW, NOW + timedelta(minutes=1))["outcome"] == "unknown"


def test_recovery_uses_guarded_audit_and_rejects_later_retrip(session_factory):
    from trading_assistant.risk.breakers import BreakerScope, BreakerService
    breakers = BreakerService(session_factory)
    scope = BreakerScope.operator_global()
    state = breakers.trip(scope, "fixture", "daemon", request_id="trip", now=NOW)
    health = {"captured_at": NOW.isoformat(), "remote_open_orders": 0, "local_enumeration": "confirmed",
              "other_active_breakers": [], "order_reconciliation": "clean", "position_reconciliation": "clean"}
    breakers.reset(scope, actor="operator", reason="fixture", prior_health=health,
                   expected_generation=state.generation, request_id="reset", now=NOW + timedelta(seconds=1))
    found = read_recovery(session_factory, NOW, NOW + timedelta(minutes=1))
    assert found["outcome"] == "pass"
    assert found["transition"] == "operator_global_reset"
    breakers.trip(scope, "later", "daemon", request_id="retrip", now=NOW + timedelta(minutes=2))
    assert read_recovery(session_factory, NOW, NOW + timedelta(minutes=3))["outcome"] == "unknown"


def test_recovery_rejects_other_current_latch_and_unstructured_health(session_factory):
    from trading_assistant.risk.breakers import BreakerScope, BreakerService
    breakers = BreakerService(session_factory)
    scope = BreakerScope.operator_global()
    state = breakers.trip(scope, "fixture", "daemon", request_id="trip", now=NOW)
    breakers.reset(scope, actor="operator", reason="fixture", prior_health={"healthy": True},
                   expected_generation=state.generation, request_id="reset", now=NOW + timedelta(seconds=1))
    assert read_recovery(session_factory, NOW, NOW + timedelta(minutes=1))["outcome"] == "unknown"


@pytest.mark.parametrize("damage", ["encrypted_audit", "generation", "other_latch"])
def test_recovery_rejects_tamper_and_contradictory_current_state(session_factory, damage):
    import sqlite3
    from trading_assistant.db.models import CircuitBreakerState
    from trading_assistant.risk.breakers import BreakerScope, BreakerService
    breakers = BreakerService(session_factory)
    scope = BreakerScope.operator_global()
    state = breakers.trip(scope, "fixture", "daemon", request_id="trip", now=NOW)
    health = {"captured_at": NOW.isoformat(), "remote_open_orders": 0, "local_enumeration": "confirmed", "other_active_breakers": [],
              "order_reconciliation": "clean", "position_reconciliation": "clean"}
    breakers.reset(scope, actor="operator", reason="fixture", prior_health=health, expected_generation=state.generation,
                   request_id="reset", now=NOW + timedelta(seconds=1))
    if damage == "encrypted_audit":
        with sqlite3.connect(session_factory.kw["bind"].url.database) as connection:
            connection.execute("UPDATE audit_events SET detail_json='tampered' WHERE action='circuit_breaker.reset'")
    elif damage == "generation":
        with session_factory.begin() as session:
            session.get(CircuitBreakerState, "operator_global").generation += 1
    else:
        breakers.trip(BreakerScope.broker_drift(), "fixture", "daemon", request_id="other", now=NOW + timedelta(seconds=2))
    assert read_recovery(session_factory, NOW, NOW + timedelta(minutes=1))["outcome"] == "unknown"


def test_mock_drill_requires_validated_matching_report_and_importer_provenance(tmp_path):
    import hashlib
    import json
    from tests.release_report_fixture import install_contract, report_fixture
    install_contract(tmp_path)
    directory = tmp_path / ".local/verification"
    directory.mkdir(parents=True)
    report = report_fixture("a" * 40)
    raw = json.dumps(report).encode()
    digest = hashlib.sha256(raw).hexdigest()
    path = directory / "release-results.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    provenance = {"source": "github_actions_artifact", "repository": "avinashamanchi/robinhoodassistant",
                  "github_run_id": 123, "github_run_attempt": 1, "artifact_id": 42,
                  "artifact_digest": "sha256:" + "b" * 64, "workflow_path": ".github/workflows/ci.yml",
                  "run_url": "https://github.com/avinashamanchi/robinhoodassistant/actions/runs/123", "report_sha256": digest}
    proof = directory / f"provenance-{digest}.json"
    proof.write_text(json.dumps(provenance))
    proof.chmod(0o600)
    assert read_mock_drill(tmp_path, "a" * 40, datetime(2026, 10, 2, tzinfo=timezone.utc))["outcome"] == "pass"
    assert read_mock_drill(tmp_path, "c" * 40, datetime(2026, 10, 2, tzinfo=timezone.utc))["outcome"] == "unknown"
    provenance["source"] = "local"
    proof.write_text(json.dumps(provenance))
    assert read_mock_drill(tmp_path, "a" * 40, datetime(2026, 10, 2, tzinfo=timezone.utc))["outcome"] == "unknown"


def test_transition_receipts_remain_historical_and_revalidate_current_state(tmp_path):
    from trading_assistant.ops.acceptance_campaign import CampaignCapture, report_for_store
    from trading_assistant.ops.operational_state import StateStore
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    class Evidence:
        def cycles(self): return []
    at = [NOW]
    outcome = ["pass"]
    def transitions(_cycles, _start, _now):
        return {"recovery": {"outcome": outcome[0], "transition": "operator_global_reset", "observed_at": NOW.isoformat(), "reference": "sha256:" + "a" * 64}}
    store = StateStore(tmp_path)
    capture = CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: at[0], transition_reader=transitions)
    capture.capture()
    at[0] += timedelta(minutes=10)
    capture.capture()
    report = report_for_store(store, at[0])
    assert report["checks"]["recovery"] == "pass"
    assert report["latest_receipts"]["recovery"]["observed_at"] == NOW.isoformat()
    outcome[0] = "unknown"
    at[0] += timedelta(minutes=1)
    capture.capture()
    assert report_for_store(store, at[0])["checks"]["recovery"] == "unknown"


def test_encrypted_lifecycle_proof_flows_into_persisted_campaign(session_factory, tmp_path):
    from trading_assistant.ops.acceptance_campaign import CampaignCapture, report_for_store, start_campaign
    from trading_assistant.ops.operational_state import StateStore
    class Clock:
        def session_range(self, start, end):
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "alpaca_calendar", "sessions": []}
    order_id = make_order(session_factory)
    class Evidence:
        def cycles(self): return [cycle(order_id)]
    now = [NOW + timedelta(minutes=1)]
    store = StateStore(tmp_path)
    start_campaign(store, NOW, SCOPE)
    capture = CampaignCapture(store, Clock(), Evidence(), SCOPE, None, now=lambda: now[0],
        transition_reader=lambda cycles, start, at: {"lifecycle": read_lifecycle(session_factory, cycles, SCOPE, start, at)})
    capture.capture()
    report = report_for_store(store, now[0])
    assert report["checks"]["lifecycle"] == "pass"
    assert report["latest_receipts"]["lifecycle"]["method"] == "persisted_broker_lifecycle"
    with session_factory.begin() as session:
        session.get(Order, order_id).status = "canceled"
    now[0] += timedelta(minutes=1)
    capture.capture()
    assert report_for_store(store, now[0])["checks"]["lifecycle"] == "unknown"


def test_lifecycle_rejects_naive_and_future_cycle_evidence(session_factory):
    from dataclasses import replace
    order_id = make_order(session_factory)
    original = cycle(order_id)
    naive = replace(original, detail={**original.detail, "started_at": "2026-11-17T12:00:00"})
    assert read_lifecycle(session_factory, [naive], SCOPE, NOW, NOW + timedelta(minutes=1))["outcome"] == "unknown"
    future = replace(original, created_at=NOW + timedelta(minutes=10))
    assert read_lifecycle(session_factory, [original, future], SCOPE, NOW, NOW + timedelta(minutes=1))["outcome"] == "unknown"


def test_lifecycle_reads_production_encrypted_cycle_and_order_proof(session_factory):
    from trading_assistant.autopilot.evidence import EvidenceStore
    order_id = make_order(session_factory)
    supplied = cycle(order_id)
    evidence = EvidenceStore(session_factory, actor="autopilot:fixture")
    evidence.record_cycle(key=supplied.idempotency_key, session=NOW.date(), result_code="executed", detail=supplied.detail)
    now = max(datetime.now(timezone.utc) + timedelta(minutes=1), NOW + timedelta(minutes=1))
    assert read_lifecycle(session_factory, evidence.cycles(), SCOPE, NOW, now)["outcome"] == "pass"
