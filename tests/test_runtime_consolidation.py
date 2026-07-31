from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from trading_assistant.db.migrate import upgrade
from trading_assistant.db.session import (
    create_db_engine,
    make_session_factory,
)
from trading_assistant.ops import runtime_consolidation as consolidation_module
from trading_assistant.ops.backup import (
    EncryptedBackupReceipt,
    backup_database as real_backup_database,
    list_committed_backups,
)
from trading_assistant.ops.runtime_consolidation import (
    ConsolidationError,
    ConsolidationRoots,
    LogicalSummary,
    consolidate_runtime,
)
from trading_assistant.ops.tenure import (
    ProcessIdentity,
    ProcessProof,
    RuntimeTenureService,
    TenureUnavailable,
)


BACKUP_KEY = b"r" * 32
BACKUP_KEY_ID = "runtime-consolidation-2026"
CONSOLIDATION_IDENTITY = ProcessIdentity(
    pid=7317,
    start_identity="pytest-runtime-consolidation",
)


class OfflineProcessInspector:
    def inspect(self, _identity: ProcessIdentity) -> ProcessProof:
        return ProcessProof.NOT_SAME


@dataclass(frozen=True)
class RuntimeFixture:
    source_root: Path
    destination_root: Path
    source_database: Path
    destination_database: Path
    roots: ConsolidationRoots


@dataclass(frozen=True)
class DatabaseSnapshot:
    device: int
    inode: int
    digest: str
    rows: tuple[tuple[int, str], ...]


def _create_current_database(path: Path, value: str) -> None:
    engine = create_db_engine(f"sqlite:///{path}")
    try:
        assert upgrade(engine) is None
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE consolidation_probe ("
                "id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO consolidation_probe (id, value) VALUES (?, ?)",
                (1, value),
            )
    finally:
        engine.dispose()
    os.chmod(path, 0o600)


@pytest.fixture
def runtime_fixture(tmp_path: Path) -> RuntimeFixture:
    destination_root = tmp_path / "canonical-runtime"
    source_root = (
        destination_root / ".worktrees" / "safety-foundation"
    )
    source_root.mkdir(parents=True)
    os.chmod(destination_root, 0o700)
    os.chmod(destination_root / ".worktrees", 0o700)
    os.chmod(source_root, 0o700)
    source_database = source_root / "trading_assistant.db"
    destination_database = destination_root / "trading_assistant.db"
    _create_current_database(source_database, "source-row")
    _create_current_database(destination_database, "destination-row")
    return RuntimeFixture(
        source_root=source_root,
        destination_root=destination_root,
        source_database=source_database,
        destination_database=destination_database,
        roots=ConsolidationRoots(
            source_root=source_root,
            destination_root=destination_root,
        ),
    )


def _read_rows(path: Path) -> tuple[tuple[int, str], ...]:
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        uri=True,
    )
    try:
        return tuple(
            connection.execute(
                "SELECT id, value FROM consolidation_probe ORDER BY id"
            )
        )
    finally:
        connection.close()


def _snapshot(path: Path) -> DatabaseSnapshot:
    status = path.stat(follow_symlinks=False)
    return DatabaseSnapshot(
        device=status.st_dev,
        inode=status.st_ino,
        digest=hashlib.sha256(path.read_bytes()).hexdigest(),
        rows=_read_rows(path),
    )


def _assert_unchanged(path: Path, before: DatabaseSnapshot) -> None:
    status = path.stat(follow_symlinks=False)
    assert (status.st_dev, status.st_ino) == (
        before.device,
        before.inode,
    )
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before.digest
    assert _read_rows(path) == before.rows


def _consolidate(
    fixture: RuntimeFixture,
    *,
    source_root: Path | None = None,
    destination_root: Path | None = None,
    roots: ConsolidationRoots | None = None,
):
    return consolidate_runtime(
        source_root or fixture.source_root,
        destination_root or fixture.destination_root,
        backup_key=BACKUP_KEY,
        backup_key_id=BACKUP_KEY_ID,
        process_identity=CONSOLIDATION_IDENTITY,
        process_inspector=OfflineProcessInspector(),
        roots=roots or fixture.roots,
    )


def _allow_cooperative_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        consolidation_module,
        "prove_app_absent",
        lambda _root, *, port: port == 8020,
    )


