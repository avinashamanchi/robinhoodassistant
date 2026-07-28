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
import tempfile
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
_CHUNK_BYTES = 1_048_576
_MAX_HEADER_BYTES = 4096
_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,63}")
_COMMITTED_NAME = re.compile(
    r"^\d{8}T\d{12}Z-"
    r"(before-sensitive-v1|whole-database-v1)"
    r"\.sqlite3\.aesgcm$"
)
_COMMIT_STATE_MAGIC = b"TA-BACKUP-COMMIT-STATE\x00"
_COMMIT_STATE_VERSION = 1
_COMMIT_STATE_BYTES = 1024
_COMMIT_STATE_DIGEST_BYTES = hashlib.sha256().digest_size
_COMMIT_STATE_PHASES = {
    "PENDING": 0,
    "COMMITTED": 1,
    "RETIRED": 2,
}
_TRANSACTION_ID = re.compile(r"[0-9a-f]{32}")


class EncryptedBackupError(RuntimeError):
    """Stable, content-free failure from encrypted backup handling."""

    def __init__(self, stable_code: str) -> None:
        self.stable_code = stable_code
        super().__init__(stable_code)


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


def _unlink_private_temp(path: Path | None) -> None:
    if path is None:
        return
    path.unlink(missing_ok=True)
    path.with_name(f"{path.name}-wal").unlink(missing_ok=True)
    path.with_name(f"{path.name}-shm").unlink(missing_ok=True)


def _pending_anchor(path: Path) -> Path:
    return path.with_name(f".{path.name}.pending")


def _commit_state_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.commit-state")


def _state_payload(state: _ArtifactCommitState) -> dict[str, object]:
    return {
        "artifact_device": state.artifact_device,
        "artifact_inode": state.artifact_inode,
        "artifact_name": state.artifact_name,
        "artifact_size": state.artifact_size,
        "generation": state.generation,
        "phase": state.phase,
        "state_device": state.state_device,
        "state_inode": state.state_inode,
        "transaction_id": state.transaction_id,
        "version": _COMMIT_STATE_VERSION,
    }


def _encode_commit_state(state: _ArtifactCommitState) -> bytes:
    payload = _canonical_json(_state_payload(state))
    body_size = _COMMIT_STATE_BYTES - _COMMIT_STATE_DIGEST_BYTES
    prefix = (
        _COMMIT_STATE_MAGIC
        + struct.pack(">I", len(payload))
        + payload
    )
    if len(prefix) > body_size:
        raise EncryptedBackupError("encrypted_backup_state_invalid")
    body = prefix + (b"\x00" * (body_size - len(prefix)))
    return body + hashlib.sha256(body).digest()


def _positive_record_integer(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
    )


