"""Private crash-safe filesystem transaction support for encrypted backups."""

from __future__ import annotations

from collections.abc import Callable
import ctypes
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
import time


BACKUP_CHUNK_BYTES = 1_048_576
TRANSACTION_PREFIX = ".backup-txn-"
TRANSACTION_DIRECTORY = re.compile(
    rf"{re.escape(TRANSACTION_PREFIX)}([0-9a-f]{{32}})"
)
QUARANTINE_PREFIX = ".backup-quarantine-"
QUARANTINE_DIRECTORY = re.compile(
    rf"{re.escape(QUARANTINE_PREFIX)}"
    r"([0-9a-f]{32})-([0-9a-f]{32})"
)
TRANSACTION_MANIFEST_MAGIC = b"TA-BACKUP-TRANSACTION\x00"
TRANSACTION_MANIFEST_VERSION = 3
TRANSACTION_MANIFEST_BYTES = 2048
TRANSACTION_MANIFEST_NAME = "manifest"
SNAPSHOT_NAME = "snapshot.sqlite3"
VERIFICATION_NAME = "verification.sqlite3"
ENCRYPTED_NAME = "encrypted.aesgcm"
OPERATION_MEMBER_NAMES = (
    SNAPSHOT_NAME,
    VERIFICATION_NAME,
    ENCRYPTED_NAME,
)
OWNERSHIP_LINK_PREFIX = ".backup-owner-"
OWNERSHIP_LINK_NAMES = frozenset(
    f"{OWNERSHIP_LINK_PREFIX}{name}" for name in OPERATION_MEMBER_NAMES
)
TRANSACTION_MEMBER_NAMES = frozenset(
    {
        TRANSACTION_MANIFEST_NAME,
        *OPERATION_MEMBER_NAMES,
        *OWNERSHIP_LINK_NAMES,
    }
)
_RECORD_DIGEST_BYTES = hashlib.sha256().digest_size
_TRANSACTION_ID = re.compile(r"[0-9a-f]{32}")
_LOCK_TIMEOUT_SECONDS = 0.25
_LOCK_RETRY_SECONDS = 0.005
_DARWIN_RENAME_EXCL = 0x00000004
_DARWIN_RENAME_NOFOLLOW_ANY = 0x00000010
_LINUX_RENAME_NOREPLACE = 1
_QUARANTINE_NAME_ATTEMPTS = 8


class EncryptedBackupError(RuntimeError):
    """Stable, content-free failure from encrypted backup handling."""

    def __init__(self, stable_code: str) -> None:
        self.stable_code = stable_code
        super().__init__(stable_code)


@dataclass(frozen=True)
class _BackupTransactionMember:
    name: str
    device: int
    inode: int
    file_type: str
    mode: int


@dataclass(frozen=True)
class _BackupTransactionManifest:
    transaction_id: str
    directory_name: str
    directory_device: int
    directory_inode: int
    manifest_device: int
    manifest_inode: int
    members: tuple[_BackupTransactionMember, ...]


@dataclass
class BackupTransaction:
    destination: Path
    destination_descriptor: int
    directory: Path
    directory_descriptor: int
    manifest_descriptor: int
    manifest: _BackupTransactionManifest
    manifest_locked: bool = True
    authorized_artifact_links: int = 0


@dataclass(frozen=True)
class _RecoveryMemberHandle:
    member: _BackupTransactionMember
    primary_descriptor: int | None
    ownership_descriptor: int
    initial_primary_stat: os.stat_result | None
    initial_ownership_stat: os.stat_result


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


