"""Crash-safe transaction-directory and final-commit regressions."""

from __future__ import annotations

import fcntl
import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import time

import pytest

from trading_assistant.ops import backup as backup_module
from trading_assistant.ops.backup import (
    create_encrypted_database_backup,
    list_committed_backups,
)


BACKUP_KEY = b"T" * 32
BACKUP_KEY_ID = "transaction-directory-key"
SCHEMA_HEAD = "20260727_0015"
_TRANSACTION_PREFIX = ".backup-txn-"
_TRANSACTION_MEMBERS = {
    "manifest",
    "snapshot.sqlite3",
    "snapshot.sqlite3-journal",
    "snapshot.sqlite3-shm",
    "snapshot.sqlite3-wal",
    "verification.sqlite3",
    "verification.sqlite3-journal",
    "verification.sqlite3-shm",
    "verification.sqlite3-wal",
    "encrypted.aesgcm",
}


def _seed_source(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE sample (id INTEGER PRIMARY KEY, narrative TEXT)"
        )
        connection.execute(
            "INSERT INTO sample(narrative) VALUES (?)",
            ("plaintext-crash-marker-" + ("x" * 131_072),),
        )
        connection.commit()


def _create(source: Path, destination: Path, **kwargs):
    return create_encrypted_database_backup(
        source,
        destination,
        backup_key=BACKUP_KEY,
        backup_key_id=BACKUP_KEY_ID,
        schema_head=SCHEMA_HEAD,
        **kwargs,
    )


def _process_context():
    method = (
        "fork"
        if "fork" in multiprocessing.get_all_start_methods()
        else "spawn"
    )
    return multiprocessing.get_context(method)


def _crash_at_stage(
    source: Path,
    destination: Path,
    stage: str,
) -> None:
    def crash(current: str) -> None:
        if current == stage:
            os._exit(73)

    _create(source, destination, stage_hook=crash)
    os._exit(74)


def _pause_then_crash(
    source: Path,
    destination: Path,
    ready,
    release,
) -> None:
    def pause(current: str) -> None:
        if current == "snapshot_created":
            ready.set()
            release.wait(10)
            os._exit(73)

    _create(source, destination, stage_hook=pause)
    os._exit(74)


def _transaction_directories(destination: Path) -> tuple[Path, ...]:
    return tuple(
        candidate
        for candidate in destination.iterdir()
        if candidate.name.startswith(_TRANSACTION_PREFIX)
        and candidate.is_dir()
    )


def _age_tree(root: Path, timestamp: float) -> None:
    descendants = sorted(
        root.rglob("*"),
        key=lambda candidate: len(candidate.parts),
        reverse=True,
    )
    for candidate in descendants:
        os.utime(
            candidate,
            (timestamp, timestamp),
            follow_symlinks=False,
        )
    os.utime(root, (timestamp, timestamp), follow_symlinks=False)


