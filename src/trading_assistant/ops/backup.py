"""Verified encrypted online SQLite backups with bounded retention."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import os
import re
import sqlite3
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
    try:
        with Path(path).open("rb") as handle:
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
    published = False
    succeeded = False
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

        stamp = created.strftime("%Y%m%dT%H%M%S%fZ")
        target = directory / f"{stamp}-{artifact_label}.sqlite3.aesgcm"
        maintain()
        try:
            os.link(encrypted_temp, target, follow_symlinks=False)
        except FileExistsError:
            raise EncryptedBackupError("encrypted_backup_exists") from None
        published = True
        _fsync_directory(directory)
        hook("artifact_published")
        maintain()
        _unlink_private_temp(encrypted_temp)
        encrypted_temp = None
        _fsync_directory(directory)
        succeeded = True
        return EncryptedBackupReceipt(
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
        if isinstance(exc, _MaintenanceCallbackFailure):
            raise exc.cause
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, EncryptedBackupError):
            raise
        raise EncryptedBackupError("encrypted_backup_failed") from None
    finally:
        _unlink_private_temp(snapshot)
        _unlink_private_temp(encrypted_temp)
        _unlink_private_temp(verification)
        if published and not succeeded and target is not None:
            try:
                target.unlink(missing_ok=True)
                if directory is not None:
                    _fsync_directory(directory)
            except OSError:
                pass


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
        )
    except BaseException:
        primary_failure = True
        raise
    finally:
        released = guard.close()
        engine.dispose()
        if not released and not primary_failure:
            raise EncryptedBackupError(
                "backup_tenure_release_uncertain"
            )

    destination = Path(destination_dir).expanduser().resolve(strict=True)
    cutoff = time.time() - retention_days * 86400
    removed = False
    for candidate in destination.glob(
        "*-whole-database-v1.sqlite3.aesgcm"
    ):
        if (
            candidate != receipt.path
            and candidate.stat().st_mtime < cutoff
        ):
            candidate.unlink()
            removed = True
    if removed:
        _fsync_directory(destination)
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
