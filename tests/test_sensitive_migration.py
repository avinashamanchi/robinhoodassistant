"""Sensitive backup, migration, rotation, and startup trust tests."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from trading_assistant.db.migrate import upgrade
from trading_assistant.db.models import (
    AnalysisReportRow,
    AuditEvent,
    BacktestArtifact,
    BacktestRun,
    CircuitBreakerState,
    Fill,
    Heartbeat,
    LLMDecision,
    Order,
    PanicReceipt,
    Proposal,
    ReconciliationCursor,
    RiskEvent,
    Rule,
    RuleGroup,
    SensitiveMigrationState,
    StartupReconciliationState,
    TradePlanRow,
)
from trading_assistant.db.schema import schema_status
from trading_assistant.db.session import (
    create_db_engine,
    make_session_factory,
)
from trading_assistant.ops.backup import (
    EncryptedBackupReceipt,
    EncryptedBackupError,
    create_encrypted_database_backup,
    guarded_backup_maintenance,
    list_committed_backups,
    read_encrypted_backup_header,
)
from trading_assistant.ops.encrypt_sensitive import (
    SensitiveMigrationError,
    SensitiveMigrationReceipt,
    inspect_sensitive_envelopes,
    main as sensitive_cli_main,
    migrate_sensitive_fields,
    rotate_sensitive_fields,
    verify_sensitive_fields,
)
from trading_assistant.ops.safety_drill import _online_copy
from trading_assistant.ops.tenure import (
    ProcessIdentity,
    ProcessProof,
    RuntimeTenureGuard,
    RuntimeTenureService,
    TenureLost,
    TenureUncertain,
)
from trading_assistant.preflight import SensitiveEncryptionStateInspector
from trading_assistant.security.crypto import (
    SensitiveDataCipher,
    SensitiveFieldRef,
)
from trading_assistant.security.sensitive_fields import (
    SENSITIVE_FIELDS,
    bind_sensitive_cipher,
    persist_sensitive,
    sensitive_store,
)
from trading_assistant.security.secrets import RuntimeSecrets
from trading_assistant.broker.models import OrderStatus


BACKUP_KEY = bytes(range(32))
BACKUP_KEY_ID = "backup-key-2026-07"
SCHEMA_HEAD = "20260727_0014"
NOW = datetime(2026, 7, 27, 20, 30, 45, 123456, tzinfo=timezone.utc)
MARKER = "sensitive-backup-marker-never-visible"
OLD_KEY_ID = "field-key-old-2026"
NEW_KEY_ID = "field-key-new-2026"
OLD_KEY = b"o" * 32
NEW_KEY = b"n" * 32


class _OfflineInspector:
    def inspect(self, _identity):
        return ProcessProof.NOT_SAME


class _Interrupted(BaseException):
    pass


class _AdvancingClock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self):
        self.value += timedelta(microseconds=1)
        return self.value


def _legacy_engine(tmp_path):
    path = tmp_path / "legacy-sensitive.db"
    engine = create_db_engine(f"sqlite:///{path}")
    assert upgrade(engine) is None
    return engine, path


def _seed_all_registered_fields(engine) -> dict[tuple[str, str, str], str]:
    values: dict[tuple[str, str, str], str] = {}
    expires = NOW + timedelta(hours=1)
    with Session(engine) as session:
        order = Order(
            idempotency_key="legacy-order",
            ticker="AAPL",
            side="buy",
            order_type="market",
            approval_reason="legacy-order-reason",
        )
        session.add(order)
        session.flush()
        proposal = Proposal(
            order_id=order.id,
            reasoning="legacy-proposal-reasoning",
            expires_at=expires,
        )
        audit = AuditEvent(
            actor="operator",
            action="legacy",
            target_type="order",
            target_id=str(order.id),
            request_id="legacy-request",
            reason="legacy-audit-reason",
            detail_json='{"legacy":"audit-detail"}',
        )
        decision = LLMDecision(
            prompt="legacy-prompt",
            tool_calls_json='[{"legacy":"tool"}]',
            reasoning_summary="legacy-summary",
        )
        risk = RiskEvent(
            order_id=order.id,
            event_type="rejection",
            reason="legacy-risk-reason",
        )
        report = AnalysisReportRow(
            symbol="AAPL",
            as_of=NOW,
            action="BUY",
            confidence=Decimal("0.75"),
            analyst_version="v1",
            report_json='{"legacy":"report"}',
        )
        plan = TradePlanRow(
            symbol="AAPL",
            action="BUY",
            plan_json='{"legacy":"plan"}',
            sized_json='{"legacy":"sized"}',
        )
        breaker = CircuitBreakerState(
            scope_key="global",
            kind="global",
            target="",
            tripped=True,
            reason="legacy-breaker-reason",
            actor="operator",
        )
        startup = StartupReconciliationState(
            broker="mock",
            reason="legacy-reconciliation-reason",
            evidence_json='{"legacy":"evidence"}',
        )
        panic = PanicReceipt(
            account_scope="paper",
            request_id="legacy-panic",
            state="completed",
            response_json='{"legacy":"panic"}',
            started_at=NOW,
            completed_at=NOW + timedelta(minutes=1),
            expires_at=expires,
        )
        backtest_run = BacktestRun(
            label="legacy sensitive artifact run",
            config_json='{"status":"succeeded"}',
        )
        session.add(backtest_run)
        session.flush()
        backtest_artifact = BacktestArtifact(
            run_id=backtest_run.id,
            artifact_key="manifest",
            schema_version=1,
            payload_json='{"legacy":"backtest-artifact"}',
        )
        session.add_all(
            [
                proposal,
                audit,
                decision,
                risk,
                report,
                plan,
                breaker,
                startup,
                panic,
                backtest_artifact,
            ]
        )
        session.commit()
        for instance in [
            order,
            proposal,
            audit,
            decision,
            risk,
            report,
            plan,
            breaker,
            startup,
            panic,
            backtest_artifact,
        ]:
            table = instance.__table__.name
            primary_key = str(
                getattr(instance, instance.__mapper__.primary_key[0].key)
            )
            for column in SENSITIVE_FIELDS[table]:
                value = getattr(instance, column)
                if value is not None:
                    values[(table, primary_key, column)] = value
    return values


def _migration_kwargs(
    path: Path,
    backup_directory: Path,
    *,
    now=None,
    stage_hook=None,
):
    return {
        "backup_key": BACKUP_KEY,
        "backup_key_id": BACKUP_KEY_ID,
        "backup_directory": backup_directory,
        "database_path": path,
        "process_identity": ProcessIdentity(87654, "pytest-process-start"),
        "process_inspector": _OfflineInspector(),
        "now": now or _AdvancingClock(),
        "tenure_clock": lambda: NOW,
        "stage_hook": stage_hook,
    }


def _seed_database(path: Path, *, payload_bytes: int = 0) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE sample (id INTEGER PRIMARY KEY, narrative TEXT)"
        )
        connection.execute(
            "INSERT INTO sample (narrative) VALUES (?)",
            (MARKER + ("x" * payload_bytes),),
        )
        connection.commit()


def _create(
    source: Path,
    destination: Path,
    **kwargs,
):
    return create_encrypted_database_backup(
        source,
        destination,
        backup_key=BACKUP_KEY,
        backup_key_id=BACKUP_KEY_ID,
        schema_head=SCHEMA_HEAD,
        now=lambda: NOW,
        **kwargs,
    )


def test_encrypted_backup_is_verified_private_and_contains_no_sqlite_markers(
    tmp_path,
):
    source = tmp_path / "source.db"
    destination = tmp_path / "private" / "backups"
    _seed_database(source)

    receipt = _create(source, destination)

    artifact = receipt.path
    encrypted = artifact.read_bytes()
    header = read_encrypted_backup_header(artifact)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert b"SQLite format 3" not in encrypted
    assert MARKER.encode() not in encrypted
    assert header == {
        "algorithm": "AES-256-GCM",
        "chunk_bytes": 1_048_576,
        "created_at": "2026-07-27T20:30:45.123456Z",
        "key_id": BACKUP_KEY_ID,
        "schema_head": SCHEMA_HEAD,
        "source_sha256": receipt.source_sha256,
        "version": 1,
    }
    assert receipt.verified is True
    assert receipt.path_hash != receipt.source_sha256
    assert list_committed_backups(destination) == (artifact,)
    pending = destination / f".{artifact.name}.pending"
    assert pending.exists()
    assert pending.samefile(artifact)


def test_backup_streams_more_than_two_mebibytes_in_bounded_chunks(tmp_path):
    source = tmp_path / "large.db"
    destination = tmp_path / "backups"
    _seed_database(source, payload_bytes=2_500_000)
    stages: list[str] = []

    _create(source, destination, stage_hook=stages.append)

    assert stages.count("encrypt_chunk") >= 3
    assert stages.count("decrypt_chunk") >= 3


def test_backup_final_path_is_absent_until_private_verification_completes(
    tmp_path,
):
    source = tmp_path / "verify-before-publish.db"
    destination = tmp_path / "verify-before-publish-backups"
    _seed_database(source, payload_bytes=1_250_000)
    stages: list[str] = []
    published_during_verification: dict[str, tuple[str, ...]] = {}

    def observe(stage: str) -> None:
        stages.append(stage)
        if stage in {
            "verification_opened",
            "decrypt_chunk",
            "verification_hashed",
            "quick_check_complete",
        }:
            published_during_verification[stage] = tuple(
                path.name
                for path in destination.iterdir()
                if not path.name.startswith(".")
            )

    receipt = _create(source, destination, stage_hook=observe)

    assert published_during_verification
    assert set(published_during_verification.values()) == {()}
    assert stages.index("quick_check_complete") < stages.index(
        "before_artifact_commit"
    )
    assert list_committed_backups(destination) == (receipt.path,)


def test_ownership_loss_during_private_verification_never_publishes(
    tmp_path,
):
    source = tmp_path / "verification-tenure-loss.db"
    destination = tmp_path / "verification-tenure-loss-backups"
    _seed_database(source, payload_bytes=1_250_000)
    phase = "encrypting"
    visible_at_verification: tuple[str, ...] | None = None

    def observe(stage: str) -> None:
        nonlocal phase, visible_at_verification
        if stage == "verification_opened":
            phase = "verifying"
            visible_at_verification = tuple(
                path.name
                for path in destination.iterdir()
                if not path.name.startswith(".")
            )

    def ensure_owned() -> None:
        if phase == "verifying":
            raise TenureLost()

    with pytest.raises(TenureLost):
        _create(
            source,
            destination,
            ensure_maintenance=ensure_owned,
            stage_hook=observe,
        )

    assert visible_at_verification == ()
    assert destination.exists()
    assert not list(destination.iterdir())


def test_large_snapshot_expiry_aborts_before_publication_and_cleans_temps(
    tmp_path,
):
    source = tmp_path / "large-expired.db"
    destination = tmp_path / "expired-backups"
    _seed_database(source, payload_bytes=2_500_000)
    monotonic_values = iter((0.0, 0.0, 30.0))

    class InMemoryGuard:
        renewals = 0
        starts = 0

        def ensure_owned(self):
            return None

        def renew_once(self):
            self.renewals += 1
            return True

        def start(self):
            self.starts += 1

    guard = InMemoryGuard()
    maintenance = guarded_backup_maintenance(
        guard,
        ttl_seconds=30,
        monotonic=lambda: next(monotonic_values),
    )

    with pytest.raises(EncryptedBackupError) as captured:
        _create(source, destination, maintenance=maintenance)

    assert captured.value.stable_code == "backup_snapshot_tenure_expired"
    assert guard.renewals == 0
    assert guard.starts == 0
    assert destination.exists()
    assert not list(destination.iterdir())


def test_backup_renews_maintenance_within_large_snapshot_hash(tmp_path):
    source = tmp_path / "large-hash.db"
    destination = tmp_path / "hash-backups"
    _seed_database(source, payload_bytes=2_500_000)
    phase = "before"
    hash_renewals: list[str] = []

    def stage(stage_name: str) -> None:
        nonlocal phase
        if stage_name == "snapshot_created":
            phase = "hashing"
        elif stage_name == "snapshot_hashed":
            phase = "after"

    def renew() -> None:
        if phase == "hashing":
            hash_renewals.append("renew")

    _create(
        source,
        destination,
        ensure_maintenance=renew,
        stage_hook=stage,
    )

    assert len(hash_renewals) >= 4


@pytest.mark.parametrize("operation", ["migrate", "rotate"])
def test_sensitive_operations_start_renewal_only_after_snapshot_closes(
    tmp_path,
    monkeypatch,
    operation,
):
    import trading_assistant.ops.encrypt_sensitive as module

    engine, path = _legacy_engine(tmp_path)
    starts: list[str] = []
    original_start = RuntimeTenureGuard.start

    def record_start(_guard):
        starts.append("started")

    def snapshot_probe(
        _source,
        destination,
        **kwargs,
    ):
        assert starts == []
        assert "ensure_maintenance" not in kwargs
        maintenance = kwargs["maintenance"]
        maintenance.check_snapshot()
        assert starts == []
        maintenance.complete_snapshot()
        assert starts == ["started"]
        return EncryptedBackupReceipt(
            path=Path(destination) / "probe.sqlite3.aesgcm",
            path_hash="a" * 64,
            source_sha256="b" * 64,
            created_at=NOW.isoformat(),
            schema_head=SCHEMA_HEAD,
            backup_key_id=BACKUP_KEY_ID,
            verified=True,
        )

    def stop_after_backup(stage):
        if stage == "backup_verified":
            raise _Interrupted()

    monkeypatch.setattr(RuntimeTenureGuard, "start", record_start)
    monkeypatch.setattr(
        module,
        "create_encrypted_database_backup",
        snapshot_probe,
    )
    try:
        with pytest.raises(_Interrupted):
            if operation == "migrate":
                migrate_sensitive_fields(
                    engine,
                    SensitiveDataCipher(
                        {OLD_KEY_ID: OLD_KEY},
                        active_key_id=OLD_KEY_ID,
                    ),
                    **_migration_kwargs(
                        path,
                        tmp_path / "migrate-backups",
                        stage_hook=stop_after_backup,
                    ),
                )
            else:
                monkeypatch.setattr(
                    module,
                    "_state",
                    lambda _engine: SimpleNamespace(
                        state="complete",
                        active_key_id=OLD_KEY_ID,
                    ),
                )
                rotate_sensitive_fields(
                    engine,
                    old_cipher=SensitiveDataCipher(
                        {OLD_KEY_ID: OLD_KEY},
                        active_key_id=OLD_KEY_ID,
                    ),
                    new_cipher=SensitiveDataCipher(
                        {
                            OLD_KEY_ID: OLD_KEY,
                            NEW_KEY_ID: NEW_KEY,
                        },
                        active_key_id=NEW_KEY_ID,
                    ),
                    new_key_id=NEW_KEY_ID,
                    retained_key_ids=[NEW_KEY_ID],
                    **_migration_kwargs(
                        path,
                        tmp_path / "rotate-backups",
                        stage_hook=stop_after_backup,
                    ),
                )
    finally:
        monkeypatch.setattr(RuntimeTenureGuard, "start", original_start)


def test_backup_refuses_overwrite_and_preserves_first_artifact(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "backups"
    _seed_database(source)
    first = _create(source, destination)
    original = first.path.read_bytes()

    with pytest.raises(EncryptedBackupError) as exc:
        _create(source, destination)

    assert exc.value.stable_code == "encrypted_backup_exists"
    assert first.path.read_bytes() == original
    assert list_committed_backups(destination) == (first.path,)


@pytest.mark.parametrize(
    "failure_stage",
    [
        "snapshot_created",
        "snapshot_hashed",
        "header_written",
        "encrypt_chunk",
        "ciphertext_fsynced",
        "before_artifact_commit",
        "verification_opened",
        "decrypt_chunk",
        "verification_hashed",
        "quick_check_complete",
    ],
)
def test_every_backup_failure_stage_removes_all_plaintext_temps(
    tmp_path,
    failure_stage,
):
    source = tmp_path / f"{failure_stage}.db"
    destination = tmp_path / f"{failure_stage}-backups"
    _seed_database(source)

    def fail_at(stage: str) -> None:
        if stage == failure_stage:
            raise RuntimeError("injected-backup-failure")

    with pytest.raises(EncryptedBackupError) as exc:
        _create(source, destination, stage_hook=fail_at)

    assert exc.value.stable_code == "encrypted_backup_failed"
    assert MARKER not in str(exc.value)
    assert not list(destination.iterdir())


def test_backup_cancellation_removes_plaintext_and_unpublished_artifact(
    tmp_path,
):
    source = tmp_path / "cancel.db"
    destination = tmp_path / "cancel-backups"
    _seed_database(source)

    def cancel(stage: str) -> None:
        if stage == "before_artifact_commit":
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        _create(source, destination, stage_hook=cancel)

    assert not list(destination.iterdir())


@pytest.mark.parametrize("length", [0, 16, 31, 33, 64])
def test_backup_requires_a_dedicated_exact_32_byte_key(tmp_path, length):
    source = tmp_path / f"bad-key-{length}.db"
    destination = tmp_path / "bad-key-backups"
    _seed_database(source)

    with pytest.raises(EncryptedBackupError) as exc:
        create_encrypted_database_backup(
            source,
            destination,
            backup_key=b"k" * length,
            backup_key_id=BACKUP_KEY_ID,
            schema_head=SCHEMA_HEAD,
            now=lambda: NOW,
        )

    assert exc.value.stable_code == "backup_key_invalid"
    assert not destination.exists()


def test_backup_temps_are_mode_0600_while_failure_hook_inspects_directory(
    tmp_path,
):
    source = tmp_path / "modes.db"
    destination = tmp_path / "mode-backups"
    _seed_database(source)
    observed_directory_modes: list[int] = []
    observed_member_modes: list[int] = []

    def inspect_modes(stage: str) -> None:
        if stage in {"snapshot_created", "verification_opened"}:
            transactions = tuple(
                candidate
                for candidate in destination.iterdir()
                if candidate.name.startswith(".backup-txn-")
            )
            assert len(transactions) == 1
            transaction = transactions[0]
            observed_directory_modes.append(
                stat.S_IMODE(transaction.stat().st_mode)
            )
            observed_member_modes.extend(
                stat.S_IMODE(candidate.stat().st_mode)
                for candidate in transaction.iterdir()
            )

    _create(source, destination, stage_hook=inspect_modes)

    assert observed_directory_modes
    assert observed_member_modes
    assert set(observed_directory_modes) == {0o700}
    assert set(observed_member_modes) == {0o600}


def test_migration_backs_up_before_mutation_and_encrypts_every_registered_field(
    tmp_path,
):
    engine, path = _legacy_engine(tmp_path)
    originals = _seed_all_registered_fields(engine)
    backup_directory = tmp_path / "private-backups"
    stages: list[str] = []

    def observe(stage: str) -> None:
        stages.append(stage)
        if stage == "before_first_row_mutation":
            artifacts = list(backup_directory.glob("*.aesgcm"))
            assert len(artifacts) == 1
            encrypted = artifacts[0].read_bytes()
            assert b"SQLite format 3" not in encrypted
            assert not any(
                plaintext.encode() in encrypted
                for plaintext in originals.values()
            )

    receipt = migrate_sensitive_fields(
        engine,
        SensitiveDataCipher(
            {OLD_KEY_ID: OLD_KEY},
            active_key_id=OLD_KEY_ID,
        ),
        **_migration_kwargs(
            path,
            backup_directory,
            stage_hook=observe,
        ),
    )

    assert receipt.status == "complete"
    assert receipt.active_key_id == OLD_KEY_ID
    assert receipt.rows_total == 11
    assert stages.index("backup_verified") < stages.index(
        "before_first_row_mutation"
    )
    with engine.connect() as connection:
        state = connection.execute(
            text("SELECT * FROM sensitive_migration_state")
        ).mappings().one()
        assert state["state"] == "complete"
        assert state["active_key_id"] == OLD_KEY_ID
        assert state["rows_total"] == state["rows_completed"] == 11
        assert state["backup_path_hash"] == receipt.backup_path_hash
        for (table, row, column), plaintext in originals.items():
            stored = connection.execute(
                text(
                    f'SELECT "{column}" FROM "{table}" '
                    f'WHERE "{_primary_key(connection, table)}" = :row'
                ),
                {"row": row},
            ).scalar_one()
            assert stored.startswith(f"enc:v1:{OLD_KEY_ID}:")
            assert (
                SensitiveDataCipher(
                    {OLD_KEY_ID: OLD_KEY},
                    active_key_id=OLD_KEY_ID,
                ).decrypt(
                    stored,
                    SensitiveFieldRef(table, row, column, 1),
                )
                == plaintext
            )


def _primary_key(connection, table: str) -> str:
    rows = connection.execute(
        text(f'PRAGMA table_info("{table}")')
    ).mappings()
    primary = [row["name"] for row in rows if row["pk"]]
    assert len(primary) == 1
    return str(primary[0])


def test_interrupted_migration_resumes_from_authoritative_scan_not_counters(
    tmp_path,
):
    engine, path = _legacy_engine(tmp_path)
    with Session(engine) as session:
        session.add_all(
            [
                Order(
                    idempotency_key=f"legacy-{index}",
                    ticker="AAPL",
                    side="buy",
                    order_type="market",
                    approval_reason=f"legacy-reason-{index}",
                )
                for index in range(205)
            ]
        )
        session.commit()
    backup_directory = tmp_path / "resume-backups"
    batches: list[int] = []

    def interrupt(stage: str) -> None:
        if stage.startswith("batch_committed:"):
            batches.append(int(stage.rsplit(":", 1)[1]))
            if len(batches) == 1:
                raise _Interrupted()

    with pytest.raises(_Interrupted):
        migrate_sensitive_fields(
            engine,
            SensitiveDataCipher(
                {OLD_KEY_ID: OLD_KEY},
                active_key_id=OLD_KEY_ID,
            ),
            **_migration_kwargs(
                path,
                backup_directory,
                now=_AdvancingClock(),
                stage_hook=interrupt,
            ),
        )
    with engine.begin() as connection:
        state = connection.execute(
            text("SELECT state,rows_completed FROM sensitive_migration_state")
        ).one()
        assert state[0] == "migrating"
        assert state[1] == 100
        connection.execute(
            text(
                "UPDATE sensitive_migration_state "
                "SET rows_total=100,rows_completed=0"
            )
        )

    resume_clock = _AdvancingClock()
    resume_clock.value += timedelta(seconds=1)
    receipt = migrate_sensitive_fields(
        engine,
        SensitiveDataCipher(
            {OLD_KEY_ID: OLD_KEY},
            active_key_id=OLD_KEY_ID,
        ),
        **_migration_kwargs(
            path,
            backup_directory,
            now=resume_clock,
        ),
    )

    assert receipt.rows_total == 205
    assert verify_sensitive_fields(
        engine,
        SensitiveDataCipher(
            {OLD_KEY_ID: OLD_KEY},
            active_key_id=OLD_KEY_ID,
        ),
        configured_active_key_id=OLD_KEY_ID,
    ).rows_total == 205


def test_large_authoritative_scans_renew_maintenance_within_scan(
    tmp_path,
    monkeypatch,
):
    from trading_assistant.ops.tenure import RuntimeTenureGuard

    engine, path = _legacy_engine(tmp_path)
    with Session(engine) as session:
        session.add_all(
            [
                Order(
                    idempotency_key=f"renew-{index}",
                    ticker="AAPL",
                    side="buy",
                    order_type="market",
                    approval_reason=f"renew-reason-{index}",
                )
                for index in range(205)
            ]
        )
        session.commit()
    renewals: list[str] = []
    original = RuntimeTenureGuard.renew_once

    def record(self):
        renewals.append("renew")
        return original(self)

    monkeypatch.setattr(RuntimeTenureGuard, "renew_once", record)

    migrate_sensitive_fields(
        engine,
        SensitiveDataCipher(
            {OLD_KEY_ID: OLD_KEY},
            active_key_id=OLD_KEY_ID,
        ),
        **_migration_kwargs(path, tmp_path / "scan-renew-backups"),
    )

    assert len(renewals) >= 12


def test_maintenance_renewal_loss_durably_fails_before_mutation(
    tmp_path,
    monkeypatch,
):
    from trading_assistant.ops.tenure import RuntimeTenureGuard

    engine, path = _legacy_engine(tmp_path)
    _seed_all_registered_fields(engine)
    monkeypatch.setattr(
        RuntimeTenureGuard,
        "renew_once",
        lambda _self: False,
    )

    with pytest.raises(SensitiveMigrationError) as captured:
        migrate_sensitive_fields(
            engine,
            SensitiveDataCipher(
                {OLD_KEY_ID: OLD_KEY},
                active_key_id=OLD_KEY_ID,
            ),
            **_migration_kwargs(
                path,
                tmp_path / "renewal-loss-backups",
            ),
        )

    assert (
        captured.value.stable_code
        == "sensitive_migration_tenure_lost"
    )
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT state FROM sensitive_migration_state")
        ) == "failed"
        assert connection.scalar(
            text(
                "SELECT approval_reason FROM orders "
                "WHERE idempotency_key='legacy-order'"
            )
        ) == "legacy-order-reason"
    backup_directory = tmp_path / "renewal-loss-backups"
    assert backup_directory.exists()
    assert not list(backup_directory.iterdir())


def test_maintenance_release_uncertainty_durably_blocks_startup(
    tmp_path,
    monkeypatch,
):
    from trading_assistant.ops.tenure import RuntimeTenureGuard

    engine, path = _legacy_engine(tmp_path)
    _seed_all_registered_fields(engine)
    monkeypatch.setattr(
        RuntimeTenureGuard,
        "close",
        lambda _self: False,
    )

    with pytest.raises(SensitiveMigrationError) as captured:
        migrate_sensitive_fields(
            engine,
            SensitiveDataCipher(
                {OLD_KEY_ID: OLD_KEY},
                active_key_id=OLD_KEY_ID,
            ),
            **_migration_kwargs(
                path,
                tmp_path / "release-uncertain-backups",
            ),
        )

    assert (
        captured.value.stable_code
        == "sensitive_migration_release_uncertain"
    )
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT state FROM sensitive_migration_state")
        ) == "failed"


def test_release_response_loss_resolves_only_from_exact_database_truth(
    tmp_path,
    monkeypatch,
):
    engine, path = _legacy_engine(tmp_path)
    _seed_all_registered_fields(engine)
    original_release = RuntimeTenureService._release

    def commit_release_then_lose_response(self, *args, **kwargs):
        assert original_release(self, *args, **kwargs) is True
        raise TenureUncertain()

    monkeypatch.setattr(
        RuntimeTenureService,
        "_release",
        commit_release_then_lose_response,
    )
    cipher = SensitiveDataCipher(
        {OLD_KEY_ID: OLD_KEY},
        active_key_id=OLD_KEY_ID,
    )

    receipt = migrate_sensitive_fields(
        engine,
        cipher,
        **_migration_kwargs(
            path,
            tmp_path / "release-response-loss-backups",
        ),
    )

    assert receipt.status == "complete"
    with engine.connect() as connection:
        tenure = connection.execute(
            text(
                "SELECT state,owner_id,generation,released_at,expires_at "
                "FROM runtime_tenures "
                "WHERE resource_key='sensitive-migration:global'"
            )
        ).mappings().one()
        assert tenure["state"] == "released"
        assert tenure["generation"] == 2
        assert tenure["released_at"] == tenure["expires_at"]
    assert SensitiveEncryptionStateInspector(
        engine,
        schema_version=1,
        active_key_id=OLD_KEY_ID,
        cipher=cipher,
    ).inspect().passed


def test_mid_batch_expiry_fences_source_transaction_and_preserves_rows(
    tmp_path,
):
    engine, path = _legacy_engine(tmp_path)
    _seed_all_registered_fields(engine)

    class TenureClock:
        value = NOW

        def __call__(self):
            return self.value

    tenure_clock = TenureClock()
    observed: list[str] = []

    def expire_before_commit(stage: str) -> None:
        observed.append(stage)
        if stage == "before_batch_commit":
            tenure_clock.value += timedelta(seconds=31)

    kwargs = _migration_kwargs(
        path,
        tmp_path / "mid-batch-expiry-backups",
        stage_hook=expire_before_commit,
    )
    kwargs["tenure_clock"] = tenure_clock

    with pytest.raises(SensitiveMigrationError) as captured:
        migrate_sensitive_fields(
            engine,
            SensitiveDataCipher(
                {OLD_KEY_ID: OLD_KEY},
                active_key_id=OLD_KEY_ID,
            ),
            **kwargs,
        )

    assert "before_batch_commit" in observed
    assert (
        captured.value.stable_code
        == "sensitive_migration_tenure_lost"
    )
    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT approval_reason FROM orders "
                "WHERE idempotency_key='legacy-order'"
            )
        ) == "legacy-order-reason"
        # Authority can no longer be proven, so the predecessor may not
        # rewrite terminal evidence. Migrating plus the held/expired tenure
        # remains fail-closed for startup.
        assert connection.scalar(
            text("SELECT state FROM sensitive_migration_state")
        ) == "migrating"


def test_release_uncertainty_after_successor_fence_does_not_write_state(
    tmp_path,
    monkeypatch,
):
    from trading_assistant.ops.tenure import RuntimeTenureGuard

    engine, path = _legacy_engine(tmp_path)
    _seed_all_registered_fields(engine)
    successor_owner = "11111111-1111-4111-8111-111111111111"

    def successor_wins_before_close(self):
        self._stop.set()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE runtime_tenures "
                    "SET owner_id=:owner_id,generation=generation+1,"
                    "pid=99001,process_start_identity='successor-start',"
                    "renewed_at=:renewed,expires_at=:expires "
                    "WHERE resource_key='sensitive-migration:global'"
                ),
                {
                    "owner_id": successor_owner,
                    "renewed": NOW,
                    "expires": NOW + timedelta(seconds=60),
                },
            )
        return False

    monkeypatch.setattr(
        RuntimeTenureGuard,
        "close",
        successor_wins_before_close,
    )

    with pytest.raises(SensitiveMigrationError) as captured:
        migrate_sensitive_fields(
            engine,
            SensitiveDataCipher(
                {OLD_KEY_ID: OLD_KEY},
                active_key_id=OLD_KEY_ID,
            ),
            **_migration_kwargs(
                path,
                tmp_path / "successor-release-backups",
            ),
        )

    assert (
        captured.value.stable_code
        == "sensitive_migration_release_uncertain"
    )
    with engine.connect() as connection:
        state = connection.execute(
            text(
                "SELECT state,active_key_id "
                "FROM sensitive_migration_state"
            )
        ).one()
        tenure = connection.execute(
            text(
                "SELECT state,owner_id FROM runtime_tenures "
                "WHERE resource_key='sensitive-migration:global'"
            )
        ).one()
    assert state == ("complete", OLD_KEY_ID)
    assert tenure == ("held", successor_owner)


def test_frozen_clock_allows_equal_started_and_completed_timestamps(tmp_path):
    engine, path = _legacy_engine(tmp_path)
    _seed_all_registered_fields(engine)
    cipher = SensitiveDataCipher(
        {OLD_KEY_ID: OLD_KEY},
        active_key_id=OLD_KEY_ID,
    )

    migrate_sensitive_fields(
        engine,
        cipher,
        **_migration_kwargs(
            path,
            tmp_path / "frozen-clock-backups",
            now=lambda: NOW,
        ),
    )

    assert verify_sensitive_fields(
        engine,
        cipher,
        configured_active_key_id=OLD_KEY_ID,
    ).status == "verified"
    with Session(engine) as session:
        state = session.get(SensitiveMigrationState, 1)
        assert state.started_at == state.completed_at


def test_completed_migration_rerun_is_a_verified_read_only_noop(tmp_path):
    engine, path = _legacy_engine(tmp_path)
    _seed_all_registered_fields(engine)
    cipher = SensitiveDataCipher(
        {OLD_KEY_ID: OLD_KEY},
        active_key_id=OLD_KEY_ID,
    )
    kwargs = _migration_kwargs(path, tmp_path / "noop-backups")
    migrate_sensitive_fields(engine, cipher, **kwargs)
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        receipt = migrate_sensitive_fields(engine, cipher, **kwargs)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert receipt.status == "verified_noop"
    assert not any(
        statement.lstrip().upper().startswith(
            ("INSERT", "UPDATE", "DELETE", "BEGIN IMMEDIATE")
        )
        for statement in statements
    )
    assert len(list((tmp_path / "noop-backups").glob("*.aesgcm"))) == 1


def test_completed_migration_allows_new_encrypted_rows_at_restart_and_verify(
    tmp_path,
):
    engine, path = _legacy_engine(tmp_path)
    _seed_all_registered_fields(engine)
    cipher = SensitiveDataCipher(
        {OLD_KEY_ID: OLD_KEY},
        active_key_id=OLD_KEY_ID,
    )
    migrate_sensitive_fields(
        engine,
        cipher,
        **_migration_kwargs(path, tmp_path / "cardinality-insert-backups"),
    )
    factory = make_session_factory(engine)
    bind_sensitive_cipher(factory, cipher)
    with factory() as session:
        order = persist_sensitive(
            session,
            Order(
                idempotency_key="post-migration-order",
                ticker="AAPL",
                side="buy",
                order_type="market",
            ),
            {"approval_reason": "post-migration reviewed order"},
            session_factory=factory,
        )
        persist_sensitive(
            session,
            Proposal(
                order_id=order.id,
                expires_at=NOW + timedelta(hours=2),
            ),
            {"reasoning": "post-migration proposal"},
            session_factory=factory,
        )
        persist_sensitive(
            session,
            AuditEvent(
                actor="operator",
                action="post-migration",
                target_type="order",
                target_id=str(order.id),
                request_id="post-migration-audit",
                result_code="created",
            ),
            {
                "reason": "post-migration audit",
                "detail_json": '{"source":"normal-runtime-write"}',
            },
            session_factory=factory,
        )
        session.commit()

    verified = verify_sensitive_fields(
        engine,
        cipher,
        configured_active_key_id=OLD_KEY_ID,
    )
    startup = SensitiveEncryptionStateInspector(
        engine,
        schema_version=1,
        active_key_id=OLD_KEY_ID,
        cipher=cipher,
    ).inspect()

    assert verified.status == "verified"
    assert verified.rows_total == 14
    assert startup.passed


def test_completed_migration_allows_encrypted_row_deletion_at_restart_and_verify(
    tmp_path,
):
    engine, path = _legacy_engine(tmp_path)
    _seed_all_registered_fields(engine)
    cipher = SensitiveDataCipher(
        {OLD_KEY_ID: OLD_KEY},
        active_key_id=OLD_KEY_ID,
    )
    migrate_sensitive_fields(
        engine,
        cipher,
        **_migration_kwargs(path, tmp_path / "cardinality-delete-backups"),
    )
    factory = make_session_factory(engine)
    bind_sensitive_cipher(factory, cipher)
    with factory() as session:
        report = session.scalar(
            select(AnalysisReportRow).where(
                AnalysisReportRow.symbol == "AAPL"
            )
        )
        assert report is not None
        sensitive_store(session, factory).delete(report)
        session.commit()

    verified = verify_sensitive_fields(
        engine,
        cipher,
        configured_active_key_id=OLD_KEY_ID,
    )
    startup = SensitiveEncryptionStateInspector(
        engine,
        schema_version=1,
        active_key_id=OLD_KEY_ID,
        cipher=cipher,
    ).inspect()

    assert verified.status == "verified"
    assert verified.rows_total == 10
    assert startup.passed


def test_malformed_envelope_fails_durably_without_exposing_value(tmp_path):
    engine, path = _legacy_engine(tmp_path)
    _seed_all_registered_fields(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE orders SET approval_reason="
                "'enc:v1:field-key-old-2026:not-valid'"
            )
        )

    with pytest.raises(SensitiveMigrationError) as exc:
        migrate_sensitive_fields(
            engine,
            SensitiveDataCipher(
                {OLD_KEY_ID: OLD_KEY},
                active_key_id=OLD_KEY_ID,
            ),
            **_migration_kwargs(path, tmp_path / "failed-backups"),
        )

    assert exc.value.stable_code == "sensitive_migration_data_invalid"
    assert "not-valid" not in str(exc.value)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT state FROM sensitive_migration_state")
        ).scalar_one() == "failed"


def test_rotation_resumes_mixed_old_new_and_updates_state_only_after_verify(
    tmp_path,
):
    engine, path = _legacy_engine(tmp_path)
    _seed_all_registered_fields(engine)
    old_cipher = SensitiveDataCipher(
        {OLD_KEY_ID: OLD_KEY},
        active_key_id=OLD_KEY_ID,
    )
    migrate_sensitive_fields(
        engine,
        old_cipher,
        **_migration_kwargs(path, tmp_path / "migration-backups"),
    )
    new_cipher = SensitiveDataCipher(
        {OLD_KEY_ID: OLD_KEY, NEW_KEY_ID: NEW_KEY},
        active_key_id=NEW_KEY_ID,
    )
    state_during_batches: list[str] = []

    def observe(stage: str) -> None:
        if stage.startswith("batch_committed:"):
            with engine.connect() as connection:
                state_during_batches.append(
                    connection.execute(
                        text(
                            "SELECT state FROM sensitive_migration_state"
                        )
                    ).scalar_one()
                )
            if len(state_during_batches) == 1:
                raise _Interrupted()

    with pytest.raises(_Interrupted):
        rotate_sensitive_fields(
            engine,
            old_cipher=old_cipher,
            new_cipher=new_cipher,
            new_key_id=NEW_KEY_ID,
            retained_key_ids=(OLD_KEY_ID, NEW_KEY_ID),
            **_migration_kwargs(
                path,
                tmp_path / "rotation-backups",
                stage_hook=observe,
            ),
        )
    assert state_during_batches == ["rotating"]

    rotation_resume_clock = _AdvancingClock()
    rotation_resume_clock.value += timedelta(seconds=1)
    receipt = rotate_sensitive_fields(
        engine,
        old_cipher=old_cipher,
        new_cipher=new_cipher,
        new_key_id=NEW_KEY_ID,
        retained_key_ids=(OLD_KEY_ID, NEW_KEY_ID),
        **_migration_kwargs(
            path,
            tmp_path / "rotation-backups",
            now=rotation_resume_clock,
        ),
    )

    assert receipt.status == "complete"
    assert receipt.old_key_id == OLD_KEY_ID
    assert receipt.old_key_status == "retained"
    assert receipt.active_key_id == NEW_KEY_ID
    assert verify_sensitive_fields(
        engine,
        new_cipher,
        configured_active_key_id=NEW_KEY_ID,
    ).rows_total == 11


@pytest.mark.parametrize(
    ("replacement", "expected_code"),
    [
        ("legacy-plaintext", "sensitive_plaintext_detected"),
        (
            "enc:v1:field-key-unknown-2026:AAAA",
            "sensitive_key_unavailable",
        ),
        (
            "enc:v1:field-key-old-2026:not-canonical",
            "sensitive_envelope_invalid",
        ),
    ],
)
def test_startup_crypto_scan_returns_stable_redacted_field_codes(
    tmp_path,
    replacement,
    expected_code,
):
    engine, path = _legacy_engine(tmp_path)
    _seed_all_registered_fields(engine)
    cipher = SensitiveDataCipher(
        {OLD_KEY_ID: OLD_KEY},
        active_key_id=OLD_KEY_ID,
    )
    migrate_sensitive_fields(
        engine,
        cipher,
        **_migration_kwargs(path, tmp_path / "startup-scan-backups"),
    )
    # Simulate out-of-band on-disk corruption through an independent SQLite
    # connection; the application's runtime boundary correctly rejects this.
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE orders SET approval_reason=? "
            "WHERE idempotency_key='legacy-order'",
            (replacement,),
        )

    with pytest.raises(SensitiveMigrationError) as exc:
        inspect_sensitive_envelopes(
            engine,
            cipher,
            active_key_id=OLD_KEY_ID,
            schema_version=1,
        )

    assert exc.value.stable_code == expected_code
    assert replacement not in str(exc.value)


def test_startup_crypto_scan_blocks_valid_retained_key_as_mixed_state(
    tmp_path,
):
    engine, path = _legacy_engine(tmp_path)
    _seed_all_registered_fields(engine)
    old_cipher = SensitiveDataCipher(
        {OLD_KEY_ID: OLD_KEY},
        active_key_id=OLD_KEY_ID,
    )
    migrate_sensitive_fields(
        engine,
        old_cipher,
        **_migration_kwargs(path, tmp_path / "mixed-scan-backups"),
    )
    new_cipher = SensitiveDataCipher(
        {OLD_KEY_ID: OLD_KEY, NEW_KEY_ID: NEW_KEY},
        active_key_id=NEW_KEY_ID,
    )
    replacement = new_cipher.encrypt(
        "replacement",
        SensitiveFieldRef("orders", "1", "approval_reason", 1),
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE orders SET approval_reason=? WHERE id=1",
            (replacement,),
        )

    with pytest.raises(SensitiveMigrationError) as exc:
        inspect_sensitive_envelopes(
            engine,
            new_cipher,
            active_key_id=OLD_KEY_ID,
            schema_version=1,
        )

    assert exc.value.stable_code == "sensitive_mixed_key"


@pytest.mark.parametrize(
    ("command", "operation"),
    [
        (["migrate"], "migrate"),
        (["verify"], "verify"),
        (["rotate", "--new-key-id", NEW_KEY_ID], "rotate"),
    ],
)
def test_cli_uses_injected_secrets_and_prints_only_stable_receipt(
    monkeypatch,
    tmp_path,
    app_config,
    command,
    operation,
    capsys,
):
    database_marker = "secret-database-path-marker"
    secret_marker = "secret-key-material-marker"
    encryption = app_config.encryption.model_copy(
        update={
            "active_key_id": OLD_KEY_ID,
            "retained_key_ids": [NEW_KEY_ID],
            "backup_key_id": BACKUP_KEY_ID,
            "backup_directory": tmp_path / "private-backups",
        }
    )
    config = app_config.model_copy(update={"encryption": encryption})
    runtime_secrets = RuntimeSecrets(
        database_url=SecretStr(
            f"sqlite:///{tmp_path}/{database_marker}.db"
        ),
        candidate_signing_key=SecretStr(
            base64.b64encode(b"c" * 32).decode()
        ),
        backup_encryption_key=SecretStr(
            base64.b64encode(b"b" * 32).decode()
        ),
        field_encryption_keys={
            OLD_KEY_ID: SecretStr(
                base64.b64encode(OLD_KEY).decode()
            ),
            NEW_KEY_ID: SecretStr(
                base64.b64encode(NEW_KEY).decode()
            ),
        },
        app_api_token=SecretStr(secret_marker),
    )
    engine_marker = SimpleNamespace(
        url=SimpleNamespace(
            database=str(tmp_path / f"{database_marker}.db")
        )
    )
    calls: list[tuple[str, object]] = []

    def fake_loader(role, *, config):
        calls.append(
            ("secrets", (role, config.encryption.active_key_id))
        )
        return runtime_secrets

    def fake_engine_factory(database_url):
        assert database_marker in database_url.get_secret_value()
        calls.append(("engine", database_url))
        return engine_marker

    def fake_migrate(engine, cipher, **kwargs):
        assert engine is engine_marker
        assert cipher.active_key_id == OLD_KEY_ID
        assert kwargs["backup_key"] == b"b" * 32
        calls.append(("operation", "migrate"))
        return SensitiveMigrationReceipt(
            operation="migrate",
            status="complete",
            active_key_id=OLD_KEY_ID,
            rows_total=7,
            backup_path_hash="a" * 64,
        )

    def fake_verify(engine, cipher, **kwargs):
        assert engine is engine_marker
        assert cipher.active_key_id == OLD_KEY_ID
        assert kwargs["configured_active_key_id"] == OLD_KEY_ID
        calls.append(("operation", "verify"))
        return SensitiveMigrationReceipt(
            operation="verify",
            status="verified",
            active_key_id=OLD_KEY_ID,
            rows_total=7,
            backup_path_hash="a" * 64,
        )

    def fake_rotate(engine, **kwargs):
        assert engine is engine_marker
        assert kwargs["old_cipher"].active_key_id == OLD_KEY_ID
        assert kwargs["new_cipher"].active_key_id == NEW_KEY_ID
        assert kwargs["new_key_id"] == NEW_KEY_ID
        assert kwargs["retained_key_ids"] == [NEW_KEY_ID]
        assert kwargs["backup_key"] == b"b" * 32
        calls.append(("operation", "rotate"))
        return SensitiveMigrationReceipt(
            operation="rotate",
            status="complete",
            active_key_id=NEW_KEY_ID,
            rows_total=7,
            backup_path_hash="a" * 64,
            old_key_id=OLD_KEY_ID,
            old_key_status="retained",
        )

    monkeypatch.setattr(
        "trading_assistant.ops.encrypt_sensitive."
        "migrate_sensitive_fields",
        fake_migrate,
    )
    monkeypatch.setattr(
        "trading_assistant.ops.encrypt_sensitive."
        "verify_sensitive_fields",
        fake_verify,
    )
    monkeypatch.setattr(
        "trading_assistant.ops.encrypt_sensitive."
        "rotate_sensitive_fields",
        fake_rotate,
    )

    assert (
        sensitive_cli_main(
            command,
            config_loader=lambda: config,
            secrets_loader=fake_loader,
            engine_factory=fake_engine_factory,
            process_inspector=_OfflineInspector(),
            process_identity=ProcessIdentity(
                65432,
                "cli-test-process",
            ),
        )
        == 0
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["operation"] == operation
    assert payload["rows_total"] == 7
    assert payload["backup_path_hash"] == "a" * 64
    assert captured.err == ""
    assert database_marker not in captured.out
    assert secret_marker not in captured.out
    assert calls[-1] == ("operation", operation)


def test_cli_rotate_requires_exact_new_key_id():
    with pytest.raises(SystemExit) as captured:
        sensitive_cli_main(["rotate"])
    assert captured.value.code == 2


def _rehearsal_truth(path: Path) -> dict[str, object]:
    """Capture value-free business and safety state from an isolated fixture."""
    assert path.is_file()
    with sqlite3.connect(path) as connection:
        tables = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        )
        counts = tuple(
            (
                table,
                connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0],
            )
            for table in tables
            if table != "runtime_tenures"
        )
        queries = {
            "orders": (
                "SELECT id, idempotency_key, ticker, side, order_type, "
                "status, broker_order_id, submission_attempt, "
                "acceptance_state, plan_cancel_state, version "
                "FROM orders ORDER BY id"
            ),
            "fills": (
                "SELECT id, order_id, ticker, side, qty, price, "
                "broker_fill_id, reconciliation_state, filled_at "
                "FROM fills ORDER BY id"
            ),
            "proposals": (
                "SELECT id, order_id, source_rule_group_id, source_rule_id, "
                "plan_generation, ttl_minutes, created_at, expires_at "
                "FROM proposals ORDER BY id"
            ),
            "rule_groups": (
                "SELECT id, group_key, state, lease_owner, lease_expires_at, "
                "terminal_rule_id, version, reconciliation_required "
                "FROM rule_groups ORDER BY id"
            ),
            "rules": (
                "SELECT id, group_id, ticker, condition_json, action_json, "
                "state, plan_id, kind, fraction, hwm, deadline, "
                "pre_approved, activation, terminal_on_trigger "
                "FROM rules ORDER BY id"
            ),
            "breakers": (
                "SELECT scope_key, kind, target, tripped, generation, "
                "updated_at FROM circuit_breaker_state ORDER BY scope_key"
            ),
            "reconciliation": (
                "SELECT broker, stream, last_activity_id, last_activity_at, "
                "version FROM reconciliation_cursors "
                "ORDER BY broker, stream"
            ),
            "startup_reconciliation": (
                "SELECT broker, generation, completed_generation, status, "
                "actor, request_id, started_at, completed_at, updated_at "
                "FROM startup_reconciliation_state ORDER BY broker"
            ),
            "heartbeats": (
                "SELECT id, source, at FROM heartbeats ORDER BY id"
            ),
        }
        return {
            "counts": counts,
            **{
                name: tuple(connection.execute(query).fetchall())
                for name, query in queries.items()
            },
        }


def test_generated_copy_rehearses_sensitive_migration_without_business_mutation(
    tmp_path,
):
    """The irreversible path is proven only on generated, private copies."""
    source_engine, source_path = _legacy_engine(tmp_path)
    plaintext_values = _seed_all_registered_fields(source_engine)
    with Session(source_engine) as session:
        order = session.scalar(
            select(Order).where(Order.idempotency_key == "legacy-order")
        )
        assert order is not None
        order.status = OrderStatus.FILLED.value
        order.broker_order_id = "generated-paper-order"
        session.add(
            Fill(
                order_id=order.id,
                ticker="AAPL",
                side="buy",
                qty=Decimal("2"),
                price=Decimal("100"),
                broker_fill_id="generated-fill",
                filled_at=NOW,
            )
        )
        group = RuleGroup(group_key="generated-copy-group", state="active")
        session.add(group)
        session.flush()
        session.add(
            Rule(
                group_id=group.id,
                ticker="AAPL",
                condition_json='{"operator":"below","value":"90"}',
                action_json='{"side":"buy","notional":"50"}',
                state="active",
            )
        )
        session.add(
            ReconciliationCursor(
                broker="mock",
                stream="trade_updates",
                last_activity_id="generated-activity",
                last_activity_at=NOW,
                version=4,
            )
        )
        session.add(Heartbeat(source="daemon", at=NOW))
        session.commit()

    source_engine.dispose()
    with sqlite3.connect(source_path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert connection.execute(
            "PRAGMA journal_mode=DELETE"
        ).fetchone() == ("delete",)

    source_before = _rehearsal_truth(source_path)
    source_file_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    copy_path = _online_copy(
        source_path.resolve(strict=True),
        (tmp_path / "migration-rehearsal" / "source-copy.sqlite3").resolve(),
    )
    assert stat.S_IMODE(copy_path.stat().st_mode) == 0o600
    copy_before = _rehearsal_truth(copy_path)
    assert copy_before == source_before

    copy_engine = create_db_engine(f"sqlite:///{copy_path}")
    cipher = SensitiveDataCipher(
        {OLD_KEY_ID: OLD_KEY},
        active_key_id=OLD_KEY_ID,
    )
    receipt = migrate_sensitive_fields(
        copy_engine,
        cipher,
        **_migration_kwargs(
            copy_path,
            tmp_path / "migration-rehearsal" / "encrypted-backups",
        ),
    )
    verified = verify_sensitive_fields(
        copy_engine,
        cipher,
        configured_active_key_id=OLD_KEY_ID,
    )
    copy_schema = schema_status(copy_engine)
    sensitive_row_count = len(
        {(table, row_id) for table, row_id, _field in plaintext_values}
    )
    assert receipt.status == "complete"
    assert verified.status == "verified"
    assert receipt.rows_total == sensitive_row_count
    assert verified.rows_total == sensitive_row_count
    assert copy_schema.ready is True
    copy_engine.dispose()

    copy_after = _rehearsal_truth(copy_path)
    assert copy_after == copy_before
    assert _rehearsal_truth(source_path) == source_before
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == source_file_hash

    with sqlite3.connect(copy_path) as connection:
        for table, fields in SENSITIVE_FIELDS.items():
            for field in fields:
                values = tuple(
                    row[0]
                    for row in connection.execute(
                        f'SELECT "{field}" FROM "{table}" '
                        f'WHERE "{field}" IS NOT NULL AND "{field}" != \'\''
                    )
                )
                assert all(
                    value.startswith(f"enc:v1:{OLD_KEY_ID}:")
                    for value in values
                )
        unsafe_order_count = connection.execute(
            "SELECT COUNT(*) FROM orders WHERE status IN "
            "('approval_recorded','approved','submitting','submitted','canceled')"
        ).fetchone()[0]
        assert unsafe_order_count == 0

    manifest = {
        "schema_version": 1,
        "fixture": "generated_only",
        "source_path_hash": hashlib.sha256(
            str(source_path).encode("utf-8")
        ).hexdigest(),
        "source_bytes": source_path.stat().st_size,
        "copy_mode": "0600",
        "alembic_current": copy_schema.current,
        "sensitive_status": verified.status,
        "sensitive_rows": verified.rows_total,
        "business_truth_unchanged": True,
        "broker_writes": 0,
        "normal_database_touched": False,
    }
    serialized = json.dumps(manifest, sort_keys=True)
    assert str(source_path) not in serialized
    assert str(copy_path) not in serialized
    assert all(value not in serialized for value in plaintext_values.values())
    manifest_path = tmp_path / "migration-rehearsal-manifest.json"
    descriptor = os.open(
        manifest_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(serialized)
        handle.write("\n")
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