def _hold_manifest_lock(manifest: Path, ready, release) -> None:
    descriptor = os.open(
        manifest,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        ready.set()
        release.wait(10)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@pytest.mark.parametrize(
    "stage",
    [
        "transaction_manifest_durable",
        "snapshot_created",
        "snapshot_hashed",
        "header_written",
        "encrypt_chunk",
        "ciphertext_fsynced",
        "verification_opened",
        "decrypt_chunk",
        "verification_hashed",
        "quick_check_complete",
    ],
)
def test_real_crash_files_are_manifest_owned_and_ttl_recoverable(
    tmp_path,
    stage,
):
    """Removing manifest-first creation or exact recovery leaks crash plaintext."""

    source = tmp_path / f"{stage}.db"
    destination = tmp_path / f"{stage}-backups"
    _seed_source(source)
    context = _process_context()
    process = context.Process(
        target=_crash_at_stage,
        args=(source, destination, stage),
    )
    process.start()
    process.join(timeout=10)

    assert process.exitcode == 73
    transactions = _transaction_directories(destination)
    assert len(transactions) == 1
    transaction = transactions[0]
    assert stat.S_IMODE(transaction.stat().st_mode) == 0o700
    members = {candidate.name for candidate in transaction.iterdir()}
    assert "manifest" in members
    assert members <= _TRANSACTION_MEMBERS
    manifest = transaction / "manifest"
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert manifest.read_bytes().startswith(b"TA-BACKUP-TRANSACTION\x00")

    root_plaintext = [
        candidate
        for candidate in destination.iterdir()
        if candidate.is_file()
        and candidate.read_bytes()[:16] == b"SQLite format 3\x00"
    ]
    assert root_plaintext == []

    operator_owned = destination / "operator-owned"
    operator_owned.write_bytes(b"preserve")
    recovery_now = time.time()
    backup_module._recover_backup_orphans(
        destination,
        orphan_ttl_seconds=60,
        now=lambda: recovery_now,
    )
    assert transaction.exists()

    _age_tree(transaction, recovery_now - 120)
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

    assert not transaction.exists()
    assert operator_owned.read_bytes() == b"preserve"
    assert list_committed_backups(destination) == ()


def test_busy_transaction_manifest_is_bounded_and_preserved(tmp_path):
    """Replacing bounded manifest ownership with blocking locks hangs recovery."""

    source = tmp_path / "busy.db"
    destination = tmp_path / "busy-backups"
    _seed_source(source)
    context = _process_context()
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_pause_then_crash,
        args=(source, destination, ready, release),
    )
    process.start()
    assert ready.wait(timeout=5)
    transaction = _transaction_directories(destination)[0]
    recovery_now = time.time()
    _age_tree(transaction, recovery_now - 120)

    started = time.monotonic()
    backup_module._recover_backup_orphans(
        destination,
        orphan_ttl_seconds=60,
        now=lambda: recovery_now,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 1.5
    assert transaction.exists()
    release.set()
    process.join(timeout=10)
    assert process.exitcode == 73

    _age_tree(transaction, recovery_now - 120)
    backup_module._recover_backup_orphans(
        destination,
        orphan_ttl_seconds=60,
        now=lambda: recovery_now,
    )
    assert not transaction.exists()


@pytest.mark.parametrize(
    "damage",
    ["corrupt_manifest", "extra_member", "symlink_member"],
)
def test_recovery_preserves_ambiguous_transaction_directories(
    tmp_path,
    damage,
):
    """Broad directory deletion must not consume malformed/operator content."""

    source = tmp_path / f"{damage}.db"
    destination = tmp_path / f"{damage}-backups"
    _seed_source(source)
    context = _process_context()
    process = context.Process(
        target=_crash_at_stage,
        args=(source, destination, "snapshot_created"),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 73
    transaction = _transaction_directories(destination)[0]
    if damage == "corrupt_manifest":
        manifest = transaction / "manifest"
        encoded = bytearray(manifest.read_bytes())
        encoded[-1] ^= 0x01
        manifest.write_bytes(encoded)
    elif damage == "extra_member":
        (transaction / "operator.txt").write_bytes(b"preserve")
    else:
        (transaction / "encrypted.aesgcm").symlink_to(source)
    recovery_now = time.time()
    _age_tree(transaction, recovery_now - 120)

    backup_module._recover_backup_orphans(
        destination,
        orphan_ttl_seconds=60,
        now=lambda: recovery_now,
    )

    assert transaction.exists()


def test_manifest_copy_and_symlink_namespace_are_not_adopted(tmp_path):
    """Removing inode/name binding lets copied manifests authorize deletion."""

    source = tmp_path / "copied-manifest.db"
    destination = tmp_path / "copied-manifest-backups"
    _seed_source(source)
    context = _process_context()
    process = context.Process(
        target=_crash_at_stage,
        args=(source, destination, "snapshot_created"),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 73
    original = _transaction_directories(destination)[0]
    copied = destination / f"{_TRANSACTION_PREFIX}{'c' * 32}"
    copied.mkdir(mode=0o700)
    shutil.copyfile(original / "manifest", copied / "manifest")
    os.chmod(copied / "manifest", 0o600)
    symlink = destination / f"{_TRANSACTION_PREFIX}{'d' * 32}"
    symlink.symlink_to(original, target_is_directory=True)
    recovery_now = time.time()
    _age_tree(original, recovery_now - 120)
    _age_tree(copied, recovery_now - 120)

    backup_module._recover_backup_orphans(
        destination,
        orphan_ttl_seconds=60,
        now=lambda: recovery_now,
    )

    assert not original.exists()
    assert copied.exists()
    assert symlink.is_symlink()


def test_success_leaves_no_operation_transaction_or_root_plaintext(tmp_path):
    """Skipping precommit transaction cleanup leaves sensitive operation state."""

    source = tmp_path / "success.db"
    destination = tmp_path / "success-backups"
    _seed_source(source)

    observed_precommit = False

    def inspect(stage: str) -> None:
        nonlocal observed_precommit
        if stage == "before_artifact_commit":
            observed_precommit = True
            assert _transaction_directories(destination) == ()
            assert list_committed_backups(destination) == ()

    receipt = _create(source, destination, stage_hook=inspect)

    assert observed_precommit is True
    assert _transaction_directories(destination) == ()
    assert not tuple(destination.glob(".sensitive-*"))
    assert list_committed_backups(destination) == (receipt.path,)


def test_real_crash_after_transaction_cleanup_recovers_pending_publication(
    tmp_path,
):
    """Moving transaction cleanup after commit can strand plaintext on crash."""

    source = tmp_path / "precommit-crash.db"
    destination = tmp_path / "precommit-crash-backups"
    _seed_source(source)
    context = _process_context()
    process = context.Process(
        target=_crash_at_stage,
        args=(source, destination, "before_artifact_commit"),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 73
    assert _transaction_directories(destination) == ()
    assert list_committed_backups(destination) == ()
    assert len(tuple(destination.glob(".*.commit-state"))) == 1
    assert len(tuple(destination.glob("*.sqlite3.aesgcm"))) == 1

    recovery_now = time.time()
    for candidate in destination.iterdir():
        os.utime(
            candidate,
            (recovery_now - 120, recovery_now - 120),
            follow_symlinks=False,
        )
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

    assert tuple(destination.iterdir()) == ()


def test_partial_transaction_cleanup_is_retryable(tmp_path, monkeypatch):
    """Deleting by broad glob or removing manifest first breaks retry safety."""

    source = tmp_path / "partial-cleanup.db"
    destination = tmp_path / "partial-cleanup-backups"
    _seed_source(source)
    context = _process_context()
    process = context.Process(
        target=_crash_at_stage,
        args=(source, destination, "snapshot_created"),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 73
    transaction = _transaction_directories(destination)[0]
    recovery_now = time.time()
    _age_tree(transaction, recovery_now - 120)
    original_unlink = os.unlink
    failed_once = False

    def fail_one_member(path, *args, **kwargs):
        nonlocal failed_once
        if path == "snapshot.sqlite3" and not failed_once:
            failed_once = True
            raise OSError("injected partial cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", fail_one_member)
    backup_module._recover_backup_orphans(
        destination,
        orphan_ttl_seconds=60,
        now=lambda: recovery_now,
    )
    assert failed_once is True
    assert transaction.exists()
    assert (transaction / "manifest").exists()

    monkeypatch.setattr(os, "unlink", original_unlink)
    backup_module._recover_backup_orphans(
        destination,
        orphan_ttl_seconds=60,
        now=lambda: recovery_now,
    )
    assert not transaction.exists()


def test_committed_transition_has_no_fallible_postcommit_work(
    tmp_path,
    monkeypatch,
):
    """A post-COMMITTED verification/restoration chain can raise yet list."""

    source = tmp_path / "final-commit.db"
    destination = tmp_path / "final-commit-backups"
    _seed_source(source)
    original_pwrite = os.pwrite
    original_verify = backup_module._verify_matching_artifact
    original_unlink = Path.unlink
    committed_written = False
    postcommit_verifications = 0
    restoration_writes = 0
    cleanup_attempts = 0

    def chained_state_write(descriptor, data, offset):
        nonlocal committed_written, restoration_writes
        if b'"phase":"COMMITTED"' in data:
            written = original_pwrite(descriptor, data, offset)
            committed_written = True
            return written
        if committed_written and (
            b'"phase":"PENDING"' in data or b'"phase":"RETIRED"' in data
        ):
            restoration_writes += 1
            raise OSError("injected restoration failure")
        return original_pwrite(descriptor, data, offset)

    def reject_postcommit_verification(path, state, descriptor):
        nonlocal postcommit_verifications
        if committed_written:
            postcommit_verifications += 1
            raise OSError("injected postcommit verification failure")
        return original_verify(path, state, descriptor)

    def reject_postcommit_cleanup(path: Path, *args, **kwargs):
        nonlocal cleanup_attempts
        if committed_written:
            cleanup_attempts += 1
            raise OSError("injected postcommit cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "pwrite", chained_state_write)
    monkeypatch.setattr(
        backup_module,
        "_verify_matching_artifact",
        reject_postcommit_verification,
    )
    monkeypatch.setattr(Path, "unlink", reject_postcommit_cleanup)

    receipt = _create(source, destination)

    assert committed_written is True
    assert postcommit_verifications == 0
    assert restoration_writes == 0
    assert cleanup_attempts == 0
    monkeypatch.setattr(os, "pwrite", original_pwrite)
    monkeypatch.setattr(
        backup_module,
        "_verify_matching_artifact",
        original_verify,
    )
    monkeypatch.setattr(Path, "unlink", original_unlink)
    assert list_committed_backups(destination) == (receipt.path,)
