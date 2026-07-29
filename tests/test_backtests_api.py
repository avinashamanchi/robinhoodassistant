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
from trading_assistant.config import BacktestConfig, FillConfig
from tests.app_factory import create_app
from trading_assistant.db.models import (
    AuditEvent,
    BacktestArtifact,
    BacktestMetricRow,
    BacktestRun,
    ConcurrencyLease,
    MutationInterlock,
    utcnow,
)
from trading_assistant.security.sensitive_fields import (
    persist_sensitive,
    sensitive_store,
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
    assert rep["artifact_status"] == {"status": "available"}
    manifest = rep["manifest"]
    assert manifest["data_source"] == "synthetic"
    assert manifest["validation"] == {
        "status": "unavailable",
        "reason": "not_run",
    }
    assert manifest["episodes"] == {"status": "not_run"}
    assert manifest["holdout_access_log"]
    assert rep["series"]
    series = rep["series"][0]
    assert series["strategy_equity"]
    assert series["benchmark_equity"]
    assert len(series["strategy_drawdown"]) == len(
        series["strategy_equity"]
    )
    assert len(series["benchmark_drawdown"]) == len(
        series["benchmark_equity"]
    )
    assert series["actual_total_fees"] >= 0
    assert series["benchmark_actual_total_fees"] >= 0
    assert "slippage_bps" in series["cost_assumptions"]
    assert "realized_slippage_dollars" not in series
    with svc.session_factory() as session:
        manifest_row = session.query(BacktestArtifact).filter_by(
            run_id=run_id,
            artifact_key="manifest",
        ).one()
    assert manifest_row.payload_json.startswith(
        "enc:v1:pytest-field-key-2026:"
    )
    assert json.loads(
        decrypt_test_sensitive(manifest_row, "payload_json")
    ) == manifest


def test_list_exposes_only_bounded_simulation_policy(client):
    c, svc = client

    response = c.get("/backtests").json()

    assert response["simulation_policy"] == {
        "max_runtime_seconds": (
            svc.config.security.backtest_limits.runtime_seconds
        ),
        "max_symbols": svc.config.security.backtest_limits.max_symbols,
        "max_calendar_days": (
            svc.config.security.backtest_limits.max_calendar_days
        ),
        "window_requests": (
            svc.config.security.rate_limits.backtest.requests
        ),
        "global_window_requests": (
            svc.config.security.rate_limits.backtest.global_requests
        ),
        "window_seconds": (
            svc.config.security.rate_limits.backtest.window_seconds
        ),
        "daily_requests": (
            svc.config.security.rate_limits.backtest.daily_requests
        ),
        "global_daily_requests": (
            svc.config.security.rate_limits.backtest.global_daily_requests
        ),
        "concurrency": (
            svc.config.security.rate_limits.backtest.concurrency
        ),
        "llm_enabled": (
            svc.config.security.provider_budget.backtest_llm_enabled
        ),
        "saved_run_page_limit": 25,
    }
    serialized = json.dumps(response["simulation_policy"])
    for forbidden in (
        "api_key",
        "secret",
        "database",
        "password",
        "credential",
    ):
        assert forbidden not in serialized.lower()


def test_missing_or_unknown_run_status_never_defaults_to_success(client):
    c, svc = client
    with svc.session_factory() as session:
        missing = BacktestRun(
            label="missing status",
            config_json="{}",
        )
        unknown = BacktestRun(
            label="unknown status",
            config_json=json.dumps({"status": "unexpected"}),
        )
        session.add_all([missing, unknown])
        session.commit()
        missing_id = missing.id
        unknown_id = unknown.id

    payload = c.get("/backtests").json()
    listed = {
        row["run_id"]: row["status"]
        for row in payload["backtests"]
    }

    assert listed[missing_id] == "unknown"
    assert listed[unknown_id] == "unknown"
    assert c.get(f"/backtests/{missing_id}/report").json()["status"] == (
        "unknown"
    )
    assert c.get(f"/backtests/{unknown_id}/report").json()["status"] == (
        "unknown"
    )


def test_list_backtests_uses_strict_cursor_pages(client):
    c, svc = client
    with svc.session_factory() as session:
        session.add_all(
            [
                BacktestRun(
                    label=f"cursor run {index:02d}",
                    config_json=json.dumps({"status": "succeeded"}),
                )
                for index in range(31)
            ]
        )
        session.commit()

    first = c.get("/backtests")

    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["pagination"]["limit"] == 25
    assert len(first_payload["backtests"]) == 25
    first_ids = [
        row["run_id"] for row in first_payload["backtests"]
    ]
    assert first_ids == sorted(first_ids, reverse=True)
    assert first_payload["pagination"]["next_cursor"] == first_ids[-1]

    second = c.get(
        "/backtests",
        params={"cursor": first_payload["pagination"]["next_cursor"]},
    )

    assert second.status_code == 200
    second_payload = second.json()
    second_ids = [
        row["run_id"] for row in second_payload["backtests"]
    ]
    assert len(second_ids) == 6
    assert second_ids == sorted(second_ids, reverse=True)
    assert set(first_ids).isdisjoint(second_ids)
    assert second_payload["pagination"] == {
        "limit": 25,
        "next_cursor": None,
    }


@pytest.mark.parametrize("cursor", ("0", "-1", "not-an-integer"))
def test_list_backtests_rejects_invalid_cursor(client, cursor):
    c, _svc = client

    response = c.get("/backtests", params={"cursor": cursor})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    "mutation",
    (
        "null_metric",
        "string_metric",
        "boolean_metric",
        "beat_mismatch",
        "column_mismatch",
        "extra_field",
        "missing_field",
    ),
)
def test_report_rejects_malformed_metric_rows(client, mutation):
    c, svc = client
    run_id, _ = runner.run_synthetic_backtest(
        svc.session_factory,
        symbols=["TREND"],
        bars=5,
        actor="test:invalid-metrics",
        reason=f"corrupt metric row: {mutation}",
        request_id=f"backtest-invalid-metrics-{mutation}",
    )
    with svc.session_factory() as session:
        row = (
            session.query(BacktestMetricRow)
            .filter_by(run_id=run_id)
            .order_by(BacktestMetricRow.id)
            .first()
        )
        payload = json.loads(row.metrics_json)
        if mutation == "null_metric":
            payload["metrics"]["total_return_pct"] = None
        elif mutation == "string_metric":
            payload["metrics"]["sharpe"] = "1.25"
        elif mutation == "boolean_metric":
            payload["metrics"]["turnover"] = False
        elif mutation == "beat_mismatch":
            payload["beat_buy_and_hold"] = not payload[
                "beat_buy_and_hold"
            ]
        elif mutation == "column_mismatch":
            payload["symbol"] = "MSFT"
        elif mutation == "extra_field":
            payload["metrics"]["invented_edge"] = 999.0
        else:
            del payload["metrics"]["cagr_pct"]
        row.metrics_json = json.dumps(payload, allow_nan=False)
        session.commit()

    report = c.get(f"/backtests/{run_id}/report").json()

    assert report["status"] == "succeeded"
    assert report["rows"] == []
    assert report["artifact_status"] == {
        "status": "unavailable",
        "reason": "metric_rows_invalid",
    }
    assert "manifest" not in report
    assert "series" not in report


