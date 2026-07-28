"""Round-5 crash-consistency regressions for encrypted backup publication."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
from threading import Event

import pytest

from trading_assistant.ops import backup as backup_module
from trading_assistant.ops.backup import (
    EncryptedBackupError,
    create_encrypted_database_backup,
    list_committed_backups,
    read_encrypted_backup_header,
)


BACKUP_KEY = b"5" * 32
BACKUP_KEY_ID = "round5-backup-key"
SCHEMA_HEAD = "20260727_0015"
BACKUP_NOW = datetime(
    2026,
    7,
    28,
    18,
    45,
    12,
    345678,
    tzinfo=timezone.utc,
)


def _seed_source(path: Path, *, payload_bytes: int = 32_768) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE sample (id INTEGER PRIMARY KEY, payload BLOB NOT NULL)"
        )
        connection.execute(
            "INSERT INTO sample(payload) VALUES (zeroblob(?))",
            (payload_bytes,),
        )
        connection.commit()


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


def _replace_state_payload(
    state_path: Path,
    update,
) -> None:
    encoded = state_path.read_bytes()
    magic = b"TA-BACKUP-COMMIT-STATE\x00"
    body_size = 1024 - hashlib.sha256().digest_size
    length_start = len(magic)
    payload_length = struct.unpack(
        ">I",
        encoded[length_start : length_start + 4],
    )[0]
    payload_start = length_start + 4
    payload = json.loads(
        encoded[
            payload_start : payload_start + payload_length
        ].decode("utf-8")
    )
    update(payload)
    replacement = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    prefix = magic + struct.pack(">I", len(replacement)) + replacement
    body = prefix + (b"\x00" * (body_size - len(prefix)))
    state_path.write_bytes(body + hashlib.sha256(body).digest())


def test_link_success_then_cancel_and_cleanup_failure_remains_uncommitted(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "ambiguous-link.db"
    destination = tmp_path / "ambiguous-link-backups"
    _seed_source(source)
    destination.mkdir()
    unrelated = destination / ".operator-owned"
    unrelated.write_bytes(b"preserve")
    original_link = os.link
    original_unlink = Path.unlink
    linked_target: Path | None = None

    def link_then_cancel(src, dst, *args, **kwargs):
        nonlocal linked_target
        result = original_link(src, dst, *args, **kwargs)
        candidate = Path(dst)
        if (
            candidate.parent == destination
            and not candidate.name.startswith(".")
            and candidate.name.endswith(".sqlite3.aesgcm")
        ):
            linked_target = candidate
            raise asyncio.CancelledError()
        return result

    def refuse_operation_cleanup(path: Path, *args, **kwargs):
        if (
            linked_target is not None
            and path.parent == destination
            and path != unrelated
        ):
            raise OSError("injected owned-path cleanup refusal")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "link", link_then_cancel)
    monkeypatch.setattr(Path, "unlink", refuse_operation_cleanup)

    with pytest.raises(asyncio.CancelledError):
        _create(source, destination)

    assert linked_target is not None
    assert linked_target.exists()
    assert _anchor_path(linked_target).exists()
    assert list_committed_backups(destination) == ()
    with pytest.raises(
        EncryptedBackupError,
        match="^encrypted_backup_not_committed$",
    ):
        read_encrypted_backup_header(linked_target)
    assert unrelated.read_bytes() == b"preserve"


def test_target_directory_fsync_failure_never_commits_visible_target(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "target-fsync.db"
    destination = tmp_path / "target-fsync-backups"
    _seed_source(source)
    original_fsync_directory = backup_module._fsync_directory
    observed_pending_target = False

    def fail_after_target_link(directory: Path) -> None:
        nonlocal observed_pending_target
        public_targets = tuple(destination.glob("*.sqlite3.aesgcm"))
        if public_targets:
            observed_pending_target = True
            assert list_committed_backups(destination) == ()
            raise OSError("injected target-directory fsync failure")
        original_fsync_directory(directory)

    monkeypatch.setattr(
        backup_module,
        "_fsync_directory",
        fail_after_target_link,
    )

    with pytest.raises(
        EncryptedBackupError,
        match="^encrypted_backup_failed$",
    ):
        _create(source, destination)

    assert observed_pending_target is True
    assert list_committed_backups(destination) == ()


def test_commit_write_exception_after_durable_write_reconciles_to_receipt(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "commit-reconcile.db"
    destination = tmp_path / "commit-reconcile-backups"
    _seed_source(source)
    original_pwrite = os.pwrite
    injected = False

    def durable_write_then_raise(descriptor, data, offset):
        nonlocal injected
        written = original_pwrite(descriptor, data, offset)
        if b'"phase":"COMMITTED"' in data:
            injected = True
            raise OSError("injected post-durable-write exception")
        return written

    monkeypatch.setattr(os, "pwrite", durable_write_then_raise)

    receipt = _create(source, destination)

    assert injected is True
    assert list_committed_backups(destination) == (receipt.path,)
    assert read_encrypted_backup_header(receipt.path)["source_sha256"] == (
        receipt.source_sha256
    )


def test_torn_commit_write_with_cleanup_failure_remains_uncommitted(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "torn-commit.db"
    destination = tmp_path / "torn-commit-backups"
    _seed_source(source)
    original_pwrite = os.pwrite
    original_unlink = Path.unlink
    torn = False

    def tear_commit_write(descriptor, data, offset):
        nonlocal torn
        if b'"phase":"COMMITTED"' in data:
            torn = True
            original_pwrite(
                descriptor,
                data[: len(data) // 2],
                offset,
            )
            raise OSError("injected torn commit-state write")
        return original_pwrite(descriptor, data, offset)

    def preserve_torn_image(path: Path, *args, **kwargs):
        if torn and path.parent == destination:
            raise OSError("simulated cleanup loss after torn write")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "pwrite", tear_commit_write)
    monkeypatch.setattr(Path, "unlink", preserve_torn_image)

    with pytest.raises(
        EncryptedBackupError,
        match="^encrypted_backup_failed$",
    ):
        _create(source, destination)

    assert torn is True
    public_targets = tuple(destination.glob("*.sqlite3.aesgcm"))
    assert len(public_targets) == 1
    assert list_committed_backups(destination) == ()
    with pytest.raises(
        EncryptedBackupError,
        match="^encrypted_backup_not_committed$",
    ):
        read_encrypted_backup_header(public_targets[0])


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "corrupt",
        "torn",
        "inode_replaced",
        "malformed_phase",
    ],
)
def test_invalid_commit_state_fails_closed_for_listing_and_header(
    tmp_path,
    damage,
):
    source = tmp_path / f"state-{damage}.db"
    destination = tmp_path / f"state-{damage}-backups"
    _seed_source(source)
    receipt = _create(source, destination)
    state = _state_path(receipt.path)

    if damage == "missing":
        state.unlink(missing_ok=True)
    elif damage == "corrupt":
        state.write_bytes(b"x" * 1024)
    elif damage == "torn":
        original = state.read_bytes() if state.exists() else b"x" * 1024
        state.write_bytes(original[: len(original) // 2])
    elif damage == "inode_replaced":
        original = state.read_bytes() if state.exists() else b"x" * 1024
        state.unlink(missing_ok=True)
        state.write_bytes(original)
    else:
        _replace_state_payload(
            state,
            lambda payload: payload.update(
                {"phase": ["COMMITTED"]}
            ),
        )

    assert receipt.path.exists()
    assert _anchor_path(receipt.path).exists()
    assert list_committed_backups(destination) == ()
    with pytest.raises(
        EncryptedBackupError,
        match="^encrypted_backup_not_committed$",
    ):
        read_encrypted_backup_header(receipt.path)


def test_commit_state_copied_from_another_artifact_is_mismatched(
    tmp_path,
):
    source = tmp_path / "mismatched-state.db"
    destination = tmp_path / "mismatched-state-backups"
    _seed_source(source)
    first = _create(source, destination)
    second = _create(
        source,
        destination,
        now=BACKUP_NOW + timedelta(microseconds=1),
    )

    _state_path(first.path).write_bytes(
        _state_path(second.path).read_bytes()
    )

    assert list_committed_backups(destination) == (second.path,)
    with pytest.raises(
        EncryptedBackupError,
        match="^encrypted_backup_not_committed$",
    ):
        read_encrypted_backup_header(first.path)


@pytest.mark.parametrize(
    "crash_stage",
    [
        "pending_state_durable",
        "target_linked_pending",
        "target_directory_fsynced",
    ],
)
def test_crash_at_each_two_phase_boundary_remains_uncommitted(
    tmp_path,
    monkeypatch,
    crash_stage,
):
    source = tmp_path / f"crash-{crash_stage}.db"
    destination = tmp_path / f"crash-{crash_stage}-backups"
    _seed_source(source)
    reached: list[str] = []
    crashed = False
    original_unlink = Path.unlink

    def crash(stage: str) -> None:
        nonlocal crashed
        reached.append(stage)
        if stage == crash_stage:
            crashed = True
            raise asyncio.CancelledError()

    def preserve_crash_image(path: Path, *args, **kwargs):
        if crashed and path.parent == destination:
            raise OSError("simulated process death before cleanup")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", preserve_crash_image)

    with pytest.raises(asyncio.CancelledError):
        _create(source, destination, stage_hook=crash)

    assert crash_stage in reached
    assert list_committed_backups(destination) == ()
    assert len(tuple(destination.glob(".*.commit-state"))) == 1
    public_targets = tuple(destination.glob("*.sqlite3.aesgcm"))
    if crash_stage == "pending_state_durable":
        assert public_targets == ()
    else:
        assert len(public_targets) == 1


def test_commit_has_no_fallible_readback_after_durable_state_write(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "no-postcommit-readback.db"
    destination = tmp_path / "no-postcommit-readback-backups"
    _seed_source(source)
    original_pwrite = os.pwrite
    original_fsync = os.fsync
    original_pread = os.pread
    commit_write_seen = False
    commit_fsync_complete = False
    fail_reads = True

    def observe_commit_write(descriptor, data, offset):
        nonlocal commit_write_seen
        written = original_pwrite(descriptor, data, offset)
        if b'"phase":"COMMITTED"' in data:
            commit_write_seen = True
        return written

    def observe_commit_fsync(descriptor):
        nonlocal commit_fsync_complete
        result = original_fsync(descriptor)
        if commit_write_seen:
            commit_fsync_complete = True
        return result

    def reject_postcommit_readback(descriptor, length, offset):
        if commit_fsync_complete and fail_reads:
            raise OSError("postcommit readback is forbidden")
        return original_pread(descriptor, length, offset)

    monkeypatch.setattr(os, "pwrite", observe_commit_write)
    monkeypatch.setattr(os, "fsync", observe_commit_fsync)
    monkeypatch.setattr(os, "pread", reject_postcommit_readback)

    receipt = _create(source, destination)

    fail_reads = False
    assert commit_fsync_complete is True
    assert list_committed_backups(destination) == (receipt.path,)


def test_reader_cannot_observe_commit_before_state_fsync_returns(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "reader-visibility.db"
    destination = tmp_path / "reader-visibility-backups"
    _seed_source(source)
    original_pwrite = os.pwrite
    original_fsync = os.fsync
    original_flock = fcntl.flock
    commit_write_seen = Event()
    commit_fsync_entered = Event()
    release_commit_fsync = Event()
    reader_attempted = Event()

    def observe_commit_write(descriptor, data, offset):
        written = original_pwrite(descriptor, data, offset)
        if b'"phase":"COMMITTED"' in data:
            commit_write_seen.set()
        return written

    def pause_commit_fsync(descriptor):
        if commit_write_seen.is_set():
            commit_fsync_entered.set()
            if not release_commit_fsync.wait(timeout=10):
                raise TimeoutError("test did not release commit-state fsync")
        return original_fsync(descriptor)

    def observe_reader_lock(descriptor, operation):
        if (
            commit_fsync_entered.is_set()
            and operation == fcntl.LOCK_SH
        ):
            reader_attempted.set()
        return original_flock(descriptor, operation)

    monkeypatch.setattr(os, "pwrite", observe_commit_write)
    monkeypatch.setattr(os, "fsync", pause_commit_fsync)
    monkeypatch.setattr(fcntl, "flock", observe_reader_lock)

    with ThreadPoolExecutor(max_workers=2) as pool:
        backup_future = pool.submit(_create, source, destination)
        assert commit_fsync_entered.wait(timeout=10)
        reader_future = pool.submit(list_committed_backups, destination)
        assert reader_attempted.wait(timeout=10)
        assert reader_future.done() is False
        release_commit_fsync.set()
        receipt = backup_future.result(timeout=10)
        assert reader_future.result(timeout=10) == (receipt.path,)


def test_success_receipt_follows_target_and_commit_state_durability(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "durable-receipt.db"
    destination = tmp_path / "durable-receipt-backups"
    _seed_source(source)
    original_fsync_directory = backup_module._fsync_directory
    original_pwrite = os.pwrite
    original_fsync = os.fsync
    events: list[str] = []
    commit_write_seen = False

    def observe_directory_fsync(directory: Path) -> None:
        original_fsync_directory(directory)
        if tuple(destination.glob("*.sqlite3.aesgcm")):
            events.append("target-directory-durable")
            assert list_committed_backups(destination) == ()

    def observe_state_write(descriptor, data, offset):
        nonlocal commit_write_seen
        written = original_pwrite(descriptor, data, offset)
        if b'"phase":"COMMITTED"' in data:
            commit_write_seen = True
            events.append("commit-state-written")
        return written

    def observe_state_fsync(descriptor):
        nonlocal commit_write_seen
        result = original_fsync(descriptor)
        if commit_write_seen:
            events.append("commit-state-durable")
            commit_write_seen = False
        return result

    monkeypatch.setattr(
        backup_module,
        "_fsync_directory",
        observe_directory_fsync,
    )
    monkeypatch.setattr(os, "pwrite", observe_state_write)
    monkeypatch.setattr(os, "fsync", observe_state_fsync)

    receipt = _create(source, destination)

    assert events == [
        "target-directory-durable",
        "commit-state-written",
        "commit-state-durable",
    ]
    assert _state_path(receipt.path).stat().st_size == 1024
    assert list_committed_backups(destination) == (receipt.path,)


def test_retention_durably_uncommits_before_partial_cleanup(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "retention.db"
    destination = tmp_path / "retention-backups"
    _seed_source(source)
    receipt = _create(
        source,
        destination,
        now=BACKUP_NOW - timedelta(days=30),
        artifact_label="whole-database-v1",
    )
    old_time = (BACKUP_NOW - timedelta(days=30)).timestamp()
    os.utime(receipt.path, (old_time, old_time))
    original_unlink = Path.unlink

    def refuse_artifact_cleanup(path: Path, *args, **kwargs):
        if path in {
            receipt.path,
            _anchor_path(receipt.path),
            _state_path(receipt.path),
        }:
            raise OSError("injected retention cleanup refusal")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_artifact_cleanup)

    with pytest.raises(OSError, match="retention cleanup refusal"):
        backup_module._prune_committed_backups(
            destination,
            artifact_label="whole-database-v1",
            cutoff=BACKUP_NOW.timestamp(),
        )

    assert receipt.path.exists()
    assert list_committed_backups(destination) == ()


def test_concurrent_name_collision_has_one_artifact_and_one_state_record(
    tmp_path,
):
    source = tmp_path / "concurrent-collision.db"
    destination = tmp_path / "concurrent-collision-backups"
    _seed_source(source)

    def attempt():
        try:
            return ("ok", _create(source, destination).path)
        except EncryptedBackupError as exc:
            return ("error", exc.stable_code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: attempt(), range(2)))

    assert sorted(outcome[0] for outcome in outcomes) == ["error", "ok"]
    assert [outcome for outcome in outcomes if outcome[0] == "error"] == [
        ("error", "encrypted_backup_exists")
    ]
    committed = list_committed_backups(destination)
    assert len(committed) == 1
    assert tuple(destination.glob(".*.commit-state")) == (
        _state_path(committed[0]),
    )
