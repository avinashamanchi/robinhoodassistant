"""Operational backups and the explicit Alpaca paper order drill."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trading_assistant.app.main import create_test_app as create_app
from trading_assistant.config import BrokerKind, TradingMode
from trading_assistant.db.migrate import upgrade
from trading_assistant.db.models import PanicReceipt, utcnow
from trading_assistant.db.session import (
    create_db_engine,
    make_session_factory,
)
from trading_assistant.ops.backup import (
    EncryptedBackupError,
    backup_database,
    create_encrypted_database_backup,
    list_committed_backups,
    main as backup_main,
    read_encrypted_backup_header,
)
from trading_assistant.ops.paper_drill import PaperDrillError, run_paper_drill
from trading_assistant.ops.tenure import (
    ProcessIdentity,
    ProcessProof,
    RuntimeTenureGuard,
    RuntimeTenureService,
    TenureUnavailable,
)
from trading_assistant.security.sensitive_fields import sensitive_store
from trading_assistant.security.secrets import RuntimeSecrets
from tests.safety_helpers import operation_deadline


class _StubAgent:
    def chat(self, message: str, **context):
        return {"reply": "", "tool_calls": []}


def test_operations_posture_facade_only_forwards_read_principal(
    make_service,
):
    from datetime import datetime, timezone

    from trading_assistant.operations import OperationsService
    from trading_assistant.operations.security_posture import (
        SecurityPostureReport,
    )

    seen: list[str] = []
    expected = SecurityPostureReport(
        observed_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
        checks=(),
    )

    class Reader:
        def report(self, *, limit_principal):
            seen.append(limit_principal)
            return expected

    operations = OperationsService(
        make_service(),
        security_posture_reader=Reader(),
    )

    assert operations.security_posture(
        limit_principal="session:21:operator"
    ) is expected
    assert seen == ["session:21:operator"]


BACKUP_KEY = b"b" * 32
BACKUP_KEY_ID = "operational-backup-2026"
BACKUP_MARKER = b"operational-backup-marker-never-plaintext"
BACKUP_IDENTITY = ProcessIdentity(87654, "pytest-backup-process-start")


class _OfflineInspector:
    def inspect(self, _identity):
        return ProcessProof.NOT_SAME


def _operational_database(tmp_path, state: str):
    source = tmp_path / f"{state}.sqlite3"
    engine = create_db_engine(f"sqlite:///{source}")
    assert upgrade(engine) is None
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE operational_backup_probe "
            "(value BLOB NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO operational_backup_probe (value) VALUES (?)",
            (BACKUP_MARKER,),
        )
        if state == "migrating":
            connection.exec_driver_sql(
                "UPDATE sensitive_migration_state "
                "SET state='migrating',"
                "started_at=CURRENT_TIMESTAMP,"
                "updated_at=CURRENT_TIMESTAMP"
            )
        elif state == "complete":
            connection.exec_driver_sql(
                "UPDATE sensitive_migration_state "
                "SET state='complete',"
                "started_at=CURRENT_TIMESTAMP,"
                "completed_at=CURRENT_TIMESTAMP,"
                "updated_at=CURRENT_TIMESTAMP,"
                "backup_path_hash=?",
                ("a" * 64,),
            )
    engine.dispose()
    return source


@pytest.mark.parametrize("migration_state", ["required", "migrating", "complete"])
def test_operational_backup_is_encrypted_for_every_migration_state(
    tmp_path,
    migration_state,
):
    source = _operational_database(tmp_path, migration_state)
    destination = tmp_path / "backups"
    receipt = backup_database(
        source,
        destination,
        retention_days=14,
        backup_key=BACKUP_KEY,
        backup_key_id=BACKUP_KEY_ID,
        process_identity=BACKUP_IDENTITY,
        process_inspector=_OfflineInspector(),
    )

    artifact = receipt.path
    encrypted = artifact.read_bytes()
    assert receipt.verified is True
    assert artifact.suffix == ".aesgcm"
    assert read_encrypted_backup_header(artifact)["key_id"] == BACKUP_KEY_ID
    assert b"SQLite format 3" not in encrypted
    assert BACKUP_MARKER not in encrypted
    assert list(destination.glob("*.sqlite3")) == []
    assert list_committed_backups(destination) == (artifact,)
    assert (destination / f".{artifact.name}.pending").samefile(artifact)
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700


def test_large_operational_backup_completes_without_source_renewal_livelock(
    tmp_path,
):
    source = _operational_database(tmp_path, "required")
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE large_backup_probe (payload BLOB NOT NULL)"
        )
        connection.execute(
            "INSERT INTO large_backup_probe(payload) "
            "VALUES (zeroblob(2097152))"
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    destination = tmp_path / "large-backups"

    started = time.monotonic()
    with operation_deadline(8.0):
        receipt = backup_database(
            source,
            destination,
            retention_days=14,
            backup_key=BACKUP_KEY,
            backup_key_id=BACKUP_KEY_ID,
            process_identity=BACKUP_IDENTITY,
            process_inspector=_OfflineInspector(),
        )
    elapsed = time.monotonic() - started

    assert elapsed < 8.0
    assert receipt.verified is True
    assert read_encrypted_backup_header(receipt.path)[
        "source_sha256"
    ] == receipt.source_sha256
    wal = source.with_name(f"{source.name}-wal")
    assert not wal.exists() or wal.stat().st_size < 1_048_576
    assert list_committed_backups(destination) == (receipt.path,)


def test_operational_backup_rotates_only_its_encrypted_artifacts(tmp_path):
    source = _operational_database(tmp_path, "required")
    destination = tmp_path / "backups"
    destination.mkdir()
    old_receipt = create_encrypted_database_backup(
        source,
        destination,
        backup_key=BACKUP_KEY,
        backup_key_id=BACKUP_KEY_ID,
        schema_head="20260727_0015",
        now=lambda: utcnow() - timedelta(days=30),
        artifact_label="whole-database-v1",
    )
    old_match = old_receipt.path
    old_anchor = destination / f".{old_match.name}.pending"
    old_state = destination / f".{old_match.name}.commit-state"
    legacy_match = (
        destination
        / "20000101T000000000000Z-whole-database-v1.sqlite3.aesgcm"
    )
    legacy_match.write_bytes(b"legacy-target-anchor-only")
    legacy_anchor = destination / f".{legacy_match.name}.pending"
    os.link(legacy_match, legacy_anchor)
    unrelated = destination / "keep-me.aesgcm"
    unrelated.write_bytes(b"unrelated")
    old_time = time.time() - 30 * 86400
    os.utime(old_match, (old_time, old_time))
    os.utime(legacy_match, (old_time, old_time))
    os.utime(unrelated, (old_time, old_time))

    receipt = backup_database(
        source,
        destination,
        retention_days=14,
        backup_key=BACKUP_KEY,
        backup_key_id=BACKUP_KEY_ID,
        process_identity=BACKUP_IDENTITY,
        process_inspector=_OfflineInspector(),
    )

    assert receipt.path.exists()
    assert not old_match.exists()
    assert not old_anchor.exists()
    assert not old_state.exists()
    assert legacy_match.exists()
    assert legacy_anchor.exists()
    assert unrelated.exists()


def test_operational_backup_requires_exclusive_maintenance_tenure(tmp_path):
    source = _operational_database(tmp_path, "required")
    engine = create_db_engine(f"sqlite:///{source}")
    inspector = _OfflineInspector()
    RuntimeTenureService(
        make_session_factory(engine),
        process_inspector=inspector,
    ).acquire_runtime(
        "app",
        ProcessIdentity(9001, "active-app-during-backup"),
        ttl_seconds=30,
    )
    destination = tmp_path / "blocked-backups"

    with pytest.raises(TenureUnavailable) as captured:
        backup_database(
            source,
            destination,
            retention_days=14,
            backup_key=BACKUP_KEY,
            backup_key_id=BACKUP_KEY_ID,
            process_identity=BACKUP_IDENTITY,
            process_inspector=inspector,
        )

    assert captured.value.stable_code == "runtime_tenure_active"
    assert not destination.exists() or list(destination.iterdir()) == []


def test_operational_backup_release_uncertainty_never_commits_artifact(
    tmp_path,
    monkeypatch,
):
    source = _operational_database(tmp_path, "required")
    destination = tmp_path / "release-uncertain-backups"
    original_close = RuntimeTenureGuard.close

    def close_but_lose_confirmation(guard):
        assert original_close(guard) is True
        return False

    monkeypatch.setattr(
        RuntimeTenureGuard,
        "close",
        close_but_lose_confirmation,
    )

    with pytest.raises(
        EncryptedBackupError,
        match="^backup_tenure_release_uncertain$",
    ):
        backup_database(
            source,
            destination,
            retention_days=14,
            backup_key=BACKUP_KEY,
            backup_key_id=BACKUP_KEY_ID,
            process_identity=BACKUP_IDENTITY,
            process_inspector=_OfflineInspector(),
        )

    assert list_committed_backups(destination) == ()
    assert not tuple(destination.glob("*.aesgcm"))


def test_operational_backup_cli_prints_only_stable_encrypted_receipt(
    tmp_path,
    app_config,
    capsys,
):
    source = _operational_database(tmp_path, "required")
    destination = tmp_path / "cli-backups"
    secrets = RuntimeSecrets(
        database_url=f"sqlite:///{source}",
        backup_encryption_key=base64.b64encode(BACKUP_KEY).decode(),
    )
    config = app_config.model_copy(
        update={
            "encryption": app_config.encryption.model_copy(
                update={"backup_key_id": BACKUP_KEY_ID}
            )
        }
    )

    result = backup_main(
        ["--destination", str(destination), "--retention-days", "14"],
        config_loader=lambda: config,
        secrets_loader=lambda *_args, **_kwargs: secrets,
        process_identity=BACKUP_IDENTITY,
        process_inspector=_OfflineInspector(),
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert result == 0
    assert payload == {
        "backup_key_id": BACKUP_KEY_ID,
        "path_hash": payload["path_hash"],
        "status": "verified",
    }
    assert len(payload["path_hash"]) == 64
    assert str(source) not in output
    assert str(destination) not in output
    assert BACKUP_MARKER.decode() not in output


def test_scheduled_backup_and_operator_docs_have_no_plaintext_entrypoint():
    install = Path("scripts/launchd/install.sh").read_text(encoding="utf-8")
    launchd_readme = Path("scripts/launchd/README.md").read_text(
        encoding="utf-8"
    )
    runbook = Path("docs/RUNBOOK.md").read_text(encoding="utf-8")
    ops_readme = Path("docs/ops/README.md").read_text(encoding="utf-8")

    assert "trading_assistant.ops.backup" in install
    assert '"$PROJ/backups"' not in install
    assert ".local/encrypted-backups" in install
    for document in (launchd_readme, runbook, ops_readme):
        assert "whole-database-v1.sqlite3.aesgcm" in document
        assert "standalone SQLite files" not in document
        assert "trading-assistant-*.sqlite3 |" not in document
        assert 'sqlite3 "$backup_file"' not in document


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
        assert json.loads(
            sensitive_store(session).read(durable, "response_json")
        ) == receipt
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