def test_runner_persists_exact_applied_cost_and_holdout_config(client):
    c, svc = client
    applied = BacktestConfig(
        fills=FillConfig(
            market="next_bar_open",
            limit="bar_range_cross",
            max_participation_pct=7.5,
        ),
        slippage_bps={"equity": 8.25, "crypto": 31.5},
        fees_bps={"equity": 0.0, "crypto": 27.0},
        holdout_months=6,
    )
    run_id, _ = runner.run_synthetic_backtest(
        svc.session_factory,
        symbols=["TREND"],
        bars=5,
        actor="test:exact-config",
        reason="persist exact applied cost model",
        request_id="backtest-exact-config",
        backtest_config=applied,
    )

    rep = c.get(f"/backtests/{run_id}/report").json()

    assert rep["artifact_status"] == {"status": "available"}
    assert rep["manifest"]["backtest_config"] == applied.model_dump(
        mode="json"
    )
    assert rep["manifest"]["holdout_start"] is not None
    assert all(
        row["cost_assumptions"]["slippage_bps"]
        == applied.slippage_bps
        for row in rep["series"]
    )
    assert all(
        row["cost_assumptions"]["fees_bps"] == applied.fees_bps
        for row in rep["series"]
    )


def test_runner_rejects_invalid_holdout_override_before_data_load(
    session_factory,
    monkeypatch,
):
    data_calls = 0

    def forbidden_source(*args, **kwargs):
        nonlocal data_calls
        data_calls += 1
        raise AssertionError("invalid config reached data loading")

    monkeypatch.setattr(runner, "build_synthetic_source", forbidden_source)

    with pytest.raises(ValueError):
        runner.BacktestRunner(session_factory).run(
            symbols=["TREND"],
            actor="operator:test",
            reason="reject invalid holdout override",
            request_id="backtest-invalid-holdout",
            bars=5,
            holdout_months=0,
        )

    assert data_calls == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "negative_fee",
        "naive_timestamp",
        "wrong_identity",
        "cost_mismatch",
        "schema_mismatch",
    ],
)
def test_report_rejects_invalid_encrypted_series(client, mutation):
    c, svc = client
    run_id, _ = runner.run_synthetic_backtest(
        svc.session_factory,
        symbols=["TREND"],
        bars=5,
        actor="test:invalid-artifact",
        reason=f"corrupt {mutation}",
        request_id=f"backtest-invalid-{mutation}",
    )
    with svc.session_factory() as session:
        artifact = (
            session.query(BacktestArtifact)
            .filter(
                BacktestArtifact.run_id == run_id,
                BacktestArtifact.artifact_key.like("series:%"),
            )
            .order_by(BacktestArtifact.artifact_key)
            .first()
        )
        store = sensitive_store(session, svc.session_factory)
        payload = json.loads(store.read(artifact, "payload_json"))
        if mutation == "negative_fee":
            payload["actual_total_fees"] = -1.0
        elif mutation == "naive_timestamp":
            payload["strategy_equity"][0]["at"] = "2026-07-29T09:30:00"
        elif mutation == "wrong_identity":
            payload["symbol"] = "WRONG"
        elif mutation == "cost_mismatch":
            payload["cost_assumptions"]["slippage_bps"]["equity"] = 999
        else:
            payload["schema_version"] = 2
        store.write_many(
            artifact,
            {
                "payload_json": json.dumps(
                    payload,
                    sort_keys=True,
                    allow_nan=False,
                )
            },
        )
        session.commit()

    rep = c.get(f"/backtests/{run_id}/report").json()

    assert rep["artifact_status"] == {
        "status": "unavailable",
        "reason": "artifact_invalid",
    }
    assert "manifest" not in rep
    assert "series" not in rep


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_asset_class",
        "unknown_asset_class",
        "negative_cost",
        "boolean_cost",
    ],
)
def test_report_rejects_invalid_persisted_cost_maps(client, mutation):
    c, svc = client
    run_id, _ = runner.run_synthetic_backtest(
        svc.session_factory,
        symbols=["TREND"],
        bars=5,
        actor="test:invalid-cost-map",
        reason=f"corrupt persisted cost map: {mutation}",
        request_id=f"backtest-invalid-cost-map-{mutation}",
    )
    with svc.session_factory() as session:
        artifact = (
            session.query(BacktestArtifact)
            .filter(
                BacktestArtifact.run_id == run_id,
                BacktestArtifact.artifact_key == "manifest",
            )
            .one()
        )
        store = sensitive_store(session, svc.session_factory)
        payload = json.loads(store.read(artifact, "payload_json"))
        costs = payload["backtest_config"]["slippage_bps"]
        if mutation == "missing_asset_class":
            del costs["crypto"]
        elif mutation == "unknown_asset_class":
            costs["options"] = 10.0
        elif mutation == "negative_cost":
            costs["equity"] = -0.01
        else:
            costs["equity"] = True
        store.write_many(
            artifact,
            {
                "payload_json": json.dumps(
                    payload,
                    sort_keys=True,
                    allow_nan=False,
                )
            },
        )
        session.commit()

    rep = c.get(f"/backtests/{run_id}/report").json()

    assert rep["artifact_status"] == {
        "status": "unavailable",
        "reason": "artifact_invalid",
    }
    assert "manifest" not in rep
    assert "series" not in rep


