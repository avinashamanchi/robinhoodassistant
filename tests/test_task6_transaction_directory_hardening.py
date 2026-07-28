"""Crash-safe transaction-directory and final-commit regressions."""

from __future__ import annotations

import fcntl
import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import time

import pytest

from trading_assistant.ops import backup as backup_module
from trading_assistant.ops import backup_transaction as transaction_module
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
    "verification.sqlite3",
    "encrypted.aesgcm",
}
_CRASH_STAGES = [
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
]


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


def _namespace_identities(
    directory: Path,
) -> dict[str, tuple[int, int, int, int]]:
    result: dict[str, tuple[int, int, int, int]] = {}
    for candidate in directory.iterdir():
        identity = candidate.lstat()
        result[candidate.name] = (
            stat.S_IFMT(identity.st_mode),
            identity.st_dev,
            identity.st_ino,
            identity.st_size,
        )
    return result


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


def _hold_partial_backup_transaction(
    destination: Path,
    ready,
    release,
) -> None:
    destination.mkdir(mode=0o700)
    transaction = transaction_module.create_backup_transaction(destination)
    try:
        os.unlink(
            transaction_module.ENCRYPTED_NAME,
            dir_fd=transaction.directory_descriptor,
        )
        transaction_module.retry_fsync(
            transaction.directory_descriptor
        )
        ready.set()
        release.wait(10)
    finally:
        transaction_module.close_backup_transaction(
            transaction,
            remove=False,
        )