def _remove_database(path: Path) -> None:
    for candidate in (
        path.with_name(f"{path.name}-shm"),
        path.with_name(f"{path.name}-wal"),
        path,
    ):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


@pytest.mark.parametrize("root_kind", ["source", "destination"])
def test_rejects_root_symlink_without_changing_destination(
    runtime_fixture: RuntimeFixture,
    tmp_path: Path,
    root_kind: str,
):
    before = _snapshot(runtime_fixture.destination_database)
    target = (
        runtime_fixture.source_root
        if root_kind == "source"
        else runtime_fixture.destination_root
    )
    alias = tmp_path / f"{root_kind}-root-link"
    alias.symlink_to(target, target_is_directory=True)
    roots = replace(
        runtime_fixture.roots,
        **{f"{root_kind}_root": alias},
    )

    with pytest.raises(ConsolidationError) as exc:
        _consolidate(
            runtime_fixture,
            source_root=(
                alias
                if root_kind == "source"
                else runtime_fixture.source_root
            ),
            destination_root=(
                alias
                if root_kind == "destination"
                else runtime_fixture.destination_root
            ),
            roots=roots,
        )

    assert exc.value.stable_code == "root_invalid"
    _assert_unchanged(runtime_fixture.destination_database, before)


@pytest.mark.parametrize(
    "mutation",
    [
        "symlink",
        "hardlink",
        "fifo",
        "directory",
        "missing",
    ],
)
def test_rejects_invalid_source_database_entry_without_changing_destination(
    runtime_fixture: RuntimeFixture,
    mutation: str,
):
    before = _snapshot(runtime_fixture.destination_database)
    source = runtime_fixture.source_database
    if mutation == "symlink":
        substitute = source.with_name("source-substitute.db")
        _create_current_database(substitute, "substitute-row")
        source.unlink()
        source.symlink_to(substitute)
    elif mutation == "hardlink":
        os.link(source, source.with_name("source-hardlink.db"))
    elif mutation == "fifo":
        source.unlink()
        os.mkfifo(source, 0o600)
    elif mutation == "directory":
        source.unlink()
        source.mkdir(mode=0o700)
    elif mutation == "missing":
        source.unlink()
    else:  # pragma: no cover - parametrization is intentionally exhaustive.
        raise AssertionError(mutation)

    with pytest.raises(ConsolidationError) as exc:
        _consolidate(runtime_fixture)

    assert exc.value.stable_code in {
        "database_path_invalid",
        "source_database_missing",
    }
    _assert_unchanged(runtime_fixture.destination_database, before)


def test_rejects_same_database_inode_without_changing_destination(
    runtime_fixture: RuntimeFixture,
):
    runtime_fixture.destination_database.unlink()
    os.link(
        runtime_fixture.source_database,
        runtime_fixture.destination_database,
    )
    before = _snapshot(runtime_fixture.destination_database)

    with pytest.raises(ConsolidationError) as exc:
        _consolidate(runtime_fixture)

    assert exc.value.stable_code in {
        "database_alias",
        "database_path_invalid",
    }
    _assert_unchanged(runtime_fixture.destination_database, before)


def test_rejects_lexical_root_alias_without_changing_destination(
    runtime_fixture: RuntimeFixture,
):
    before = _snapshot(runtime_fixture.destination_database)
    alias_child = runtime_fixture.source_root / "alias-child"
    alias_child.mkdir(mode=0o700)
    aliased_source = alias_child / ".."

    with pytest.raises(ConsolidationError) as exc:
        _consolidate(
            runtime_fixture,
            source_root=aliased_source,
        )

    assert exc.value.stable_code == "root_alias"
    _assert_unchanged(runtime_fixture.destination_database, before)


@pytest.mark.parametrize("root_kind", ["source", "destination"])
def test_rejects_group_or_world_accessible_root_without_changing_destination(
    runtime_fixture: RuntimeFixture,
    root_kind: str,
):
    before = _snapshot(runtime_fixture.destination_database)
    root = (
        runtime_fixture.source_root
        if root_kind == "source"
        else runtime_fixture.destination_root
    )
    os.chmod(root, 0o770)

    with pytest.raises(ConsolidationError) as exc:
        _consolidate(runtime_fixture)

    assert exc.value.stable_code == "root_permissions_invalid"
    _assert_unchanged(runtime_fixture.destination_database, before)


