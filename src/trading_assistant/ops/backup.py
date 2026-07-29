"""Verified encrypted online SQLite backups with bounded retention."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
import fcntl
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pydantic import SecretStr
from sqlalchemy.engine import URL, make_url

from ..config import load_config
from ..db.schema import require_current_schema, schema_status
from ..db.session import create_db_engine, make_session_factory
from ..security.secrets import (
    load_role_secrets,
    secret_value,
    validate_base64_key,
)
from .backup_transaction import (
    BACKUP_CHUNK_BYTES as _CHUNK_BYTES,
    ENCRYPTED_NAME as _ENCRYPTED_NAME,
    QUARANTINE_DIRECTORY as _QUARANTINE_DIRECTORY,
    SNAPSHOT_NAME as _SNAPSHOT_NAME,
    TRANSACTION_DIRECTORY as _TRANSACTION_DIRECTORY,
    VERIFICATION_NAME as _VERIFICATION_NAME,
    BackupTransaction as _BackupTransaction,
    EncryptedBackupError,
    acquire_bounded_lock as _acquire_bounded_lock,
    authorize_transaction_artifact_links as _authorize_transaction_artifact_links,
    close_backup_transaction as _close_backup_transaction,
    create_backup_transaction as _create_backup_transaction,
    decode_checksummed_record as _decode_checksummed_record,
    encode_checksummed_record as _encode_checksummed_record,
    fsync_and_hash_transaction_artifact as _fsync_and_hash_transaction_artifact,
    hash_artifact_descriptor as _hash_artifact_descriptor,
    hash_transaction_member as _hash_transaction_member,
    open_transaction_member as _open_transaction_member,
    positive_record_integer as _positive_record_integer,
    pread_exact as _pread_exact,
    pwrite_all as _pwrite_all,
    recover_backup_transaction as _recover_backup_transaction,
    retry_flock as _retry_flock,
    retry_fsync as _retry_fsync,
    transaction_member_path as _transaction_member_path,
    validate_backup_transaction as _validate_backup_transaction,
)
from .tenure import (
    LocalProcessInspector,
    ProcessIdentity,
    ProcessInspector,
    RuntimeTenureGuard,
    RuntimeTenureService,
)

_ENCRYPTED_MAGIC = b"TA-SENSITIVE-BACKUP\x00"
_ENCRYPTED_VERSION = 1
_NONCE_BYTES = 12
_TAG_BYTES = 16
_MAX_HEADER_BYTES = 4096
_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,63}")
_COMMITTED_NAME = re.compile(
    r"^\d{8}T\d{12}Z-"
    r"(before-sensitive-v1|whole-database-v1)"
    r"\.sqlite3\.aesgcm$"
)
_COMMIT_STATE_MAGIC = b"TA-BACKUP-COMMIT-STATE\x00"
_COMMIT_STATE_VERSION = 2
_COMMIT_STATE_BYTES = 1024
_COMMIT_STATE_PHASES = {
    "PENDING": 0,
    "COMMITTED": 1,
    "RETIRED": 2,
}
_TRANSACTION_ID = re.compile(r"[0-9a-f]{32}")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_STATE_ACCESS_FAILURES = {
    "encrypted_backup_state_busy",
    "encrypted_backup_state_unavailable",
}
_ORPHAN_RECOVERY_TTL_SECONDS = 86_400


class _MaintenanceCallbackFailure(BaseException):
    def __init__(self, cause: BaseException) -> None:
        self.cause = cause


@dataclass(frozen=True)
class EncryptedBackupReceipt:
    path: Path
    path_hash: str
    source_sha256: str
    created_at: str
    schema_head: str
    backup_key_id: str
    verified: bool


@dataclass(frozen=True)
class BackupMaintenance:
    """Three-phase maintenance callbacks for one SQLite source snapshot."""

    check_snapshot: Callable[[], None]
    complete_snapshot: Callable[[], None]
    ensure_owned: Callable[[], None]


@dataclass(frozen=True)
class _ArtifactCommitState:
    """Fixed identity carried by one durable publication state record."""

    phase: Literal["PENDING", "COMMITTED", "RETIRED"]
    generation: int
    transaction_id: str
    artifact_name: str
    artifact_device: int
    artifact_inode: int
    artifact_size: int
    artifact_sha256: str
    state_device: int
    state_inode: int


def guarded_backup_maintenance(
    guard: RuntimeTenureGuard,
    *,
    ttl_seconds: int,
    monotonic: Callable[[], float] = time.monotonic,
) -> BackupMaintenance:
    """Delay source-writing renewal until the SQLite snapshot is closed."""

    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or ttl_seconds <= 1
    ):
        raise ValueError("backup_tenure_ttl_invalid")
    deadline = monotonic() + max(1.0, ttl_seconds - 1.0)
    snapshot_completed = False

    def check_snapshot() -> None:
        guard.ensure_owned()
        if snapshot_completed or monotonic() >= deadline:
            raise EncryptedBackupError("backup_snapshot_tenure_expired")

    def complete_snapshot() -> None:
        nonlocal snapshot_completed
        if snapshot_completed:
            raise EncryptedBackupError(
                "backup_snapshot_transition_invalid"
            )
        check_snapshot()
        if not guard.renew_once():
            raise EncryptedBackupError("backup_tenure_lost")
        guard.start()
        snapshot_completed = True

    def ensure_owned() -> None:
        if not snapshot_completed:
            raise EncryptedBackupError(
                "backup_snapshot_transition_invalid"
            )
        guard.ensure_owned()

    return BackupMaintenance(
        check_snapshot=check_snapshot,
        complete_snapshot=complete_snapshot,
        ensure_owned=ensure_owned,
    )


def _canonical_json(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _pending_anchor(path: Path) -> Path:
    return path.with_name(f".{path.name}.pending")


def _commit_state_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.commit-state")


def _state_payload(state: _ArtifactCommitState) -> dict[str, object]:
    return {
        "artifact_device": state.artifact_device,
        "artifact_inode": state.artifact_inode,
        "artifact_name": state.artifact_name,
        "artifact_sha256": state.artifact_sha256,
        "artifact_size": state.artifact_size,
        "generation": state.generation,
        "phase": state.phase,
        "state_device": state.state_device,
        "state_inode": state.state_inode,
        "transaction_id": state.transaction_id,
        "version": _COMMIT_STATE_VERSION,
    }


def _encode_commit_state(state: _ArtifactCommitState) -> bytes:
    return _encode_checksummed_record(
        _COMMIT_STATE_MAGIC,
        _state_payload(state),
        record_bytes=_COMMIT_STATE_BYTES,
        invalid_code="encrypted_backup_state_invalid",
    )


def _decode_commit_state(encoded: bytes) -> _ArtifactCommitState:
    payload = _decode_checksummed_record(
        encoded,
        _COMMIT_STATE_MAGIC,
        record_bytes=_COMMIT_STATE_BYTES,
        invalid_code="encrypted_backup_state_invalid",
    )
    if (
        set(payload)
        != {
            "artifact_device",
            "artifact_inode",
            "artifact_name",
            "artifact_sha256",
            "artifact_size",
            "generation",
            "phase",
            "state_device",
            "state_inode",
            "transaction_id",
            "version",
        }
        or type(payload.get("version")) is not int
        or payload.get("version") != _COMMIT_STATE_VERSION
    ):
        raise EncryptedBackupError("encrypted_backup_state_invalid")
    phase = payload.get("phase")
    generation = payload.get("generation")
    transaction_id = payload.get("transaction_id")
    artifact_name = payload.get("artifact_name")
    artifact_sha256 = payload.get("artifact_sha256")
    if (
        not isinstance(phase, str)
        or phase not in _COMMIT_STATE_PHASES
        or type(generation) is not int
        or generation != _COMMIT_STATE_PHASES[phase]
        or not isinstance(transaction_id, str)
        or _TRANSACTION_ID.fullmatch(transaction_id) is None
        or not isinstance(artifact_name, str)
        or _COMMITTED_NAME.fullmatch(artifact_name) is None
        or not isinstance(artifact_sha256, str)
        or _SHA256_HEX.fullmatch(artifact_sha256) is None
        or not all(
            _positive_record_integer(payload.get(field))
            for field in (
                "artifact_device",
                "artifact_inode",
                "artifact_size",
                "state_device",
                "state_inode",
            )
        )
    ):
        raise EncryptedBackupError("encrypted_backup_state_invalid")
    return _ArtifactCommitState(
        phase=phase,
        generation=generation,
        transaction_id=transaction_id,
        artifact_name=artifact_name,
        artifact_device=payload["artifact_device"],
        artifact_inode=payload["artifact_inode"],
        artifact_size=payload["artifact_size"],
        artifact_sha256=artifact_sha256,
        state_device=payload["state_device"],
        state_inode=payload["state_inode"],
    )
def _state_open_flags(*, writable: bool) -> int:
    flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(
        os,
        "O_NOFOLLOW",
        0,
    )
    if writable:
        flags |= getattr(os, "O_DSYNC", os.O_SYNC)
    flags |= getattr(os, "O_NONBLOCK", 0)
    return flags


def _state_descriptor_identity(
    descriptor: int,
    state_path: Path,
) -> os.stat_result:
    descriptor_stat = os.fstat(descriptor)
    path_stat = state_path.lstat()
    if not (
        stat.S_ISREG(descriptor_stat.st_mode)
        and stat.S_ISREG(path_stat.st_mode)
        and descriptor_stat.st_dev == path_stat.st_dev
        and descriptor_stat.st_ino == path_stat.st_ino
    ):
        raise EncryptedBackupError("encrypted_backup_state_invalid")
    return descriptor_stat


def _open_existing_state_descriptor(
    state_path: Path,
    *,
    writable: bool,
) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(
            state_path,
            _state_open_flags(writable=writable),
        )
    except FileNotFoundError:
        raise
    except OSError:
        raise EncryptedBackupError(
            "encrypted_backup_state_unavailable"
        ) from None
    try:
        descriptor_stat = os.fstat(descriptor)
        if not stat.S_ISREG(descriptor_stat.st_mode):
            raise EncryptedBackupError(
                "encrypted_backup_state_unavailable"
            )
        if descriptor_stat.st_size != _COMMIT_STATE_BYTES:
            raise EncryptedBackupError(
                "encrypted_backup_state_invalid"
            )
        path_stat = state_path.lstat()
        if not (
            stat.S_ISREG(path_stat.st_mode)
            and descriptor_stat.st_dev == path_stat.st_dev
            and descriptor_stat.st_ino == path_stat.st_ino
        ):
            raise EncryptedBackupError(
                "encrypted_backup_state_unavailable"
            )
        return descriptor, descriptor_stat
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _locked_state_descriptor(
    state_path: Path,
):
    descriptor, descriptor_stat = _open_existing_state_descriptor(
        state_path,
        writable=False,
    )
    locked = False
    try:
        _acquire_bounded_lock(descriptor, fcntl.LOCK_SH)
        locked = True
        descriptor_stat = _state_descriptor_identity(
            descriptor,
            state_path,
        )
        yield descriptor, descriptor_stat
    finally:
        try:
            if locked:
                _retry_flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _read_commit_state(
    descriptor: int,
    descriptor_stat: os.stat_result,
    state_path: Path,
) -> _ArtifactCommitState:
    current_stat = _state_descriptor_identity(
        descriptor,
        state_path,
    )
    if not (
        descriptor_stat.st_dev == current_stat.st_dev
        and descriptor_stat.st_ino == current_stat.st_ino
        and current_stat.st_size == _COMMIT_STATE_BYTES
    ):
        raise EncryptedBackupError("encrypted_backup_state_invalid")
    encoded = _pread_exact(descriptor, _COMMIT_STATE_BYTES, 0)
    state = _decode_commit_state(encoded)
    final_stat = _state_descriptor_identity(descriptor, state_path)
    expected_name = state_path.name
    if not (
        expected_name.startswith(".")
        and expected_name.endswith(".commit-state")
        and state.artifact_name
        == expected_name[1 : -len(".commit-state")]
        and state.state_device == final_stat.st_dev
        and state.state_inode == final_stat.st_ino
        and final_stat.st_size == _COMMIT_STATE_BYTES
    ):
        raise EncryptedBackupError("encrypted_backup_state_invalid")
    return state


def _write_commit_state(
    descriptor: int,
    state: _ArtifactCommitState,
    *,
    state_path: Path,
    verify_readback: bool,
) -> None:
    _pwrite_all(descriptor, _encode_commit_state(state))
    _retry_fsync(descriptor)
    if verify_readback:
        descriptor_stat = _state_descriptor_identity(
            descriptor,
            state_path,
        )
        if (
            _read_commit_state(
                descriptor,
                descriptor_stat,
                state_path,
            )
            != state
        ):
            raise EncryptedBackupError(
                "encrypted_backup_state_write_failed"
            )


def _matching_artifact_stat(
    path: Path,
    state: _ArtifactCommitState,
    descriptor: int,
) -> os.stat_result:
    artifact_stat = os.fstat(descriptor)
    path_stat = path.lstat()
    anchor_stat = _pending_anchor(path).lstat()
    if not (
        path.name == state.artifact_name
        and stat.S_ISREG(artifact_stat.st_mode)
        and stat.S_ISREG(path_stat.st_mode)
        and stat.S_ISREG(anchor_stat.st_mode)
        and artifact_stat.st_dev == path_stat.st_dev
        and artifact_stat.st_ino == path_stat.st_ino
        and artifact_stat.st_dev == anchor_stat.st_dev
        and artifact_stat.st_ino == anchor_stat.st_ino
        and artifact_stat.st_dev == state.artifact_device
        and artifact_stat.st_ino == state.artifact_inode
        and artifact_stat.st_size == state.artifact_size
    ):
        raise EncryptedBackupError("encrypted_backup_state_invalid")
    return artifact_stat


def _verify_matching_artifact(
    path: Path,
    state: _ArtifactCommitState,
    descriptor: int,
) -> None:
    before = _matching_artifact_stat(path, state, descriptor)
    artifact_sha256 = _hash_artifact_descriptor(
        descriptor,
        expected_size=state.artifact_size,
    )
    after = _matching_artifact_stat(path, state, descriptor)
    if not (
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_ctime_ns == after.st_ctime_ns
        and hmac.compare_digest(
            artifact_sha256,
            state.artifact_sha256,
        )
    ):
        raise EncryptedBackupError("encrypted_backup_state_invalid")


def _open_matching_artifact(
    path: Path,
    state: _ArtifactCommitState,
) -> int:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        _verify_matching_artifact(path, state, descriptor)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _locked_artifact_state(path: Path):
    state_path = _commit_state_path(path)
    with _locked_state_descriptor(state_path) as (
        state_descriptor,
        state_stat,
    ):
        state = _read_commit_state(
            state_descriptor,
            state_stat,
            state_path,
        )
        artifact_descriptor = _open_matching_artifact(path, state)
        try:
            yield state, state_descriptor, artifact_descriptor
        finally:
            try:
                _verify_matching_artifact(
                    path,
                    state,
                    artifact_descriptor,
                )
                final_state_stat = _state_descriptor_identity(
                    state_descriptor,
                    state_path,
                )
                if (
                    _read_commit_state(
                        state_descriptor,
                        final_state_stat,
                        state_path,
                    )
                    != state
                ):
                    raise EncryptedBackupError(
                        "encrypted_backup_state_invalid"
                    )
            finally:
                os.close(artifact_descriptor)


def _authoritative_state(
    path: Path,
    phase: Literal["PENDING", "COMMITTED", "RETIRED"],
) -> _ArtifactCommitState | None:
    try:
        with _locked_artifact_state(path) as (
            state,
            _state_descriptor,
            _artifact_descriptor,
        ):
            if state.phase == phase:
                return state
    except EncryptedBackupError as exc:
        if exc.stable_code in _STATE_ACCESS_FAILURES:
            raise
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return None


def _is_committed_artifact(path: Path) -> bool:
    return (
        _COMMITTED_NAME.fullmatch(path.name) is not None
        and _authoritative_state(path, "COMMITTED") is not None
    )


@contextmanager
def _open_committed_artifact(path: Path):
    if _COMMITTED_NAME.fullmatch(path.name) is None:
        raise EncryptedBackupError("encrypted_backup_not_committed")
    try:
        with _locked_artifact_state(path) as (
            state,
            _state_descriptor,
            artifact_descriptor,
        ):
            if state.phase != "COMMITTED":
                raise EncryptedBackupError(
                    "encrypted_backup_not_committed"
                )
            with os.fdopen(os.dup(artifact_descriptor), "rb") as handle:
                yield handle
    except EncryptedBackupError as exc:
        if (
            exc.stable_code == "encrypted_backup_not_committed"
            or exc.stable_code in _STATE_ACCESS_FAILURES
        ):
            raise
        raise EncryptedBackupError(
            "encrypted_backup_not_committed"
        ) from None
    except OSError:
        raise EncryptedBackupError(
            "encrypted_backup_not_committed"
        ) from None


def list_committed_backups(
    destination_dir: str | Path,
    *,
    artifact_label: Literal[
        "before-sensitive-v1",
        "whole-database-v1",
    ]
    | None = None,
) -> tuple[Path, ...]:
    """List only artifacts that crossed the atomic commit boundary."""

    directory = Path(destination_dir).expanduser()
    if not directory.exists():
        return ()
    if (
        not directory.is_dir()
        or directory.is_symlink()
        or artifact_label
        not in {
            None,
            "before-sensitive-v1",
            "whole-database-v1",
        }
    ):
        raise EncryptedBackupError("backup_directory_invalid")
    suffix = (
        None
        if artifact_label is None
        else f"-{artifact_label}.sqlite3.aesgcm"
    )
    return tuple(
        sorted(
            candidate
            for candidate in directory.iterdir()
            if (suffix is None or candidate.name.endswith(suffix))
            and _is_committed_artifact(candidate)
        )
    )


def _create_pending_commit_state(
    target: Path,
    artifact_stat: os.stat_result,
    artifact_sha256: str,
    transaction_id: str,
) -> _ArtifactCommitState:
    if _SHA256_HEX.fullmatch(artifact_sha256) is None:
        raise EncryptedBackupError("encrypted_backup_state_invalid")
    state_path = _commit_state_path(target)
    descriptor = os.open(
        state_path,
        _state_open_flags(writable=True) | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    locked = False
    try:
        os.fchmod(descriptor, 0o600)
        _acquire_bounded_lock(descriptor, fcntl.LOCK_EX)
        locked = True
        state_stat = os.fstat(descriptor)
        path_stat = state_path.lstat()
        if not (
            stat.S_ISREG(artifact_stat.st_mode)
            and stat.S_ISREG(state_stat.st_mode)
            and stat.S_ISREG(path_stat.st_mode)
            and state_stat.st_dev == path_stat.st_dev
            and state_stat.st_ino == path_stat.st_ino
        ):
            raise EncryptedBackupError(
                "encrypted_backup_state_invalid"
            )
        state = _ArtifactCommitState(
            phase="PENDING",
            generation=_COMMIT_STATE_PHASES["PENDING"],
            transaction_id=transaction_id,
            artifact_name=target.name,
            artifact_device=artifact_stat.st_dev,
            artifact_inode=artifact_stat.st_ino,
            artifact_size=artifact_stat.st_size,
            artifact_sha256=artifact_sha256,
            state_device=state_stat.st_dev,
            state_inode=state_stat.st_ino,
        )
        _write_commit_state(
            descriptor,
            state,
            state_path=state_path,
            verify_readback=True,
        )
        return state
    finally:
        try:
            if locked:
                _retry_flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _transition_commit_state(
    target: Path,
    expected: _ArtifactCommitState,
    phase: Literal["COMMITTED", "RETIRED"],
) -> _ArtifactCommitState:
    expected_generation = _COMMIT_STATE_PHASES[expected.phase]
    next_generation = _COMMIT_STATE_PHASES[phase]
    if (
        expected.generation != expected_generation
        or next_generation != expected.generation + 1
    ):
        raise EncryptedBackupError("encrypted_backup_state_invalid")
    state_path = _commit_state_path(target)
    state_descriptor: int | None = None
    artifact_descriptor: int | None = None
    locked = False
    transitioned = replace(
        expected,
        phase=phase,
        generation=next_generation,
    )

    def observe_state() -> _ArtifactCommitState:
        assert state_descriptor is not None
        descriptor_stat = _state_descriptor_identity(
            state_descriptor,
            state_path,
        )
        observed = _read_commit_state(
            state_descriptor,
            descriptor_stat,
            state_path,
        )
        return observed

    def persist_and_prove(
        state: _ArtifactCommitState,
    ) -> _ArtifactCommitState:
        assert state_descriptor is not None
        write_failure: BaseException | None = None
        try:
            _write_commit_state(
                state_descriptor,
                state,
                state_path=state_path,
                verify_readback=False,
            )
        except BaseException as exc:
            write_failure = exc
        proof_failure: BaseException | None = None
        observed: _ArtifactCommitState | None = None
        try:
            observed = observe_state()
        except BaseException as exc:
            proof_failure = exc
        if proof_failure is None and observed == state:
            return observed
        if write_failure is not None:
            raise write_failure
        if proof_failure is None:
            raise EncryptedBackupError(
                "encrypted_backup_state_invalid"
            )
        raise proof_failure

    try:
        state_descriptor, _ = _open_existing_state_descriptor(
            state_path,
            writable=True,
        )
        _acquire_bounded_lock(state_descriptor, fcntl.LOCK_EX)
        locked = True
        state_stat = _state_descriptor_identity(
            state_descriptor,
            state_path,
        )
        current = _read_commit_state(
            state_descriptor,
            state_stat,
            state_path,
        )
        if current != expected:
            raise EncryptedBackupError(
                "encrypted_backup_state_invalid"
            )
        # This is the last artifact operation. The fixed-state write, fsync,
        # and same-descriptor readback below are the irrevocable commit point.
        artifact_descriptor = _open_matching_artifact(target, current)
        try:
            return persist_and_prove(transitioned)
        except BaseException as transition_failure:
            # A BaseException can arrive after the durable readback but before
            # the nested return reaches this frame. Re-read through the same
            # still-locked descriptor before any restoration attempt. One
            # retry covers an exception at the first reconciliation boundary.
            for _attempt in range(2):
                try:
                    observed = observe_state()
                except BaseException:
                    continue
                if observed == transitioned:
                    return observed
                if observed == expected:
                    raise transition_failure
                break
            try:
                persist_and_prove(expected)
            except BaseException as restore_failure:
                if phase != "COMMITTED":
                    raise EncryptedBackupError(
                        "encrypted_backup_state_uncertain"
                    ) from restore_failure
                fail_closed = replace(
                    expected,
                    phase="RETIRED",
                    generation=_COMMIT_STATE_PHASES["RETIRED"],
                )
                try:
                    persist_and_prove(fail_closed)
                except BaseException as retire_failure:
                    raise EncryptedBackupError(
                        "encrypted_backup_state_uncertain"
                    ) from retire_failure
            raise transition_failure
    finally:
        if artifact_descriptor is not None:
            try:
                os.close(artifact_descriptor)
            except BaseException:
                pass
        if state_descriptor is not None and locked:
            try:
                _retry_flock(state_descriptor, fcntl.LOCK_UN)
            except BaseException:
                pass
        if state_descriptor is not None:
            try:
                os.close(state_descriptor)
            except BaseException:
                pass


def _matching_regular_file(
    path: Path,
    *,
    device: int,
    inode: int,
) -> bool:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(path_stat.st_mode)
        and path_stat.st_dev == device
        and path_stat.st_ino == inode
    )


def _unlink_matching_regular_file(
    path: Path,
    *,
    device: int,
    inode: int,
) -> None:
    if _matching_regular_file(path, device=device, inode=inode):
        path.unlink()


def _created_at(now: Callable[[], datetime]) -> tuple[datetime, str]:
    value = now()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise EncryptedBackupError("backup_timestamp_invalid")
    value = value.astimezone(timezone.utc)
    canonical = value.isoformat(timespec="microseconds").replace(
        "+00:00",
        "Z",
    )
    return value, canonical


def _parse_header_stream(handle) -> tuple[dict[str, object], bytes, bytes]:
    magic = handle.read(len(_ENCRYPTED_MAGIC))
    encoded_length = handle.read(4)
    if magic != _ENCRYPTED_MAGIC or len(encoded_length) != 4:
        raise EncryptedBackupError("encrypted_backup_format_invalid")
    header_length = struct.unpack(">I", encoded_length)[0]
    if header_length <= 0 or header_length > _MAX_HEADER_BYTES:
        raise EncryptedBackupError("encrypted_backup_format_invalid")
    encoded_header = handle.read(header_length)
    nonce = handle.read(_NONCE_BYTES)
    if len(encoded_header) != header_length or len(nonce) != _NONCE_BYTES:
        raise EncryptedBackupError("encrypted_backup_format_invalid")
    try:
        header = json.loads(encoded_header.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EncryptedBackupError(
            "encrypted_backup_format_invalid"
        ) from None
    if (
        not isinstance(header, dict)
        or _canonical_json(header) != encoded_header
        or set(header)
        != {
            "algorithm",
            "chunk_bytes",
            "created_at",
            "key_id",
            "schema_head",
            "source_sha256",
            "version",
        }
        or header.get("algorithm") != "AES-256-GCM"
        or header.get("chunk_bytes") != _CHUNK_BYTES
        or header.get("version") != _ENCRYPTED_VERSION
        or not isinstance(header.get("created_at"), str)
        or not isinstance(header.get("schema_head"), str)
        or not isinstance(header.get("key_id"), str)
        or _KEY_ID.fullmatch(str(header["key_id"])) is None
        or not isinstance(header.get("source_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", str(header["source_sha256"]))
        is None
    ):
        raise EncryptedBackupError("encrypted_backup_format_invalid")
    aad = magic + encoded_length + encoded_header
    return header, nonce, aad


def read_encrypted_backup_header(path: str | Path) -> dict[str, object]:
    """Return only canonical non-secret metadata from an encrypted artifact."""
    artifact = Path(path)
    try:
        with _open_committed_artifact(artifact) as handle:
            header, _nonce, _aad = _parse_header_stream(handle)
        return header
    except EncryptedBackupError:
        raise
    except Exception:
        raise EncryptedBackupError(
            "encrypted_backup_format_invalid"
        ) from None


def create_encrypted_database_backup(
    source: str | Path,
    destination_dir: str | Path,
    *,
    backup_key: bytes,
    backup_key_id: str,
    schema_head: str,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ensure_maintenance: Callable[[], None] | None = None,
    maintenance: BackupMaintenance | None = None,
    stage_hook: Callable[[str], None] | None = None,
    before_commit: Callable[[], None] | None = None,
    artifact_label: Literal[
        "before-sensitive-v1",
        "whole-database-v1",
    ] = "before-sensitive-v1",
) -> EncryptedBackupReceipt:
    """Create, privately verify, then atomically publish one encrypted snapshot."""

    if not isinstance(backup_key, bytes) or len(backup_key) != 32:
        raise EncryptedBackupError("backup_key_invalid")
    if (
        not isinstance(backup_key_id, str)
        or _KEY_ID.fullmatch(backup_key_id) is None
        or not isinstance(schema_head, str)
        or not schema_head
        or len(schema_head) > 64
        or artifact_label
        not in {"before-sensitive-v1", "whole-database-v1"}
    ):
        raise EncryptedBackupError("backup_metadata_invalid")
    if maintenance is not None and ensure_maintenance is not None:
        raise EncryptedBackupError("backup_maintenance_invalid")
    hook = stage_hook or (lambda _stage: None)
    maintain_callback = (
        maintenance.ensure_owned
        if maintenance is not None
        else ensure_maintenance or (lambda: None)
    )
    snapshot_check_callback = (
        maintenance.check_snapshot
        if maintenance is not None
        else (lambda: None)
    )
    snapshot_complete_callback = (
        maintenance.complete_snapshot
        if maintenance is not None
        else (lambda: None)
    )

    def invoke(callback: Callable[[], None]) -> None:
        try:
            callback()
        except BaseException as exc:
            raise _MaintenanceCallbackFailure(exc) from None

    def maintain() -> None:
        invoke(maintain_callback)

    def check_snapshot() -> None:
        invoke(snapshot_check_callback)

    def complete_snapshot() -> None:
        invoke(snapshot_complete_callback)

    transaction: _BackupTransaction | None = None
    directory: Path | None = None
    target: Path | None = None
    pending_state: _ArtifactCommitState | None = None
    receipt: EncryptedBackupReceipt | None = None
    try:
        source_path = Path(source).expanduser().resolve(strict=True)
        if not source_path.is_file():
            raise EncryptedBackupError("backup_source_invalid")
        requested_directory = Path(destination_dir).expanduser()
        if requested_directory.is_symlink():
            raise EncryptedBackupError("backup_directory_invalid")
        requested_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = requested_directory.resolve(strict=True)
        if not directory.is_dir() or directory.is_symlink():
            raise EncryptedBackupError("backup_directory_invalid")
        os.chmod(directory, 0o700)

        # All operation members are precreated and inode-bound by the durable,
        # checksummed manifest before any sensitive content can exist.
        transaction = _create_backup_transaction(directory)
        hook("transaction_manifest_durable")
        check_snapshot()
        snapshot = _transaction_member_path(
            transaction,
            _SNAPSHOT_NAME,
        )
        source_uri = f"{source_path.as_uri()}?mode=ro"
        with (
            sqlite3.connect(source_uri, uri=True) as source_connection,
            sqlite3.connect(snapshot) as snapshot_connection,
        ):
            if snapshot_connection.execute(
                "PRAGMA journal_mode=OFF"
            ).fetchone() != ("off",):
                raise EncryptedBackupError(
                    "encrypted_backup_transaction_invalid"
                )
            snapshot_connection.execute("PRAGMA temp_store=MEMORY")
            source_connection.backup(
                snapshot_connection,
                pages=256,
                progress=lambda _status, _remaining, _total: (
                    check_snapshot()
                ),
            )
            if snapshot_connection.execute(
                "PRAGMA journal_mode=OFF"
            ).fetchone() != ("off",):
                raise EncryptedBackupError(
                    "encrypted_backup_transaction_invalid"
                )
        _validate_backup_transaction(transaction)
        snapshot_descriptor = _open_transaction_member(
            transaction,
            _SNAPSHOT_NAME,
            os.O_RDONLY,
        )
        os.close(snapshot_descriptor)
        complete_snapshot()
        hook("snapshot_created")
        maintain()

        source_sha256 = _hash_transaction_member(
            transaction,
            _SNAPSHOT_NAME,
            ensure_maintenance=maintain,
        )
        hook("snapshot_hashed")
        created, created_at = _created_at(now)
        header = {
            "algorithm": "AES-256-GCM",
            "chunk_bytes": _CHUNK_BYTES,
            "created_at": created_at,
            "key_id": backup_key_id,
            "schema_head": schema_head,
            "source_sha256": source_sha256,
            "version": _ENCRYPTED_VERSION,
        }
        encoded_header = _canonical_json(header)
        encoded_length = struct.pack(">I", len(encoded_header))
        aad = _ENCRYPTED_MAGIC + encoded_length + encoded_header
        nonce = os.urandom(_NONCE_BYTES)
        encryptor = Cipher(
            algorithms.AES(backup_key),
            modes.GCM(nonce),
        ).encryptor()
        encryptor.authenticate_additional_data(aad)
        with (
            os.fdopen(
                _open_transaction_member(
                    transaction,
                    _SNAPSHOT_NAME,
                    os.O_RDONLY,
                ),
                "rb",
                buffering=0,
            ) as plaintext,
            os.fdopen(
                _open_transaction_member(
                    transaction,
                    _ENCRYPTED_NAME,
                    os.O_RDWR,
                ),
                "r+b",
                buffering=0,
            ) as ciphertext,
        ):
            ciphertext.truncate(0)
            ciphertext.write(aad)
            ciphertext.write(nonce)
            hook("header_written")
            while True:
                maintain()
                chunk = plaintext.read(_CHUNK_BYTES)
                if not chunk:
                    break
                ciphertext.write(encryptor.update(chunk))
                hook("encrypt_chunk")
            ciphertext.write(encryptor.finalize())
            ciphertext.write(encryptor.tag)
            ciphertext.flush()
            os.fsync(ciphertext.fileno())
        _validate_backup_transaction(transaction)
        hook("ciphertext_fsynced")
        maintain()

        verification = _transaction_member_path(
            transaction,
            _VERIFICATION_NAME,
        )
        hook("verification_opened")
        with (
            os.fdopen(
                _open_transaction_member(
                    transaction,
                    _ENCRYPTED_NAME,
                    os.O_RDONLY,
                ),
                "rb",
                buffering=0,
            ) as ciphertext,
            os.fdopen(
                _open_transaction_member(
                    transaction,
                    _VERIFICATION_NAME,
                    os.O_RDWR,
                ),
                "r+b",
                buffering=0,
            ) as plaintext,
        ):
            plaintext.truncate(0)
            parsed_header, parsed_nonce, parsed_aad = _parse_header_stream(
                ciphertext
            )
            if parsed_header != header or parsed_nonce != nonce:
                raise EncryptedBackupError(
                    "encrypted_backup_format_invalid"
                )
            ciphertext_start = ciphertext.tell()
            ciphertext.seek(0, os.SEEK_END)
            artifact_size = ciphertext.tell()
            ciphertext_bytes = (
                artifact_size - ciphertext_start - _TAG_BYTES
            )
            if ciphertext_bytes <= 0:
                raise EncryptedBackupError(
                    "encrypted_backup_format_invalid"
                )
            ciphertext.seek(artifact_size - _TAG_BYTES)
            tag = ciphertext.read(_TAG_BYTES)
            ciphertext.seek(ciphertext_start)
            decryptor = Cipher(
                algorithms.AES(backup_key),
                modes.GCM(parsed_nonce, tag),
            ).decryptor()
            decryptor.authenticate_additional_data(parsed_aad)
            remaining = ciphertext_bytes
            verification_hash = hashlib.sha256()
            while remaining:
                maintain()
                encrypted_chunk = ciphertext.read(
                    min(_CHUNK_BYTES, remaining)
                )
                if not encrypted_chunk:
                    raise EncryptedBackupError(
                        "encrypted_backup_format_invalid"
                    )
                remaining -= len(encrypted_chunk)
                decrypted_chunk = decryptor.update(encrypted_chunk)
                plaintext.write(decrypted_chunk)
                verification_hash.update(decrypted_chunk)
                hook("decrypt_chunk")
            final = decryptor.finalize()
            plaintext.write(final)
            verification_hash.update(final)
            plaintext.flush()
            os.fsync(plaintext.fileno())
        _validate_backup_transaction(transaction)
        if verification_hash.hexdigest() != source_sha256:
            raise EncryptedBackupError("encrypted_backup_hash_mismatch")
        hook("verification_hashed")
        maintain()
        with sqlite3.connect(
            f"{verification.as_uri()}?mode=ro",
            uri=True,
        ) as connection:
            progress_failure: list[BaseException] = []

            def quick_check_progress() -> int:
                try:
                    maintain()
                except BaseException as exc:
                    progress_failure.append(exc)
                    return 1
                return 0

            connection.set_progress_handler(
                quick_check_progress,
                100_000,
            )
            try:
                check = connection.execute(
                    "PRAGMA quick_check"
                ).fetchone()
            except sqlite3.DatabaseError:
                if progress_failure:
                    raise progress_failure[0]
                raise
            finally:
                connection.set_progress_handler(None, 0)
            if progress_failure:
                raise progress_failure[0]
        if check != ("ok",):
            raise EncryptedBackupError("encrypted_backup_quick_check_failed")
        _validate_backup_transaction(transaction)
        hook("quick_check_complete")
        maintain()

        stamp = created.strftime("%Y%m%dT%H%M%S%fZ")
        target = directory / f"{stamp}-{artifact_label}.sqlite3.aesgcm"
        anchor = _pending_anchor(target)
        state_path = _commit_state_path(target)
        if (
            _path_entry_exists(target)
            or _path_entry_exists(anchor)
            or _path_entry_exists(state_path)
        ):
            raise EncryptedBackupError("encrypted_backup_exists")

        artifact_stat, artifact_sha256 = (
            _fsync_and_hash_transaction_artifact(transaction)
        )
        try:
            pending_state = _create_pending_commit_state(
                target,
                artifact_stat,
                artifact_sha256,
                transaction.manifest.transaction_id,
            )
        except FileExistsError:
            raise EncryptedBackupError("encrypted_backup_exists") from None
        _retry_fsync(transaction.destination_descriptor)
        hook("pending_state_durable")

        try:
            os.link(
                _ENCRYPTED_NAME,
                anchor.name,
                src_dir_fd=transaction.directory_descriptor,
                dst_dir_fd=transaction.destination_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            raise EncryptedBackupError("encrypted_backup_exists") from None
        _retry_fsync(transaction.destination_descriptor)
        hook("anchor_linked_pending")

        try:
            os.link(
                anchor.name,
                target.name,
                src_dir_fd=transaction.destination_descriptor,
                dst_dir_fd=transaction.destination_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            raise EncryptedBackupError("encrypted_backup_exists") from None
        hook("target_linked_pending")
        _retry_fsync(transaction.destination_descriptor)
        hook("target_directory_fsynced")

        observed_pending = _authoritative_state(target, "PENDING")
        if observed_pending != pending_state:
            raise EncryptedBackupError(
                "encrypted_backup_state_invalid"
            )
        _authorize_transaction_artifact_links(
            transaction,
            additional_links=2,
        )

        # All plaintext/ciphertext operation members and their manifest are
        # removed and both directories fsynced before commit can begin.
        _close_backup_transaction(transaction, remove=True)
        transaction = None

        hook("before_artifact_commit")
        maintain()
        if before_commit is not None:
            invoke(before_commit)
        receipt = EncryptedBackupReceipt(
            path=target,
            path_hash=hashlib.sha256(
                str(target).encode("utf-8")
            ).hexdigest(),
            source_sha256=source_sha256,
            created_at=created_at,
            schema_head=schema_head,
            backup_key_id=backup_key_id,
            verified=True,
        )
    except BaseException as exc:
        cleanup_failure: BaseException | None = None
        if transaction is not None:
            try:
                _close_backup_transaction(transaction, remove=True)
            except BaseException as cleanup_exc:
                cleanup_failure = cleanup_exc
        if target is not None and pending_state is not None:
            for owned_path, device, inode in (
                (
                    target,
                    pending_state.artifact_device,
                    pending_state.artifact_inode,
                ),
                (
                    _pending_anchor(target),
                    pending_state.artifact_device,
                    pending_state.artifact_inode,
                ),
                (
                    _commit_state_path(target),
                    pending_state.state_device,
                    pending_state.state_inode,
                ),
            ):
                try:
                    _unlink_matching_regular_file(
                        owned_path,
                        device=device,
                        inode=inode,
                    )
                except BaseException as cleanup_exc:
                    if cleanup_failure is None:
                        cleanup_failure = cleanup_exc
            if directory is not None:
                try:
                    _fsync_directory(directory)
                except BaseException as cleanup_exc:
                    if cleanup_failure is None:
                        cleanup_failure = cleanup_exc
        if isinstance(exc, _MaintenanceCallbackFailure):
            failure = exc.cause
        elif isinstance(
            exc,
            (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
        ):
            failure = exc
        elif isinstance(exc, EncryptedBackupError):
            failure = exc
        else:
            failure = EncryptedBackupError("encrypted_backup_failed")
        if cleanup_failure is not None:
            raise failure from cleanup_failure
        raise failure from None

    if target is None or pending_state is None or receipt is None:
        raise EncryptedBackupError("encrypted_backup_failed")
    # No exception cleanup scope surrounds this final transition. All hooks,
    # maintenance checks, receipt construction, artifact verification, and
    # operation-directory cleanup have already completed.
    try:
        _transition_commit_state(
            target,
            pending_state,
            "COMMITTED",
        )
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except EncryptedBackupError:
        raise
    except BaseException:
        # The transition proves any ambiguous durable COMMITTED write before
        # returning or raising. This wrapper normalizes only a proven
        # non-commit failure and deliberately performs no cleanup.
        raise EncryptedBackupError("encrypted_backup_failed") from None
    return receipt


def _retire_committed_backup(
    candidate: Path,
) -> _ArtifactCommitState:
    committed = _authoritative_state(candidate, "COMMITTED")
    if committed is None:
        raise EncryptedBackupError("encrypted_backup_not_committed")
    return _transition_commit_state(
        candidate,
        committed,
        "RETIRED",
    )


def _artifact_for_state_path(state_path: Path) -> Path | None:
    name = state_path.name
    if not (
        name.startswith(".")
        and name.endswith(".commit-state")
    ):
        return None
    artifact_name = name[1 : -len(".commit-state")]
    if _COMMITTED_NAME.fullmatch(artifact_name) is None:
        return None
    return state_path.with_name(artifact_name)


def _path_is_older_than(path: Path, cutoff: float) -> bool:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return True
    return (
        stat.S_ISREG(path_stat.st_mode)
        and path_stat.st_mtime < cutoff
    )


def _path_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _open_recovery_artifact(
    path: Path,
    state: _ArtifactCommitState,
    *,
    cutoff: float,
) -> int | None:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
    except FileNotFoundError:
        return None
    except OSError:
        raise EncryptedBackupError(
            "encrypted_backup_state_invalid"
        ) from None
    try:
        descriptor_stat = os.fstat(descriptor)
        path_stat = path.lstat()
        artifact_sha256 = _hash_artifact_descriptor(
            descriptor,
            expected_size=state.artifact_size,
        )
        final_descriptor_stat = os.fstat(descriptor)
        if not (
            stat.S_ISREG(descriptor_stat.st_mode)
            and stat.S_ISREG(path_stat.st_mode)
            and descriptor_stat.st_dev == path_stat.st_dev
            and descriptor_stat.st_ino == path_stat.st_ino
            and descriptor_stat.st_dev == state.artifact_device
            and descriptor_stat.st_ino == state.artifact_inode
            and descriptor_stat.st_size == state.artifact_size
            and descriptor_stat.st_mtime < cutoff
            and descriptor_stat.st_mtime_ns
            == final_descriptor_stat.st_mtime_ns
            and descriptor_stat.st_ctime_ns
            == final_descriptor_stat.st_ctime_ns
            and hmac.compare_digest(
                artifact_sha256,
                state.artifact_sha256,
            )
        ):
            raise EncryptedBackupError(
                "encrypted_backup_state_invalid"
            )
        final_stat = path.lstat()
        if not (
            stat.S_ISREG(final_stat.st_mode)
            and final_stat.st_dev == descriptor_stat.st_dev
            and final_stat.st_ino == descriptor_stat.st_ino
        ):
            raise EncryptedBackupError(
                "encrypted_backup_state_invalid"
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _recover_state_backed_orphan(
    state_path: Path,
    *,
    cutoff: float,
    durable_directory: Path,
) -> None:
    artifact = _artifact_for_state_path(state_path)
    if artifact is None:
        return
    state_descriptor: int | None = None
    artifact_descriptors: list[int] = []
    locked = False
    try:
        state_descriptor, state_stat = _open_existing_state_descriptor(
            state_path,
            writable=True,
        )
        _acquire_bounded_lock(state_descriptor, fcntl.LOCK_EX)
        locked = True
        state_stat = _state_descriptor_identity(
            state_descriptor,
            state_path,
        )
        state = _read_commit_state(
            state_descriptor,
            state_stat,
            state_path,
        )
        if (
            state.phase == "COMMITTED"
            or state_stat.st_mtime >= cutoff
        ):
            return
        anchor = _pending_anchor(artifact)
        for candidate in (artifact, anchor):
            if not _path_is_older_than(candidate, cutoff):
                return
            descriptor = _open_recovery_artifact(
                candidate,
                state,
                cutoff=cutoff,
            )
            if descriptor is not None:
                artifact_descriptors.append(descriptor)
        if len(artifact_descriptors) == 2:
            first = os.fstat(artifact_descriptors[0])
            second = os.fstat(artifact_descriptors[1])
            if not (
                first.st_dev == second.st_dev
                and first.st_ino == second.st_ino
            ):
                return
        final_state_stat = _state_descriptor_identity(
            state_descriptor,
            state_path,
        )
        if not (
            state_stat.st_mtime_ns == final_state_stat.st_mtime_ns
            and state_stat.st_ctime_ns == final_state_stat.st_ctime_ns
            and final_state_stat.st_mtime < cutoff
            and all(
                os.fstat(descriptor).st_mtime < cutoff
                for descriptor in artifact_descriptors
            )
        ):
            return

        for candidate in (artifact, anchor):
            _unlink_matching_regular_file(
                candidate,
                device=state.artifact_device,
                inode=state.artifact_inode,
            )
        _fsync_directory(durable_directory)
        _unlink_matching_regular_file(
            state_path,
            device=state.state_device,
            inode=state.state_inode,
        )
        _fsync_directory(durable_directory)
    except FileNotFoundError:
        return
    except EncryptedBackupError:
        return
    except OSError:
        return
    finally:
        for descriptor in artifact_descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if state_descriptor is not None and locked:
            try:
                _retry_flock(state_descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        if state_descriptor is not None:
            try:
                os.close(state_descriptor)
            except OSError:
                pass


def _recover_backup_orphans(
    destination_dir: str | Path,
    *,
    orphan_ttl_seconds: int = _ORPHAN_RECOVERY_TTL_SECONDS,
    now: Callable[[], float] = time.time,
) -> None:
    """Conservatively remove only aged, protocol-owned crash images."""

    if (
        isinstance(orphan_ttl_seconds, bool)
        or not isinstance(orphan_ttl_seconds, int)
        or orphan_ttl_seconds <= 0
    ):
        raise ValueError("orphan_ttl_seconds must be positive")
    destination = Path(destination_dir).expanduser()
    if not destination.exists():
        return
    if not destination.is_dir() or destination.is_symlink():
        raise EncryptedBackupError("backup_directory_invalid")
    durable_directory = destination.resolve(strict=True)
    cutoff = now() - orphan_ttl_seconds
    candidates = tuple(destination.iterdir())
    for candidate in candidates:
        if (
            _TRANSACTION_DIRECTORY.fullmatch(candidate.name) is not None
            or _QUARANTINE_DIRECTORY.fullmatch(candidate.name) is not None
        ):
            _recover_backup_transaction(
                durable_directory,
                candidate.name,
                cutoff=cutoff,
            )
    for state_path in candidates:
        if _artifact_for_state_path(state_path) is not None:
            _recover_state_backed_orphan(
                state_path,
                cutoff=cutoff,
                durable_directory=durable_directory,
            )


def _committed_artifact_mtime(candidate: Path) -> float | None:
    try:
        with _locked_artifact_state(candidate) as (
            state,
            _state_descriptor,
            artifact_descriptor,
        ):
            if state.phase != "COMMITTED":
                return None
            return os.fstat(artifact_descriptor).st_mtime
    except EncryptedBackupError as exc:
        if exc.stable_code in _STATE_ACCESS_FAILURES:
            raise
        return None
    except OSError:
        return None


def _prune_committed_backups(
    destination_dir: str | Path,
    *,
    artifact_label: Literal[
        "before-sensitive-v1",
        "whole-database-v1",
    ],
    cutoff: float,
) -> None:
    destination = Path(destination_dir).expanduser()
    if not destination.exists():
        return
    durable_directory = destination.resolve(strict=True)
    for candidate in list_committed_backups(
        destination,
        artifact_label=artifact_label,
    ):
        artifact_mtime = _committed_artifact_mtime(candidate)
        if artifact_mtime is None or artifact_mtime >= cutoff:
            continue
        retired = _retire_committed_backup(candidate)

        # RETIRED is durably authoritative before any name is removed.
        # Partial deletion therefore stays fail closed. Each unlink is limited
        # to the exact device/inode captured by this artifact's state record.
        _unlink_matching_regular_file(
            candidate,
            device=retired.artifact_device,
            inode=retired.artifact_inode,
        )
        _unlink_matching_regular_file(
            _pending_anchor(candidate),
            device=retired.artifact_device,
            inode=retired.artifact_inode,
        )
        _fsync_directory(durable_directory)
        _unlink_matching_regular_file(
            _commit_state_path(candidate),
            device=retired.state_device,
            inode=retired.state_inode,
        )
        _fsync_directory(durable_directory)


def backup_database(
    source: str | Path,
    destination_dir: str | Path,
    retention_days: int = 14,
    *,
    backup_key: bytes,
    backup_key_id: str,
    process_identity: ProcessIdentity,
    process_inspector: ProcessInspector,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    tenure_clock: Callable[[], datetime] = lambda: datetime.now(
        timezone.utc
    ),
) -> EncryptedBackupReceipt:
    """Create one verified encrypted whole-database operational backup."""
    if (
        isinstance(retention_days, bool)
        or not isinstance(retention_days, int)
        or retention_days <= 0
    ):
        raise ValueError("retention_days must be positive")
    source_path = Path(source).expanduser().resolve(strict=True)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    source_url = URL.create(
        "sqlite",
        database=str(source_path),
    ).render_as_string(hide_password=False)
    _recover_backup_orphans(destination_dir)
    _prune_committed_backups(
        destination_dir,
        artifact_label="whole-database-v1",
        cutoff=time.time() - retention_days * 86400,
    )
    engine = create_db_engine(source_url)
    require_current_schema(engine)
    head = schema_status(engine).head
    service = RuntimeTenureService(
        make_session_factory(engine),
        process_inspector=process_inspector,
        clock=tenure_clock,
    )
    handle = service.acquire_maintenance(
        process_identity,
        ttl_seconds=30,
    )
    guard = RuntimeTenureGuard(
        handle,
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    maintenance = guarded_backup_maintenance(
        guard,
        ttl_seconds=30,
    )
    primary_failure = False
    released = False
    disposed = False

    def release_before_commit() -> None:
        nonlocal released, disposed
        if not guard.close():
            raise EncryptedBackupError(
                "backup_tenure_release_uncertain"
            )
        released = True
        engine.dispose()
        disposed = True

    try:
        receipt = create_encrypted_database_backup(
            source_path,
            destination_dir,
            backup_key=backup_key,
            backup_key_id=backup_key_id,
            schema_head=head,
            now=now,
            maintenance=maintenance,
            artifact_label="whole-database-v1",
            before_commit=release_before_commit,
        )
    except BaseException:
        primary_failure = True
        raise
    finally:
        if not released and not guard.closed:
            close_result = guard.close()
            if not close_result and not primary_failure:
                raise EncryptedBackupError(
                    "backup_tenure_release_uncertain"
                )
        if not disposed:
            engine.dispose()
    return receipt


def database_path(database_url: str | SecretStr) -> Path:
    url = make_url(secret_value(database_url))
    if url.drivername != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("backup supports only file-backed SQLite DATABASE_URL values")
    return Path(url.database)


def main(
    argv: list[str] | None = None,
    *,
    config_loader=load_config,
    secrets_loader=load_role_secrets,
    process_identity: ProcessIdentity | None = None,
    process_inspector: ProcessInspector | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination")
    parser.add_argument("--retention-days", type=int, default=14)
    args = parser.parse_args(argv)
    from ..logging import runtime_startup

    config = config_loader()
    secrets = secrets_loader("backup", config=config)
    inspector = process_inspector or LocalProcessInspector()
    identity = process_identity or inspector.current()
    key_buffer = validate_base64_key(
        "backup_encryption_key",
        secrets.backup_encryption_key,
    )
    try:
        with runtime_startup("backup", secrets):
            receipt = backup_database(
                database_path(secrets.database_url),
                (
                    args.destination
                    if args.destination is not None
                    else config.encryption.backup_directory
                ),
                args.retention_days,
                backup_key=bytes(key_buffer),
                backup_key_id=config.encryption.backup_key_id,
                process_identity=identity,
                process_inspector=inspector,
            )
            print(
                json.dumps(
                    {
                        "backup_key_id": receipt.backup_key_id,
                        "path_hash": receipt.path_hash,
                        "status": "verified",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 0
    finally:
        for index in range(len(key_buffer)):
            key_buffer[index] = 0


if __name__ == "__main__":
    raise SystemExit(main())