@pytest.mark.parametrize(
    "stage",
    _CRASH_STAGES,
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
    assert members == _TRANSACTION_MEMBERS
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


def test_manifest_is_durable_only_after_all_members_are_precreated(tmp_path):
    """Creating a member after manifest durability leaves an adoption gap."""

    source = tmp_path / "precreated-members.db"
    destination = tmp_path / "precreated-members-backups"
    _seed_source(source)
    context = _process_context()
    process = context.Process(
        target=_crash_at_stage,
        args=(source, destination, "transaction_manifest_durable"),
    )
    process.start()
    process.join(timeout=10)

    assert process.exitcode == 73
    transaction = _transaction_directories(destination)[0]
    assert {candidate.name for candidate in transaction.iterdir()} == (
        _TRANSACTION_MEMBERS
    )
    for name in _TRANSACTION_MEMBERS - {"manifest"}:
        member = transaction / name
        assert member.is_file()
        assert member.stat().st_size == 0
        assert stat.S_IMODE(member.stat().st_mode) == 0o600


@pytest.mark.parametrize("stage", _CRASH_STAGES)
def test_recovery_preserves_operator_replacement_at_every_crash_stage(
    tmp_path,
    stage,
):
    """Deleting an allowed-name replacement consumes operator-owned content."""

    source = tmp_path / f"replacement-{stage}.db"
    destination = tmp_path / f"replacement-{stage}-backups"
    _seed_source(source)
    context = _process_context()
    process = context.Process(
        target=_crash_at_stage,
        args=(source, destination, stage),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 73
    transaction = _transaction_directories(destination)[0]
    replacement = transaction / "encrypted.aesgcm"
    if replacement.exists() or replacement.is_symlink():
        replacement.unlink()
    replacement.write_bytes(b"operator-owned-replacement")
    replacement.chmod(0o600)
    recovery_now = time.time()
    _age_tree(transaction, recovery_now - 120)
    before_recovery = _namespace_identities(transaction)

    backup_module._recover_backup_orphans(
        destination,
        orphan_ttl_seconds=60,
        now=lambda: recovery_now,
    )

    assert transaction.exists()
    assert _namespace_identities(transaction) == before_recovery
    assert replacement.read_bytes() == b"operator-owned-replacement"


@pytest.mark.parametrize("stage", _CRASH_STAGES)
def test_recovery_preserves_unrecorded_old_sidecar_name(
    tmp_path,
    stage,
):
    """An old allowlist must not authorize deletion of an unrecorded sidecar."""

    source = tmp_path / f"unrecorded-sidecar-{stage}.db"
    destination = tmp_path / f"unrecorded-sidecar-{stage}-backups"
    _seed_source(source)
    context = _process_context()
    process = context.Process(
        target=_crash_at_stage,
        args=(source, destination, stage),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 73
    transaction = _transaction_directories(destination)[0]
    sidecar = transaction / "snapshot.sqlite3-wal"
    sidecar.write_bytes(b"operator-owned-sidecar")
    sidecar.chmod(0o600)
    recovery_now = time.time()
    _age_tree(transaction, recovery_now - 120)
    before_recovery = _namespace_identities(transaction)

    backup_module._recover_backup_orphans(
        destination,
        orphan_ttl_seconds=60,
        now=lambda: recovery_now,
    )

    assert transaction.exists()
    assert _namespace_identities(transaction) == before_recovery
    assert sidecar.read_bytes() == b"operator-owned-sidecar"


@pytest.mark.parametrize(
    "missing_names",
    [
        ("snapshot.sqlite3",),
        ("snapshot.sqlite3", "verification.sqlite3"),
        (
            "snapshot.sqlite3",
            "verification.sqlite3",
            "encrypted.aesgcm",
        ),
    ],
    ids=["one-absent", "two-absent", "all-absent"],
)
def test_recovery_converges_when_recorded_members_are_already_absent(
    tmp_path,
    missing_names,
):
    """Any absent recorded member means exact cleanup already completed."""

    source = tmp_path / "missing-member.db"
    destination = tmp_path / "missing-member-backups"
    _seed_source(source)
    context = _process_context()
    process = context.Process(
        target=_crash_at_stage,
        args=(source, destination, "quick_check_complete"),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 73
    transaction = _transaction_directories(destination)[0]
    for name in missing_names:
        (transaction / name).unlink()
    recovery_now = time.time()
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
    assert tuple(destination.iterdir()) == ()


@pytest.mark.parametrize("damage", ["extra", "replacement"])
def test_partial_recovery_preserves_extra_or_replaced_entries(
    tmp_path,
    damage,
):
    """Subset tolerance must not authorize extra or replaced content."""

    source = tmp_path / f"partial-{damage}.db"
    destination = tmp_path / f"partial-{damage}-backups"
    _seed_source(source)
    context = _process_context()
    process = context.Process(
        target=_crash_at_stage,
        args=(source, destination, "quick_check_complete"),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 73
    transaction = _transaction_directories(destination)[0]
    (transaction / "encrypted.aesgcm").unlink()
    if damage == "extra":
        protected = transaction / "operator.txt"
    else:
        protected = transaction / "snapshot.sqlite3"
        protected.unlink()
    protected.write_bytes(b"operator-owned-content")
    protected.chmod(0o600)
    recovery_now = time.time()
    _age_tree(transaction, recovery_now - 120)
    before_recovery = _namespace_identities(transaction)

    for _attempt in range(2):
        backup_module._recover_backup_orphans(
            destination,
            orphan_ttl_seconds=60,
            now=lambda: recovery_now,
        )

    assert transaction.exists()
    assert _namespace_identities(transaction) == before_recovery
    assert protected.read_bytes() == b"operator-owned-content"


def test_aged_partial_active_transaction_is_protected_by_manifest_lock(
    tmp_path,
):
    """TTL alone cannot recover a partial transaction while its lock is held."""

    destination = tmp_path / "active-partial-backups"
    context = _process_context()
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_partial_backup_transaction,
        args=(destination, ready, release),
    )
    process.start()
    assert ready.wait(timeout=5)
    transaction = _transaction_directories(destination)[0]
    recovery_now = time.time()
    _age_tree(transaction, recovery_now - 120)
    before_recovery = _namespace_identities(transaction)

    started = time.monotonic()
    backup_module._recover_backup_orphans(
        destination,
        orphan_ttl_seconds=60,
        now=lambda: recovery_now,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 1.5
    assert _namespace_identities(transaction) == before_recovery
    release.set()
    process.join(timeout=10)
    assert process.exitcode == 0

    for _attempt in range(2):
        backup_module._recover_backup_orphans(
            destination,
            orphan_ttl_seconds=60,
            now=lambda: recovery_now,
        )
    assert not transaction.exists()


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
        encrypted = transaction / "encrypted.aesgcm"
        encrypted.unlink()
        encrypted.symlink_to(source)
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


def test_partial_cleanup_recovery_converges_after_unlink_failure(
    tmp_path,
    monkeypatch,
):
    """A transient member unlink failure must not strand plaintext forever."""

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
    original_fsync = transaction_module.retry_fsync
    failed_once = False
    directory_identity = transaction.stat()
    directory_fsyncs = 0

    def fail_one_member(path, *args, **kwargs):
        nonlocal failed_once
        if path == "snapshot.sqlite3" and not failed_once:
            failed_once = True
            raise OSError("injected partial cleanup failure")
        return original_unlink(path, *args, **kwargs)

    def track_directory_fsync(descriptor):
        nonlocal directory_fsyncs
        identity = os.fstat(descriptor)
        if (
            identity.st_dev == directory_identity.st_dev
            and identity.st_ino == directory_identity.st_ino
        ):
            directory_fsyncs += 1
        return original_fsync(descriptor)

    monkeypatch.setattr(os, "unlink", fail_one_member)
    monkeypatch.setattr(
        transaction_module,
        "retry_fsync",
        track_directory_fsync,
    )
    backup_module._recover_backup_orphans(
        destination,
        orphan_ttl_seconds=60,
        now=lambda: recovery_now,
    )
    assert failed_once is True
    assert transaction.exists()
    assert (transaction / "manifest").exists()
    assert {candidate.name for candidate in transaction.iterdir()} == {
        "manifest",
        "snapshot.sqlite3",
        "verification.sqlite3",
    }
    assert directory_fsyncs >= 1

    monkeypatch.setattr(os, "unlink", original_unlink)
    delayed_recovery = recovery_now + 120
    for _attempt in range(2):
        backup_module._recover_backup_orphans(
            destination,
            orphan_ttl_seconds=60,
            now=lambda: delayed_recovery,
        )
    assert not transaction.exists()
    assert tuple(destination.iterdir()) == ()


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


@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_interrupt_after_committed_readback_reconciles_without_restore(
    tmp_path,
    monkeypatch,
    interrupt_type,
):
    """A return-boundary interrupt must not make a committed API call raise."""

    source = tmp_path / f"return-gap-{interrupt_type.__name__}.db"
    destination = (
        tmp_path / f"return-gap-{interrupt_type.__name__}-backups"
    )
    _seed_source(source)
    original_read = backup_module._read_commit_state
    original_pwrite = os.pwrite
    original_flock = fcntl.flock
    original_close = os.close
    original_unlink = Path.unlink
    commit_descriptor: int | None = None
    commit_written = False
    interrupt_armed = False
    interrupt_fired = False
    reconciliation_read_failures = 0
    restoration_writes = 0
    unlock_failures = 0
    close_failures = 0
    cleanup_attempts = 0

    def track_commit_write(descriptor, data, offset):
        nonlocal commit_descriptor, commit_written, restoration_writes
        if b'"phase":"COMMITTED"' in data:
            commit_descriptor = descriptor
            commit_written = True
        elif commit_written and (
            b'"phase":"PENDING"' in data or b'"phase":"RETIRED"' in data
        ):
            restoration_writes += 1
            raise OSError("injected post-commit restoration failure")
        return original_pwrite(descriptor, data, offset)

    def arm_after_committed_readback(*args, **kwargs):
        nonlocal interrupt_armed, reconciliation_read_failures
        if interrupt_fired and reconciliation_read_failures == 0:
            reconciliation_read_failures += 1
            raise OSError("injected transient reconciliation read failure")
        observed = original_read(*args, **kwargs)
        if observed.phase == "COMMITTED" and not interrupt_fired:
            interrupt_armed = True
        return observed

    def interrupt_successful_nested_return(frame, event, arg):
        nonlocal interrupt_fired
        if (
            interrupt_armed
            and not interrupt_fired
            and event == "return"
            and frame.f_code.co_name == "persist_and_prove"
        ):
            interrupt_fired = True
            raise interrupt_type("injected committed-return interrupt")
        return interrupt_successful_nested_return

    def fail_unlock_after_commit(descriptor, operation):
        nonlocal unlock_failures
        if (
            descriptor == commit_descriptor
            and interrupt_fired
            and operation == fcntl.LOCK_UN
            and unlock_failures == 0
        ):
            original_flock(descriptor, operation)
            unlock_failures += 1
            raise OSError("injected post-commit unlock failure")
        return original_flock(descriptor, operation)

    def fail_close_after_commit(descriptor):
        nonlocal close_failures
        result = original_close(descriptor)
        if (
            descriptor == commit_descriptor
            and interrupt_fired
            and close_failures == 0
        ):
            close_failures += 1
            raise OSError("injected post-commit close failure")
        return result

    def reject_postcommit_cleanup(path: Path, *args, **kwargs):
        nonlocal cleanup_attempts
        if interrupt_fired and path.parent == destination:
            cleanup_attempts += 1
            raise OSError("injected post-commit cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "pwrite", track_commit_write)
    monkeypatch.setattr(
        backup_module,
        "_read_commit_state",
        arm_after_committed_readback,
    )
    monkeypatch.setattr(fcntl, "flock", fail_unlock_after_commit)
    monkeypatch.setattr(os, "close", fail_close_after_commit)
    monkeypatch.setattr(Path, "unlink", reject_postcommit_cleanup)
    previous_trace = sys.gettrace()
    sys.settrace(interrupt_successful_nested_return)
    try:
        receipt = _create(source, destination)
    finally:
        sys.settrace(previous_trace)

    assert interrupt_fired is True
    assert reconciliation_read_failures == 1
    assert restoration_writes == 0
    assert unlock_failures == 1
    assert close_failures == 1
    assert cleanup_attempts == 0
    assert list_committed_backups(destination) == (receipt.path,)
