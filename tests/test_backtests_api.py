"""/backtests endpoints: run, list, report, UI, 404."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import trading_assistant.backtest.runner as runner
from trading_assistant.app.limits import ConcurrencyLeaseService
from tests.app_factory import create_app
from trading_assistant.db.models import (
    AuditEvent,
    BacktestRun,
    ConcurrencyLease,
    MutationInterlock,
    utcnow,
)
from trading_assistant.strategies.base import SignalAction
from tests.conftest import decrypt_test_sensitive

TOKEN = "test-backtests-operator-secret"
_STATIC = Path("src/trading_assistant/app/static")


class StubAgent:
    def chat(self, message: str, **context):
        return {"reply": "", "tool_calls": []}


@pytest.fixture
def client(make_service, authenticate_client):
    svc = make_service()
    app = create_app(
        service=svc,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    test_client, csrf = authenticate_client(
        TestClient(app, raise_server_exceptions=False),
        TOKEN,
    )
    test_client.headers.update({"X-CSRF-Token": csrf})
    return test_client, svc


def _seed(svc, bars=420):
    run_id, _ = runner.run_synthetic_backtest(
        svc.session_factory,
        symbols=["TREND"],
        bars=bars,
        actor="test:seed",
        reason="seed backtest fixture",
        request_id="backtest-seed",
    )
    return run_id


def _report():
    return SimpleNamespace(
        to_dict=lambda: {
            "disclaimer": "Simulated — test report",
            "rows": [],
        }
    )


def _two_authenticated_clients(service, authenticate_client):
    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    first, first_csrf = authenticate_client(TestClient(app), TOKEN)
    second, second_csrf = authenticate_client(TestClient(app), TOKEN)
    return app, first, first_csrf, second, second_csrf


def test_list_and_report(client):
    c, svc = client
    run_id = _seed(svc)
    listed = c.get("/backtests").json()["backtests"]
    assert any(b["run_id"] == run_id for b in listed)

    rep = c.get(f"/backtests/{run_id}/report").json()
    assert rep["disclaimer"].startswith("Simulated")
    assert len(rep["rows"]) >= 1
    first = rep["rows"][0]
    assert "metrics" in first and "benchmark_buy_and_hold" in first
    assert "beat_buy_and_hold" in first


def test_report_404(client):
    c, _ = client
    assert c.get("/backtests/9999/report").status_code == 404


def test_ui_served(client):
    c, _ = client
    r = c.get("/backtests/ui")
    assert r.status_code == 200
    assert "Simulated" in r.text  # mandatory disclaimer present in the page
    script = (_STATIC / "js" / "backtests.js").read_text(
        encoding="utf-8"
    )
    assert "backtest-reason" in script
    assert "reason" in script


def test_run_endpoint_persists(client, monkeypatch):
    c, svc = client
    orig = runner.run_synthetic_backtest
    monkeypatch.setattr(
        runner,
        "run_synthetic_backtest",
        lambda sf, symbols=None, **kw: orig(
            sf,
            symbols=["TREND"],
            bars=420,
            **kw,
        ),
    )
    response = c.post(
        "/backtests/run",
        json={"reason": "compare synthetic strategies"},
        headers={"Idempotency-Key": "backtest-run-persist"},
    )
    res = response.json()
    assert "run_id" in res
    assert res["report"]["disclaimer"].startswith("Simulated")
    # The run is now listable.
    assert any(b["run_id"] == res["run_id"] for b in c.get("/backtests").json()["backtests"])
    with svc.session_factory() as session:
        audit = session.query(AuditEvent).filter_by(
            action="backtest.run",
            target_id=str(res["run_id"]),
        ).one()
    assert audit.actor == "operator:local"
    assert decrypt_test_sensitive(
        audit,
        "reason",
    ) == "compare synthetic strategies"
    assert audit.request_id == response.headers["X-Request-ID"]
    assert audit.result_code == "succeeded"


def test_run_endpoint_rejects_blank_reason(client):
    c, _ = client

    response = c.post(
        "/backtests/run",
        json={"reason": " "},
        headers={"Idempotency-Key": "backtest-run-blank-reason"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_failed_backtest_launch_is_sanitized_and_audited(
    client, monkeypatch
):
    c, svc = client

    def explode(*args, **kwargs):
        raise RuntimeError("provider-secret-backtest-prompt")

    monkeypatch.setattr(runner, "build_synthetic_source", explode)

    response = c.post(
        "/backtests/run",
        json={"reason": "failure-path review"},
        headers={"Idempotency-Key": "backtest-run-failure"},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "provider-secret-backtest-prompt" not in response.text
    with svc.session_factory() as session:
        audit = session.query(AuditEvent).filter_by(
            action="backtest.run",
            result_code="failed",
        ).one()
    assert audit.actor == "operator:local"
    assert decrypt_test_sensitive(
        audit,
        "reason",
    ) == "failure-path review"
    assert audit.request_id == response.headers["X-Request-ID"]
    assert "provider-secret-backtest-prompt" not in (
        decrypt_test_sensitive(audit, "detail_json")
    )


def test_second_backtest_is_globally_busy_and_starts_no_runner(
    make_service,
    authenticate_client,
    monkeypatch,
):
    service = make_service()
    app, owner_client, owner_csrf, follower_client, follower_csrf = (
        _two_authenticated_clients(service, authenticate_client)
    )
    started = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()
    observed_acquires: list[tuple[str, int]] = []
    original_acquire = app.state.leases.acquire

    def observed_acquire(resource_key, **kwargs):
        observed_acquires.append(
            (resource_key, kwargs["ttl_seconds"])
        )
        return original_acquire(resource_key, **kwargs)

    def blocking_run(*args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            started.set()
            assert release.wait(timeout=5)
        return 71 + call_number, _report()

    app.state.leases.acquire = observed_acquire
    monkeypatch.setattr(runner, "run_synthetic_backtest", blocking_run)

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(
            owner_client.post,
            "/backtests/run",
            json={"reason": "global backtest owner"},
            headers={
                "X-CSRF-Token": owner_csrf,
                "Idempotency-Key": "backtest-global-owner",
            },
        )
        assert started.wait(timeout=5)
        follower = follower_client.post(
            "/backtests/run",
            json={"reason": "global backtest follower"},
            headers={
                "X-CSRF-Token": follower_csrf,
                "Idempotency-Key": "backtest-global-follower",
            },
        )
        release.set()
        owner_response = owner.result(timeout=5)

    assert owner_response.status_code == 200
    assert follower.status_code == 409
    assert follower.json()["error"]["code"] == "backtest_busy"
    assert calls == 1
    assert observed_acquires == [("backtest:global", 1_500)]


def test_expired_backtest_lease_is_reclaimed_with_exact_fence(
    client,
    monkeypatch,
):
    test_client, service = client
    expired_at = utcnow() - timedelta(seconds=1)
    with service.session_factory() as session:
        session.add(
            ConcurrencyLease(
                resource_key="backtest:global",
                owner="crashed-owner",
                generation=7,
                expires_at=expired_at,
            )
        )
        session.add(
            MutationInterlock(
                resource_key="backtest:global",
                owner="crashed-owner",
                generation=7,
                operation="backtest",
                state="active",
                outcome_code="",
                worker_finished_at=None,
                created_at=expired_at,
                updated_at=expired_at,
            )
        )
        session.commit()

    ConcurrencyLeaseService(
        service.session_factory
    ).prune_expired(
        utcnow(),
        limit=500,
    )
    with service.session_factory() as session:
        assert (
            session.get(ConcurrencyLease, "backtest:global")
            is not None
        )

    calls = 0

    def completed_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        return 81, _report()

    monkeypatch.setattr(
        runner,
        "run_synthetic_backtest",
        completed_run,
    )

    response = test_client.post(
        "/backtests/run",
        json={"reason": "reclaim crashed backtest"},
        headers={"Idempotency-Key": "backtest-expired-reclaim"},
    )

    assert response.status_code == 200
    assert calls == 1
    with service.session_factory() as session:
        lease = session.get(ConcurrencyLease, "backtest:global")
        assert lease.owner == ""
        assert lease.generation == 9
        assert (
            session.get(MutationInterlock, "backtest:global")
            is None
        )


def test_expired_backtest_lease_is_not_reclaimed_past_durable_interlock(
    client,
    monkeypatch,
):
    test_client, service = client
    finished_at = utcnow()
    with service.session_factory() as session:
        session.add(
            ConcurrencyLease(
                resource_key="backtest:global",
                owner="crashed-owner",
                generation=11,
                expires_at=finished_at - timedelta(seconds=1),
            )
        )
        session.add(
            MutationInterlock(
                resource_key="backtest:global",
                owner="crashed-owner",
                generation=11,
                operation="backtest",
                state="uncertain",
                outcome_code="handler_failed",
                worker_finished_at=finished_at,
                created_at=finished_at,
                updated_at=finished_at,
            )
        )
        session.commit()

    calls = 0

    def forbidden_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        return 82, _report()

    monkeypatch.setattr(
        runner,
        "run_synthetic_backtest",
        forbidden_run,
    )

    response = test_client.post(
        "/backtests/run",
        json={"reason": "blocked crashed backtest"},
        headers={"Idempotency-Key": "backtest-expired-blocked"},
    )

    assert response.status_code == 409
    assert (
        response.json()["error"]["code"]
        == "mutation_reconciliation_required"
    )
    assert calls == 0
    with service.session_factory() as session:
        latch = session.get(MutationInterlock, "backtest:global")
        assert latch.owner == "crashed-owner"
        assert latch.generation == 11
        assert latch.state == "uncertain"


@pytest.mark.parametrize(
    "payload",
    [
        {
            "reason": "too many symbols",
            "symbols": [f"SYM{i:02d}" for i in range(21)],
        },
        {
            "reason": "too many calendar days",
            "start_date": date(2000, 1, 1).isoformat(),
            "end_date": (
                date(2000, 1, 1) + timedelta(days=3_000)
            ).isoformat(),
        },
    ],
    ids=["symbols", "inclusive-calendar-days"],
)
def test_backtest_bounds_reject_before_lease_or_runner(
    client,
    monkeypatch,
    payload,
    request,
):
    test_client, _service = client
    lease_calls = 0
    runner_calls = 0
    original_acquire = test_client.app.state.leases.acquire

    def observed_acquire(*args, **kwargs):
        nonlocal lease_calls
        lease_calls += 1
        return original_acquire(*args, **kwargs)

    def forbidden_run(*args, **kwargs):
        nonlocal runner_calls
        runner_calls += 1
        return 83, _report()

    test_client.app.state.leases.acquire = observed_acquire
    monkeypatch.setattr(
        runner,
        "run_synthetic_backtest",
        forbidden_run,
    )

    response = test_client.post(
        "/backtests/run",
        json=payload,
        headers={
            "Idempotency-Key": f"backtest-bounds-{request.node.callspec.id}"
        },
    )

    assert response.status_code == 422
    assert (
        response.json()["error"]["code"]
        == "backtest_bounds_exceeded"
    )
    assert lease_calls == 0
    assert runner_calls == 0


def test_backtest_bounds_allow_exact_symbol_and_inclusive_day_ceilings(
    client,
    monkeypatch,
):
    test_client, _service = client
    observed: dict[str, object] = {}

    def completed_run(*args, **kwargs):
        observed["symbols"] = kwargs["symbols"]
        observed["start_date"] = kwargs["start_date"]
        observed["end_date"] = kwargs["end_date"]
        return 84, _report()

    monkeypatch.setattr(
        runner,
        "run_synthetic_backtest",
        completed_run,
    )
    start = date(2000, 1, 1)
    symbols = [f"SYM{i:02d}" for i in range(20)]

    response = test_client.post(
        "/backtests/run",
        json={
            "reason": "exact configured bounds",
            "symbols": symbols,
            "start_date": start.isoformat(),
            "end_date": (
                start + timedelta(days=2_999)
            ).isoformat(),
        },
        headers={"Idempotency-Key": "backtest-exact-bounds"},
    )

    assert response.status_code == 200
    assert observed["symbols"] == symbols
    assert observed["start_date"] == start
    assert observed["end_date"] == start + timedelta(days=2_999)


def test_backtest_timeout_persists_status_and_stops_before_later_provider_call(
    session_factory,
    monkeypatch,
):
    provider_calls = 0

    class StepClock:
        expired = False

        def __call__(self):
            return 1_200.0 if self.expired else 0.0

    clock = StepClock()

    class ProviderStrategy:
        name = "provider_strategy"

        def on_bar(self, features):
            nonlocal provider_calls
            provider_calls += 1
            clock.expired = True
            return SimpleNamespace(
                action=SignalAction.HOLD,
                size_hint=None,
            )

    monkeypatch.setattr(
        runner,
        "STRATEGIES",
        [ProviderStrategy],
    )
    stop_event = threading.Event()
    backtest_runner = runner.BacktestRunner(
        session_factory,
        runtime_seconds=1_200,
        monotonic=clock,
    )

    with pytest.raises(runner.BacktestTimedOut) as timeout:
        backtest_runner.run(
            symbols=["TREND"],
            actor="operator:test",
            reason="cooperative timeout",
            request_id="backtest-timeout",
            bars=420,
            stop_event=stop_event,
        )

    assert provider_calls == 1
    assert stop_event.is_set()
    assert timeout.value.run_id is not None
    with session_factory() as session:
        persisted = session.get(
            BacktestRun,
            timeout.value.run_id,
        )
        assert json.loads(persisted.config_json)["status"] == "timed_out"
        audit = session.query(AuditEvent).filter_by(
            action="backtest.run",
            result_code="timed_out",
        ).one()
    assert audit.request_id == "backtest-timeout"


def test_backtest_deadline_crossing_during_persistence_reconciles_same_run(
    session_factory,
    monkeypatch,
):
    class StepClock:
        expired = False

        def __call__(self):
            return 1_200.0 if self.expired else 0.0

    clock = StepClock()
    original_persist = runner.persist_report
    persisted_ids: list[int] = []

    def persist_then_cross_deadline(*args, **kwargs):
        run_id = original_persist(*args, **kwargs)
        persisted_ids.append(run_id)
        clock.expired = True
        return run_id

    monkeypatch.setattr(
        runner,
        "persist_report",
        persist_then_cross_deadline,
    )
    stop_event = threading.Event()

    with pytest.raises(runner.BacktestTimedOut) as timeout:
        runner.BacktestRunner(
            session_factory,
            runtime_seconds=1_200,
            monotonic=clock,
        ).run(
            symbols=["TREND"],
            actor="operator:test",
            reason="deadline crossed during persistence",
            request_id="backtest-persistence-timeout",
            bars=5,
            stop_event=stop_event,
        )

    assert stop_event.is_set()
    assert timeout.value.run_id == persisted_ids[0]
    with session_factory() as session:
        runs = session.query(BacktestRun).all()
        audits = session.query(AuditEvent).filter_by(
            action="backtest.run",
            request_id="backtest-persistence-timeout",
        ).all()
    assert [run.id for run in runs] == persisted_ids
    assert json.loads(runs[0].config_json)["status"] == "timed_out"
    assert len(audits) == 1
    assert audits[0].target_id == str(persisted_ids[0])
    assert audits[0].result_code == "timed_out"
    assert json.loads(
        decrypt_test_sensitive(audits[0], "detail_json")
    )["stage"] == (
        "post_persistence"
    )


def test_backtest_cancellation_checks_before_data_load(
    session_factory,
    monkeypatch,
):
    stop_event = threading.Event()
    stop_event.set()
    source_calls = 0

    def forbidden_source(*args, **kwargs):
        nonlocal source_calls
        source_calls += 1
        raise AssertionError("data/provider load followed cancellation")

    monkeypatch.setattr(
        runner,
        "build_synthetic_source",
        forbidden_source,
    )

    with pytest.raises(runner.BacktestTimedOut):
        runner.BacktestRunner(
            session_factory,
            runtime_seconds=1_200,
        ).run(
            symbols=["TREND"],
            actor="operator:test",
            reason="cancel before data",
            request_id="backtest-cancel-before-data",
            stop_event=stop_event,
        )

    assert source_calls == 0


def test_backtest_cancellation_checks_before_each_data_load(
    session_factory,
    monkeypatch,
):
    stop_event = threading.Event()
    data_calls = 0
    original_make_bars = runner.make_bars

    def first_data_call_then_cancel(*args, **kwargs):
        nonlocal data_calls
        data_calls += 1
        result = original_make_bars(*args, **kwargs)
        stop_event.set()
        return result

    monkeypatch.setattr(
        runner,
        "make_bars",
        first_data_call_then_cancel,
    )

    with pytest.raises(runner.BacktestTimedOut):
        runner.BacktestRunner(
            session_factory,
            runtime_seconds=1_200,
        ).run(
            symbols=["TREND", "CHOP"],
            actor="operator:test",
            reason="cancel between data loads",
            request_id="backtest-cancel-between-data",
            stop_event=stop_event,
        )

    assert data_calls == 1


def test_backtest_timeout_response_releases_only_exact_fenced_latch(
    client,
    monkeypatch,
):
    test_client, service = client
    observed: dict[str, object] = {}

    def timed_out(*args, **kwargs):
        observed.update(kwargs)
        raise runner.BacktestTimedOut(run_id=91)

    monkeypatch.setattr(
        runner,
        "run_synthetic_backtest",
        timed_out,
    )
    monkeypatch.setattr(
        "trading_assistant.app.main.time.monotonic",
        lambda: 10.0,
    )

    response = test_client.post(
        "/backtests/run",
        json={"reason": "deadline response"},
        headers={"Idempotency-Key": "backtest-timeout-response"},
    )

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "backtest_timed_out"
    assert observed["runtime_seconds"] == 1_200
    assert observed["deadline"] == 1_210.0
    assert isinstance(observed["stop_event"], threading.Event)
    with service.session_factory() as session:
        lease = session.get(ConcurrencyLease, "backtest:global")
        assert lease.owner == ""
        assert (
            session.get(MutationInterlock, "backtest:global")
            is None
        )