def test_report_rejects_unknown_artifact_keys(client):
    c, svc = client
    run_id, _ = runner.run_synthetic_backtest(
        svc.session_factory,
        symbols=["TREND"],
        bars=5,
        actor="test:mixed-artifact",
        reason="reject mixed artifact set",
        request_id="backtest-mixed-artifact",
    )
    with svc.session_factory() as session:
        persist_sensitive(
            session,
            BacktestArtifact(
                run_id=run_id,
                artifact_key="unexpected",
                schema_version=1,
            ),
            {"payload_json": '{"schema_version":1}'},
            session_factory=svc.session_factory,
        )
        session.commit()

    rep = c.get(f"/backtests/{run_id}/report").json()

    assert rep["artifact_status"] == {
        "status": "unavailable",
        "reason": "artifact_invalid",
    }


def test_report_rejects_missing_artifact_keys(client):
    c, svc = client
    run_id, _ = runner.run_synthetic_backtest(
        svc.session_factory,
        symbols=["TREND"],
        bars=5,
        actor="test:missing-artifact",
        reason="reject incomplete artifact set",
        request_id="backtest-missing-artifact",
    )
    with svc.session_factory() as session:
        artifact = (
            session.query(BacktestArtifact)
            .filter(
                BacktestArtifact.run_id == run_id,
                BacktestArtifact.artifact_key.like("series:%"),
            )
            .order_by(BacktestArtifact.artifact_key.desc())
            .first()
        )
        sensitive_store(
            session,
            svc.session_factory,
        ).delete(artifact)
        session.commit()

    rep = c.get(f"/backtests/{run_id}/report").json()

    assert rep["artifact_status"] == {
        "status": "unavailable",
        "reason": "artifact_invalid",
    }