def _decode_commit_state(encoded: bytes) -> _ArtifactCommitState:
    if len(encoded) != _COMMIT_STATE_BYTES:
        raise EncryptedBackupError("encrypted_backup_state_invalid")
    body = encoded[:-_COMMIT_STATE_DIGEST_BYTES]
    digest = encoded[-_COMMIT_STATE_DIGEST_BYTES:]
    if not hmac.compare_digest(hashlib.sha256(body).digest(), digest):
        raise EncryptedBackupError("encrypted_backup_state_invalid")
    if not body.startswith(_COMMIT_STATE_MAGIC):
        raise EncryptedBackupError("encrypted_backup_state_invalid")
    length_start = len(_COMMIT_STATE_MAGIC)
    length_end = length_start + 4
    payload_length = struct.unpack(
        ">I",
        body[length_start:length_end],
    )[0]
    payload_end = length_end + payload_length
    if (
        payload_length <= 0
        or payload_end > len(body)
        or any(body[payload_end:])
    ):
        raise EncryptedBackupError("encrypted_backup_state_invalid")
    encoded_payload = body[length_end:payload_end]
    try:
        payload = json.loads(encoded_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EncryptedBackupError(
            "encrypted_backup_state_invalid"
        ) from None
    if (
        not isinstance(payload, dict)
        or _canonical_json(payload) != encoded_payload
        or set(payload)
        != {
            "artifact_device",
            "artifact_inode",
            "artifact_name",
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
    if (
        not isinstance(phase, str)
        or phase not in _COMMIT_STATE_PHASES
        or type(generation) is not int
        or generation != _COMMIT_STATE_PHASES[phase]
        or not isinstance(transaction_id, str)
        or _TRANSACTION_ID.fullmatch(transaction_id) is None
        or not isinstance(artifact_name, str)
        or _COMMITTED_NAME.fullmatch(artifact_name) is None
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
    return flags


@contextmanager
def _locked_state_descriptor(
    state_path: Path,
    *,
    writable: bool,
):
    descriptor = os.open(
        state_path,
        _state_open_flags(writable=writable),
    )
    locked = False
    try:
        fcntl.flock(
            descriptor,
            fcntl.LOCK_EX if writable else fcntl.LOCK_SH,
        )
        locked = True
        descriptor_stat = os.fstat(descriptor)
        path_stat = state_path.lstat()
        if not (
            stat.S_ISREG(descriptor_stat.st_mode)
            and stat.S_ISREG(path_stat.st_mode)
            and descriptor_stat.st_dev == path_stat.st_dev
            and descriptor_stat.st_ino == path_stat.st_ino
        ):
            raise EncryptedBackupError(
                "encrypted_backup_state_invalid"
            )
        yield descriptor, descriptor_stat
    finally:
        try:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _read_commit_state(
    descriptor: int,
    descriptor_stat: os.stat_result,
    state_path: Path,
) -> _ArtifactCommitState:
    if descriptor_stat.st_size != _COMMIT_STATE_BYTES:
        raise EncryptedBackupError("encrypted_backup_state_invalid")
    encoded = os.pread(descriptor, _COMMIT_STATE_BYTES + 1, 0)
    state = _decode_commit_state(encoded)
    expected_name = state_path.name
    if not (
        expected_name.startswith(".")
        and expected_name.endswith(".commit-state")
        and state.artifact_name
        == expected_name[1 : -len(".commit-state")]
        and state.state_device == descriptor_stat.st_dev
        and state.state_inode == descriptor_stat.st_ino
    ):
        raise EncryptedBackupError("encrypted_backup_state_invalid")
    return state


def _write_commit_state(
    descriptor: int,
    state: _ArtifactCommitState,
    *,
    verify_readback: bool,
) -> None:
    encoded = _encode_commit_state(state)
    written = os.pwrite(descriptor, encoded, 0)
    if written != len(encoded):
        raise EncryptedBackupError("encrypted_backup_state_write_failed")
    os.fsync(descriptor)
    if (
        verify_readback
        and os.pread(descriptor, _COMMIT_STATE_BYTES + 1, 0) != encoded
    ):
        raise EncryptedBackupError("encrypted_backup_state_write_failed")


def _open_matching_artifact(
    path: Path,
    state: _ArtifactCommitState,
) -> int:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
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
            raise EncryptedBackupError(
                "encrypted_backup_state_invalid"
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _locked_artifact_state(
    path: Path,
    *,
    writable: bool,
):
    state_path = _commit_state_path(path)
    with _locked_state_descriptor(
        state_path,
        writable=writable,
    ) as (state_descriptor, state_stat):
        state = _read_commit_state(
            state_descriptor,
            state_stat,
            state_path,
        )
        artifact_descriptor = _open_matching_artifact(path, state)
        try:
            yield state, state_descriptor, artifact_descriptor
        finally:
            os.close(artifact_descriptor)


def _authoritative_state(
    path: Path,
    phase: Literal["PENDING", "COMMITTED", "RETIRED"],
    *,
    transaction_id: str | None = None,
) -> _ArtifactCommitState | None:
    try:
        with _locked_artifact_state(
            path,
            writable=False,
        ) as (state, _state_descriptor, _artifact_descriptor):
            if (
                state.phase == phase
                and (
                    transaction_id is None
                    or state.transaction_id == transaction_id
                )
            ):
                return state
    except (EncryptedBackupError, OSError):
        pass
    return None


def _is_authoritative_phase(
    path: Path,
    phase: Literal["PENDING", "COMMITTED", "RETIRED"],
    *,
    transaction_id: str | None = None,
) -> bool:
    return (
        _authoritative_state(
            path,
            phase,
            transaction_id=transaction_id,
        )
        is not None
    )


def _is_committed_artifact(path: Path) -> bool:
    return (
        _COMMITTED_NAME.fullmatch(path.name) is not None
        and _is_authoritative_phase(path, "COMMITTED")
    )


@contextmanager
def _open_committed_artifact(path: Path):
    if _COMMITTED_NAME.fullmatch(path.name) is None:
        raise EncryptedBackupError("encrypted_backup_not_committed")
    try:
        with _locked_artifact_state(
            path,
            writable=False,
        ) as (state, _state_descriptor, artifact_descriptor):
            if state.phase != "COMMITTED":
                raise EncryptedBackupError(
                    "encrypted_backup_not_committed"
                )
            with os.fdopen(os.dup(artifact_descriptor), "rb") as handle:
                yield handle
    except EncryptedBackupError as exc:
        if exc.stable_code == "encrypted_backup_not_committed":
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
    anchor_stat: os.stat_result,
    transaction_id: str,
) -> _ArtifactCommitState:
    state_path = _commit_state_path(target)
    descriptor = os.open(
        state_path,
        _state_open_flags(writable=True) | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    locked = False
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        state_stat = os.fstat(descriptor)
        path_stat = state_path.lstat()
        if not (
            stat.S_ISREG(anchor_stat.st_mode)
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
            artifact_device=anchor_stat.st_dev,
            artifact_inode=anchor_stat.st_ino,
            artifact_size=anchor_stat.st_size,
            state_device=state_stat.st_dev,
            state_inode=state_stat.st_ino,
        )
        _write_commit_state(
            descriptor,
            state,
            verify_readback=True,
        )
        return state
    finally:
        try:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
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
    with _locked_artifact_state(
        target,
        writable=True,
    ) as (current, state_descriptor, _artifact_descriptor):
        if current != expected:
            raise EncryptedBackupError(
                "encrypted_backup_state_invalid"
            )
        transitioned = replace(
            current,
            phase=phase,
            generation=next_generation,
        )
        _write_commit_state(
            state_descriptor,
            transitioned,
            verify_readback=False,
        )
        return transitioned


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


def _private_temp(directory: Path, prefix: str) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=prefix, dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    return Path(name)


def _hash_file(
    path: Path,
    *,
    ensure_maintenance: Callable[[], None] | None = None,
) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as handle:
        while True:
            if ensure_maintenance is not None:
                ensure_maintenance()
            chunk = handle.read(_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


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
    snapshot: Path | None = None
    encrypted_temp: Path | None = None
    verification: Path | None = None
    target: Path | None = None
    anchor: Path | None = None
    anchor_identity: tuple[int, int] | None = None
    pending_state: _ArtifactCommitState | None = None
    receipt: EncryptedBackupReceipt | None = None
    directory: Path | None = None
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

        check_snapshot()
        snapshot = _private_temp(directory, ".sensitive-snapshot-")
        source_uri = f"{source_path.as_uri()}?mode=ro"
        with (
            sqlite3.connect(source_uri, uri=True) as source_connection,
            sqlite3.connect(snapshot) as snapshot_connection,
        ):
            source_connection.backup(
                snapshot_connection,
                pages=256,
                progress=lambda _status, _remaining, _total: (
                    check_snapshot()
                ),
            )
            snapshot_connection.execute(
                "PRAGMA journal_mode=DELETE"
            ).fetchone()
        os.chmod(snapshot, 0o600)
        complete_snapshot()
        hook("snapshot_created")
        maintain()

        source_sha256 = _hash_file(
            snapshot,
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
        encrypted_temp = _private_temp(directory, ".sensitive-encrypted-")
        encryptor = Cipher(
            algorithms.AES(backup_key),
            modes.GCM(nonce),
        ).encryptor()
        encryptor.authenticate_additional_data(aad)
        with (
            snapshot.open("rb", buffering=0) as plaintext,
            encrypted_temp.open("r+b", buffering=0) as ciphertext,
        ):
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
        hook("ciphertext_fsynced")
        maintain()

        verification = _private_temp(directory, ".sensitive-verify-")
        hook("verification_opened")
        with (
            encrypted_temp.open("rb", buffering=0) as ciphertext,
            verification.open("r+b", buffering=0) as plaintext,
        ):
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
        hook("quick_check_complete")
        maintain()

        stamp = created.strftime("%Y%m%dT%H%M%S%fZ")
        target = directory / f"{stamp}-{artifact_label}.sqlite3.aesgcm"
        anchor = _pending_anchor(target)
        state_path = _commit_state_path(target)
        if (
            target.exists()
            or target.is_symlink()
            or anchor.exists()
            or anchor.is_symlink()
            or state_path.exists()
            or state_path.is_symlink()
        ):
            raise EncryptedBackupError("encrypted_backup_exists")

        # Plaintext cleanup and every maintenance callback complete before the
        # publication transaction. The hidden anchor and fixed-size state file
        # are made durable while the state is PENDING. A public target can then
        # be safely orphaned after an ambiguous link because official readers
        # require a valid, inode-bound COMMITTED state record.
        _unlink_private_temp(snapshot)
        snapshot = None
        _unlink_private_temp(verification)
        verification = None
        try:
            os.link(encrypted_temp, anchor, follow_symlinks=False)
        except FileExistsError:
            raise EncryptedBackupError("encrypted_backup_exists") from None
        anchor_stat = anchor.lstat()
        if not stat.S_ISREG(anchor_stat.st_mode):
            raise EncryptedBackupError("encrypted_backup_state_invalid")
        anchor_identity = (anchor_stat.st_dev, anchor_stat.st_ino)
        _unlink_private_temp(encrypted_temp)
        encrypted_temp = None
        _fsync_directory(directory)

        transaction_id = os.urandom(16).hex()
        try:
            pending_state = _create_pending_commit_state(
                target,
                anchor_stat,
                transaction_id,
            )
        except FileExistsError:
            raise EncryptedBackupError("encrypted_backup_exists") from None
        _fsync_directory(directory)
        hook("pending_state_durable")

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
        try:
            os.link(anchor, target, follow_symlinks=False)
        except FileExistsError:
            raise EncryptedBackupError("encrypted_backup_exists") from None
        hook("target_linked_pending")
        _fsync_directory(directory)
        hook("target_directory_fsynced")
        _transition_commit_state(
            target,
            pending_state,
            "COMMITTED",
        )
        return receipt
    except BaseException as exc:
        if (
            receipt is not None
            and target is not None
            and pending_state is not None
            and _is_authoritative_phase(
                target,
                "COMMITTED",
                transaction_id=pending_state.transaction_id,
            )
        ):
            return receipt
        cleanup_failure: BaseException | None = None
        for private_path in (
            snapshot,
            encrypted_temp,
            verification,
        ):
            try:
                _unlink_private_temp(private_path)
            except BaseException as cleanup_exc:
                if cleanup_failure is None:
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
        elif anchor is not None and anchor_identity is not None:
            try:
                _unlink_matching_regular_file(
                    anchor,
                    device=anchor_identity[0],
                    inode=anchor_identity[1],
                )
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


def _retire_committed_backup(
    candidate: Path,
) -> _ArtifactCommitState:
    committed = _authoritative_state(candidate, "COMMITTED")
    if committed is None:
        raise EncryptedBackupError("encrypted_backup_not_committed")
    try:
        return _transition_commit_state(
            candidate,
            committed,
            "RETIRED",
        )
    except BaseException:
        retired = _authoritative_state(
            candidate,
            "RETIRED",
            transaction_id=committed.transaction_id,
        )
        if retired is not None:
            return retired
        raise


def _committed_artifact_mtime(candidate: Path) -> float | None:
    try:
        with _locked_artifact_state(
            candidate,
            writable=False,
        ) as (state, _state_descriptor, artifact_descriptor):
            if state.phase != "COMMITTED":
                return None
            return os.fstat(artifact_descriptor).st_mtime
    except (EncryptedBackupError, OSError):
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
        _prune_committed_backups(
            destination_dir,
            artifact_label="whole-database-v1",
            cutoff=time.time() - retention_days * 86400,
        )
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
