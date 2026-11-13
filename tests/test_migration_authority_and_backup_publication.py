"""Round-4 regressions for migration authority and backup publication."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from tests.safety_helpers import bootstrap_database_to_revision
from trading_assistant.db import migration_authority as authority_module
from trading_assistant.db.migration_authority import (
    MigrationAuthority,
    issue_bootstrap_authority,
    issue_maintenance_authority,
)
from trading_assistant.db.schema import schema_status
from trading_assistant.db.session import (
    create_db_engine,
    make_session_factory,
)
from trading_assistant.ops.tenure import (
    ProcessIdentity,
    ProcessProof,
    RuntimeTenureGuard,
    RuntimeTenureService,
    install_runtime_mutation_barrier,
)
from trading_assistant.ops import backup as backup_module
from trading_assistant.ops.backup import (
    EncryptedBackupError,
    create_encrypted_database_backup,
    list_committed_backups,
    read_encrypted_backup_header,
)
from trading_assistant.ops.tenure import TenureLost


BACKUP_KEY = b"r" * 32
BACKUP_KEY_ID = "round4-backup-key"
BACKUP_NOW = datetime(
    2026,
    7,
    28,
    12,
    34,
    56,
    789012,
    tzinfo=timezone.utc,
)


def _url(path: Path) -> str:
    return f"sqlite:///{path}"


def _config(path: Path, connection, authority: object) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", _url(path))
    config.attributes["connection"] = connection
    config.attributes["migration_authority"] = authority
    return config


class _AbsentProcessInspector:
    def inspect(self, _identity: ProcessIdentity) -> ProcessProof:
        return ProcessProof.NOT_SAME


@contextmanager
def _held_authority(engine, connection, *, pid: int):
    service = RuntimeTenureService(
        make_session_factory(engine),
        process_inspector=_AbsentProcessInspector(),
    )
    handle = service.acquire_maintenance(
        ProcessIdentity(pid, f"round4-maintenance-{pid}"),
        ttl_seconds=30,
    )
    guard = RuntimeTenureGuard(
        handle,
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    guard.start()
    barrier = install_runtime_mutation_barrier(engine, guard)
    try:
        yield (
            issue_maintenance_authority(
                connection,
                guard=guard,
                barrier=barrier,
            ),
            guard,
        )
    finally:
        barrier.close()
        if not guard.closed:
            guard.close()


def _clone_as_hostile_subclass(
    source: MigrationAuthority,
    *,
    connection,
) -> MigrationAuthority:
    class HostileMigrationAuthority(MigrationAuthority):
        @property
        def mode(self):
            return "maintenance"

        def _validate_maintenance_binding(self, _connection) -> None:
            return None

        def assert_owned(self, _connection) -> None:
            return None

        @contextmanager
        def schema_fence(self, _connection):
            yield

    hostile = object.__new__(HostileMigrationAuthority)
    for slot in MigrationAuthority.__slots__:
        object.__setattr__(
            hostile,
            slot,
            object.__getattribute__(source, slot),
        )
    object.__setattr__(hostile, "_connection", connection)
    object.__setattr__(hostile, "_mode", "maintenance")
    return hostile


def _empty_bootstrap_token(tmp_path: Path) -> tuple[object, object]:
    engine = create_db_engine(_url(tmp_path / "token-source.db"))
    connection = engine.connect()
    return engine, connection


def _seed_backup_source(path: Path, *, payload_bytes: int = 0) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE round4_backup_probe "
            "(id INTEGER PRIMARY KEY, payload BLOB NOT NULL)"
        )
        connection.execute(
            "INSERT INTO round4_backup_probe(payload) VALUES (?)",
            (b"x" * payload_bytes,),
        )


def _create_backup(
    source: Path,
    destination: Path,
    **kwargs,
):
    return create_encrypted_database_backup(
        source,
        destination,
        backup_key=BACKUP_KEY,
        backup_key_id=BACKUP_KEY_ID,
        schema_head="20260727_0015",
        now=lambda: BACKUP_NOW,
        **kwargs,
    )


def test_hostile_authority_subclass_cannot_upgrade_without_maintenance_tenure(
    tmp_path,
):
    path = tmp_path / "hostile-subclass.db"
    bootstrap_database_to_revision(_url(path), "20260727_0014")
    engine = create_db_engine(_url(path))
    token_engine, token_connection = _empty_bootstrap_token(tmp_path)
    try:
        seed = issue_bootstrap_authority(token_connection)
        with engine.connect() as connection:
            hostile = _clone_as_hostile_subclass(
                seed,
                connection=connection,
            )
            before_version = connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            before_columns = tuple(
                column["name"]
                for column in inspect(connection).get_columns("trade_plans")
            )
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM runtime_tenures "
                    "WHERE resource_key='sensitive-migration:global' "
                    "AND state='held'"
                )
            ) == 0

            with pytest.raises(
                RuntimeError,
                match="^schema_migration_authority_required$",
            ):
                command.upgrade(
                    _config(path, connection, hostile),
                    "head",
                )

            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == before_version
            assert tuple(
                column["name"]
                for column in inspect(connection).get_columns("trade_plans")
            ) == before_columns

            with pytest.raises(
                RuntimeError,
                match="^schema_migration_authority_required$",
            ):
                command.upgrade(
                    _config(path, connection, hostile),
                    "head",
                )
    finally:
        token_connection.close()
        token_engine.dispose()
        engine.dispose()


@pytest.mark.parametrize(
    ("operation", "destination"),
    [
        ("stamp", "head"),
        ("upgrade", "20260727_0014"),
        ("upgrade", "base"),
        ("downgrade", "base"),
    ],
)
def test_bootstrap_authority_refuses_operation_or_destination_confusion(
    tmp_path,
    operation,
    destination,
):
    path = tmp_path / f"bootstrap-{operation}-{destination}.db"
    engine = create_db_engine(_url(path))
    with engine.connect() as connection:
        authority = issue_bootstrap_authority(connection)
        config = _config(path, connection, authority)

        with pytest.raises(
            RuntimeError,
            match="^schema_migration_authority_required$",
        ):
            getattr(command, operation)(config, destination)

        status = schema_status(engine)
        assert status.ready is False
        if "alembic_version" in inspect(connection).get_table_names():
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) is None

        with pytest.raises(
            RuntimeError,
            match="^schema_migration_authority_required$",
        ):
            command.upgrade(config, "head")
    engine.dispose()


def test_upgrade_authority_cannot_be_reinterpreted_as_downgrade_authority(
    tmp_path,
):
    path = tmp_path / "maintenance-operation-confusion.db"
    bootstrap_database_to_revision(_url(path), "head")
    engine = create_db_engine(_url(path))
    with engine.connect() as connection:
        with _held_authority(
            engine,
            connection,
            pid=94801,
        ) as (authority, _guard):
            config = _config(path, connection, authority)
            with pytest.raises(
                RuntimeError,
                match="^schema_migration_authority_required$",
            ):
                command.downgrade(config, "20260727_0014")

            assert schema_status(engine).ready is True
            with pytest.raises(
                RuntimeError,
                match="^schema_migration_authority_required$",
            ):
                command.upgrade(config, "head")
    engine.dispose()


def test_maintenance_upgrade_authority_rejects_stamp_without_version_drift(
    tmp_path,
):
    path = tmp_path / "maintenance-stamp-confusion.db"
    bootstrap_database_to_revision(_url(path), "20260727_0014")
    engine = create_db_engine(_url(path))
    with engine.connect() as connection:
        before_columns = tuple(
            column["name"]
            for column in inspect(connection).get_columns("trade_plans")
        )
        connection.rollback()
        with _held_authority(
            engine,
            connection,
            pid=94803,
        ) as (authority, _guard):
            with pytest.raises(
                RuntimeError,
                match="^schema_migration_authority_required$",
            ):
                command.stamp(
                    _config(path, connection, authority),
                    "head",
                )
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260727_0014"
        assert tuple(
            column["name"]
            for column in inspect(connection).get_columns("trade_plans")
        ) == before_columns
    engine.dispose()


def test_hostile_subclass_is_rejected_by_every_authority_boundary(tmp_path):
    engine, connection = _empty_bootstrap_token(tmp_path)
    wrong_engine = create_db_engine(_url(tmp_path / "wrong.db"))
    try:
        seed = issue_bootstrap_authority(connection)
        hostile = _clone_as_hostile_subclass(seed, connection=connection)
        boundaries = (
            lambda: authority_module.activate_migration_authority(
                hostile,
                connection,
                destination_revisions=(
                    schema_status(engine).head,
                ),
            ),
            lambda: authority_module.assert_migration_authority(
                hostile,
                connection,
                allowed_modes=frozenset({"bootstrap"}),
            ),
            lambda: authority_module.finish_migration_authority(
                hostile,
                connection,
            ),
        )
        for boundary in boundaries:
            with pytest.raises(
                RuntimeError,
                match="^schema_migration_authority_required$",
            ):
                boundary()

        with wrong_engine.connect() as wrong_connection:
            with pytest.raises(
                RuntimeError,
                match="^schema_migration_authority_required$",
            ):
                authority_module.activate_migration_authority(
                    hostile,
                    wrong_connection,
                    destination_revisions=(
                        schema_status(engine).head,
                    ),
                )

        authority_module.retire_migration_authority(hostile)
        with pytest.raises(
            RuntimeError,
            match="^schema_migration_authority_required$",
        ):
            authority_module.activate_migration_authority(
                hostile,
                connection,
                destination_revisions=(
                    schema_status(engine).head,
                ),
            )
    finally:
        connection.close()
        engine.dispose()
        wrong_engine.dispose()


def test_exact_authority_is_consumed_by_assert_and_finish_misuse(tmp_path):
    first_engine, first_connection = _empty_bootstrap_token(tmp_path)
    wrong_engine = create_db_engine(_url(tmp_path / "assert-wrong.db"))
    second_engine = create_db_engine(_url(tmp_path / "finish-source.db"))
    second_connection = second_engine.connect()
    try:
        head = schema_status(first_engine).head
        first = issue_bootstrap_authority(first_connection)
        assert type(first) is MigrationAuthority
        authority_module.activate_migration_authority(
            first,
            first_connection,
            destination_revisions=(head,),
        )
        with wrong_engine.connect() as wrong_connection:
            with pytest.raises(
                RuntimeError,
                match="^schema_migration_authority_required$",
            ):
                authority_module.assert_migration_authority(
                    first,
                    wrong_connection,
                    allowed_modes=frozenset({"bootstrap"}),
                )
        with pytest.raises(
            RuntimeError,
            match="^schema_migration_authority_required$",
        ):
            authority_module.assert_migration_authority(
                first,
                first_connection,
                allowed_modes=frozenset({"bootstrap"}),
            )

        second = issue_bootstrap_authority(second_connection)
        authority_module.activate_migration_authority(
            second,
            second_connection,
            destination_revisions=(head,),
        )
        with pytest.raises(
            RuntimeError,
            match="^schema_migration_authority_required$",
        ):
            authority_module.finish_migration_authority(
                second,
                second_connection,
            )
        with pytest.raises(
            RuntimeError,
            match="^schema_migration_authority_required$",
        ):
            authority_module.activate_migration_authority(
                second,
                second_connection,
                destination_revisions=(head,),
            )
    finally:
        first_connection.close()
        first_engine.dispose()
        wrong_engine.dispose()
        second_connection.close()
        second_engine.dispose()


def test_maintenance_upgrade_with_no_revision_step_is_refused_and_spent(
    tmp_path,
):
    path = tmp_path / "maintenance-no-step.db"
    bootstrap_database_to_revision(_url(path), "head")
    engine = create_db_engine(_url(path))
    with engine.connect() as connection:
        with _held_authority(
            engine,
            connection,
            pid=94802,
        ) as (authority, _guard):
            config = _config(path, connection, authority)
            with pytest.raises(
                RuntimeError,
                match="^schema_migration_authority_required$",
            ):
                command.upgrade(config, "head")
            assert schema_status(engine).ready is True
            with pytest.raises(
                RuntimeError,
                match="^schema_migration_authority_required$",
            ):
                command.upgrade(config, "head")
    engine.dispose()


@pytest.mark.parametrize(
    "loss_stage",
    [
        "verification_opened",
        "decrypt_chunk",
        "verification_hashed",
        "quick_check_complete",
        "before_artifact_commit",
    ],
)
def test_tenure_loss_at_every_verification_boundary_never_commits_backup(
    tmp_path,
    loss_stage,
):
    source = tmp_path / f"loss-{loss_stage}.db"
    destination = tmp_path / f"loss-{loss_stage}-backups"
    _seed_backup_source(source, payload_bytes=1_250_000)
    lost = False

    def lose(stage: str) -> None:
        nonlocal lost
        if stage == loss_stage:
            lost = True

    def ensure_owned() -> None:
        if lost:
            raise TenureLost()

    with pytest.raises(TenureLost):
        _create_backup(
            source,
            destination,
            stage_hook=lose,
            ensure_maintenance=ensure_owned,
        )

    assert list_committed_backups(destination) == ()
    assert not tuple(destination.glob("*.aesgcm"))


def test_cleanup_error_after_tenure_loss_cannot_expose_committed_backup(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "cleanup-loss.db"
    destination = tmp_path / "cleanup-loss-backups"
    _seed_backup_source(source)
    armed = False
    original_unlink = Path.unlink

    def fail_cleanup(path: Path, *args, **kwargs):
        if armed and path.parent == destination and path.name.startswith("."):
            raise OSError("hostile cleanup refusal")
        return original_unlink(path, *args, **kwargs)

    def lose(stage: str) -> None:
        nonlocal armed
        if stage == "before_artifact_commit":
            armed = True

    def ensure_owned() -> None:
        if armed:
            raise TenureLost()

    monkeypatch.setattr(Path, "unlink", fail_cleanup)

    with pytest.raises(TenureLost):
        _create_backup(
            source,
            destination,
            stage_hook=lose,
            ensure_maintenance=ensure_owned,
        )

    assert list_committed_backups(destination) == ()
    assert not tuple(destination.glob("*.aesgcm"))


def test_target_collision_is_not_a_committed_backup(tmp_path):
    source = tmp_path / "collision.db"
    destination = tmp_path / "collision-backups"
    _seed_backup_source(source)
    destination.mkdir()
    collision = (
        destination
        / "20260728T123456789012Z-before-sensitive-v1.sqlite3.aesgcm"
    )
    collision.write_bytes(b"attacker-controlled collision")

    with pytest.raises(EncryptedBackupError) as captured:
        _create_backup(source, destination)

    assert captured.value.stable_code == "encrypted_backup_exists"
    assert list_committed_backups(destination) == ()
    with pytest.raises(
        EncryptedBackupError,
        match="^encrypted_backup_not_committed$",
    ):
        read_encrypted_backup_header(collision)


def test_directory_fsync_failure_occurs_before_committed_name_is_visible(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "fsync.db"
    destination = tmp_path / "fsync-backups"
    _seed_backup_source(source)
    visible_during_fsync: list[tuple[str, ...]] = []
    original_fsync = os.fsync

    def fail_fsync(descriptor: int) -> None:
        original_fsync(descriptor)
        if (
            destination.exists()
            and os.fstat(descriptor).st_dev == destination.stat().st_dev
            and os.fstat(descriptor).st_ino == destination.stat().st_ino
        ):
            visible_during_fsync.append(
                tuple(path.name for path in destination.glob("*.aesgcm"))
            )
            raise OSError("injected directory fsync failure")

    monkeypatch.setattr(os, "fsync", fail_fsync)

    with pytest.raises(EncryptedBackupError) as captured:
        _create_backup(source, destination)

    assert captured.value.stable_code == "encrypted_backup_failed"
    assert visible_during_fsync
    assert set(visible_during_fsync) == {()}
    assert list_committed_backups(destination) == ()


def test_successful_backup_has_one_authenticated_committed_artifact(tmp_path):
    source = tmp_path / "committed.db"
    destination = tmp_path / "committed-backups"
    _seed_backup_source(source)

    receipt = _create_backup(source, destination)

    assert list_committed_backups(destination) == (receipt.path,)
    assert read_encrypted_backup_header(receipt.path)["source_sha256"] == (
        receipt.source_sha256
    )


def test_final_precommit_callback_failure_never_commits_backup(tmp_path):
    source = tmp_path / "precommit-failure.db"
    destination = tmp_path / "precommit-failure-backups"
    _seed_backup_source(source)
    visible_at_callback: tuple[Path, ...] | None = None

    def fail_before_commit() -> None:
        nonlocal visible_at_callback
        visible_at_callback = list_committed_backups(destination)
        raise TenureLost()

    with pytest.raises(TenureLost):
        _create_backup(
            source,
            destination,
            before_commit=fail_before_commit,
        )

    assert visible_at_callback == ()
    assert list_committed_backups(destination) == ()
    assert not tuple(destination.glob("*.aesgcm"))