def test_legacy_report_refuses_to_reconstruct_artifacts(client):
    c, svc = client
    with svc.session_factory() as session:
        run = BacktestRun(
            label="legacy aggregate-only run",
            config_json=json.dumps({"status": "succeeded"}),
        )
        session.add(run)
        session.commit()
        run_id = run.id

    rep = c.get(f"/backtests/{run_id}/report").json()

    assert rep["artifact_status"] == {
        "status": "unavailable",
        "reason": "not_persisted_for_legacy_run",
    }
    assert "manifest" not in rep
    assert "series" not in rep


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


def test_run_endpoint_normalizes_and_deduplicates_symbols(
    client,
    monkeypatch,
):
    c, _svc = client
    observed: dict[str, object] = {}

    def completed_run(*args, **kwargs):
        observed["symbols"] = kwargs["symbols"]
        return 701, _report()

    monkeypatch.setattr(
        runner,
        "run_synthetic_backtest",
        completed_run,
    )

    response = c.post(
        "/backtests/run",
        json={
            "reason": "normalize direct API symbols",
            "symbols": [" aapl ", "AAPL", "btc/usd", "BTC/USD"],
        },
        headers={"Idempotency-Key": "backtest-normalized-symbols"},
    )

    assert response.status_code == 200
    assert observed["symbols"] == ["AAPL", "BTC/USD"]


@pytest.mark.parametrize(
    "symbols",
    (
        [""],
        ["AAPL<script>"],
        ["SYMBOL-NAME-THAT-IS-TOO-LONG"],
    ),
)
def test_run_endpoint_rejects_invalid_symbols_before_runner(
    client,
    monkeypatch,
    symbols,
):
    c, _svc = client
    runner_calls = 0

    def forbidden_run(*args, **kwargs):
        nonlocal runner_calls
        runner_calls += 1
        return 702, _report()

    monkeypatch.setattr(
        runner,
        "run_synthetic_backtest",
        forbidden_run,
    )

    response = c.post(
        "/backtests/run",
        json={
            "reason": "reject malformed direct API symbol",
            "symbols": symbols,
        },
        headers={
            "Idempotency-Key": (
                "backtest-invalid-symbol-"
                + str(len(symbols[0]))
            )
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert runner_calls == 0


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
    test_client, service = client
    observed: dict[str, object] = {}

    def completed_run(*args, **kwargs):
        observed["symbols"] = kwargs["symbols"]
        observed["start_date"] = kwargs["start_date"]
        observed["end_date"] = kwargs["end_date"]
        observed["backtest_config"] = kwargs["backtest_config"]
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
    assert (
        observed["backtest_config"]
        == service.config.backtest
    )


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
