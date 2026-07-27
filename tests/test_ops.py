"""Operational backups and the explicit Alpaca paper order drill."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from trading_assistant.app.main import create_app
from trading_assistant.config import BrokerKind, TradingMode
from trading_assistant.db.models import PanicReceipt, utcnow
from trading_assistant.ops.backup import backup_database
from trading_assistant.ops.paper_drill import PaperDrillError, run_paper_drill


class _StubAgent:
    def chat(self, message: str, **context):
        return {"reply": "", "tool_calls": []}


def test_online_backup_is_valid_and_rotates_only_matching_old_files(tmp_path):
    source = tmp_path / "live.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('preserved')")
        connection.commit()

    destination = tmp_path / "backups"
    destination.mkdir()
    old_match = destination / "trading-assistant-20000101T000000Z.sqlite3"
    old_match.write_bytes(b"old")
    unrelated = destination / "keep-me.sqlite3"
    unrelated.write_bytes(b"unrelated")
    old_time = time.time() - 30 * 86400
    os.utime(old_match, (old_time, old_time))
    os.utime(unrelated, (old_time, old_time))

    created = backup_database(source, destination, retention_days=14)

    with sqlite3.connect(created) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone() == (
            "preserved",
        )
    assert not old_match.exists()
    assert unrelated.exists()
    assert list(destination.glob("*.tmp*")) == []
    assert not created.with_name(f"{created.name}-wal").exists()
    assert not created.with_name(f"{created.name}-shm").exists()
    assert stat.S_IMODE(created.stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700


def test_paper_drill_refuses_live_configuration(app_config):
    live_config = app_config.model_copy(
        update={
            "trading": app_config.trading.model_copy(
                update={"mode": TradingMode.LIVE, "broker": BrokerKind.ALPACA}
            )
        }
    )

    with pytest.raises(PaperDrillError, match="paper"):
        run_paper_drill(live_config, service=None)


def test_paper_drill_proposes_accepts_and_cancels_through_service(
    app_config, make_service
):
    paper_config = app_config.model_copy(
        update={
            "trading": app_config.trading.model_copy(
                update={"mode": TradingMode.PAPER, "broker": BrokerKind.ALPACA}
            )
        }
    )
    service = make_service()

    result = run_paper_drill(
        paper_config, service=service, symbol="AAPL", test_notional=Decimal("1.25")
    )

    assert result["broker_accepted"] is True
    assert result["terminal_status"] == "canceled"
    assert service.broker.submit_calls == 1


def test_concurrent_panic_requests_share_one_durable_90_second_receipt(
    make_service,
    authenticate_client,
):
    service = make_service()
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="panic-coalesce-secret",
        planning=None,
    )
    owner_client, owner_csrf = authenticate_client(
        TestClient(app),
        "panic-coalesce-secret",
    )
    follower_client, follower_csrf = authenticate_client(
        TestClient(app),
        "panic-coalesce-secret",
    )
    receipt = {
        "safe": True,
        "local_enumeration": "confirmed",
        "remote_enumeration": "confirmed",
        "confirmed_canceled": ["paper-order-1"],
        "unconfirmed_order_ids": [],
    }
    owner_started = threading.Event()
    release_owner = threading.Event()
    calls = 0
    calls_lock = threading.Lock()
    observed_ttls: list[int] = []
    acquired_fences = []
    original_acquire = app.state.leases.acquire
    original_inspect = app.state.leases.inspect
    follower_observed_owner = threading.Event()

    def observed_acquire(*args, **kwargs):
        observed_ttls.append(kwargs["ttl_seconds"])
        acquired = original_acquire(*args, **kwargs)
        acquired_fences.append(acquired)
        return acquired

    def observed_inspect(*args, **kwargs):
        observed = original_inspect(*args, **kwargs)
        if observed.acquired:
            follower_observed_owner.set()
        return observed

    def blocking_panic(**context):
        nonlocal calls
        with calls_lock:
            calls += 1
        owner_started.set()
        assert release_owner.wait(timeout=5)
        return receipt

    app.state.leases.acquire = observed_acquire
    app.state.leases.inspect = observed_inspect
    service.panic = blocking_panic
    started_before = utcnow()

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(
            owner_client.post,
            "/panic",
            json={"reason": "panic receipt owner"},
            headers={
                "X-CSRF-Token": owner_csrf,
                "Idempotency-Key": "panic-90-owner",
            },
        )
        assert owner_started.wait(timeout=5)
        follower = pool.submit(
            follower_client.post,
            "/panic",
            json={"reason": "panic receipt follower"},
            headers={
                "X-CSRF-Token": follower_csrf,
                "Idempotency-Key": "panic-90-follower",
            },
        )
        assert follower_observed_owner.wait(timeout=5)
        release_owner.set()
        owner_response = owner.result(timeout=5)
        follower_response = follower.result(timeout=5)

    assert owner_response.status_code == 200
    assert follower_response.status_code == 200
    assert owner_response.json() == follower_response.json() == receipt
    assert calls == 1
    assert observed_ttls == [90]
    with service.session_factory() as session:
        durable = session.get(PanicReceipt, "alpaca-paper")
        assert durable.state == "completed"
        assert durable.request_id == acquired_fences[0].owner
        assert (
            durable.lease_generation
            == acquired_fences[0].generation
        )
        assert json.loads(durable.response_json) == receipt
        assert durable.completed_at is not None
        assert durable.expires_at >= started_before + timedelta(seconds=89)


def test_panic_owner_exception_persists_failed_without_response_payload(
    make_service,
    authenticate_client,
):
    service = make_service()

    def failed_panic(**context):
        raise RuntimeError("provider-secret-must-not-persist")

    service.panic = failed_panic
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="panic-failed-secret",
        planning=None,
    )
    client, csrf = authenticate_client(
        TestClient(app, raise_server_exceptions=False),
        "panic-failed-secret",
    )

    response = client.post(
        "/panic",
        json={"reason": "panic owner failure"},
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "panic-owner-failed",
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "panic_incomplete"
    assert "provider-secret-must-not-persist" not in response.text
    with service.session_factory() as session:
        durable = session.get(PanicReceipt, "alpaca-paper")
        assert durable.state == "failed"
        assert durable.response_json is None


def test_panic_follower_wait_is_bounded_by_request_timeout(
    make_service,
    authenticate_client,
):
    service = make_service()
    service.config = service.config.model_copy(
        update={
            "trading": service.config.trading.model_copy(
                update={"request_timeout_seconds": 0.1}
            )
        }
    )
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="panic-wait-secret",
        planning=None,
    )
    owner_client, owner_csrf = authenticate_client(
        TestClient(app),
        "panic-wait-secret",
    )
    follower_client, follower_csrf = authenticate_client(
        TestClient(app),
        "panic-wait-secret",
    )
    owner_started = threading.Event()
    release_owner = threading.Event()
    follower_observed_owner = threading.Event()
    original_inspect = app.state.leases.inspect
    calls = 0

    def observed_inspect(*args, **kwargs):
        observed = original_inspect(*args, **kwargs)
        if observed.acquired:
            follower_observed_owner.set()
        return observed

    def blocking_panic(**context):
        nonlocal calls
        calls += 1
        owner_started.set()
        assert release_owner.wait(timeout=5)
        return {"safe": True}

    app.state.leases.inspect = observed_inspect
    service.panic = blocking_panic

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(
            owner_client.post,
            "/panic",
            json={"reason": "bounded wait owner"},
            headers={
                "X-CSRF-Token": owner_csrf,
                "Idempotency-Key": "panic-bounded-owner",
            },
        )
        assert owner_started.wait(timeout=5)
        follower = pool.submit(
            follower_client.post,
            "/panic",
            json={"reason": "bounded wait follower"},
            headers={
                "X-CSRF-Token": follower_csrf,
                "Idempotency-Key": "panic-bounded-follower",
            },
        )
        assert follower_observed_owner.wait(timeout=5)
        try:
            follower_response = follower.result(timeout=1)
        finally:
            release_owner.set()
        owner_response = owner.result(timeout=5)

    assert follower_response.status_code == 503
    assert (
        follower_response.json()["error"]["code"]
        == "panic_incomplete"
    )
    assert owner_response.status_code == 200
    assert calls == 1