@pytest.mark.parametrize("database_kind", ["source", "destination"])
def test_rejects_group_or_world_accessible_database_without_mutating_rows(
    runtime_fixture: RuntimeFixture,
    database_kind: str,
):
    before = _snapshot(runtime_fixture.destination_database)
    database = (
        runtime_fixture.source_database
        if database_kind == "source"
        else runtime_fixture.destination_database
    )
    os.chmod(database, 0o660)

    with pytest.raises(ConsolidationError) as exc:
        _consolidate(runtime_fixture)

    assert exc.value.stable_code == "database_permissions_invalid"
    _assert_unchanged(runtime_fixture.destination_database, before)


def test_rejects_root_outside_injected_exact_pair_without_changing_destination(
    runtime_fixture: RuntimeFixture,
    tmp_path: Path,
):
    before = _snapshot(runtime_fixture.destination_database)
    unapproved = tmp_path / "unapproved-source"
    unapproved.mkdir(mode=0o700)

    with pytest.raises(ConsolidationError) as exc:
        _consolidate(
            runtime_fixture,
            source_root=unapproved,
        )

    assert exc.value.stable_code == "root_mismatch"
    _assert_unchanged(runtime_fixture.destination_database, before)


def test_rejects_non_sqlite_bytes_without_changing_destination(
    runtime_fixture: RuntimeFixture,
):
    before = _snapshot(runtime_fixture.destination_database)
    _remove_database(runtime_fixture.source_database)
    runtime_fixture.source_database.write_bytes(b"not a sqlite database")
    os.chmod(runtime_fixture.source_database, 0o600)

    with pytest.raises(ConsolidationError) as exc:
        _consolidate(runtime_fixture)

    assert exc.value.stable_code == "database_format_invalid"
    _assert_unchanged(runtime_fixture.destination_database, before)


def test_rejects_in_memory_database_name_without_changing_destination(
    runtime_fixture: RuntimeFixture,
):
    before = _snapshot(runtime_fixture.destination_database)
    roots = replace(runtime_fixture.roots, database_name=":memory:")

    with pytest.raises(ConsolidationError) as exc:
        _consolidate(runtime_fixture, roots=roots)

    assert exc.value.stable_code == "database_url_invalid"
    _assert_unchanged(runtime_fixture.destination_database, before)


