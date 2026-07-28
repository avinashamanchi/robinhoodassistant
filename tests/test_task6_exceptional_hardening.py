"""Exceptional post-round-5 encrypted-backup protocol regressions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import multiprocessing
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import time

import pytest

from trading_assistant.db.migrate import upgrade
from trading_assistant.db.session import create_db_engine
from trading_assistant.ops import backup as backup_module
from trading_assistant.ops.backup import (
    EncryptedBackupError,
    create_encrypted_database_backup,
    list_committed_backups,
    read_encrypted_backup_header,
)
from trading_assistant.ops.tenure import ProcessIdentity, ProcessProof


BACKUP_KEY = b"E" * 32
BACKUP_KEY_ID = "exceptional-backup-key"
SCHEMA_HEAD = "20260727_0015"
BACKUP_NOW = datetime(
    2026,
    7,
    28,
    20,
    15,
    12,
    345678,
    tzinfo=timezone.utc,
)
BACKUP_IDENTITY = ProcessIdentity(
    87655,
    "exceptional-backup-process-start",
)


class _OfflineInspector:
    def inspect(self, _identity):
        return ProcessProof.NOT_SAME


def _seed_source(path: Path, *, payload_bytes: int = 65_536) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE sample (id INTEGER PRIMARY KEY, payload BLOB NOT NULL)"
        )
        connection.execute(
            "INSERT INTO sample(payload) VALUES (zeroblob(?))",
            (payload_bytes,),
        )
        connection.commit()


def _operational_source(path: Path) -> Path:
    engine = create_db_engine(f"sqlite:///{path}")
    try:
        assert upgrade(engine) is None
    finally:
        engine.dispose()
    return path


def _create(
    source: Path,
    destination: Path,
    *,
    now: datetime = BACKUP_NOW,
    **kwargs,
):
    return create_encrypted_database_backup(
        source,
        destination,
        backup_key=BACKUP_KEY,
        backup_key_id=BACKUP_KEY_ID,
        schema_head=SCHEMA_HEAD,
        now=lambda: now,
        **kwargs,
    )


def _state_path(artifact: Path) -> Path:
    return artifact.with_name(f".{artifact.name}.commit-state")


def _anchor_path(artifact: Path) -> Path:
    return artifact.with_name(f".{artifact.name}.pending")


def _artifact_from_state_path_for_test(state: Path) -> Path:
    return state.with_name(state.name[1 : -len(".commit-state")])


def _process_context():
    method = (
        "fork"
        if "fork" in multiprocessing.get_all_start_methods()
        else "spawn"
    )
    return multiprocessing.get_context(method)


def _hold_exclusive_state_lock(
    state_path: Path,
    ready,
    release,
) -> None:
    descriptor = os.open(state_path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        ready.set()
        release.wait(10)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _run_consumer_in_child(
    action: str,
    destination: Path,
    artifact: Path,
    result_queue,
) -> None:
    try:
        if action == "list":
            result = list_committed_backups(destination)
        elif action == "header":
            result = read_encrypted_backup_header(artifact)
        elif action == "prune":
            result = backup_module._prune_committed_backups(
                destination,
                artifact_label="whole-database-v1",
                cutoff=time.time(),
            )
        else:
            raise AssertionError(action)
    except EncryptedBackupError as exc:
        result_queue.put(("error", exc.stable_code))
    except BaseException as exc:
        result_queue.put(("unexpected", type(exc).__name__))
    else:
        result_queue.put(("ok", bool(result)))


def _crash_backup_at_stage(
    source: Path,
    destination: Path,
    stage: str,
) -> None:
    def crash(selected_stage: str) -> None:
        if selected_stage == stage:
            os._exit(73)

    _create(source, destination, stage_hook=crash)
    os._exit(74)


def _age_protocol_files(destination: Path, timestamp: float) -> None:
    for candidate in destination.iterdir():
        if candidate.name != "operator-owned":
            os.utime(
                candidate,
                (timestamp, timestamp),
                follow_symlinks=False,
            )


def _flip_same_size_byte(path: Path, *, offset: int | None = None) -> None:
    size = path.stat().st_size
    selected = size // 2 if offset is None else offset
    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        original = os.pread(descriptor, 1, selected)
        assert len(original) == 1
        assert os.pwrite(
            descriptor,
            bytes([original[0] ^ 0x01]),
            selected,
        ) == 1
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def test_same_size_ciphertext_corruption_fails_listing_and_header(
    tmp_path,
):
    source = tmp_path / "same-size-corruption.db"
    destination = tmp_path / "same-size-corruption-backups"
    _seed_source(source)
    receipt = _create(source, destination)

    _flip_same_size_byte(receipt.path)

    assert list_committed_backups(destination) == ()
    with pytest.raises(
        EncryptedBackupError,
        match="^encrypted_backup_not_committed$",
    ):
        read_encrypted_backup_header(receipt.path)


def test_corrupt_committed_artifact_is_not_pruned_as_valid(
    tmp_path,
):
    source = tmp_path / "corrupt-retention.db"
    destination = tmp_path / "corrupt-retention-backups"
    _seed_source(source)
    receipt = _create(
        source,
        destination,
        now=BACKUP_NOW - timedelta(days=30),
        artifact_label="whole-database-v1",
    )
    _flip_same_size_byte(receipt.path)
    old_time = (BACKUP_NOW - timedelta(days=30)).timestamp()
    os.utime(receipt.path, (old_time, old_time))

    backup_module._prune_committed_backups(
        destination,
        artifact_label="whole-database-v1",
        cutoff=BACKUP_NOW.timestamp(),
    )

    assert receipt.path.exists()
    assert _anchor_path(receipt.path).exists()
    assert _state_path(receipt.path).exists()
    assert list_committed_backups(destination) == ()


@pytest.mark.parametrize("damage", ["truncate", "extend"])
def test_changed_ciphertext_length_fails_closed(tmp_path, damage):
    source = tmp_path / f"{damage}.db"
    destination = tmp_path / f"{damage}-backups"
    _seed_source(source)
    receipt = _create(source, destination)
    original_size = receipt.path.stat().st_size

    if damage == "truncate":
        with receipt.path.open("r+b", buffering=0) as handle:
            handle.truncate(original_size - 1)
            os.fsync(handle.fileno())
    else:
        with receipt.path.open("ab", buffering=0) as handle:
            handle.write(b"x")
            os.fsync(handle.fileno())

    assert list_committed_backups(destination) == ()
    with pytest.raises(
        EncryptedBackupError,
        match="^encrypted_backup_not_committed$",
    ):
        read_encrypted_backup_header(receipt.path)


@pytest.mark.parametrize("damage", ["inode_swap", "symlink_swap"])
def test_artifact_path_replacement_fails_closed(tmp_path, damage):
    source = tmp_path / f"{damage}.db"
    destination = tmp_path / f"{damage}-backups"
    _seed_source(source)
    receipt = _create(source, destination)
    replacement = destination / "replacement"
    replacement.write_bytes(receipt.path.read_bytes())
    receipt.path.unlink()
    if damage == "inode_swap":
        replacement.rename(receipt.path)
    else:
        receipt.path.symlink_to(replacement)

    assert list_committed_backups(destination) == ()
    with pytest.raises(
        EncryptedBackupError,
        match="^encrypted_backup_not_committed$",
    ):
        read_encrypted_backup_header(receipt.path)


def test_same_inode_hardlink_mutation_fails_closed(tmp_path):
    source = tmp_path / "hardlink-mutation.db"
    destination = tmp_path / "hardlink-mutation-backups"
    _seed_source(source)
    receipt = _create(source, destination)
    alias = destination / "artifact-alias"
    os.link(receipt.path, alias, follow_symlinks=False)

    _flip_same_size_byte(alias)

    assert receipt.path.stat().st_ino == alias.stat().st_ino
    assert list_committed_backups(destination) == ()


def test_header_detects_mutation_between_identity_validation_and_use(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "header-mutation.db"
    destination = tmp_path / "header-mutation-backups"
    _seed_source(source)
    receipt = _create(source, destination)
    original_parse = backup_module._parse_header_stream
    mutated = False

    def mutate_then_parse(handle):
        nonlocal mutated
        _flip_same_size_byte(receipt.path, offset=receipt.path.stat().st_size - 1)
        mutated = True
        return original_parse(handle)

    monkeypatch.setattr(
        backup_module,
        "_parse_header_stream",
        mutate_then_parse,
    )

    with pytest.raises(
        EncryptedBackupError,
        match="^encrypted_backup_not_committed$",
    ):
        read_encrypted_backup_header(receipt.path)

    assert mutated is True
    assert list_committed_backups(destination) == ()


def test_header_detects_path_swap_between_validation_and_use(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "header-path-swap.db"
    destination = tmp_path / "header-path-swap-backups"
    _seed_source(source)
    receipt = _create(source, destination)
    original_parse = backup_module._parse_header_stream
    original_bytes = receipt.path.read_bytes()
    swapped = False

    def swap_then_parse(handle):
        nonlocal swapped
        receipt.path.unlink()
        receipt.path.write_bytes(original_bytes)
        swapped = True
        return original_parse(handle)

    monkeypatch.setattr(
        backup_module,
        "_parse_header_stream",
        swap_then_parse,
    )

    with pytest.raises(
        EncryptedBackupError,
        match="^encrypted_backup_not_committed$",
    ):
        read_encrypted_backup_header(receipt.path)

    assert swapped is True
    assert list_committed_backups(destination) == ()


def test_header_detects_state_path_swap_between_validation_and_use(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "header-state-path-swap.db"
    destination = tmp_path / "header-state-path-swap-backups"
    _seed_source(source)
    receipt = _create(source, destination)
    state = _state_path(receipt.path)
    original_state = state.read_bytes()
    original_parse = backup_module._parse_header_stream
    swapped = False

    def swap_state_then_parse(handle):
        nonlocal swapped
        state.unlink()
        state.write_bytes(original_state)
        swapped = True
        return original_parse(handle)

    monkeypatch.setattr(
        backup_module,
        "_parse_header_stream",
        swap_state_then_parse,
    )

    with pytest.raises(
        EncryptedBackupError,
        match="^encrypted_backup_not_committed$",
    ):
        read_encrypted_backup_header(receipt.path)

    assert swapped is True
    assert list_committed_backups(destination) == ()


@pytest.mark.parametrize(
    ("failure_stage", "failure_type"),
    [
        ("pwrite", OSError),
        ("pwrite", asyncio.CancelledError),
        ("fsync", OSError),
        ("fsync", asyncio.CancelledError),
    ],
)
def test_durable_commit_reconciles_on_same_lock_despite_later_errors(
    tmp_path,
    monkeypatch,
    failure_stage,
    failure_type,
):
    source = tmp_path / f"commit-{failure_stage}-{failure_type.__name__}.db"
    destination = (
        tmp_path
        / f"commit-{failure_stage}-{failure_type.__name__}-backups"
    )
    _seed_source(source)
    original_pwrite = os.pwrite
    original_fsync = os.fsync
    original_flock = fcntl.flock
    original_unlink = Path.unlink
    creation_active = True
    commit_descriptor: int | None = None
    durable_commit_written = False
    injected_primary = False
    injected_unlock = False
    shared_lock_attempts_during_create = 0
    cleanup_attempts_after_commit = 0

    def inject_commit_write(descriptor, data, offset):
        nonlocal commit_descriptor
        nonlocal durable_commit_written
        nonlocal injected_primary
        written = original_pwrite(descriptor, data, offset)
        if b'"phase":"COMMITTED"' in data:
            commit_descriptor = descriptor
            durable_commit_written = True
            if failure_stage == "pwrite" and not injected_primary:
                injected_primary = True
                raise failure_type("injected post-write failure")
        return written

    def inject_commit_fsync(descriptor):
        nonlocal injected_primary
        result = original_fsync(descriptor)
        if (
            descriptor == commit_descriptor
            and failure_stage == "fsync"
            and not injected_primary
        ):
            injected_primary = True
            raise failure_type("injected post-fsync failure")
        return result

    def inject_lock_errors(descriptor, operation):
        nonlocal injected_unlock
        nonlocal shared_lock_attempts_during_create
        if (
            descriptor == commit_descriptor
            and durable_commit_written
            and operation == fcntl.LOCK_UN
            and not injected_unlock
        ):
            original_flock(descriptor, operation)
            injected_unlock = True
            raise OSError("injected post-commit unlock failure")
        if (
            creation_active
            and injected_unlock
            and operation & fcntl.LOCK_SH
        ):
            shared_lock_attempts_during_create += 1
            raise BlockingIOError("injected transient shared-lock failure")
        return original_flock(descriptor, operation)

    def block_cleanup(path: Path, *args, **kwargs):
        nonlocal cleanup_attempts_after_commit
        if (
            creation_active
            and durable_commit_written
            and path.parent == destination
        ):
            cleanup_attempts_after_commit += 1
            raise OSError("injected cleanup refusal")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "pwrite", inject_commit_write)
    monkeypatch.setattr(os, "fsync", inject_commit_fsync)
    monkeypatch.setattr(fcntl, "flock", inject_lock_errors)
    monkeypatch.setattr(Path, "unlink", block_cleanup)

    try:
        receipt = _create(source, destination)
    finally:
        creation_active = False

    assert injected_primary is True
    assert injected_unlock is True
    assert shared_lock_attempts_during_create == 0
    assert cleanup_attempts_after_commit == 0
    assert list_committed_backups(destination) == (receipt.path,)


def test_torn_commit_is_durably_restored_to_pending_before_failure(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "torn-restored-pending.db"
    destination = tmp_path / "torn-restored-pending-backups"
    _seed_source(source)
    original_pwrite = os.pwrite
    original_unlink = Path.unlink
    torn = False

    def tear_commit_write(descriptor, data, offset):
        nonlocal torn
        if b'"phase":"COMMITTED"' in data and not torn:
            torn = True
            original_pwrite(descriptor, data[: len(data) // 2], offset)
            raise OSError("injected torn commit-state write")
        return original_pwrite(descriptor, data, offset)

    def preserve_failed_image(path: Path, *args, **kwargs):
        if torn and path.parent == destination:
            raise OSError("injected cleanup refusal")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "pwrite", tear_commit_write)
    monkeypatch.setattr(Path, "unlink", preserve_failed_image)

    with pytest.raises(
        EncryptedBackupError,
        match="^encrypted_backup_failed$",
    ):
        _create(source, destination)

    target = next(destination.glob("*.sqlite3.aesgcm"))
    assert (
        backup_module._authoritative_state(target, "PENDING")
        is not None
    )
    assert list_committed_backups(destination) == ()


def test_failed_postcommit_verification_uses_durable_retired_fallback(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "retired-fallback.db"
    destination = tmp_path / "retired-fallback-backups"
    _seed_source(source)
    original_pwrite = os.pwrite
    original_verify = backup_module._verify_matching_artifact
    original_unlink = Path.unlink
    commit_written = False
    verification_failed = False
    pending_restore_failed = False

    def fail_pending_restore(descriptor, data, offset):
        nonlocal commit_written
        nonlocal pending_restore_failed
        if b'"phase":"COMMITTED"' in data:
            commit_written = True
        elif (
            commit_written
            and b'"phase":"PENDING"' in data
            and not pending_restore_failed
        ):
            pending_restore_failed = True
            raise OSError("injected pending restore failure")
        return original_pwrite(descriptor, data, offset)

    def fail_postcommit_verification(path, state, descriptor):
        nonlocal verification_failed
        if (
            commit_written
            and state.phase == "COMMITTED"
            and not verification_failed
        ):
            verification_failed = True
            raise OSError("injected postcommit artifact verification failure")
        return original_verify(path, state, descriptor)

    def preserve_failed_image(path: Path, *args, **kwargs):
        if commit_written and path.parent == destination:
            raise OSError("injected cleanup refusal")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "pwrite", fail_pending_restore)
    monkeypatch.setattr(
        backup_module,
        "_verify_matching_artifact",
        fail_postcommit_verification,
    )
    monkeypatch.setattr(Path, "unlink", preserve_failed_image)

    with pytest.raises(EncryptedBackupError):
        _create(source, destination)

    target = next(destination.glob("*.sqlite3.aesgcm"))
    assert verification_failed is True
    assert pending_restore_failed is True
    assert list_committed_backups(destination) == ()
    assert (
        backup_module._authoritative_state(target, "RETIRED")
        is not None
    )


def test_interrupted_state_syscalls_retry_without_losing_commit(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "eintr.db"
    destination = tmp_path / "eintr-backups"
    _seed_source(source)
    original_pwrite = os.pwrite
    original_pread = os.pread
    original_flock = fcntl.flock
    pwrite_interrupted = False
    pread_interrupted = False
    flock_interrupted = False

    def interrupt_pending_write_once(descriptor, data, offset):
        nonlocal pwrite_interrupted
        if b'"phase":"PENDING"' in data and not pwrite_interrupted:
            pwrite_interrupted = True
            raise InterruptedError()
        return original_pwrite(descriptor, data, offset)

    def interrupt_state_read_once(descriptor, length, offset):
        nonlocal pread_interrupted
        if length in {1024, 1025} and not pread_interrupted:
            pread_interrupted = True
            raise InterruptedError()
        return original_pread(descriptor, length, offset)

    def interrupt_lock_once(descriptor, operation):
        nonlocal flock_interrupted
        if (
            operation & fcntl.LOCK_EX
            and not flock_interrupted
        ):
            flock_interrupted = True
            raise InterruptedError()
        return original_flock(descriptor, operation)

    monkeypatch.setattr(os, "pwrite", interrupt_pending_write_once)
    monkeypatch.setattr(os, "pread", interrupt_state_read_once)
    monkeypatch.setattr(fcntl, "flock", interrupt_lock_once)

    receipt = _create(source, destination)

    assert pwrite_interrupted is True
    assert pread_interrupted is True
    assert flock_interrupted is True
    assert list_committed_backups(destination) == (receipt.path,)


def test_short_state_reads_and_writes_complete_exact_record(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "short-state-io.db"
    destination = tmp_path / "short-state-io-backups"
    _seed_source(source)
    original_pwrite = os.pwrite
    original_pread = os.pread
    short_write_seen = False
    short_read_seen = False

    def short_pending_write_once(descriptor, data, offset):
        nonlocal short_write_seen
        if b'"phase":"PENDING"' in data and not short_write_seen:
            short_write_seen = True
            midpoint = len(data) // 2
            return original_pwrite(
                descriptor,
                data[:midpoint],
                offset,
            )
        return original_pwrite(descriptor, data, offset)

    def short_state_read_once(descriptor, length, offset):
        nonlocal short_read_seen
        if length == 1024 and not short_read_seen:
            short_read_seen = True
            return original_pread(descriptor, length // 2, offset)
        return original_pread(descriptor, length, offset)

    monkeypatch.setattr(os, "pwrite", short_pending_write_once)
    monkeypatch.setattr(os, "pread", short_state_read_once)

    receipt = _create(source, destination)

    assert short_write_seen is True
    assert short_read_seen is True
    assert list_committed_backups(destination) == (receipt.path,)


def test_commit_state_binds_complete_ciphertext_digest(tmp_path):
    source = tmp_path / "state-digest.db"
    destination = tmp_path / "state-digest-backups"
    _seed_source(source)
    receipt = _create(source, destination)
    state = backup_module._authoritative_state(
        receipt.path,
        "COMMITTED",
    )

    assert state is not None
    assert state.artifact_sha256 == hashlib.sha256(
        receipt.path.read_bytes()
    ).hexdigest()


@pytest.mark.parametrize("kind", ["fifo", "socket"])
@pytest.mark.parametrize("action", ["list", "header", "prune"])
def test_nonregular_commit_state_fails_bounded_and_unavailable(
    tmp_path,
    kind,
    action,
):
    short_directory: tempfile.TemporaryDirectory[str] | None = None
    if kind == "socket":
        short_directory = tempfile.TemporaryDirectory(
            prefix="t",
            dir="/tmp",
        )
        destination = Path(short_directory.name)
    else:
        destination = tmp_path / f"{kind}-{action}-backups"
        destination.mkdir()
    label = (
        "whole-database-v1"
        if action == "prune"
        else "before-sensitive-v1"
    )
    artifact = (
        destination
        / f"20260728T201512345678Z-{label}.sqlite3.aesgcm"
    )
    artifact.write_bytes(b"not-an-official-artifact")
    os.link(artifact, _anchor_path(artifact), follow_symlinks=False)
    state = _state_path(artifact)
    server: socket.socket | None = None
    if kind == "fifo":
        os.mkfifo(state, 0o600)
    else:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(state))

    context = _process_context()
    results = context.Queue()
    process = context.Process(
        target=_run_consumer_in_child,
        args=(action, destination, artifact, results),
    )
    process.start()
    process.join(timeout=2)
    if process.is_alive():
        process.terminate()
        process.join(timeout=2)
    try:
        assert process.exitcode == 0
        assert results.get(timeout=1) == (
            "error",
            "encrypted_backup_state_unavailable",
        )
    finally:
        if server is not None:
            server.close()
        if short_directory is not None:
            short_directory.cleanup()


@pytest.mark.parametrize("action", ["list", "header", "prune"])
def test_held_commit_state_lock_fails_bounded_and_busy(
    tmp_path,
    action,
):
    source = tmp_path / f"busy-{action}.db"
    destination = tmp_path / f"busy-{action}-backups"
    _seed_source(source)
    receipt = _create(
        source,
        destination,
        artifact_label=(
            "whole-database-v1"
            if action == "prune"
            else "before-sensitive-v1"
        ),
    )
    context = _process_context()
    ready = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_exclusive_state_lock,
        args=(_state_path(receipt.path), ready, release),
    )
    holder.start()
    assert ready.wait(timeout=5)
    results = context.Queue()
    consumer = context.Process(
        target=_run_consumer_in_child,
        args=(action, destination, receipt.path, results),
    )
    consumer.start()
    consumer.join(timeout=2)
    if consumer.is_alive():
        consumer.terminate()
        consumer.join(timeout=2)
    release.set()
    holder.join(timeout=5)

    assert holder.exitcode == 0
    assert consumer.exitcode == 0
    assert results.get(timeout=1) == (
        "error",
        "encrypted_backup_state_busy",
    )


@pytest.mark.parametrize("action", ["list", "header", "prune"])
def test_nonregular_artifact_target_fails_closed_without_blocking(
    tmp_path,
    action,
):
    source = tmp_path / f"fifo-artifact-{action}.db"
    destination = tmp_path / f"fifo-artifact-{action}-backups"
    _seed_source(source)
    receipt = _create(
        source,
        destination,
        artifact_label=(
            "whole-database-v1"
            if action == "prune"
            else "before-sensitive-v1"
        ),
    )
    receipt.path.unlink()
    os.mkfifo(receipt.path, 0o600)
    context = _process_context()
    results = context.Queue()
    consumer = context.Process(
        target=_run_consumer_in_child,
        args=(action, destination, receipt.path, results),
    )
    consumer.start()
    consumer.join(timeout=2)
    if consumer.is_alive():
        consumer.terminate()
        consumer.join(timeout=2)

    assert consumer.exitcode == 0
    expected = (
        ("error", "encrypted_backup_not_committed")
        if action == "header"
        else ("ok", False)
    )
    assert results.get(timeout=1) == expected


def test_backup_entrypoint_fails_bounded_on_busy_retention_state(tmp_path):
    source = _operational_source(tmp_path / "busy-backup-source.db")
    retained_source = tmp_path / "busy-retained-source.db"
    _seed_source(retained_source)
    destination = tmp_path / "busy-backup-entrypoint"
    retained = _create(
        retained_source,
        destination,
        artifact_label="whole-database-v1",
    )
    context = _process_context()
    ready = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_exclusive_state_lock,
        args=(_state_path(retained.path), ready, release),
    )
    holder.start()
    assert ready.wait(timeout=5)
    started = time.monotonic()
    try:
        with pytest.raises(
            EncryptedBackupError,
            match="^encrypted_backup_state_busy$",
        ):
            backup_module.backup_database(
                source,
                destination,
                retention_days=14,
                backup_key=BACKUP_KEY,
                backup_key_id=BACKUP_KEY_ID,
                process_identity=BACKUP_IDENTITY,
                process_inspector=_OfflineInspector(),
            )
    finally:
        elapsed = time.monotonic() - started
        release.set()
        holder.join(timeout=5)

    assert holder.exitcode == 0
    assert elapsed < 1.5
    assert list_committed_backups(destination) == (retained.path,)


@pytest.mark.parametrize(
    ("stage", "survivor_shape"),
    [
        ("pending_state_durable", "pending_without_target"),
        ("target_directory_fsynced", "pending_with_target"),
        ("pending_state_durable", "anchor_only"),
        ("pending_state_durable", "state_only"),
    ],
)
def test_crash_images_recover_only_after_ttl_and_are_idempotent(
    tmp_path,
    stage,
    survivor_shape,
):
    source = tmp_path / f"crash-{survivor_shape}.db"
    destination = tmp_path / f"crash-{survivor_shape}-backups"
    _seed_source(source)
    context = _process_context()
    process = context.Process(
        target=_crash_backup_at_stage,
        args=(source, destination, stage),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 73
    state = next(destination.glob(".*.commit-state"))
    artifact_name = state.name[1 : -len(".commit-state")]
    artifact = destination / artifact_name
    anchor = _anchor_path(artifact)
    if survivor_shape == "anchor_only":
        state.unlink()
    elif survivor_shape == "state_only":
        anchor.unlink()
    operator_owned = destination / "operator-owned"
    operator_owned.write_bytes(b"preserve")
    recovery_now = time.time()

    backup_module._recover_backup_orphans(
        destination,
        orphan_ttl_seconds=60,
        now=lambda: recovery_now,
    )
    assert any(
        candidate.name != "operator-owned"
        for candidate in destination.iterdir()
    )

    _age_protocol_files(destination, recovery_now - 120)
    backup_module._recover_backup_orphans(
        destination,
        orphan_ttl_seconds=60,
        now=lambda: recovery_now,
    )
    backup_module._recover_backup_orphans(
        destination,
        orphan_ttl_seconds=60,
        now=lambda: recovery_now,
    )

    assert tuple(destination.iterdir()) == (operator_owned,)


def test_recovery_finishes_retired_partial_cleanup(tmp_path):
    source = tmp_path / "retired-partial.db"
    destination = tmp_path / "retired-partial-backups"
    _seed_source(source)
    receipt = _create(
        source,
        destination,
        artifact_label="whole-database-v1",
    )
    backup_module._retire_committed_backup(receipt.path)
    receipt.path.unlink()
    recovery_now = time.time()
    _age_protocol_files(destination, recovery_now - 120)

    backup_module._recover_backup_orphans(
        destination,
        orphan_ttl_seconds=60,
        now=lambda: recovery_now,
    )

    assert tuple(destination.iterdir()) == ()


def test_recovery_preserves_busy_pending_state(tmp_path):
    source = tmp_path / "busy-pending-recovery.db"
    destination = tmp_path / "busy-pending-recovery-backups"
    _seed_source(source)
    context = _process_context()
    process = context.Process(
        target=_crash_backup_at_stage,
        args=(source, destination, "target_directory_fsynced"),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 73
    state = next(destination.glob(".*.commit-state"))
    artifact = _artifact_from_state_path_for_test(state)
    anchor = _anchor_path(artifact)
    recovery_now = time.time()
    _age_protocol_files(destination, recovery_now - 120)
    ready = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_exclusive_state_lock,
        args=(state, ready, release),
    )
    holder.start()
    assert ready.wait(timeout=5)
    try:
        backup_module._recover_backup_orphans(
            destination,
            orphan_ttl_seconds=60,
            now=lambda: recovery_now,
        )
    finally:
        release.set()
        holder.join(timeout=5)

    assert holder.exitcode == 0
    assert state.exists()
    assert artifact.exists()
    assert anchor.exists()


def test_recovery_rechecks_artifact_age_on_validated_descriptor(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "recovery-age-race.db"
    destination = tmp_path / "recovery-age-race-backups"
    _seed_source(source)
    context = _process_context()
    process = context.Process(
        target=_crash_backup_at_stage,
        args=(source, destination, "target_directory_fsynced"),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 73
    state = next(destination.glob(".*.commit-state"))
    artifact = _artifact_from_state_path_for_test(state)
    anchor = _anchor_path(artifact)
    anchor.unlink()
    recovery_now = time.time()
    _age_protocol_files(destination, recovery_now - 120)
    original_hash = backup_module._hash_artifact_descriptor
    touched = False

    def touch_after_hash(descriptor, *, expected_size):
        nonlocal touched
        result = original_hash(
            descriptor,
            expected_size=expected_size,
        )
        if not touched and os.fstat(descriptor).st_ino == artifact.stat().st_ino:
            touched = True
            os.utime(artifact, (recovery_now, recovery_now))
        return result

    monkeypatch.setattr(
        backup_module,
        "_hash_artifact_descriptor",
        touch_after_hash,
    )

    backup_module._recover_backup_orphans(
        destination,
        orphan_ttl_seconds=60,
        now=lambda: recovery_now,
    )

    assert touched is True
    assert state.exists()
    assert artifact.exists()
    assert not anchor.exists()


def test_recovery_preserves_corrupt_busy_and_operator_owned_images(
    tmp_path,
):
    source = tmp_path / "preserved-images.db"
    destination = tmp_path / "preserved-images-backups"
    _seed_source(source)
    receipt = _create(source, destination)
    state = _state_path(receipt.path)
    encoded = bytearray(state.read_bytes())
    encoded[-1] ^= 0x01
    state.write_bytes(encoded)
    operator_anchor = (
        destination
        / ".20260728T201512345679Z-before-sensitive-v1.sqlite3.aesgcm.pending"
    )
    operator_anchor.write_bytes(b"operator-owned")
    recovery_now = time.time()
    _age_protocol_files(destination, recovery_now - 120)

    backup_module._recover_backup_orphans(
        destination,
        orphan_ttl_seconds=60,
        now=lambda: recovery_now,
    )

    assert receipt.path.exists()
    assert _anchor_path(receipt.path).exists()
    assert state.exists()
    assert operator_anchor.read_bytes() == b"operator-owned"


@pytest.mark.parametrize("blocking_name", ["target", "state"])
def test_anchor_recovery_preserves_broken_symlink_namespace(
    tmp_path,
    blocking_name,
):
    source = tmp_path / f"symlink-{blocking_name}.db"
    destination = tmp_path / f"symlink-{blocking_name}-backups"
    _seed_source(source)
    context = _process_context()
    process = context.Process(
        target=_crash_backup_at_stage,
        args=(source, destination, "pending_state_durable"),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 73
    state = next(destination.glob(".*.commit-state"))
    artifact = _artifact_from_state_path_for_test(state)
    anchor = _anchor_path(artifact)
    state.unlink()
    blocking_path = (
        artifact if blocking_name == "target" else _state_path(artifact)
    )
    blocking_path.symlink_to(destination / "missing-operator-target")
    recovery_now = time.time()
    _age_protocol_files(destination, recovery_now - 120)

    backup_module._recover_backup_orphans(
        destination,
        orphan_ttl_seconds=60,
        now=lambda: recovery_now,
    )

    assert anchor.exists()
    assert blocking_path.is_symlink()


def test_backup_gets_fresh_snapshot_lease_after_slow_destination_work(
    tmp_path,
    monkeypatch,
):
    source = _operational_source(tmp_path / "lease-sequencing.db")
    destination = tmp_path / "lease-sequencing-backups"
    fake_monotonic = [0.0]
    events: list[object] = []
    original_acquire = (
        backup_module.RuntimeTenureService.acquire_maintenance
    )
    original_guarded = backup_module.guarded_backup_maintenance

    def recover(*_args, **_kwargs):
        events.append("recover")

    def slow_prune(*_args, **_kwargs):
        events.append("prune")
        fake_monotonic[0] += 31.0

    def observe_acquire(service, *args, **kwargs):
        events.append(("acquire", fake_monotonic[0]))
        return original_acquire(service, *args, **kwargs)

    def injected_guard_clock(guard, *, ttl_seconds):
        events.append(("lease_started", fake_monotonic[0]))
        return original_guarded(
            guard,
            ttl_seconds=ttl_seconds,
            monotonic=lambda: fake_monotonic[0],
        )

    monkeypatch.setattr(
        backup_module,
        "_recover_backup_orphans",
        recover,
        raising=False,
    )
    monkeypatch.setattr(
        backup_module,
        "_prune_committed_backups",
        slow_prune,
    )
    monkeypatch.setattr(
        backup_module.RuntimeTenureService,
        "acquire_maintenance",
        observe_acquire,
    )
    monkeypatch.setattr(
        backup_module,
        "guarded_backup_maintenance",
        injected_guard_clock,
    )

    receipt = backup_module.backup_database(
        source,
        destination,
        retention_days=14,
        backup_key=BACKUP_KEY,
        backup_key_id=BACKUP_KEY_ID,
        process_identity=BACKUP_IDENTITY,
        process_inspector=_OfflineInspector(),
    )

    assert receipt.verified is True
    assert events[:4] == [
        "recover",
        "prune",
        ("acquire", 31.0),
        ("lease_started", 31.0),
    ]