def _rename_no_replace(
    parent_descriptor: int,
    source_name: str,
    destination_name: str,
) -> None:
    """Rename one basename atomically without replacing an existing entry."""

    if (
        not source_name
        or not destination_name
        or "/" in source_name
        or "/" in destination_name
        or source_name in {".", ".."}
        or destination_name in {".", ".."}
    ):
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        try:
            rename = library.renameatx_np
        except AttributeError:
            raise EncryptedBackupError(
                "encrypted_backup_transaction_unavailable"
            ) from None
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        flags = _DARWIN_RENAME_EXCL | _DARWIN_RENAME_NOFOLLOW_ANY
    elif sys.platform.startswith("linux"):
        try:
            rename = library.renameat2
        except AttributeError:
            raise EncryptedBackupError(
                "encrypted_backup_transaction_unavailable"
            ) from None
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        flags = _LINUX_RENAME_NOREPLACE
    else:
        raise EncryptedBackupError(
            "encrypted_backup_transaction_unavailable"
        )
    source = os.fsencode(source_name)
    destination = os.fsencode(destination_name)
    while True:
        ctypes.set_errno(0)
        result = rename(
            parent_descriptor,
            source,
            parent_descriptor,
            destination,
            flags,
        )
        if result == 0:
            return
        error_number = ctypes.get_errno()
        if error_number == errno.EINTR:
            continue
        if error_number <= 0:
            raise EncryptedBackupError(
                "encrypted_backup_transaction_unavailable"
            )
        raise OSError(
            error_number,
            os.strerror(error_number),
            source_name,
            destination_name,
        )


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
        "manifest_device": manifest.manifest_device,
        "manifest_inode": manifest.manifest_inode,
        "members": [
            {
                "device": member.device,
                "file_type": member.file_type,
                "inode": member.inode,
                "mode": member.mode,
                "name": member.name,
            }
            for member in manifest.members
        ],
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
            "manifest_device",
            "manifest_inode",
            "members",
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
    encoded_members = payload.get("members")
    if (
        not isinstance(transaction_id, str)
        or _TRANSACTION_ID.fullmatch(transaction_id) is None
        or not isinstance(directory_name, str)
        or TRANSACTION_DIRECTORY.fullmatch(directory_name) is None
        or directory_name != f"{TRANSACTION_PREFIX}{transaction_id}"
        or not positive_record_integer(payload.get("directory_device"))
        or not positive_record_integer(payload.get("directory_inode"))
        or not positive_record_integer(payload.get("manifest_device"))
        or not positive_record_integer(payload.get("manifest_inode"))
        or not isinstance(encoded_members, list)
        or len(encoded_members) != len(OPERATION_MEMBER_NAMES)
    ):
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    members: list[_BackupTransactionMember] = []
    for encoded_member in encoded_members:
        if (
            not isinstance(encoded_member, dict)
            or set(encoded_member)
            != {
                "device",
                "file_type",
                "inode",
                "mode",
                "name",
            }
            or not isinstance(encoded_member.get("name"), str)
            or encoded_member.get("name") not in OPERATION_MEMBER_NAMES
            or encoded_member.get("file_type") != "regular"
            or type(encoded_member.get("mode")) is not int
            or encoded_member.get("mode") != 0o600
            or not positive_record_integer(encoded_member.get("device"))
            or not positive_record_integer(encoded_member.get("inode"))
        ):
            raise EncryptedBackupError(
                "encrypted_backup_transaction_invalid"
            )
        members.append(
            _BackupTransactionMember(
                name=encoded_member["name"],
                device=encoded_member["device"],
                inode=encoded_member["inode"],
                file_type=encoded_member["file_type"],
                mode=encoded_member["mode"],
            )
        )
    members.sort(key=lambda member: member.name)
    if tuple(member.name for member in members) != tuple(
        sorted(OPERATION_MEMBER_NAMES)
    ):
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    return _BackupTransactionManifest(
        transaction_id=transaction_id,
        directory_name=directory_name,
        directory_device=payload["directory_device"],
        directory_inode=payload["directory_inode"],
        manifest_device=payload["manifest_device"],
        manifest_inode=payload["manifest_inode"],
        members=tuple(members),
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


def _namespace_belongs_to_transaction(
    manifest: _BackupTransactionManifest,
    namespace_name: str,
) -> bool:
    if namespace_name == manifest.directory_name:
        return True
    quarantine_match = QUARANTINE_DIRECTORY.fullmatch(namespace_name)
    return (
        quarantine_match is not None
        and quarantine_match.group(1) == manifest.transaction_id
    )


def _directory_identity(
    transaction: BackupTransaction,
    *,
    namespace_name: str | None = None,
) -> os.stat_result:
    _destination_identity(
        transaction.destination,
        transaction.destination_descriptor,
    )
    resolved_name = namespace_name or transaction.manifest.directory_name
    if not _namespace_belongs_to_transaction(
        transaction.manifest,
        resolved_name,
    ):
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    descriptor_stat = os.fstat(transaction.directory_descriptor)
    path_stat = os.stat(
        resolved_name,
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
        and descriptor_stat.st_dev
        == transaction.manifest.manifest_device
        and descriptor_stat.st_ino
        == transaction.manifest.manifest_inode
        and descriptor_stat.st_size == TRANSACTION_MANIFEST_BYTES
        and stat.S_IMODE(descriptor_stat.st_mode) == 0o600
    ):
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    return descriptor_stat


def _read_manifest(
    transaction: BackupTransaction,
    *,
    namespace_name: str | None = None,
) -> _BackupTransactionManifest:
    _directory_identity(
        transaction,
        namespace_name=namespace_name,
    )
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


def _recorded_member(
    manifest: _BackupTransactionManifest,
    name: str,
) -> _BackupTransactionMember:
    for member in manifest.members:
        if member.name == name:
            return member
    raise EncryptedBackupError(
        "encrypted_backup_transaction_invalid"
    )


def _member_stat_matches(
    descriptor_stat: os.stat_result,
    path_stat: os.stat_result,
    member: _BackupTransactionMember,
) -> bool:
    return (
        member.file_type == "regular"
        and stat.S_ISREG(descriptor_stat.st_mode)
        and stat.S_ISREG(path_stat.st_mode)
        and descriptor_stat.st_dev == path_stat.st_dev
        and descriptor_stat.st_ino == path_stat.st_ino
        and descriptor_stat.st_dev == member.device
        and descriptor_stat.st_ino == member.inode
        and stat.S_IMODE(descriptor_stat.st_mode) == member.mode
        and stat.S_IMODE(path_stat.st_mode) == member.mode
    )


def _stable_member_identity_matches(
    initial_stat: os.stat_result,
    current_stat: os.stat_result,
) -> bool:
    return (
        initial_stat.st_dev == current_stat.st_dev
        and initial_stat.st_ino == current_stat.st_ino
        and initial_stat.st_mode == current_stat.st_mode
        and initial_stat.st_uid == current_stat.st_uid
        and initial_stat.st_gid == current_stat.st_gid
        and initial_stat.st_nlink == current_stat.st_nlink
        and initial_stat.st_size == current_stat.st_size
        and initial_stat.st_mtime_ns == current_stat.st_mtime_ns
        and initial_stat.st_ctime_ns == current_stat.st_ctime_ns
    )


def _ownership_link_name(member_name: str) -> str:
    if member_name not in OPERATION_MEMBER_NAMES:
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    return f"{OWNERSHIP_LINK_PREFIX}{member_name}"


def _open_recorded_name(
    transaction: BackupTransaction,
    member: _BackupTransactionMember,
    name: str,
    flags: int,
) -> int:
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
        if not _member_stat_matches(
            descriptor_stat,
            path_stat,
            member,
        ):
            raise EncryptedBackupError(
                "encrypted_backup_transaction_invalid"
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_recorded_member(
    transaction: BackupTransaction,
    member: _BackupTransactionMember,
    flags: int,
) -> int:
    return _open_recorded_name(
        transaction,
        member,
        member.name,
        flags,
    )


def _owned_pair_matches(
    primary_stat: os.stat_result,
    ownership_stat: os.stat_result,
    member: _BackupTransactionMember,
    *,
    expected_nlink: int,
) -> bool:
    return (
        _member_stat_matches(
            primary_stat,
            ownership_stat,
            member,
        )
        and primary_stat.st_nlink == expected_nlink
        and ownership_stat.st_nlink == expected_nlink
    )


def _expected_member_link_count(
    transaction: BackupTransaction,
    member: _BackupTransactionMember,
) -> int:
    additional = (
        transaction.authorized_artifact_links
        if member.name == ENCRYPTED_NAME
        else 0
    )
    return 2 + additional


def validate_backup_transaction(
    transaction: BackupTransaction,
) -> set[str]:
    _read_manifest(transaction)
    members = set(os.listdir(transaction.directory_descriptor))
    if members != TRANSACTION_MEMBER_NAMES:
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    descriptors: list[int] = []
    try:
        for member in transaction.manifest.members:
            primary_descriptor = _open_recorded_member(
                transaction,
                member,
                os.O_RDONLY,
            )
            descriptors.append(primary_descriptor)
            ownership_descriptor = _open_recorded_name(
                transaction,
                member,
                _ownership_link_name(member.name),
                os.O_RDONLY,
            )
            descriptors.append(ownership_descriptor)
            if not _owned_pair_matches(
                os.fstat(primary_descriptor),
                os.fstat(ownership_descriptor),
                member,
                expected_nlink=_expected_member_link_count(
                    transaction,
                    member,
                ),
            ):
                raise EncryptedBackupError(
                    "encrypted_backup_transaction_invalid"
                )
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    return members


def authorize_transaction_artifact_links(
    transaction: BackupTransaction,
    *,
    additional_links: int,
) -> None:
    """Authorize a proven publication's exact extra encrypted-file links."""

    if additional_links != 2 or transaction.authorized_artifact_links != 0:
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    transaction.authorized_artifact_links = additional_links
    try:
        validate_backup_transaction(transaction)
    except BaseException:
        transaction.authorized_artifact_links = 0
        raise


def _open_recovery_members(
    transaction: BackupTransaction,
    *,
    namespace_name: str,
) -> list[_RecoveryMemberHandle]:
    """Open only present manifest members; reject every ambiguous entry."""

    _read_manifest(
        transaction,
        namespace_name=namespace_name,
    )
    recorded_names = {
        member.name for member in transaction.manifest.members
    }
    ownership_names = {
        _ownership_link_name(name) for name in recorded_names
    }
    namespace = set(os.listdir(transaction.directory_descriptor))
    present_names = namespace - {TRANSACTION_MANIFEST_NAME}
    if (
        TRANSACTION_MANIFEST_NAME not in namespace
        or not present_names <= (recorded_names | ownership_names)
    ):
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    opened: list[_RecoveryMemberHandle] = []
    try:
        for member in transaction.manifest.members:
            primary_present = member.name in present_names
            ownership_name = _ownership_link_name(member.name)
            ownership_present = ownership_name in present_names
            if primary_present and not ownership_present:
                raise EncryptedBackupError(
                    "encrypted_backup_transaction_invalid"
                )
            if not ownership_present:
                continue
            ownership_descriptor = _open_recorded_name(
                transaction,
                member,
                ownership_name,
                os.O_RDONLY,
            )
            primary_descriptor: int | None = None
            try:
                acquire_bounded_lock(
                    ownership_descriptor,
                    fcntl.LOCK_EX,
                )
                initial_ownership_stat = os.fstat(
                    ownership_descriptor
                )
                initial_primary_stat: os.stat_result | None = None
                if primary_present:
                    primary_descriptor = _open_recorded_member(
                        transaction,
                        member,
                        os.O_RDONLY,
                    )
                    initial_primary_stat = os.fstat(
                        primary_descriptor
                    )
                    if not _owned_pair_matches(
                        initial_primary_stat,
                        initial_ownership_stat,
                        member,
                        expected_nlink=2,
                    ):
                        raise EncryptedBackupError(
                            "encrypted_backup_transaction_invalid"
                        )
                elif initial_ownership_stat.st_nlink != 1:
                    raise EncryptedBackupError(
                        "encrypted_backup_transaction_invalid"
                    )
                opened.append(
                    _RecoveryMemberHandle(
                        member=member,
                        primary_descriptor=primary_descriptor,
                        ownership_descriptor=ownership_descriptor,
                        initial_primary_stat=initial_primary_stat,
                        initial_ownership_stat=initial_ownership_stat,
                    )
                )
            except BaseException:
                if primary_descriptor is not None:
                    os.close(primary_descriptor)
                os.close(ownership_descriptor)
                raise
        if set(os.listdir(transaction.directory_descriptor)) != namespace:
            raise EncryptedBackupError(
                "encrypted_backup_transaction_invalid"
            )
        _validate_recovery_namespace(
            transaction,
            namespace_name,
            opened,
        )
        return opened
    except BaseException:
        for handle in opened:
            if handle.primary_descriptor is not None:
                os.close(handle.primary_descriptor)
            os.close(handle.ownership_descriptor)
        raise


def _capture_member(
    directory_descriptor: int,
    name: str,
    descriptor: int,
) -> _BackupTransactionMember:
    descriptor_stat = os.fstat(descriptor)
    path_stat = os.stat(
        name,
        dir_fd=directory_descriptor,
        follow_symlinks=False,
    )
    mode = stat.S_IMODE(descriptor_stat.st_mode)
    if not (
        stat.S_ISREG(descriptor_stat.st_mode)
        and stat.S_ISREG(path_stat.st_mode)
        and descriptor_stat.st_dev == path_stat.st_dev
        and descriptor_stat.st_ino == path_stat.st_ino
        and mode == 0o600
        and stat.S_IMODE(path_stat.st_mode) == mode
    ):
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    return _BackupTransactionMember(
        name=name,
        device=descriptor_stat.st_dev,
        inode=descriptor_stat.st_ino,
        file_type="regular",
        mode=mode,
    )


def _unlink_captured_member(
    directory_descriptor: int,
    member: _BackupTransactionMember,
    *,
    name: str | None = None,
) -> None:
    owned_name = member.name if name is None else name
    try:
        path_stat = os.stat(
            owned_name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    if (
        stat.S_ISREG(path_stat.st_mode)
        and path_stat.st_dev == member.device
        and path_stat.st_ino == member.inode
        and stat.S_IMODE(path_stat.st_mode) == member.mode
    ):
        os.unlink(owned_name, dir_fd=directory_descriptor)


def create_backup_transaction(destination: Path) -> BackupTransaction:
    destination_descriptor = os.open(destination, directory_open_flags())
    directory_descriptor: int | None = None
    manifest_descriptor: int | None = None
    manifest_locked = False
    directory_name: str | None = None
    manifest_member: _BackupTransactionMember | None = None
    operation_members: list[_BackupTransactionMember] = []
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
        manifest_member = _capture_member(
            directory_descriptor,
            TRANSACTION_MANIFEST_NAME,
            manifest_descriptor,
        )
        for name in OPERATION_MEMBER_NAMES:
            descriptor = os.open(
                name,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                0o600,
                dir_fd=directory_descriptor,
            )
            try:
                os.fchmod(descriptor, 0o600)
                retry_fsync(descriptor)
                member = _capture_member(
                    directory_descriptor,
                    name,
                    descriptor,
                )
                operation_members.append(member)
                ownership_name = _ownership_link_name(name)
                os.link(
                    name,
                    ownership_name,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                ownership_stat = os.stat(
                    ownership_name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if not _owned_pair_matches(
                    os.fstat(descriptor),
                    ownership_stat,
                    member,
                    expected_nlink=2,
                ):
                    raise EncryptedBackupError(
                        "encrypted_backup_transaction_invalid"
                    )
            finally:
                os.close(descriptor)
        retry_fsync(directory_descriptor)
        manifest = _BackupTransactionManifest(
            transaction_id=transaction_id,
            directory_name=directory_name,
            directory_device=directory_stat.st_dev,
            directory_inode=directory_stat.st_ino,
            manifest_device=manifest_member.device,
            manifest_inode=manifest_member.inode,
            members=tuple(
                sorted(
                    operation_members,
                    key=lambda member: member.name,
                )
            ),
        )
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
        validate_backup_transaction(transaction)
        retry_fsync(directory_descriptor)
        retry_fsync(destination_descriptor)
        return transaction
    except BaseException:
        if directory_descriptor is not None:
            for member in reversed(operation_members):
                try:
                    _unlink_captured_member(
                        directory_descriptor,
                        member,
                    )
                except OSError:
                    pass
                try:
                    _unlink_captured_member(
                        directory_descriptor,
                        member,
                        name=_ownership_link_name(member.name),
                    )
                except OSError:
                    pass
            if manifest_member is not None:
                try:
                    _unlink_captured_member(
                        directory_descriptor,
                        manifest_member,
                    )
                except OSError:
                    pass
            try:
                retry_fsync(directory_descriptor)
            except OSError:
                pass
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


def transaction_member_path(
    transaction: BackupTransaction,
    name: str,
) -> Path:
    if name not in OPERATION_MEMBER_NAMES:
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    validate_backup_transaction(transaction)
    return transaction.directory / name


def open_transaction_member(
    transaction: BackupTransaction,
    name: str,
    flags: int,
) -> int:
    if name not in OPERATION_MEMBER_NAMES:
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    validate_backup_transaction(transaction)
    return _open_recorded_member(
        transaction,
        _recorded_member(transaction.manifest, name),
        flags,
    )


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
    validate_backup_transaction(transaction)
    opened: list[tuple[_BackupTransactionMember, int, int]] = []
    try:
        for member in transaction.manifest.members:
            primary_descriptor = _open_recorded_member(
                transaction,
                member,
                os.O_RDONLY,
            )
            ownership_descriptor: int | None = None
            try:
                ownership_descriptor = _open_recorded_name(
                    transaction,
                    member,
                    _ownership_link_name(member.name),
                    os.O_RDONLY,
                )
            except BaseException:
                os.close(primary_descriptor)
                raise
            opened.append(
                (
                    member,
                    primary_descriptor,
                    ownership_descriptor,
                )
            )
        if set(os.listdir(transaction.directory_descriptor)) != (
            TRANSACTION_MEMBER_NAMES
        ):
            raise EncryptedBackupError(
                "encrypted_backup_transaction_invalid"
            )
        for member, primary_descriptor, ownership_descriptor in opened:
            if not _owned_pair_matches(
                os.fstat(primary_descriptor),
                os.fstat(ownership_descriptor),
                member,
                expected_nlink=_expected_member_link_count(
                    transaction,
                    member,
                ),
            ):
                raise EncryptedBackupError(
                    "encrypted_backup_transaction_invalid"
                )
        _unlink_opened_members(transaction, opened)
    finally:
        for (
            _member,
            primary_descriptor,
            ownership_descriptor,
        ) in opened:
            os.close(primary_descriptor)
            os.close(ownership_descriptor)
    _finish_backup_transaction_removal(transaction)


def _unlink_opened_members(
    transaction: BackupTransaction,
    opened: list[tuple[_BackupTransactionMember, int, int]],
) -> None:
    failure: BaseException | None = None
    for member, primary_descriptor, ownership_descriptor in opened:
        try:
            primary_stat = os.fstat(primary_descriptor)
            primary_path_stat = os.stat(
                member.name,
                dir_fd=transaction.directory_descriptor,
                follow_symlinks=False,
            )
            ownership_name = _ownership_link_name(member.name)
            ownership_stat = os.fstat(ownership_descriptor)
            ownership_path_stat = os.stat(
                ownership_name,
                dir_fd=transaction.directory_descriptor,
                follow_symlinks=False,
            )
            expected_link_count = _expected_member_link_count(
                transaction,
                member,
            )
            if not (
                _owned_pair_matches(
                    primary_stat,
                    ownership_stat,
                    member,
                    expected_nlink=expected_link_count,
                )
                and _member_stat_matches(
                    primary_stat,
                    primary_path_stat,
                    member,
                )
                and _member_stat_matches(
                    ownership_stat,
                    ownership_path_stat,
                    member,
                )
            ):
                raise EncryptedBackupError(
                    "encrypted_backup_transaction_invalid"
                )
            os.unlink(
                member.name,
                dir_fd=transaction.directory_descriptor,
            )
            retry_fsync(transaction.directory_descriptor)
            ownership_stat = os.fstat(ownership_descriptor)
            ownership_path_stat = os.stat(
                ownership_name,
                dir_fd=transaction.directory_descriptor,
                follow_symlinks=False,
            )
            if not (
                _member_stat_matches(
                    ownership_stat,
                    ownership_path_stat,
                    member,
                )
                and ownership_stat.st_nlink
                == expected_link_count - 1
                and ownership_path_stat.st_nlink
                == expected_link_count - 1
            ):
                raise EncryptedBackupError(
                    "encrypted_backup_transaction_invalid"
                )
            try:
                os.stat(
                    member.name,
                    dir_fd=transaction.directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise EncryptedBackupError(
                    "encrypted_backup_transaction_invalid"
                )
            os.unlink(
                ownership_name,
                dir_fd=transaction.directory_descriptor,
            )
            retry_fsync(transaction.directory_descriptor)
        except BaseException as exc:
            failure = exc
            break
    if failure is not None:
        raise failure


def _finish_backup_transaction_removal(
    transaction: BackupTransaction,
) -> None:
    _read_manifest(transaction)
    _directory_identity(transaction)
    if set(os.listdir(transaction.directory_descriptor)) != {
        TRANSACTION_MANIFEST_NAME
    }:
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
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


def _namespace_stat(
    transaction: BackupTransaction,
    namespace_name: str,
) -> os.stat_result | None:
    try:
        return os.stat(
            namespace_name,
            dir_fd=transaction.destination_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None


def _namespace_matches_directory(
    transaction: BackupTransaction,
    namespace_stat: os.stat_result | None,
) -> bool:
    if namespace_stat is None:
        return False
    descriptor_stat = os.fstat(transaction.directory_descriptor)
    return (
        stat.S_ISDIR(namespace_stat.st_mode)
        and descriptor_stat.st_dev == namespace_stat.st_dev
        and descriptor_stat.st_ino == namespace_stat.st_ino
        and descriptor_stat.st_dev
        == transaction.manifest.directory_device
        and descriptor_stat.st_ino
        == transaction.manifest.directory_inode
        and stat.S_IMODE(namespace_stat.st_mode) == 0o700
    )


def _validate_recovery_namespace(
    transaction: BackupTransaction,
    namespace_name: str,
    opened: list[_RecoveryMemberHandle],
) -> None:
    if (
        QUARANTINE_DIRECTORY.fullmatch(namespace_name) is not None
        and _namespace_stat(
            transaction,
            transaction.manifest.directory_name,
        )
        is not None
    ):
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    _read_manifest(
        transaction,
        namespace_name=namespace_name,
    )
    expected_namespace = {
        TRANSACTION_MANIFEST_NAME,
        *(
            handle.member.name
            for handle in opened
            if handle.primary_descriptor is not None
        ),
        *(
            _ownership_link_name(handle.member.name)
            for handle in opened
        ),
    }
    if set(os.listdir(transaction.directory_descriptor)) != (
        expected_namespace
    ):
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    for handle in opened:
        ownership_name = _ownership_link_name(handle.member.name)
        ownership_stat = os.fstat(handle.ownership_descriptor)
        ownership_path_stat = os.stat(
            ownership_name,
            dir_fd=transaction.directory_descriptor,
            follow_symlinks=False,
        )
        if not _member_stat_matches(
            ownership_stat,
            ownership_path_stat,
            handle.member,
        ) or not _stable_member_identity_matches(
            handle.initial_ownership_stat,
            ownership_stat,
        ):
            raise EncryptedBackupError(
                "encrypted_backup_transaction_invalid"
            )
        if handle.primary_descriptor is None:
            if ownership_stat.st_nlink != 1:
                raise EncryptedBackupError(
                    "encrypted_backup_transaction_invalid"
                )
            continue
        if handle.initial_primary_stat is None:
            raise EncryptedBackupError(
                "encrypted_backup_transaction_invalid"
            )
        primary_stat = os.fstat(handle.primary_descriptor)
        primary_path_stat = os.stat(
            handle.member.name,
            dir_fd=transaction.directory_descriptor,
            follow_symlinks=False,
        )
        if not (
            _owned_pair_matches(
                primary_stat,
                ownership_stat,
                handle.member,
                expected_nlink=2,
            )
            and _member_stat_matches(
                primary_stat,
                primary_path_stat,
                handle.member,
            )
            and _stable_member_identity_matches(
                handle.initial_primary_stat,
                primary_stat,
            )
            and _stable_member_identity_matches(
                primary_stat,
                primary_path_stat,
            )
        ):
            raise EncryptedBackupError(
                "encrypted_backup_transaction_invalid"
            )


def _restore_isolated_namespace(
    transaction: BackupTransaction,
    quarantine_name: str,
) -> bool:
    """Best-effort no-overwrite restoration after fail-closed isolation."""

    quarantine_match = QUARANTINE_DIRECTORY.fullmatch(quarantine_name)
    if (
        quarantine_match is None
        or quarantine_match.group(1) != transaction.manifest.transaction_id
        or not _namespace_matches_directory(
            transaction,
            _namespace_stat(transaction, quarantine_name),
        )
        or _namespace_stat(
            transaction,
            transaction.manifest.directory_name,
        )
        is not None
    ):
        return False
    try:
        _rename_no_replace(
            transaction.destination_descriptor,
            quarantine_name,
            transaction.manifest.directory_name,
        )
    except BaseException:
        original_stat = _namespace_stat(
            transaction,
            transaction.manifest.directory_name,
        )
        quarantine_stat = _namespace_stat(
            transaction,
            quarantine_name,
        )
        if not (
            _namespace_matches_directory(transaction, original_stat)
            and quarantine_stat is None
        ):
            return False
    try:
        retry_fsync(transaction.destination_descriptor)
    except BaseException:
        pass
    return (
        _namespace_matches_directory(
            transaction,
            _namespace_stat(
                transaction,
                transaction.manifest.directory_name,
            ),
        )
        and _namespace_stat(transaction, quarantine_name) is None
    )


def _isolate_recovery_namespace(
    transaction: BackupTransaction,
) -> str:
    """Move the exact transaction directory to a private no-replace name."""

    original_name = transaction.manifest.directory_name
    _directory_identity(transaction, namespace_name=original_name)
    for _attempt in range(_QUARANTINE_NAME_ATTEMPTS):
        quarantine_name = (
            f"{QUARANTINE_PREFIX}{transaction.manifest.transaction_id}-"
            f"{os.urandom(16).hex()}"
        )
        try:
            _rename_no_replace(
                transaction.destination_descriptor,
                original_name,
                quarantine_name,
            )
        except BaseException as exc:
            if isinstance(exc, OSError) and exc.errno == errno.EEXIST:
                continue
            if (
                _namespace_stat(transaction, original_name) is None
                and _namespace_matches_directory(
                    transaction,
                    _namespace_stat(transaction, quarantine_name),
                )
            ):
                _restore_isolated_namespace(
                    transaction,
                    quarantine_name,
                )
            raise
        try:
            retry_fsync(transaction.destination_descriptor)
            if _namespace_stat(transaction, original_name) is not None:
                raise EncryptedBackupError(
                    "encrypted_backup_transaction_invalid"
                )
            _directory_identity(
                transaction,
                namespace_name=quarantine_name,
            )
        except BaseException:
            _restore_isolated_namespace(
                transaction,
                quarantine_name,
            )
            raise
        return quarantine_name
    raise EncryptedBackupError(
        "encrypted_backup_transaction_unavailable"
    )


def _remove_isolated_recovery_namespace(
    transaction: BackupTransaction,
    quarantine_name: str,
    opened: list[_RecoveryMemberHandle],
) -> None:
    """Delete only exact members through the isolated directory descriptor.

    POSIX offers no conditional inode-unlink operation. Recovery therefore
    relies on the held manifest/member locks as its cooperative ownership
    boundary, isolates the namespace with an atomic no-replace rename, and
    revalidates every descriptor immediately before each dirfd-relative
    unlink. Any observed ambiguity leaves or restores the quarantine.
    """

    quarantine_match = QUARANTINE_DIRECTORY.fullmatch(quarantine_name)
    if (
        quarantine_match is None
        or quarantine_match.group(1) != transaction.manifest.transaction_id
    ):
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    remaining = list(opened)
    while remaining:
        _validate_recovery_namespace(
            transaction,
            quarantine_name,
            remaining,
        )
        handle = remaining[0]
        ownership_name = _ownership_link_name(handle.member.name)
        if handle.primary_descriptor is not None:
            os.unlink(
                handle.member.name,
                dir_fd=transaction.directory_descriptor,
            )
            retry_fsync(transaction.directory_descriptor)
            ownership_stat = os.fstat(handle.ownership_descriptor)
            ownership_path_stat = os.stat(
                ownership_name,
                dir_fd=transaction.directory_descriptor,
                follow_symlinks=False,
            )
            if not (
                _member_stat_matches(
                    ownership_stat,
                    ownership_path_stat,
                    handle.member,
                )
                and ownership_stat.st_nlink == 1
                and ownership_path_stat.st_nlink == 1
            ):
                raise EncryptedBackupError(
                    "encrypted_backup_transaction_invalid"
                )
            try:
                os.stat(
                    handle.member.name,
                    dir_fd=transaction.directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise EncryptedBackupError(
                    "encrypted_backup_transaction_invalid"
                )
        os.unlink(
            ownership_name,
            dir_fd=transaction.directory_descriptor,
        )
        retry_fsync(transaction.directory_descriptor)
        remaining.pop(0)
    _validate_recovery_namespace(
        transaction,
        quarantine_name,
        [],
    )
    os.unlink(
        TRANSACTION_MANIFEST_NAME,
        dir_fd=transaction.directory_descriptor,
    )
    retry_fsync(transaction.directory_descriptor)
    if os.listdir(transaction.directory_descriptor):
        raise EncryptedBackupError(
            "encrypted_backup_transaction_invalid"
        )
    os.rmdir(
        quarantine_name,
        dir_fd=transaction.destination_descriptor,
    )
    retry_fsync(transaction.destination_descriptor)


def _remove_recovered_backup_transaction(
    transaction: BackupTransaction,
    *,
    cutoff: float,
    initial_directory_stat: os.stat_result,
    namespace_name: str,
) -> bool:
    opened = _open_recovery_members(
        transaction,
        namespace_name=namespace_name,
    )
    quarantine_name: str | None = None
    removed = False
    try:
        if any(
            os.fstat(handle.ownership_descriptor).st_mtime >= cutoff
            or (
                handle.primary_descriptor is not None
                and os.fstat(handle.primary_descriptor).st_mtime
                >= cutoff
            )
            for handle in opened
        ):
            return False
        final_directory_stat = _directory_identity(
            transaction,
            namespace_name=namespace_name,
        )
        if not (
            initial_directory_stat.st_mtime_ns
            == final_directory_stat.st_mtime_ns
            and initial_directory_stat.st_ctime_ns
            == final_directory_stat.st_ctime_ns
            and final_directory_stat.st_mtime < cutoff
        ):
            return False
        _validate_recovery_namespace(
            transaction,
            namespace_name,
            opened,
        )
        if namespace_name == transaction.manifest.directory_name:
            quarantine_name = _isolate_recovery_namespace(transaction)
        else:
            quarantine_match = QUARANTINE_DIRECTORY.fullmatch(
                namespace_name
            )
            if (
                quarantine_match is None
                or quarantine_match.group(1)
                != transaction.manifest.transaction_id
            ):
                raise EncryptedBackupError(
                    "encrypted_backup_transaction_invalid"
                )
            quarantine_name = namespace_name
        _validate_recovery_namespace(
            transaction,
            quarantine_name,
            opened,
        )
        _remove_isolated_recovery_namespace(
            transaction,
            quarantine_name,
            opened,
        )
        removed = True
    finally:
        if quarantine_name is not None and not removed:
            _restore_isolated_namespace(
                transaction,
                quarantine_name,
            )
        for handle in opened:
            if handle.primary_descriptor is not None:
                os.close(handle.primary_descriptor)
            os.close(handle.ownership_descriptor)
    return True


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
    transaction_match = TRANSACTION_DIRECTORY.fullmatch(directory_name)
    quarantine_match = QUARANTINE_DIRECTORY.fullmatch(directory_name)
    if transaction_match is None and quarantine_match is None:
        return
    transaction_id = (
        transaction_match.group(1)
        if transaction_match is not None
        else quarantine_match.group(1)
    )
    original_directory_name = f"{TRANSACTION_PREFIX}{transaction_id}"
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
            and manifest_stat.st_nlink == 1
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
            manifest.directory_name == original_directory_name
            and manifest.transaction_id == transaction_id
            and manifest.directory_device == directory_stat.st_dev
            and manifest.directory_inode == directory_stat.st_ino
        ):
            return
        transaction = BackupTransaction(
            destination=destination,
            destination_descriptor=destination_descriptor,
            directory=destination / original_directory_name,
            directory_descriptor=directory_descriptor,
            manifest_descriptor=manifest_descriptor,
            manifest=manifest,
        )
        if not _remove_recovered_backup_transaction(
            transaction,
            cutoff=cutoff,
            initial_directory_stat=directory_stat,
            namespace_name=directory_name,
        ):
            return
        close_backup_transaction(transaction, remove=False)
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