def test_rejects_stale_schema_without_changing_destination(
    runtime_fixture: RuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    _allow_cooperative_absence(monkeypatch)
    before = _snapshot(runtime_fixture.destination_database)
    connection = sqlite3.connect(runtime_fixture.source_database)
    try:
        connection.execute(
            "UPDATE alembic_version SET version_num='20260729_0017'"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ConsolidationError) as exc:
        _consolidate(runtime_fixture)

    assert exc.value.stable_code == "database_schema_not_current"
    _assert_unchanged(runtime_fixture.destination_database, before)


def test_rejects_bad_quick_check_without_changing_destination(
    runtime_fixture: RuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    _allow_cooperative_absence(monkeypatch)
    before = _snapshot(runtime_fixture.destination_database)
    original = consolidation_module._quick_check_rows
    calls = 0

    def corrupt_first(connection):
        nonlocal calls
        calls += 1
        if calls == 1:
            return (("corrupt",),)
        return original(connection)

    monkeypatch.setattr(
        consolidation_module,
        "_quick_check_rows",
        corrupt_first,
    )

    with pytest.raises(ConsolidationError) as exc:
        _consolidate(runtime_fixture)

    assert exc.value.stable_code == "database_quick_check_failed"
    _assert_unchanged(runtime_fixture.destination_database, before)


def test_rejects_foreign_key_damage_without_changing_destination(
    runtime_fixture: RuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    _allow_cooperative_absence(monkeypatch)
    for path in (
        runtime_fixture.source_database,
        runtime_fixture.destination_database,
    ):
        connection = sqlite3.connect(path)
        try:
            connection.executescript(
                "PRAGMA foreign_keys=OFF;"
                "CREATE TABLE consolidation_parent (id INTEGER PRIMARY KEY);"
                "CREATE TABLE consolidation_child ("
                "id INTEGER PRIMARY KEY,"
                "parent_id INTEGER NOT NULL REFERENCES "
                "consolidation_parent(id));"
            )
            if path == runtime_fixture.source_database:
                connection.execute(
                    "INSERT INTO consolidation_child (id, parent_id) "
                    "VALUES (1, 999)"
                )
            connection.commit()
        finally:
            connection.close()
    before = _snapshot(runtime_fixture.destination_database)

    with pytest.raises(ConsolidationError) as exc:
        _consolidate(runtime_fixture)

    assert exc.value.stable_code == "database_foreign_key_check_failed"
    _assert_unchanged(runtime_fixture.destination_database, before)


@pytest.mark.parametrize("mismatch", ["application_id", "schema"])
def test_rejects_application_or_schema_identity_mismatch(
    runtime_fixture: RuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
):
    _allow_cooperative_absence(monkeypatch)
    connection = sqlite3.connect(runtime_fixture.source_database)
    try:
        if mismatch == "application_id":
            connection.execute("PRAGMA application_id=7317")
        else:
            connection.execute(
                "CREATE TABLE source_only_schema (id INTEGER PRIMARY KEY)"
            )
        connection.commit()
    finally:
        connection.close()
    before = _snapshot(runtime_fixture.destination_database)

    with pytest.raises(ConsolidationError) as exc:
        _consolidate(runtime_fixture)

    assert exc.value.stable_code == "database_identity_mismatch"
    _assert_unchanged(runtime_fixture.destination_database, before)


@pytest.mark.parametrize("database_kind", ["source", "destination"])
def test_rejects_active_writer_sidecars_without_changing_destination(
    runtime_fixture: RuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    database_kind: str,
):
    _allow_cooperative_absence(monkeypatch)
    before = _snapshot(runtime_fixture.destination_database)
    active = (
        runtime_fixture.source_database
        if database_kind == "source"
        else runtime_fixture.destination_database
    )
    for suffix in ("-wal", "-shm"):
        sidecar = active.with_name(f"{active.name}{suffix}")
        if not sidecar.exists():
            sidecar.touch(mode=0o600)
        os.chmod(sidecar, 0o600)
    monkeypatch.setattr(
        consolidation_module,
        "_database_writer_active",
        lambda path: path == active,
    )

    with pytest.raises(ConsolidationError) as exc:
        _consolidate(runtime_fixture)

    assert exc.value.stable_code == "database_writer_active"
    _assert_unchanged(runtime_fixture.destination_database, before)


@pytest.mark.parametrize(
    ("location", "tenure_kind", "role"),
    [
        ("source", "app", "app"),
        ("source", "daemon", "daemon"),
        ("source", "mcp", "mcp"),
        ("source", "validation", "validation"),
        ("source", "maintenance", "maintenance"),
        ("destination", "migration", "maintenance"),
        ("destination", "backup", "maintenance"),
    ],
)
def test_rejects_current_runtime_or_maintenance_tenure(
    runtime_fixture: RuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
    tenure_kind: str,
    role: str,
):
    del tenure_kind
    _allow_cooperative_absence(monkeypatch)
    before = _snapshot(runtime_fixture.destination_database)
    path = (
        runtime_fixture.source_database
        if location == "source"
        else runtime_fixture.destination_database
    )
    engine = create_db_engine(f"sqlite:///{path}")
    service = RuntimeTenureService(
        make_session_factory(engine),
        process_inspector=OfflineProcessInspector(),
    )
    owner = ProcessIdentity(
        pid=8100 if location == "source" else 8200,
        start_identity=f"pytest-{location}-{role}",
    )
    if role == "maintenance":
        handle = service.acquire_maintenance(owner, ttl_seconds=300)
    else:
        handle = service.acquire_runtime(
            role,
            owner,
            ttl_seconds=300,
        )
    try:
        with pytest.raises(ConsolidationError) as exc:
            _consolidate(runtime_fixture)
        assert exc.value.stable_code in {
            "runtime_tenure_active",
            "maintenance_tenure_active",
        }
        _assert_unchanged(runtime_fixture.destination_database, before)
    finally:
        assert handle.release() is True
        engine.dispose()


def test_requires_cooperative_app_and_port_absence(
    runtime_fixture: RuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    before = _snapshot(runtime_fixture.destination_database)
    observed: list[tuple[Path, int]] = []

    def unproven(root: Path, *, port: int) -> bool:
        observed.append((root, port))
        return root != runtime_fixture.source_root

    monkeypatch.setattr(
        consolidation_module,
        "prove_app_absent",
        unproven,
    )

    with pytest.raises(ConsolidationError) as exc:
        _consolidate(runtime_fixture)

    assert exc.value.stable_code == "cooperative_absence_unproven"
    assert observed == [(runtime_fixture.source_root, 8020)]
    _assert_unchanged(runtime_fixture.destination_database, before)


def _install_fake_backups(
    monkeypatch: pytest.MonkeyPatch,
) -> list[Path]:
    artifacts: list[Path] = []

    def fake_backup(
        source,
        destination_dir,
        retention_days=14,
        *,
        backup_key,
        backup_key_id,
        process_identity,
        process_inspector,
        **_kwargs,
    ):
        del (
            retention_days,
            backup_key,
            process_identity,
            process_inspector,
        )
        destination = Path(destination_dir)
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        artifact = destination / (
            f"20260730T12000{len(artifacts)}Z-"
            "whole-database-v1.sqlite3.aesgcm"
        )
        artifact.write_bytes(b"synthetic-encrypted-backup")
        os.chmod(artifact, 0o600)
        artifacts.append(artifact)
        return EncryptedBackupReceipt(
            path=artifact,
            path_hash=f"{len(artifacts):064x}",
            source_sha256=hashlib.sha256(
                Path(source).read_bytes()
            ).hexdigest(),
            created_at="2026-07-30T12:00:00Z",
            schema_head="20260730_0018",
            backup_key_id=backup_key_id,
            verified=True,
        )

    monkeypatch.setattr(
        consolidation_module,
        "backup_database",
        fake_backup,
    )
    return artifacts


def test_verified_backups_precede_install_and_source_is_never_changed(
    runtime_fixture: RuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    _allow_cooperative_absence(monkeypatch)
    source_bytes = runtime_fixture.source_database.read_bytes()
    destination_inode = runtime_fixture.destination_database.stat().st_ino
    events: list[str] = []
    backup_states: list[tuple[Path, str]] = []

    def recording_backup(source, destination_dir, *args, **kwargs):
        receipt = real_backup_database(
            source,
            destination_dir,
            *args,
            **kwargs,
        )
        connection = sqlite3.connect(
            f"{Path(source).as_uri()}?mode=ro",
            uri=True,
        )
        try:
            state = connection.execute(
                "SELECT state FROM runtime_tenures "
                "WHERE resource_key='sensitive-migration:global'"
            ).fetchone()
        finally:
            connection.close()
        backup_states.append((Path(source), state[0]))
        return receipt

    monkeypatch.setattr(
        consolidation_module,
        "backup_database",
        recording_backup,
    )
    monkeypatch.setattr(
        consolidation_module,
        "_stage_event",
        events.append,
    )

    receipt = _consolidate(runtime_fixture)

    assert events[:2] == [
        "source_backup_verified",
        "destination_backup_verified",
    ]
    assert events.index("destination_backup_verified") < events.index(
        "install"
    )
    assert backup_states == [
        (runtime_fixture.source_database, "released"),
        (runtime_fixture.destination_database, "released"),
    ]
    assert runtime_fixture.source_database.exists()
    assert runtime_fixture.source_database.read_bytes() == source_bytes
    assert runtime_fixture.destination_database.stat().st_ino != (
        destination_inode
    )
    assert _read_rows(runtime_fixture.destination_database) == (
        (1, "source-row"),
    )
    assert receipt.installed is True
    assert receipt.status == "verified"
    assert receipt.destination_backup_hash is not None
    assert len(receipt.source_hash) == 64
    assert len(receipt.destination_hash) == 64
    assert len(receipt.source_backup_hash) == 64
    assert len(receipt.destination_backup_hash) == 64
    assert len(receipt.summary_digest) == 64
    assert list_committed_backups(
        runtime_fixture.source_root / ".local" / "encrypted-backups",
        artifact_label="whole-database-v1",
    )
    assert list_committed_backups(
        runtime_fixture.destination_root
        / ".local"
        / "encrypted-backups",
        artifact_label="whole-database-v1",
    )
    marker = (
        runtime_fixture.destination_root
        / ".local"
        / "runtime-consolidation.migration_uncertain"
    )
    assert not marker.exists()
    assert not tuple(
        (
            runtime_fixture.destination_root / ".local"
        ).glob("runtime-consolidation-stage-*")
    )


def test_sqlite_copy_connections_are_bound_to_validated_descriptors(
    runtime_fixture: RuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    _allow_cooperative_absence(monkeypatch)
    _install_fake_backups(monkeypatch)
    stage = "preflight"
    copy_targets: list[str] = []
    real_connect = sqlite3.connect

    def record_stage(value: str) -> None:
        nonlocal stage
        stage = value

    def recording_connect(database, *args, **kwargs):
        if stage == "sqlite_copy":
            copy_targets.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(
        consolidation_module,
        "_stage_event",
        record_stage,
    )
    monkeypatch.setattr(
        consolidation_module.sqlite3,
        "connect",
        recording_connect,
    )

    receipt = _consolidate(runtime_fixture)

    assert receipt.status == "verified"
    assert len(copy_targets) == 2
    assert copy_targets[0].startswith("file:")
    assert "runtime-consolidation-stage-" in copy_targets[0]
    assert copy_targets[0].endswith(
        "/descriptor-source.sqlite3?mode=ro&nofollow=1"
    )
    assert str(runtime_fixture.source_database) not in copy_targets[0]
    assert copy_targets[1].startswith(
        ("file:/dev/fd/", "file:/proc/self/fd/")
    )


def test_logical_summary_contract_is_immutable_and_deterministic():
    first = LogicalSummary(
        schema_head="20260730_0018",
        table_counts=(("alpha", 1), ("beta", 2)),
        digest="a" * 64,
    )
    second = LogicalSummary(
        schema_head="20260730_0018",
        table_counts=(("alpha", 1), ("beta", 2)),
        digest="a" * 64,
    )

    assert first == second
    with pytest.raises((AttributeError, TypeError)):
        first.digest = "b" * 64


@pytest.mark.parametrize("failed_backup", ["source", "destination"])
def test_backup_failure_preserves_destination_and_existing_artifacts(
    runtime_fixture: RuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    failed_backup: str,
):
    _allow_cooperative_absence(monkeypatch)
    before = _snapshot(runtime_fixture.destination_database)
    source_bytes = runtime_fixture.source_database.read_bytes()
    artifacts: list[Path] = []
    calls = 0

    def failing_backup(source, destination_dir, **kwargs):
        nonlocal calls
        calls += 1
        if (failed_backup == "source" and calls == 1) or (
            failed_backup == "destination" and calls == 2
        ):
            raise RuntimeError("injected backup failure")
        destination = Path(destination_dir)
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        artifact = destination / (
            "20260730T120000Z-whole-database-v1.sqlite3.aesgcm"
        )
        artifact.write_bytes(b"existing-encrypted-artifact")
        os.chmod(artifact, 0o600)
        artifacts.append(artifact)
        return EncryptedBackupReceipt(
            path=artifact,
            path_hash="a" * 64,
            source_sha256=hashlib.sha256(
                Path(source).read_bytes()
            ).hexdigest(),
            created_at="2026-07-30T12:00:00Z",
            schema_head="20260730_0018",
            backup_key_id=kwargs["backup_key_id"],
            verified=True,
        )

    monkeypatch.setattr(
        consolidation_module,
        "backup_database",
        failing_backup,
    )

    with pytest.raises(ConsolidationError) as exc:
        _consolidate(runtime_fixture)

    assert exc.value.stable_code == f"{failed_backup}_backup_failed"
    _assert_unchanged(runtime_fixture.destination_database, before)
    assert runtime_fixture.source_database.read_bytes() == source_bytes
    assert all(artifact.exists() for artifact in artifacts)


@pytest.mark.parametrize(
    "failed_stage",
    [
        "sqlite_copy",
        "source_check",
        "staging_check",
        "summary_compare",
        "file_fsync",
        "directory_fsync_before_install",
        "install",
    ],
)
def test_preinstall_interruption_preserves_destination_byte_for_byte(
    runtime_fixture: RuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    failed_stage: str,
):
    _allow_cooperative_absence(monkeypatch)
    artifacts = _install_fake_backups(monkeypatch)
    before = _snapshot(runtime_fixture.destination_database)
    source_bytes = runtime_fixture.source_database.read_bytes()
    events: list[str] = []

    def interrupt(stage: str) -> None:
        events.append(stage)
        if stage == failed_stage:
            raise OSError(f"injected {stage}")

    monkeypatch.setattr(
        consolidation_module,
        "_stage_event",
        interrupt,
    )

    with pytest.raises(ConsolidationError) as exc:
        _consolidate(runtime_fixture)

    assert exc.value.stable_code != "migration_uncertain"
    assert events[:2] == [
        "source_backup_verified",
        "destination_backup_verified",
    ]
    _assert_unchanged(runtime_fixture.destination_database, before)
    assert runtime_fixture.source_database.read_bytes() == source_bytes
    assert all(artifact.exists() for artifact in artifacts)
    marker = (
        runtime_fixture.destination_root
        / ".local"
        / "runtime-consolidation.migration_uncertain"
    )
    assert not marker.exists()


@pytest.mark.parametrize(
    "failed_stage",
    ["sidecar_cleanup", "directory_fsync_after_install"],
)
def test_postinstall_interruption_marks_uncertain_and_blocks_startup(
    runtime_fixture: RuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    failed_stage: str,
):
    _allow_cooperative_absence(monkeypatch)
    _install_fake_backups(monkeypatch)
    source_bytes = runtime_fixture.source_database.read_bytes()
    events: list[str] = []

    def interrupt(stage: str) -> None:
        events.append(stage)
        if stage == failed_stage:
            raise OSError(f"injected {stage}")

    monkeypatch.setattr(
        consolidation_module,
        "_stage_event",
        interrupt,
    )

    with pytest.raises(ConsolidationError) as exc:
        _consolidate(runtime_fixture)

    assert exc.value.stable_code == "migration_uncertain"
    assert events[:2] == [
        "source_backup_verified",
        "destination_backup_verified",
    ]
    assert runtime_fixture.source_database.exists()
    assert runtime_fixture.source_database.read_bytes() == source_bytes
    assert _read_rows(runtime_fixture.destination_database) == (
        (1, "source-row"),
    )
    marker = (
        runtime_fixture.destination_root
        / ".local"
        / "runtime-consolidation.migration_uncertain"
    )
    marker_status = marker.stat(follow_symlinks=False)
    assert stat.S_ISREG(marker_status.st_mode)
    assert stat.S_IMODE(marker_status.st_mode) == 0o600
    assert marker_status.st_nlink == 1

    engine = create_db_engine(
        f"sqlite:///{runtime_fixture.destination_database}"
    )
    service = RuntimeTenureService(
        make_session_factory(engine),
        process_inspector=OfflineProcessInspector(),
    )
    try:
        with pytest.raises(TenureUnavailable) as blocked:
            service.acquire_runtime(
                "app",
                ProcessIdentity(
                    pid=9901,
                    start_identity="pytest-startup-after-uncertain",
                ),
                ttl_seconds=30,
            )
        assert blocked.value.stable_code in {
            "maintenance_tenure_active",
            "maintenance_process_live",
            "maintenance_process_unknown",
            "maintenance_recovery_required",
        }
    finally:
        engine.dispose()


def test_missing_destination_is_installed_without_plaintext_archive(
    runtime_fixture: RuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    _allow_cooperative_absence(monkeypatch)
    _remove_database(runtime_fixture.destination_database)
    events: list[str] = []
    monkeypatch.setattr(
        consolidation_module,
        "_stage_event",
        events.append,
    )

    receipt = _consolidate(runtime_fixture)

    assert receipt.installed is True
    assert receipt.destination_backup_hash is None
    assert events[:2] == [
        "source_backup_verified",
        "destination_backup_verified",
    ]
    assert _read_rows(runtime_fixture.destination_database) == (
        (1, "source-row"),
    )
    assert not tuple(
        runtime_fixture.destination_root.glob(
            ".trading_assistant.db.runtime-consolidation-old-*"
        )
    )
