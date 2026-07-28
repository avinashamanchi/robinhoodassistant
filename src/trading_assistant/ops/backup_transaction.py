"""Private crash-safe filesystem transaction support for encrypted backups."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import struct
import time


BACKUP_CHUNK_BYTES = 1_048_576
TRANSACTION_PREFIX = ".backup-txn-"
TRANSACTION_DIRECTORY = re.compile(
    rf"{re.escape(TRANSACTION_PREFIX)}([0-9a-f]{{32}})"
)
TRANSACTION_MANIFEST_MAGIC = b"TA-BACKUP-TRANSACTION\x00"
TRANSACTION_MANIFEST_VERSION = 1
TRANSACTION_MANIFEST_BYTES = 512
TRANSACTION_MANIFEST_NAME = "manifest"
SNAPSHOT_NAME = "snapshot.sqlite3"
VERIFICATION_NAME = "verification.sqlite3"
ENCRYPTED_NAME = "encrypted.aesgcm"
TRANSACTION_MEMBER_NAMES = frozenset(
    {
        TRANSACTION_MANIFEST_NAME,
        SNAPSHOT_NAME,
        f"{SNAPSHOT_NAME}-journal",
        f"{SNAPSHOT_NAME}-shm",
        f"{SNAPSHOT_NAME}-wal",
        VERIFICATION_NAME,
        f"{VERIFICATION_NAME}-journal",
        f"{VERIFICATION_NAME}-shm",
        f"{VERIFICATION_NAME}-wal",
        ENCRYPTED_NAME,
    }
)
_RECORD_DIGEST_BYTES = hashlib.sha256().digest_size
_TRANSACTION_ID = re.compile(r"[0-9a-f]{32}")
_LOCK_TIMEOUT_SECONDS = 0.25
_LOCK_RETRY_SECONDS = 0.005


class EncryptedBackupError(RuntimeError):
    """Stable, content-free failure from encrypted backup handling."""

    def __init__(self, stable_code: str) -> None:
        self.stable_code = stable_code
        super().__init__(stable_code)


@dataclass(frozen=True)
class _BackupTransactionManifest:
    transaction_id: str
    directory_name: str
    directory_device: int
    directory_inode: int


@dataclass
class BackupTransaction:
    destination: Path
    destination_descriptor: int
    directory: Path
    directory_descriptor: int
    manifest_descriptor: int
    manifest: _BackupTransactionManifest
    manifest_locked: bool = True


def _canonical_json(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def encode_checksummed_record(
    magic: bytes,
    payload: dict[str, object],
    *,
    record_bytes: int,
    invalid_code: str,
) -> bytes:
    encoded_payload = _canonical_json(payload)
    body_size = record_bytes - _RECORD_DIGEST_BYTES
    prefix = magic + struct.pack(">I", len(encoded_payload)) + encoded_payload
    if len(prefix) > body_size:
        raise EncryptedBackupError(invalid_code)
    body = prefix + (b"\x00" * (body_size - len(prefix)))
    return body + hashlib.sha256(body).digest()


def decode_checksummed_record(
    encoded: bytes,
    magic: bytes,
    *,
    record_bytes: int,
    invalid_code: str,
) -> dict[str, object]:
    if len(encoded) != record_bytes:
        raise EncryptedBackupError(invalid_code)
    body = encoded[:-_RECORD_DIGEST_BYTES]
    digest = encoded[-_RECORD_DIGEST_BYTES:]
    if not hmac.compare_digest(hashlib.sha256(body).digest(), digest):
        raise EncryptedBackupError(invalid_code)
    if not body.startswith(magic):
        raise EncryptedBackupError(invalid_code)
    length_start = len(magic)
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
        raise EncryptedBackupError(invalid_code)
    encoded_payload = body[length_end:payload_end]
    try:
        payload = json.loads(encoded_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EncryptedBackupError(invalid_code) from None
    if (
        not isinstance(payload, dict)
        or _canonical_json(payload) != encoded_payload
    ):
        raise EncryptedBackupError(invalid_code)
    return payload


def positive_record_integer(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
    )


def retry_flock(descriptor: int, operation: int) -> None:
    while True:
        try:
            fcntl.flock(descriptor, operation)
            return
        except InterruptedError:
            continue


def acquire_bounded_lock(
    descriptor: int,
    operation: int,
    *,
    timeout_seconds: float = _LOCK_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
            return
        except InterruptedError:
            continue
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EncryptedBackupError(
                    "encrypted_backup_state_busy"
                ) from None
            time.sleep(min(_LOCK_RETRY_SECONDS, remaining))
        except OSError:
            raise EncryptedBackupError(
                "encrypted_backup_state_unavailable"
            ) from None


def retry_fsync(descriptor: int) -> None:
    while True:
        try:
            os.fsync(descriptor)
            return
        except InterruptedError:
            continue


def pread_retry(descriptor: int, length: int, offset: int) -> bytes:
    while True:
        try:
            return os.pread(descriptor, length, offset)
        except InterruptedError:
            continue


def pread_exact(descriptor: int, length: int, offset: int) -> bytes:
    result = bytearray()
    while len(result) < length:
        chunk = pread_retry(
            descriptor,
            length - len(result),
            offset + len(result),
        )
        if not chunk:
            break
        result.extend(chunk)
    return bytes(result)


def pwrite_all(descriptor: int, encoded: bytes) -> None:
    offset = 0
    while offset < len(encoded):
        while True:
            try:
                written = os.pwrite(
                    descriptor,
                    encoded[offset:],
                    offset,
                )
                break
            except InterruptedError:
                continue
        if written <= 0:
            raise EncryptedBackupError(
                "encrypted_backup_state_write_failed"
            )
        offset += written


def hash_artifact_descriptor(
    descriptor: int,
    *,
    expected_size: int,
) -> str:
    initial_stat = os.fstat(descriptor)
    if not (
        stat.S_ISREG(initial_stat.st_mode)
        and initial_stat.st_size == expected_size
    ):
        raise EncryptedBackupError("encrypted_backup_state_invalid")
    digest = hashlib.sha256()
    offset = 0
    while offset < expected_size:
        chunk = pread_retry(
            descriptor,
            min(BACKUP_CHUNK_BYTES, expected_size - offset),
            offset,
        )
        if not chunk:
            raise EncryptedBackupError(
                "encrypted_backup_state_invalid"
            )
        digest.update(chunk)
        offset += len(chunk)
    final_stat = os.fstat(descriptor)
    if not (
        initial_stat.st_dev == final_stat.st_dev
        and initial_stat.st_ino == final_stat.st_ino
        and initial_stat.st_size == final_stat.st_size
        and initial_stat.st_mtime_ns == final_stat.st_mtime_ns
        and initial_stat.st_ctime_ns == final_stat.st_ctime_ns
    ):
        raise EncryptedBackupError("encrypted_backup_state_invalid")
    return digest.hexdigest()


def _manifest_payload(
    manifest: _BackupTransactionManifest,
) -> dict[str, object]:
    return {
        "directory_device": manifest.directory_device,
        "directory_inode": manifest.directory_inode,
        "directory_name": manifest.directory_name,
        "transaction_id": manifest.transaction_id,
        "version": TRANSACTION_MANIFEST_VERSION,
    }


def _encode_manifest(manifest: _BackupTransactionManifest) -> bytes:
    return encode_checksummed_record(
        TRANSACTION_MANIFEST_MAGIC,
        _manifest_payload(manifest),
        record_bytes=TRANSACTION_MANIFEST_BYTES,
        invalid_code="encrypted_backup_transaction_invalid",
    )


def _decode_manifest(encoded: bytes) -> _BackupTransactionManifest:
    payload = decode_checksummed_record(
        encoded,
        TRANSACTION_MANIFEST_MAGIC,
        record_bytes=TRANSACTION_MANIFEST_BYTES,
        invalid_code="encrypted_backup_transaction_invalid",
    )
    if (
        set(payload)
        != {
            "directory_device",
            "directory_inode",
            "directory_name",
            "transaction_id",
            "version",
        }
        or type(payload.get("version")) is not int
        or payload.get("version") != TRANSACTION_MANIFEST_VERSION
    ):
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    transaction_id = payload.get("transaction_id")
    directory_name = payload.get("directory_name")
    if (
        not isinstance(transaction_id, str)
        or _TRANSACTION_ID.fullmatch(transaction_id) is None
        or not isinstance(directory_name, str)
        or TRANSACTION_DIRECTORY.fullmatch(directory_name) is None
        or directory_name != f"{TRANSACTION_PREFIX}{transaction_id}"
        or not positive_record_integer(payload.get("directory_device"))
        or not positive_record_integer(payload.get("directory_inode"))
    ):
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    return _BackupTransactionManifest(
        transaction_id=transaction_id,
        directory_name=directory_name,
        directory_device=payload["directory_device"],
        directory_inode=payload["directory_inode"],
    )


def directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _destination_identity(
    destination: Path,
    descriptor: int,
) -> os.stat_result:
    descriptor_stat = os.fstat(descriptor)
    path_stat = destination.lstat()
    if not (
        stat.S_ISDIR(descriptor_stat.st_mode)
        and stat.S_ISDIR(path_stat.st_mode)
        and descriptor_stat.st_dev == path_stat.st_dev
        and descriptor_stat.st_ino == path_stat.st_ino
        and stat.S_IMODE(descriptor_stat.st_mode) == 0o700
    ):
        raise EncryptedBackupError("backup_directory_invalid")
    return descriptor_stat


def _directory_identity(
    transaction: BackupTransaction,
) -> os.stat_result:
    _destination_identity(
        transaction.destination,
        transaction.destination_descriptor,
    )
    descriptor_stat = os.fstat(transaction.directory_descriptor)
    path_stat = os.stat(
        transaction.manifest.directory_name,
        dir_fd=transaction.destination_descriptor,
        follow_symlinks=False,
    )
    if not (
        stat.S_ISDIR(descriptor_stat.st_mode)
        and stat.S_ISDIR(path_stat.st_mode)
        and descriptor_stat.st_dev == path_stat.st_dev
        and descriptor_stat.st_ino == path_stat.st_ino
        and descriptor_stat.st_dev
        == transaction.manifest.directory_device
        and descriptor_stat.st_ino
        == transaction.manifest.directory_inode
        and stat.S_IMODE(descriptor_stat.st_mode) == 0o700
    ):
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    return descriptor_stat


def _manifest_identity(
    transaction: BackupTransaction,
) -> os.stat_result:
    descriptor_stat = os.fstat(transaction.manifest_descriptor)
    path_stat = os.stat(
        TRANSACTION_MANIFEST_NAME,
        dir_fd=transaction.directory_descriptor,
        follow_symlinks=False,
    )
    if not (
        stat.S_ISREG(descriptor_stat.st_mode)
        and stat.S_ISREG(path_stat.st_mode)
        and descriptor_stat.st_dev == path_stat.st_dev
        and descriptor_stat.st_ino == path_stat.st_ino
        and descriptor_stat.st_size == TRANSACTION_MANIFEST_BYTES
        and stat.S_IMODE(descriptor_stat.st_mode) == 0o600
    ):
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    return descriptor_stat


def _read_manifest(
    transaction: BackupTransaction,
) -> _BackupTransactionManifest:
    _directory_identity(transaction)
    before = _manifest_identity(transaction)
    manifest = _decode_manifest(
        pread_exact(
            transaction.manifest_descriptor,
            TRANSACTION_MANIFEST_BYTES,
            0,
        )
    )
    after = _manifest_identity(transaction)
    if not (
        manifest == transaction.manifest
        and before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_ctime_ns == after.st_ctime_ns
    ):
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    return manifest


def validate_backup_transaction(
    transaction: BackupTransaction,
) -> set[str]:
    _read_manifest(transaction)
    members = set(os.listdir(transaction.directory_descriptor))
    if (
        TRANSACTION_MANIFEST_NAME not in members
        or not members <= TRANSACTION_MEMBER_NAMES
    ):
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    return members


def create_backup_transaction(destination: Path) -> BackupTransaction:
    destination_descriptor = os.open(destination, directory_open_flags())
    directory_descriptor: int | None = None
    manifest_descriptor: int | None = None
    manifest_locked = False
    directory_name: str | None = None
    try:
        _destination_identity(destination, destination_descriptor)
        transaction_id = os.urandom(16).hex()
        directory_name = f"{TRANSACTION_PREFIX}{transaction_id}"
        os.mkdir(
            directory_name,
            mode=0o700,
            dir_fd=destination_descriptor,
        )
        directory_descriptor = os.open(
            directory_name,
            directory_open_flags(),
            dir_fd=destination_descriptor,
        )
        os.fchmod(directory_descriptor, 0o700)
        directory_stat = os.fstat(directory_descriptor)
        directory_path_stat = os.stat(
            directory_name,
            dir_fd=destination_descriptor,
            follow_symlinks=False,
        )
        if not (
            stat.S_ISDIR(directory_stat.st_mode)
            and stat.S_ISDIR(directory_path_stat.st_mode)
            and directory_stat.st_dev == directory_path_stat.st_dev
            and directory_stat.st_ino == directory_path_stat.st_ino
        ):
            raise EncryptedBackupError(
                "encrypted_backup_transaction_invalid"
            )
        manifest = _BackupTransactionManifest(
            transaction_id=transaction_id,
            directory_name=directory_name,
            directory_device=directory_stat.st_dev,
            directory_inode=directory_stat.st_ino,
        )
        manifest_descriptor = os.open(
            TRANSACTION_MANIFEST_NAME,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_DSYNC", os.O_SYNC),
            0o600,
            dir_fd=directory_descriptor,
        )
        os.fchmod(manifest_descriptor, 0o600)
        acquire_bounded_lock(manifest_descriptor, fcntl.LOCK_EX)
        manifest_locked = True
        transaction = BackupTransaction(
            destination=destination,
            destination_descriptor=destination_descriptor,
            directory=destination / directory_name,
            directory_descriptor=directory_descriptor,
            manifest_descriptor=manifest_descriptor,
            manifest=manifest,
        )
        pwrite_all(manifest_descriptor, _encode_manifest(manifest))
        retry_fsync(manifest_descriptor)
        _read_manifest(transaction)
        retry_fsync(directory_descriptor)
        retry_fsync(destination_descriptor)
        return transaction
    except BaseException:
        if manifest_descriptor is not None and manifest_locked:
            try:
                retry_flock(manifest_descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        if manifest_descriptor is not None:
            try:
                os.close(manifest_descriptor)
            except OSError:
                pass
        if directory_descriptor is not None:
            try:
                os.unlink(
                    TRANSACTION_MANIFEST_NAME,
                    dir_fd=directory_descriptor,
                )
            except OSError:
                pass
            try:
                os.close(directory_descriptor)
            except OSError:
                pass
        if directory_name is not None:
            try:
                os.rmdir(
                    directory_name,
                    dir_fd=destination_descriptor,
                )
                retry_fsync(destination_descriptor)
            except OSError:
                pass
        os.close(destination_descriptor)
        raise


def create_transaction_member(
    transaction: BackupTransaction,
    name: str,
) -> Path:
    if name == TRANSACTION_MANIFEST_NAME or name not in TRANSACTION_MEMBER_NAMES:
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    validate_backup_transaction(transaction)
    descriptor = os.open(
        name,
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
        0o600,
        dir_fd=transaction.directory_descriptor,
    )
    try:
        os.fchmod(descriptor, 0o600)
        retry_fsync(descriptor)
    finally:
        os.close(descriptor)
    retry_fsync(transaction.directory_descriptor)
    validate_backup_transaction(transaction)
    return transaction.directory / name


def open_transaction_member(
    transaction: BackupTransaction,
    name: str,
    flags: int,
) -> int:
    if name == TRANSACTION_MANIFEST_NAME or name not in TRANSACTION_MEMBER_NAMES:
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    validate_backup_transaction(transaction)
    descriptor = os.open(
        name,
        flags
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
        dir_fd=transaction.directory_descriptor,
    )
    try:
        descriptor_stat = os.fstat(descriptor)
        path_stat = os.stat(
            name,
            dir_fd=transaction.directory_descriptor,
            follow_symlinks=False,
        )
        if not (
            stat.S_ISREG(descriptor_stat.st_mode)
            and stat.S_ISREG(path_stat.st_mode)
            and descriptor_stat.st_dev == path_stat.st_dev
            and descriptor_stat.st_ino == path_stat.st_ino
            and stat.S_IMODE(descriptor_stat.st_mode) == 0o600
        ):
            raise EncryptedBackupError(
                "encrypted_backup_transaction_invalid"
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def hash_transaction_member(
    transaction: BackupTransaction,
    name: str,
    *,
    ensure_maintenance: Callable[[], None] | None = None,
) -> str:
    digest = hashlib.sha256()
    descriptor = open_transaction_member(
        transaction,
        name,
        os.O_RDONLY,
    )
    with os.fdopen(descriptor, "rb", buffering=0) as handle:
        while True:
            if ensure_maintenance is not None:
                ensure_maintenance()
            chunk = handle.read(BACKUP_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def fsync_and_hash_transaction_artifact(
    transaction: BackupTransaction,
) -> tuple[os.stat_result, str]:
    descriptor = open_transaction_member(
        transaction,
        ENCRYPTED_NAME,
        os.O_RDONLY,
    )
    try:
        descriptor_stat = os.fstat(descriptor)
        path_stat = os.stat(
            ENCRYPTED_NAME,
            dir_fd=transaction.directory_descriptor,
            follow_symlinks=False,
        )
        if not (
            stat.S_ISREG(descriptor_stat.st_mode)
            and stat.S_ISREG(path_stat.st_mode)
            and descriptor_stat.st_dev == path_stat.st_dev
            and descriptor_stat.st_ino == path_stat.st_ino
        ):
            raise EncryptedBackupError(
                "encrypted_backup_state_invalid"
            )
        retry_fsync(descriptor)
        artifact_sha256 = hash_artifact_descriptor(
            descriptor,
            expected_size=descriptor_stat.st_size,
        )
        final_stat = os.fstat(descriptor)
        final_path_stat = os.stat(
            ENCRYPTED_NAME,
            dir_fd=transaction.directory_descriptor,
            follow_symlinks=False,
        )
        if not (
            descriptor_stat.st_dev == final_stat.st_dev
            and descriptor_stat.st_ino == final_stat.st_ino
            and descriptor_stat.st_size == final_stat.st_size
            and descriptor_stat.st_mtime_ns == final_stat.st_mtime_ns
            and descriptor_stat.st_ctime_ns == final_stat.st_ctime_ns
            and final_stat.st_dev == final_path_stat.st_dev
            and final_stat.st_ino == final_path_stat.st_ino
        ):
            raise EncryptedBackupError(
                "encrypted_backup_state_invalid"
            )
        validate_backup_transaction(transaction)
        return final_stat, artifact_sha256
    finally:
        os.close(descriptor)


def _remove_backup_transaction(transaction: BackupTransaction) -> None:
    members = validate_backup_transaction(transaction)
    for name in sorted(members - {TRANSACTION_MANIFEST_NAME}):
        member_stat = os.stat(
            name,
            dir_fd=transaction.directory_descriptor,
            follow_symlinks=False,
        )
        if not (
            stat.S_ISREG(member_stat.st_mode)
            and stat.S_IMODE(member_stat.st_mode) == 0o600
        ):
            raise EncryptedBackupError(
                "encrypted_backup_transaction_invalid"
            )
        os.unlink(name, dir_fd=transaction.directory_descriptor)
    retry_fsync(transaction.directory_descriptor)
    _read_manifest(transaction)
    _directory_identity(transaction)
    os.unlink(
        TRANSACTION_MANIFEST_NAME,
        dir_fd=transaction.directory_descriptor,
    )
    retry_fsync(transaction.directory_descriptor)
    os.rmdir(
        transaction.manifest.directory_name,
        dir_fd=transaction.destination_descriptor,
    )
    retry_fsync(transaction.destination_descriptor)


def close_backup_transaction(
    transaction: BackupTransaction,
    *,
    remove: bool,
) -> None:
    failure: BaseException | None = None
    if remove:
        try:
            _remove_backup_transaction(transaction)
        except BaseException as exc:
            failure = exc
    if transaction.manifest_locked:
        try:
            retry_flock(
                transaction.manifest_descriptor,
                fcntl.LOCK_UN,
            )
        except BaseException as exc:
            if failure is None:
                failure = exc
        transaction.manifest_locked = False
    for descriptor in (
        transaction.manifest_descriptor,
        transaction.directory_descriptor,
        transaction.destination_descriptor,
    ):
        try:
            os.close(descriptor)
        except BaseException as exc:
            if failure is None:
                failure = exc
    if failure is not None:
        raise failure


def recover_backup_transaction(
    destination: Path,
    directory_name: str,
    *,
    cutoff: float,
) -> None:
    if TRANSACTION_DIRECTORY.fullmatch(directory_name) is None:
        return
    destination_descriptor: int | None = None
    directory_descriptor: int | None = None
    manifest_descriptor: int | None = None
    manifest_locked = False
    transaction: BackupTransaction | None = None
    try:
        destination_descriptor = os.open(
            destination,
            directory_open_flags(),
        )
        _destination_identity(destination, destination_descriptor)
        directory_descriptor = os.open(
            directory_name,
            directory_open_flags(),
            dir_fd=destination_descriptor,
        )
        directory_stat = os.fstat(directory_descriptor)
        directory_path_stat = os.stat(
            directory_name,
            dir_fd=destination_descriptor,
            follow_symlinks=False,
        )
        if not (
            stat.S_ISDIR(directory_stat.st_mode)
            and stat.S_ISDIR(directory_path_stat.st_mode)
            and directory_stat.st_dev == directory_path_stat.st_dev
            and directory_stat.st_ino == directory_path_stat.st_ino
            and stat.S_IMODE(directory_stat.st_mode) == 0o700
            and directory_stat.st_mtime < cutoff
        ):
            return
        manifest_descriptor = os.open(
            TRANSACTION_MANIFEST_NAME,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_descriptor,
        )
        manifest_stat = os.fstat(manifest_descriptor)
        manifest_path_stat = os.stat(
            TRANSACTION_MANIFEST_NAME,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if not (
            stat.S_ISREG(manifest_stat.st_mode)
            and stat.S_ISREG(manifest_path_stat.st_mode)
            and manifest_stat.st_dev == manifest_path_stat.st_dev
            and manifest_stat.st_ino == manifest_path_stat.st_ino
            and manifest_stat.st_size == TRANSACTION_MANIFEST_BYTES
            and stat.S_IMODE(manifest_stat.st_mode) == 0o600
            and manifest_stat.st_mtime < cutoff
        ):
            return
        acquire_bounded_lock(manifest_descriptor, fcntl.LOCK_EX)
        manifest_locked = True
        manifest = _decode_manifest(
            pread_exact(
                manifest_descriptor,
                TRANSACTION_MANIFEST_BYTES,
                0,
            )
        )
        if not (
            manifest.directory_name == directory_name
            and manifest.directory_device == directory_stat.st_dev
            and manifest.directory_inode == directory_stat.st_ino
        ):
            return
        transaction = BackupTransaction(
            destination=destination,
            destination_descriptor=destination_descriptor,
            directory=destination / directory_name,
            directory_descriptor=directory_descriptor,
            manifest_descriptor=manifest_descriptor,
            manifest=manifest,
        )
        members = validate_backup_transaction(transaction)
        if any(
            os.stat(
                member,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            ).st_mtime
            >= cutoff
            for member in members
        ):
            return
        final_directory_stat = _directory_identity(transaction)
        if not (
            directory_stat.st_mtime_ns
            == final_directory_stat.st_mtime_ns
            and directory_stat.st_ctime_ns
            == final_directory_stat.st_ctime_ns
            and final_directory_stat.st_mtime < cutoff
        ):
            return
        close_backup_transaction(transaction, remove=True)
        transaction = None
        destination_descriptor = None
        directory_descriptor = None
        manifest_descriptor = None
        manifest_locked = False
    except (EncryptedBackupError, FileNotFoundError, OSError):
        return
    finally:
        if transaction is not None:
            try:
                close_backup_transaction(transaction, remove=False)
            except OSError:
                pass
        else:
            if manifest_descriptor is not None and manifest_locked:
                try:
                    retry_flock(
                        manifest_descriptor,
                        fcntl.LOCK_UN,
                    )
                except OSError:
                    pass
            for descriptor in (
                manifest_descriptor,
                directory_descriptor,
                destination_descriptor,
            ):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
