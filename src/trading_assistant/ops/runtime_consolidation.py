"""Fail-closed consolidation of the isolated runtime SQLite database."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from typing import Iterator, Sequence
from uuid import uuid4

from sqlalchemy import MetaData, Table, create_engine, func, select
from sqlalchemy.pool import NullPool

from ..config import load_config
from ..db.schema import (
    SchemaOutOfDate,
    require_current_schema,
    schema_status,
)
from ..db.session import create_db_engine, make_session_factory
from ..security.secrets import (
    load_role_secrets,
    validate_base64_key,
)
from .backup import EncryptedBackupReceipt, backup_database
from .control import prove_app_absent
from .tenure import (
    LocalProcessInspector,
    ProcessIdentity,
    ProcessInspector,
    ProcessProof,
    RuntimeTenureGuard,
    RuntimeTenureService,
    TenureUnavailable,
    TenureUncertain,
)


_PRODUCTION_DESTINATION = Path(
    "/Users/avi/Desktop/robinhood/trading-assistant"
)
_PRODUCTION_SOURCE = (
    _PRODUCTION_DESTINATION / ".worktrees" / "safety-foundation"
)
_DATABASE_NAME = "trading_assistant.db"
_BACKUP_DIRECTORY = Path(".local/encrypted-backups")
_UNCERTAINTY_MARKER = Path(
    ".local/runtime-consolidation.migration_uncertain"
)
_STAGING_PREFIX = "runtime-consolidation-stage-"
_STAGING_NAME = "runtime.sqlite3"
_BOUND_SOURCE_NAME = "descriptor-source.sqlite3"
_REPLACEMENT_PREFIX = (
    ".trading_assistant.db.runtime-consolidation-old-"
)
_SQLITE_HEADER = b"SQLite format 3\x00"
_LEASE_TTL_SECONDS = 300
_LEASE_RENEWAL_SECONDS = 30
_SHA256_HEX_LENGTH = 64
_WAL_WRITE_LOCK_OFFSET = 120
_SQLITE_RESERVED_BYTE = 0x40000001


class ConsolidationError(RuntimeError):
    """One stable, value-free runtime-consolidation failure."""

    def __init__(self, stable_code: str) -> None:
        self.stable_code = stable_code
        super().__init__(stable_code)


@dataclass(frozen=True)
class ConsolidationRoots:
    """The exact pair authorized for one core invocation."""

    source_root: Path
    destination_root: Path
    database_name: str = _DATABASE_NAME
    app_port: int = 8020


@dataclass(frozen=True)
class LogicalSummary:
    schema_head: str
    table_counts: Sequence[tuple[str, int]]
    digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "table_counts", tuple(self.table_counts))


@dataclass(frozen=True)
class ConsolidationReceipt:
    source_hash: str
    destination_hash: str
    source_backup_hash: str
    destination_backup_hash: str | None
    summary_digest: str
    installed: bool
    status: str


@dataclass
class _ValidatedRoot:
    path: Path
    descriptor: int
    device: int
    inode: int

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1


@dataclass
class _ValidatedDatabase:
    path: Path
    descriptor: int
    device: int
    inode: int

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1


@dataclass
class _HeldMaintenance:
    engine: object
    guard: RuntimeTenureGuard

    def renew(self) -> None:
        if not self.guard.renew_once():
            raise ConsolidationError("maintenance_tenure_lost")

    def release(self) -> None:
        try:
            released = self.guard.close()
        finally:
            self.engine.dispose()
        if not released:
            raise ConsolidationError("maintenance_release_uncertain")

    def abandon_relocated(self) -> None:
        """Close local handles after the guarded database name moved."""

        self.engine.dispose()


def _stage_event(_stage: str) -> None:
    """Test seam at durability boundaries; production deliberately does nothing."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _safe_close(descriptor: int | None) -> None:
    if descriptor is None or descriptor < 0:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _open_flags(*, writable: bool = False, directory: bool = False) -> int:
    flags = os.O_RDWR if writable else os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    return flags


def _same_identity(
    first: os.stat_result,
    second: os.stat_result,
) -> bool:
    return (first.st_dev, first.st_ino) == (
        second.st_dev,
        second.st_ino,
    )


def _validate_exact_root(
    requested: Path,
    expected: Path,
) -> _ValidatedRoot:
    requested = Path(requested)
    expected = Path(expected)
    if not requested.is_absolute() or not expected.is_absolute():
        raise ConsolidationError("root_mismatch")
    if requested != expected:
        try:
            same_canonical = (
                requested.resolve(strict=True)
                == expected.resolve(strict=True)
            )
        except (OSError, RuntimeError):
            same_canonical = False
        raise ConsolidationError(
            "root_alias" if same_canonical else "root_mismatch"
        )
    try:
        path_status = requested.lstat()
    except OSError:
        raise ConsolidationError("root_invalid") from None
    if (
        not stat.S_ISDIR(path_status.st_mode)
        or stat.S_ISLNK(path_status.st_mode)
        or path_status.st_uid != os.getuid()
        or stat.S_IMODE(path_status.st_mode) != 0o700
    ):
        code = (
            "root_permissions_invalid"
            if stat.S_ISDIR(path_status.st_mode)
            and not stat.S_ISLNK(path_status.st_mode)
            and path_status.st_uid == os.getuid()
            else "root_invalid"
        )
        raise ConsolidationError(code)
    descriptor = -1
    try:
        descriptor = os.open(
            requested,
            _open_flags(directory=True),
        )
        descriptor_status = os.fstat(descriptor)
    except OSError:
        _safe_close(descriptor)
        raise ConsolidationError("root_invalid") from None
    if (
        not _same_identity(path_status, descriptor_status)
        or not stat.S_ISDIR(descriptor_status.st_mode)
        or descriptor_status.st_uid != os.getuid()
        or stat.S_IMODE(descriptor_status.st_mode) != 0o700
    ):
        _safe_close(descriptor)
        raise ConsolidationError("root_invalid")
    try:
        canonical = requested.resolve(strict=True)
    except (OSError, RuntimeError):
        _safe_close(descriptor)
        raise ConsolidationError("root_invalid") from None
    return _ValidatedRoot(
        path=canonical,
        descriptor=descriptor,
        device=descriptor_status.st_dev,
        inode=descriptor_status.st_ino,
    )


def _revalidate_root(root: _ValidatedRoot) -> None:
    try:
        path_status = root.path.lstat()
        descriptor_status = os.fstat(root.descriptor)
    except OSError:
        raise ConsolidationError("root_changed") from None
    if (
        not _same_identity(path_status, descriptor_status)
        or not stat.S_ISDIR(path_status.st_mode)
        or path_status.st_uid != os.getuid()
        or stat.S_IMODE(path_status.st_mode) != 0o700
        or (path_status.st_dev, path_status.st_ino)
        != (root.device, root.inode)
    ):
        raise ConsolidationError("root_changed")


def _validate_database_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or name != _DATABASE_NAME
        or name == ":memory:"
        or Path(name).name != name
        or "/" in name
        or "\x00" in name
    ):
        raise ConsolidationError("database_url_invalid")


def _descriptor_prefix(descriptor: int, length: int) -> bytes:
    try:
        return os.pread(descriptor, length, 0)
    except AttributeError:
        original = os.lseek(descriptor, 0, os.SEEK_CUR)
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            return os.read(descriptor, length)
        finally:
            os.lseek(descriptor, original, os.SEEK_SET)


def _descriptor_sqlite_uri(descriptor: int, *, writable: bool) -> str:
    try:
        held = os.fstat(descriptor)
    except OSError:
        raise ConsolidationError("descriptor_binding_invalid") from None
    if not stat.S_ISREG(held.st_mode):
        raise ConsolidationError("descriptor_binding_invalid")
    for directory in (Path("/dev/fd"), Path("/proc/self/fd")):
        candidate = directory / str(descriptor)
        probe = -1
        try:
            probe = os.open(
                candidate,
                os.O_RDWR if writable else os.O_RDONLY,
            )
            observed = os.fstat(probe)
        except OSError:
            continue
        finally:
            _safe_close(probe)
        if not (
            stat.S_ISREG(observed.st_mode)
            and _same_identity(held, observed)
        ):
            continue
        mode = "rw" if writable else "ro"
        return f"file:{candidate}?mode={mode}"
    raise ConsolidationError("descriptor_binding_invalid")


def _validate_database(
    root: _ValidatedRoot,
    *,
    database_name: str,
    required: bool,
) -> _ValidatedDatabase | None:
    path = root.path / database_name
    if not _path_lexists(path):
        if required:
            raise ConsolidationError("source_database_missing")
        return None
    try:
        path_status = path.lstat()
    except OSError:
        raise ConsolidationError("database_path_invalid") from None
    if (
        not stat.S_ISREG(path_status.st_mode)
        or stat.S_ISLNK(path_status.st_mode)
        or path_status.st_uid != os.getuid()
        or path_status.st_nlink != 1
    ):
        raise ConsolidationError("database_path_invalid")
    if stat.S_IMODE(path_status.st_mode) != 0o600:
        raise ConsolidationError("database_permissions_invalid")
    descriptor = -1
    try:
        descriptor = os.open(path, _open_flags())
        descriptor_status = os.fstat(descriptor)
    except OSError:
        _safe_close(descriptor)
        raise ConsolidationError("database_path_invalid") from None
    if (
        not _same_identity(path_status, descriptor_status)
        or not stat.S_ISREG(descriptor_status.st_mode)
        or descriptor_status.st_uid != os.getuid()
        or descriptor_status.st_nlink != 1
        or stat.S_IMODE(descriptor_status.st_mode) != 0o600
    ):
        _safe_close(descriptor)
        raise ConsolidationError("database_path_invalid")
    if _descriptor_prefix(descriptor, len(_SQLITE_HEADER)) != _SQLITE_HEADER:
        _safe_close(descriptor)
        raise ConsolidationError("database_format_invalid")
    return _ValidatedDatabase(
        path=path,
        descriptor=descriptor,
        device=descriptor_status.st_dev,
        inode=descriptor_status.st_ino,
    )


def _revalidate_database(database: _ValidatedDatabase) -> None:
    try:
        path_status = database.path.lstat()
        descriptor_status = os.fstat(database.descriptor)
    except OSError:
        raise ConsolidationError("database_changed") from None
    if (
        not _same_identity(path_status, descriptor_status)
        or (descriptor_status.st_dev, descriptor_status.st_ino)
        != (database.device, database.inode)
        or not stat.S_ISREG(descriptor_status.st_mode)
        or descriptor_status.st_uid != os.getuid()
        or descriptor_status.st_nlink != 1
        or stat.S_IMODE(descriptor_status.st_mode) != 0o600
    ):
        raise ConsolidationError("database_changed")


def _hash_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        try:
            chunk = os.pread(descriptor, 1024 * 1024, offset)
        except AttributeError:
            original = os.lseek(descriptor, 0, os.SEEK_CUR)
            try:
                os.lseek(descriptor, offset, os.SEEK_SET)
                chunk = os.read(descriptor, 1024 * 1024)
            finally:
                os.lseek(descriptor, original, os.SEEK_SET)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)
        offset += len(chunk)


def _validate_sidecar(path: Path, *, writable: bool) -> int:
    try:
        path_status = path.lstat()
    except OSError:
        raise ConsolidationError("database_sidecar_invalid") from None
    if (
        not stat.S_ISREG(path_status.st_mode)
        or stat.S_ISLNK(path_status.st_mode)
        or path_status.st_uid != os.getuid()
        or path_status.st_nlink != 1
        or stat.S_IMODE(path_status.st_mode) != 0o600
    ):
        raise ConsolidationError("database_sidecar_invalid")
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            _open_flags(writable=writable),
        )
        descriptor_status = os.fstat(descriptor)
    except OSError:
        _safe_close(descriptor)
        raise ConsolidationError("database_sidecar_invalid") from None
    if (
        not _same_identity(path_status, descriptor_status)
        or not stat.S_ISREG(descriptor_status.st_mode)
        or descriptor_status.st_nlink != 1
    ):
        _safe_close(descriptor)
        raise ConsolidationError("database_sidecar_invalid")
    return descriptor


def _binding_member_matches(
    original: Path,
    alias: Path,
    descriptor: int,
    *,
    expected_nlink: int,
) -> bool:
    try:
        original_status = original.lstat()
        alias_status = alias.lstat()
        descriptor_status = os.fstat(descriptor)
    except OSError:
        return False
    return (
        stat.S_ISREG(original_status.st_mode)
        and stat.S_ISREG(alias_status.st_mode)
        and stat.S_ISREG(descriptor_status.st_mode)
        and original_status.st_uid == os.getuid()
        and alias_status.st_uid == os.getuid()
        and descriptor_status.st_uid == os.getuid()
        and stat.S_IMODE(original_status.st_mode) == 0o600
        and stat.S_IMODE(alias_status.st_mode) == 0o600
        and stat.S_IMODE(descriptor_status.st_mode) == 0o600
        and original_status.st_nlink == expected_nlink
        and alias_status.st_nlink == expected_nlink
        and descriptor_status.st_nlink == expected_nlink
        and _same_identity(original_status, descriptor_status)
        and _same_identity(alias_status, descriptor_status)
    )


def _ensure_guards_owned(
    guards: Sequence[RuntimeTenureGuard],
) -> None:
    for guard in guards:
        guard.ensure_owned()


@contextmanager
def _bind_source_descriptors(
    root: _ValidatedRoot,
    database: _ValidatedDatabase,
    directory: Path,
    *,
    guards: Sequence[RuntimeTenureGuard],
) -> Iterator[Path]:
    directory_descriptor = -1
    sidecar_descriptors: list[int] = []
    members: list[tuple[Path, Path, int]] = []
    cleanup_failed = False
    try:
        _ensure_guards_owned(guards)
        directory_descriptor = os.open(
            directory,
            _open_flags(directory=True),
        )
        directory_status = os.fstat(directory_descriptor)
        path_status = directory.lstat()
        if (
            not stat.S_ISDIR(directory_status.st_mode)
            or directory_status.st_uid != os.getuid()
            or stat.S_IMODE(directory_status.st_mode) != 0o700
            or not _same_identity(directory_status, path_status)
        ):
            raise ConsolidationError("descriptor_binding_invalid")
        for suffix in ("", "-wal", "-shm", "-journal"):
            original = database.path.with_name(
                f"{database.path.name}{suffix}"
            )
            alias = directory / f"{_BOUND_SOURCE_NAME}{suffix}"
            if suffix:
                if not _path_lexists(original):
                    continue
                descriptor = _validate_sidecar(
                    original,
                    writable=False,
                )
                sidecar_descriptors.append(descriptor)
            else:
                descriptor = database.descriptor
            if not _binding_member_matches(
                original,
                original,
                descriptor,
                expected_nlink=1,
            ):
                raise ConsolidationError("descriptor_binding_invalid")
            _ensure_guards_owned(guards)
            try:
                os.link(
                    original.name,
                    alias.name,
                    src_dir_fd=root.descriptor,
                    dst_dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except OSError:
                raise ConsolidationError(
                    "descriptor_binding_invalid"
                ) from None
            members.append((original, alias, descriptor))
            if not _binding_member_matches(
                original,
                alias,
                descriptor,
                expected_nlink=2,
            ):
                raise ConsolidationError("descriptor_binding_invalid")
        if not members or members[0][0] != database.path:
            raise ConsolidationError("descriptor_binding_invalid")
        _ensure_guards_owned(guards)
        yield directory / _BOUND_SOURCE_NAME
    finally:
        for original, alias, descriptor in reversed(members):
            try:
                _ensure_guards_owned(guards)
                if not _binding_member_matches(
                    original,
                    alias,
                    descriptor,
                    expected_nlink=2,
                ):
                    cleanup_failed = True
                    continue
                os.unlink(
                    alias.name,
                    dir_fd=directory_descriptor,
                )
                if not _binding_member_matches(
                    original,
                    original,
                    descriptor,
                    expected_nlink=1,
                ):
                    cleanup_failed = True
            except (ConsolidationError, OSError):
                cleanup_failed = True
        for descriptor in sidecar_descriptors:
            _safe_close(descriptor)
        _safe_close(directory_descriptor)
        if cleanup_failed:
            raise ConsolidationError(
                "descriptor_binding_cleanup_failed"
            )


def _lock_byte_available(descriptor: int, offset: int) -> bool:
    try:
        fcntl.lockf(
            descriptor,
            fcntl.LOCK_EX | fcntl.LOCK_NB,
            1,
            offset,
            os.SEEK_SET,
        )
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise ConsolidationError("database_writer_unknown") from None
    try:
        fcntl.lockf(
            descriptor,
            fcntl.LOCK_UN,
            1,
            offset,
            os.SEEK_SET,
        )
    except OSError:
        raise ConsolidationError("database_writer_unknown") from None
    return True


def _database_writer_active(path: Path) -> bool:
    shm = path.with_name(f"{path.name}-shm")
    if _path_lexists(shm):
        descriptor = _validate_sidecar(shm, writable=True)
        try:
            if not _lock_byte_available(
                descriptor,
                _WAL_WRITE_LOCK_OFFSET,
            ):
                return True
        finally:
            os.close(descriptor)
    descriptor = -1
    try:
        descriptor = os.open(path, _open_flags(writable=True))
        return not _lock_byte_available(
            descriptor,
            _SQLITE_RESERVED_BYTE,
        )
    except PermissionError:
        raise ConsolidationError("database_writer_unknown") from None
    except OSError:
        raise ConsolidationError("database_writer_unknown") from None
    finally:
        _safe_close(descriptor)


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        uri=True,
        isolation_level=None,
    )
    connection.execute("PRAGMA query_only=ON")
    return connection


def _open_read_anchor(path: Path) -> sqlite3.Connection:
    connection = _open_read_only(path)
    try:
        connection.execute("BEGIN")
        connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema"
        ).fetchone()
    except sqlite3.DatabaseError:
        connection.close()
        raise ConsolidationError("database_anchor_failed") from None
    return connection


def _quick_check_rows(
    connection: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in connection.execute("PRAGMA quick_check")
    )


def _integrity_check(path: Path) -> None:
    try:
        connection = _open_read_only(path)
        try:
            if _quick_check_rows(connection) != (("ok",),):
                raise ConsolidationError(
                    "database_quick_check_failed"
                )
            foreign_keys = tuple(
                tuple(row)
                for row in connection.execute(
                    "PRAGMA foreign_key_check"
                )
            )
            if foreign_keys:
                raise ConsolidationError(
                    "database_foreign_key_check_failed"
                )
        finally:
            connection.close()
    except ConsolidationError:
        raise
    except sqlite3.DatabaseError:
        raise ConsolidationError(
            "database_quick_check_failed"
        ) from None


def _read_only_engine(path: Path):
    uri = f"{path.as_uri()}?mode=ro"
    return create_engine(
        "sqlite://",
        creator=lambda: sqlite3.connect(uri, uri=True),
        poolclass=NullPool,
        future=True,
    )


def _require_schema(path: Path) -> str:
    engine = _read_only_engine(path)
    try:
        require_current_schema(engine)
        return schema_status(engine).head
    except SchemaOutOfDate:
        raise ConsolidationError(
            "database_schema_not_current"
        ) from None
    except ConsolidationError:
        raise
    except Exception:
        raise ConsolidationError(
            "database_schema_not_current"
        ) from None
    finally:
        engine.dispose()


def _normalize_schema_sql(value: object) -> str:
    if not isinstance(value, str):
        return ""
    lines = tuple(
        " ".join(line.strip().rstrip(",").split())
        for line in value.splitlines()
        if line.strip()
    )
    if len(lines) < 3 or not lines[0].upper().startswith(
        "CREATE TABLE"
    ):
        return " ".join(value.split())
    columns: list[str] = []
    constraints: list[str] = []
    constraint_prefixes = (
        "CONSTRAINT ",
        "PRIMARY KEY",
        "FOREIGN KEY",
        "UNIQUE ",
        "UNIQUE(",
        "CHECK ",
        "CHECK(",
    )
    for line in lines[1:-1]:
        if line.upper().startswith(constraint_prefixes):
            constraints.append(line)
        else:
            columns.append(line)
    return "|".join(
        (
            lines[0],
            *columns,
            *sorted(constraints),
            lines[-1],
        )
    )


def _schema_identity(path: Path) -> tuple[int, int, str]:
    try:
        connection = _open_read_only(path)
        try:
            application_id = int(
                connection.execute(
                    "PRAGMA application_id"
                ).fetchone()[0]
            )
            user_version = int(
                connection.execute(
                    "PRAGMA user_version"
                ).fetchone()[0]
            )
            rows = tuple(
                (
                    str(row[0]),
                    str(row[1]),
                    str(row[2]),
                    _normalize_schema_sql(row[3]),
                )
                for row in connection.execute(
                    "SELECT type, name, tbl_name, sql "
                    "FROM sqlite_schema "
                    "WHERE name NOT LIKE 'sqlite_%' "
                    "ORDER BY type, name, tbl_name, sql"
                )
            )
        finally:
            connection.close()
    except (sqlite3.DatabaseError, TypeError, ValueError, IndexError):
        raise ConsolidationError("database_identity_invalid") from None
    return (
        application_id,
        user_version,
        hashlib.sha256(_canonical_json(rows)).hexdigest(),
    )


def _logical_summary(path: Path, schema_head: str) -> LogicalSummary:
    engine = _read_only_engine(path)
    try:
        with engine.connect() as connection:
            tables = tuple(
                str(row[0])
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_schema "
                    "WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY name"
                )
            )
            metadata = MetaData()
            counts = tuple(
                (
                    table_name,
                    int(
                        connection.execute(
                            select(func.count()).select_from(
                                Table(
                                    table_name,
                                    metadata,
                                    autoload_with=connection,
                                )
                            )
                        ).scalar_one()
                    ),
                )
                for table_name in tables
            )
    except Exception:
        raise ConsolidationError("logical_summary_invalid") from None
    finally:
        engine.dispose()
    payload = {
        "schema_head": schema_head,
        "table_counts": [list(item) for item in counts],
    }
    return LogicalSummary(
        schema_head=schema_head,
        table_counts=counts,
        digest=hashlib.sha256(_canonical_json(payload)).hexdigest(),
    )


def _parse_database_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ConsolidationError("runtime_tenure_uncertain")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ConsolidationError("runtime_tenure_uncertain") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _inspect_tenures_absent(
    path: Path,
    process_inspector: ProcessInspector,
) -> None:
    try:
        connection = _open_read_only(path)
        try:
            rows = tuple(
                connection.execute(
                    "SELECT role, state, pid, "
                    "process_start_identity, expires_at "
                    "FROM runtime_tenures WHERE state='held'"
                )
            )
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        raise ConsolidationError("runtime_tenure_uncertain") from None
    now = datetime.now(timezone.utc)
    for role, state, pid, start_identity, expires_at in rows:
        if state != "held":
            continue
        maintenance = role == "maintenance"
        active_code = (
            "maintenance_tenure_active"
            if maintenance
            else "runtime_tenure_active"
        )
        live_code = (
            "maintenance_process_live"
            if maintenance
            else "runtime_process_live"
        )
        unknown_code = (
            "maintenance_process_unknown"
            if maintenance
            else "runtime_process_unknown"
        )
        expiry = _parse_database_timestamp(expires_at)
        if expiry > now:
            raise ConsolidationError(active_code)
        try:
            identity = ProcessIdentity(
                pid=int(pid),
                start_identity=str(start_identity),
            )
            identity.validate()
            proof = process_inspector.inspect(identity)
        except Exception:
            raise ConsolidationError(unknown_code) from None
        if proof is ProcessProof.SAME:
            raise ConsolidationError(live_code)
        if proof is not ProcessProof.NOT_SAME:
            raise ConsolidationError(unknown_code)


def _prove_database_quiescent(
    database: _ValidatedDatabase,
    process_inspector: ProcessInspector,
) -> None:
    _revalidate_database(database)
    if _database_writer_active(database.path):
        raise ConsolidationError("database_writer_active")
    _inspect_tenures_absent(database.path, process_inspector)


def _prove_all_absent(
    source_root: _ValidatedRoot,
    destination_root: _ValidatedRoot,
    source_database: _ValidatedDatabase,
    destination_database: _ValidatedDatabase | None,
    *,
    process_inspector: ProcessInspector,
    port: int,
) -> None:
    for root in (source_root, destination_root):
        _revalidate_root(root)
        if not prove_app_absent(root.path, port=port):
            raise ConsolidationError(
                "cooperative_absence_unproven"
            )
    _prove_database_quiescent(source_database, process_inspector)
    if destination_database is not None:
        _prove_database_quiescent(
            destination_database,
            process_inspector,
        )


def _maintenance(
    path: Path,
    *,
    process_identity: ProcessIdentity,
    process_inspector: ProcessInspector,
) -> _HeldMaintenance:
    engine = create_db_engine(f"sqlite:///{path}")
    try:
        service = RuntimeTenureService(
            make_session_factory(engine),
            process_inspector=process_inspector,
        )
        handle = service.acquire_maintenance(
            process_identity,
            ttl_seconds=_LEASE_TTL_SECONDS,
        )
        guard = RuntimeTenureGuard(
            handle,
            ttl_seconds=_LEASE_TTL_SECONDS,
            renewal_interval_seconds=_LEASE_RENEWAL_SECONDS,
        )
        return _HeldMaintenance(engine=engine, guard=guard)
    except TenureUnavailable as exc:
        engine.dispose()
        raise ConsolidationError(exc.stable_code) from None
    except (TenureUncertain, Exception) as exc:
        engine.dispose()
        if isinstance(exc, ConsolidationError):
            raise
        raise ConsolidationError("runtime_tenure_uncertain") from None


def _prime_maintenance_slot(
    path: Path,
    *,
    process_identity: ProcessIdentity,
    process_inspector: ProcessInspector,
) -> None:
    held = _maintenance(
        path,
        process_identity=process_identity,
        process_inspector=process_inspector,
    )
    held.release()


def _ensure_private_directory(path: Path) -> Path:
    if _path_lexists(path):
        try:
            status = path.lstat()
        except OSError:
            raise ConsolidationError("private_directory_invalid") from None
        if (
            not stat.S_ISDIR(status.st_mode)
            or stat.S_ISLNK(status.st_mode)
            or status.st_uid != os.getuid()
            or stat.S_IMODE(status.st_mode) != 0o700
        ):
            raise ConsolidationError("private_directory_invalid")
    else:
        try:
            path.mkdir(mode=0o700)
        except OSError:
            raise ConsolidationError(
                "private_directory_invalid"
            ) from None
    try:
        canonical = path.resolve(strict=True)
        status = canonical.stat()
    except (OSError, RuntimeError):
        raise ConsolidationError("private_directory_invalid") from None
    if (
        status.st_uid != os.getuid()
        or stat.S_IMODE(status.st_mode) != 0o700
    ):
        raise ConsolidationError("private_directory_invalid")
    return canonical


def _valid_hash(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == _SHA256_HEX_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _verify_backup_receipt(
    receipt: EncryptedBackupReceipt,
    *,
    backup_key_id: str,
    directory: Path,
) -> None:
    if (
        not isinstance(receipt, EncryptedBackupReceipt)
        or receipt.verified is not True
        or receipt.backup_key_id != backup_key_id
        or not _valid_hash(receipt.path_hash)
        or not _valid_hash(receipt.source_sha256)
    ):
        raise ConsolidationError("encrypted_backup_unverified")
    try:
        artifact = receipt.path.resolve(strict=True)
        artifact.relative_to(directory.resolve(strict=True))
        status = artifact.lstat()
    except (OSError, RuntimeError, ValueError):
        raise ConsolidationError("encrypted_backup_unverified") from None
    if (
        not stat.S_ISREG(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or status.st_uid != os.getuid()
        or stat.S_IMODE(status.st_mode) & 0o077
    ):
        raise ConsolidationError("encrypted_backup_unverified")


def _fsync_directory(path: Path) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            _open_flags(directory=True),
        )
        os.fsync(descriptor)
    except OSError:
        raise ConsolidationError("directory_fsync_failed") from None
    finally:
        _safe_close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        try:
            written = os.write(descriptor, payload[offset:])
        except OSError:
            raise ConsolidationError("uncertainty_marker_failed") from None
        if written <= 0:
            raise ConsolidationError("uncertainty_marker_failed")
        offset += written


def _create_uncertainty_marker(marker: Path) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            marker,
            _open_flags(writable=True)
            | os.O_CREAT
            | os.O_EXCL,
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, b"migration_uncertain\n")
        os.fsync(descriptor)
    except FileExistsError:
        raise ConsolidationError("migration_uncertain") from None
    except ConsolidationError:
        raise
    except OSError:
        raise ConsolidationError("uncertainty_marker_failed") from None
    finally:
        _safe_close(descriptor)
    _fsync_directory(marker.parent)


def _ensure_uncertainty_marker(marker: Path) -> None:
    if not _path_lexists(marker):
        try:
            _create_uncertainty_marker(marker)
        except ConsolidationError:
            return
        return
    descriptor = -1
    try:
        descriptor = _validate_sidecar(marker, writable=True)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        _fsync_directory(marker.parent)
    except ConsolidationError:
        pass
    finally:
        _safe_close(descriptor)


def _unlink_validated_regular(path: Path) -> None:
    descriptor = _validate_sidecar(path, writable=False)
    try:
        before = os.fstat(descriptor)
        path_status = path.lstat()
        if not _same_identity(before, path_status):
            raise ConsolidationError("cleanup_identity_changed")
        os.unlink(path)
        after = os.fstat(descriptor)
        if after.st_nlink != 0:
            raise ConsolidationError("cleanup_identity_changed")
    except ConsolidationError:
        raise
    except OSError:
        raise ConsolidationError("cleanup_failed") from None
    finally:
        os.close(descriptor)


def _remove_marker(marker: Path) -> None:
    if _path_lexists(marker):
        _unlink_validated_regular(marker)
        _fsync_directory(marker.parent)


def _remove_staging(
    staging_directory: Path | None,
    staging_path: Path | None,
) -> None:
    if staging_directory is None:
        return
    candidates: tuple[Path, ...] = ()
    if staging_path is not None:
        candidates = (
            staging_path.with_name(f"{staging_path.name}-journal"),
            staging_path.with_name(f"{staging_path.name}-wal"),
            staging_path.with_name(f"{staging_path.name}-shm"),
            staging_path,
        )
    for candidate in candidates:
        if _path_lexists(candidate):
            _unlink_validated_regular(candidate)
    try:
        staging_directory.rmdir()
    except FileNotFoundError:
        return
    except OSError:
        raise ConsolidationError("staging_cleanup_failed") from None


def _validate_staging(
    staging: Path,
    descriptor: int,
) -> None:
    try:
        path_status = staging.lstat()
        descriptor_status = os.fstat(descriptor)
    except OSError:
        raise ConsolidationError("staging_identity_invalid") from None
    if (
        not _same_identity(path_status, descriptor_status)
        or not stat.S_ISREG(descriptor_status.st_mode)
        or descriptor_status.st_uid != os.getuid()
        or descriptor_status.st_nlink != 1
        or stat.S_IMODE(descriptor_status.st_mode) != 0o600
    ):
        raise ConsolidationError("staging_identity_invalid")


def _cleanup_destination_sidecars(
    destination: Path,
    source_guard: _HeldMaintenance,
) -> None:
    source_guard.guard.ensure_owned()
    for suffix in ("-wal", "-shm"):
        sidecar = destination.with_name(f"{destination.name}{suffix}")
        if not _path_lexists(sidecar):
            continue
        source_guard.guard.ensure_owned()
        _unlink_validated_regular(sidecar)
        source_guard.guard.ensure_owned()


def _replacement_matches(
    replacement: Path,
    database: _ValidatedDatabase,
) -> None:
    try:
        status = replacement.lstat()
        descriptor_status = os.fstat(database.descriptor)
    except OSError:
        raise ConsolidationError("replacement_identity_invalid") from None
    if (
        not _same_identity(status, descriptor_status)
        or not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.getuid()
        or status.st_nlink != 1
        or stat.S_IMODE(status.st_mode) != 0o600
    ):
        raise ConsolidationError("replacement_identity_invalid")


def _release_copied_source_lease(
    destination: Path,
    source_guard: RuntimeTenureGuard,
) -> None:
    handle = source_guard.handle
    now = datetime.now(timezone.utc)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(destination)
        journal_mode = connection.execute(
            "PRAGMA journal_mode=DELETE"
        ).fetchone()
        if journal_mode != ("delete",):
            raise ConsolidationError("maintenance_release_uncertain")
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT state, owner_id, generation, pid, "
            "process_start_identity "
            "FROM runtime_tenures WHERE resource_key=?",
            (handle.resource_key,),
        ).fetchone()
        if row != (
            "held",
            handle.owner_id,
            handle.generation,
            handle.identity.pid,
            handle.identity.start_identity,
        ):
            raise ConsolidationError("maintenance_release_uncertain")
        encoded_now = now.isoformat()
        result = connection.execute(
            "UPDATE runtime_tenures "
            "SET state='released', generation=generation+1, "
            "renewed_at=?, expires_at=?, released_at=? "
            "WHERE resource_key=? AND state='held' "
            "AND owner_id=? AND generation=? AND pid=? "
            "AND process_start_identity=?",
            (
                encoded_now,
                encoded_now,
                encoded_now,
                handle.resource_key,
                handle.owner_id,
                handle.generation,
                handle.identity.pid,
                handle.identity.start_identity,
            ),
        )
        if result.rowcount != 1:
            raise ConsolidationError("maintenance_release_uncertain")
        connection.commit()
    except ConsolidationError:
        if connection is not None:
            connection.rollback()
        raise
    except (sqlite3.DatabaseError, OSError):
        if connection is not None:
            try:
                connection.rollback()
            except sqlite3.DatabaseError:
                pass
        raise ConsolidationError("maintenance_release_uncertain") from None
    finally:
        if connection is not None:
            connection.close()


def _validate_inputs(
    source_root: Path,
    destination_root: Path,
    roots: ConsolidationRoots | None,
) -> tuple[
    ConsolidationRoots,
    _ValidatedRoot,
    _ValidatedRoot,
    _ValidatedDatabase,
    _ValidatedDatabase | None,
]:
    expected = roots or ConsolidationRoots(
        source_root=_PRODUCTION_SOURCE,
        destination_root=_PRODUCTION_DESTINATION,
    )
    if roots is None:
        if (
            Path(source_root).name != "safety-foundation"
            or Path(source_root).parent.name != ".worktrees"
        ):
            raise ConsolidationError("root_mismatch")
    if (
        isinstance(expected.app_port, bool)
        or not isinstance(expected.app_port, int)
        or not 1 <= expected.app_port <= 65535
    ):
        raise ConsolidationError("root_mismatch")
    _validate_database_name(expected.database_name)
    source = _validate_exact_root(
        Path(source_root),
        expected.source_root,
    )
    destination: _ValidatedRoot | None = None
    source_database: _ValidatedDatabase | None = None
    destination_database: _ValidatedDatabase | None = None
    try:
        destination = _validate_exact_root(
            Path(destination_root),
            expected.destination_root,
        )
        if roots is None and destination.path != (
            Path(
                "/Users/avi/Desktop/robinhood/trading-assistant"
            ).resolve(strict=True)
        ):
            raise ConsolidationError("root_mismatch")
        source_database = _validate_database(
            source,
            database_name=expected.database_name,
            required=True,
        )
        assert source_database is not None
        destination_database = _validate_database(
            destination,
            database_name=expected.database_name,
            required=False,
        )
        if destination_database is not None and (
            source_database.device,
            source_database.inode,
        ) == (
            destination_database.device,
            destination_database.inode,
        ):
            raise ConsolidationError("database_alias")
        return (
            expected,
            source,
            destination,
            source_database,
            destination_database,
        )
    except BaseException:
        if destination_database is not None:
            destination_database.close()
        if source_database is not None:
            source_database.close()
        if destination is not None:
            destination.close()
        source.close()
        raise


def consolidate_runtime(
    source_root: Path,
    destination_root: Path,
    *,
    backup_key: bytes,
    backup_key_id: str,
    process_identity: ProcessIdentity,
    process_inspector: ProcessInspector,
    roots: ConsolidationRoots | None = None,
) -> ConsolidationReceipt:
    """Install a verified source snapshot under durable dual maintenance."""

    if (
        not isinstance(backup_key, bytes)
        or len(backup_key) != 32
        or not isinstance(backup_key_id, str)
        or not backup_key_id
    ):
        raise ConsolidationError("backup_configuration_invalid")
    try:
        process_identity.validate()
    except ValueError:
        raise ConsolidationError("process_identity_invalid") from None
    (
        expected,
        source,
        destination,
        source_database,
        destination_database,
    ) = _validate_inputs(source_root, destination_root, roots)
    source_lease: _HeldMaintenance | None = None
    destination_lease: _HeldMaintenance | None = None
    staging_directory: Path | None = None
    staging_path: Path | None = None
    staging_descriptor = -1
    replacement: Path | None = None
    marker: Path | None = None
    source_anchor: sqlite3.Connection | None = None
    destination_anchor: sqlite3.Connection | None = None
    installed = False
    authority_changed = False
    current_stage = "preflight"
    try:
        local_directory = _ensure_private_directory(
            destination.path / ".local"
        )
        _ensure_private_directory(source.path / ".local")
        marker = destination.path / _UNCERTAINTY_MARKER
        if _path_lexists(marker):
            raise ConsolidationError("migration_uncertain")

        _prove_all_absent(
            source,
            destination,
            source_database,
            destination_database,
            process_inspector=process_inspector,
            port=expected.app_port,
        )
        _integrity_check(source_database.path)
        source_head = _require_schema(source_database.path)
        source_identity = _schema_identity(source_database.path)
        if destination_database is not None:
            _integrity_check(destination_database.path)
            destination_head = _require_schema(destination_database.path)
            destination_identity = _schema_identity(
                destination_database.path
            )
            if (
                destination_head != source_head
                or destination_identity != source_identity
            ):
                raise ConsolidationError(
                    "database_identity_mismatch"
                )
            destination_anchor = _open_read_anchor(
                destination_database.path
            )
        source_anchor = _open_read_anchor(source_database.path)

        _prime_maintenance_slot(
            source_database.path,
            process_identity=process_identity,
            process_inspector=process_inspector,
        )
        if destination_database is not None:
            _prime_maintenance_slot(
                destination_database.path,
                process_identity=process_identity,
                process_inspector=process_inspector,
            )
        _prove_all_absent(
            source,
            destination,
            source_database,
            destination_database,
            process_inspector=process_inspector,
            port=expected.app_port,
        )

        source_baseline = _logical_summary(
            source_database.path,
            source_head,
        )
        try:
            source_backup = backup_database(
                source_database.path,
                source.path / _BACKUP_DIRECTORY,
                backup_key=backup_key,
                backup_key_id=backup_key_id,
                process_identity=process_identity,
                process_inspector=process_inspector,
            )
            _verify_backup_receipt(
                source_backup,
                backup_key_id=backup_key_id,
                directory=source.path / _BACKUP_DIRECTORY,
            )
            _revalidate_database(source_database)
        except BaseException:
            raise ConsolidationError("source_backup_failed") from None
        _stage_event("source_backup_verified")
        _prove_all_absent(
            source,
            destination,
            source_database,
            destination_database,
            process_inspector=process_inspector,
            port=expected.app_port,
        )
        if _logical_summary(
            source_database.path,
            source_head,
        ) != source_baseline:
            raise ConsolidationError("source_changed")

        destination_backup: EncryptedBackupReceipt | None = None
        destination_baseline: LogicalSummary | None = None
        if destination_database is not None:
            destination_baseline = _logical_summary(
                destination_database.path,
                source_head,
            )
            try:
                destination_backup = backup_database(
                    destination_database.path,
                    destination.path / _BACKUP_DIRECTORY,
                    backup_key=backup_key,
                    backup_key_id=backup_key_id,
                    process_identity=process_identity,
                    process_inspector=process_inspector,
                )
                _verify_backup_receipt(
                    destination_backup,
                    backup_key_id=backup_key_id,
                    directory=(
                        destination.path / _BACKUP_DIRECTORY
                    ),
                )
                _revalidate_database(destination_database)
            except BaseException:
                raise ConsolidationError(
                    "destination_backup_failed"
                ) from None
        _stage_event("destination_backup_verified")
        _prove_all_absent(
            source,
            destination,
            source_database,
            destination_database,
            process_inspector=process_inspector,
            port=expected.app_port,
        )
        if _logical_summary(
            source_database.path,
            source_head,
        ) != source_baseline:
            raise ConsolidationError("source_changed")
        if (
            destination_database is not None
            and destination_baseline is not None
            and _logical_summary(
                destination_database.path,
                source_head,
            )
            != destination_baseline
        ):
            raise ConsolidationError("destination_changed")

        source_lease = _maintenance(
            source_database.path,
            process_identity=process_identity,
            process_inspector=process_inspector,
        )
        if destination_database is not None:
            try:
                destination_lease = _maintenance(
                    destination_database.path,
                    process_identity=process_identity,
                    process_inspector=process_inspector,
                )
            except BaseException:
                source_lease.release()
                source_lease = None
                raise
        source_lease.renew()
        if destination_lease is not None:
            destination_lease.renew()

        current_stage = "source_check"
        _stage_event(current_stage)
        _revalidate_database(source_database)
        _integrity_check(source_database.path)
        if _require_schema(source_database.path) != source_head:
            raise ConsolidationError("source_changed")

        try:
            staging_directory = Path(
                tempfile.mkdtemp(
                    prefix=_STAGING_PREFIX,
                    dir=local_directory,
                )
            )
            os.chmod(staging_directory, 0o700)
            staging_path = staging_directory / _STAGING_NAME
            staging_descriptor = os.open(
                staging_path,
                _open_flags(writable=True)
                | os.O_CREAT
                | os.O_EXCL,
                0o600,
            )
            os.fchmod(staging_descriptor, 0o600)
        except OSError:
            raise ConsolidationError("staging_create_failed") from None

        current_stage = "sqlite_copy"
        _stage_event(current_stage)
        _revalidate_database(source_database)
        _validate_staging(staging_path, staging_descriptor)
        staging_descriptor_uri = _descriptor_sqlite_uri(
            staging_descriptor,
            writable=True,
        )
        binding_guards = (
            (source_lease.guard, destination_lease.guard)
            if destination_lease is not None
            else (source_lease.guard,)
        )
        try:
            with _bind_source_descriptors(
                source,
                source_database,
                staging_directory,
                guards=binding_guards,
            ) as bound_source:
                source_descriptor_uri = (
                    f"{bound_source.as_uri()}"
                    "?mode=ro&nofollow=1"
                )
                with (
                    sqlite3.connect(
                        source_descriptor_uri,
                        uri=True,
                    ) as source_connection,
                    sqlite3.connect(
                        staging_descriptor_uri,
                        uri=True,
                    ) as staging_connection,
                ):
                    source_connection.execute("PRAGMA query_only=ON")
                    if staging_connection.execute(
                        "PRAGMA journal_mode=OFF"
                    ).fetchone() != ("off",):
                        raise ConsolidationError("sqlite_copy_failed")
                    source_connection.backup(staging_connection)
        except ConsolidationError:
            raise
        except sqlite3.DatabaseError:
            raise ConsolidationError("sqlite_copy_failed") from None
        _revalidate_database(source_database)
        _validate_staging(staging_path, staging_descriptor)

        current_stage = "staging_check"
        _stage_event(current_stage)
        _integrity_check(staging_path)
        staging_head = _require_schema(staging_path)
        if staging_head != source_head:
            raise ConsolidationError("staging_schema_mismatch")
        if _schema_identity(staging_path) != source_identity:
            raise ConsolidationError("staging_identity_mismatch")

        source_summary = _logical_summary(
            source_database.path,
            source_head,
        )
        staging_summary = _logical_summary(
            staging_path,
            staging_head,
        )
        current_stage = "summary_compare"
        _stage_event(current_stage)
        if source_summary != staging_summary:
            raise ConsolidationError("logical_summary_mismatch")
        source_lease.guard.ensure_owned()
        if destination_lease is not None:
            destination_lease.guard.ensure_owned()
        try:
            staging_connection = sqlite3.connect(staging_path)
            try:
                staging_journal_mode = staging_connection.execute(
                    "PRAGMA journal_mode=DELETE"
                ).fetchone()
            finally:
                staging_connection.close()
        except sqlite3.DatabaseError:
            raise ConsolidationError(
                "staging_journal_invalid"
            ) from None
        if staging_journal_mode != ("delete",):
            raise ConsolidationError("staging_journal_invalid")
        for suffix in ("-wal", "-shm"):
            staging_sidecar = staging_path.with_name(
                f"{staging_path.name}{suffix}"
            )
            if _path_lexists(staging_sidecar):
                source_lease.guard.ensure_owned()
                if destination_lease is not None:
                    destination_lease.guard.ensure_owned()
                _unlink_validated_regular(staging_sidecar)
        source_lease.guard.ensure_owned()
        if destination_lease is not None:
            destination_lease.guard.ensure_owned()

        current_stage = "file_fsync"
        _stage_event(current_stage)
        try:
            os.fsync(staging_descriptor)
        except OSError:
            raise ConsolidationError("file_fsync_failed") from None
        _validate_staging(staging_path, staging_descriptor)

        current_stage = "directory_fsync_before_install"
        _stage_event(current_stage)
        _fsync_directory(staging_directory)
        _fsync_directory(destination.path)
        _revalidate_root(source)
        _revalidate_root(destination)
        _revalidate_database(source_database)
        if destination_database is not None:
            _revalidate_database(destination_database)
        source_lease.guard.ensure_owned()
        if destination_lease is not None:
            destination_lease.guard.ensure_owned()

        current_stage = "install"
        _stage_event(current_stage)
        _create_uncertainty_marker(marker)
        if destination_database is not None:
            replacement = destination.path / (
                _REPLACEMENT_PREFIX + uuid4().hex
            )
            if _path_lexists(replacement):
                raise ConsolidationError(
                    "replacement_name_occupied"
                )
            try:
                os.rename(destination_database.path, replacement)
            except OSError:
                raise ConsolidationError("install_failed") from None
            authority_changed = True
            _fsync_directory(destination.path)
            _replacement_matches(replacement, destination_database)
        if _path_lexists(destination.path / expected.database_name):
            raise ConsolidationError("install_destination_occupied")
        try:
            os.rename(
                staging_path,
                destination.path / expected.database_name,
            )
        except OSError:
            raise ConsolidationError("install_failed") from None
        installed = True
        authority_changed = True
        staging_path = destination.path / expected.database_name
        _validate_staging(staging_path, staging_descriptor)
        _fsync_directory(destination.path)

        if destination_lease is not None:
            if destination_anchor is not None:
                destination_anchor.close()
                destination_anchor = None
            destination_lease.abandon_relocated()
        current_stage = "sidecar_cleanup"
        _stage_event(current_stage)
        _cleanup_destination_sidecars(
            destination.path / expected.database_name,
            source_lease,
        )

        current_stage = "directory_fsync_after_install"
        _stage_event(current_stage)
        _fsync_directory(destination.path)

        installed_hash = _hash_descriptor(staging_descriptor)
        source_hash = _hash_descriptor(source_database.descriptor)
        installed_summary = _logical_summary(
            destination.path / expected.database_name,
            source_head,
        )
        if installed_summary != staging_summary:
            raise ConsolidationError("logical_summary_mismatch")
        receipt = ConsolidationReceipt(
            source_hash=source_hash,
            destination_hash=installed_hash,
            source_backup_hash=source_backup.path_hash,
            destination_backup_hash=(
                destination_backup.path_hash
                if destination_backup is not None
                else None
            ),
            summary_digest=source_summary.digest,
            installed=True,
            status="verified",
        )

        os.close(staging_descriptor)
        staging_descriptor = -1
        if staging_directory is not None:
            try:
                staging_directory.rmdir()
            except OSError:
                raise ConsolidationError(
                    "staging_cleanup_failed"
                ) from None
            staging_directory = None
        if replacement is not None:
            _replacement_matches(replacement, destination_database)
            _unlink_validated_regular(replacement)
            replacement = None
            _fsync_directory(destination.path)
        copied_source_guard = source_lease.guard
        source_lease.release()
        source_lease = None
        if source_anchor is not None:
            source_anchor.close()
            source_anchor = None
        _remove_marker(marker)
        _release_installed_copy(
            destination.path / expected.database_name,
            copied_source_guard,
        )
        return receipt
    except BaseException as exc:
        if installed or authority_changed:
            if marker is not None:
                _ensure_uncertainty_marker(marker)
            if source_lease is not None:
                try:
                    source_lease.release()
                except ConsolidationError:
                    pass
                source_lease = None
            if destination_lease is not None:
                destination_lease.abandon_relocated()
                destination_lease = None
            raise ConsolidationError("migration_uncertain") from None

        if marker is not None:
            try:
                _remove_marker(marker)
            except ConsolidationError:
                pass
        if source_lease is not None:
            try:
                source_lease.release()
            except ConsolidationError:
                pass
            source_lease = None
        if destination_lease is not None:
            try:
                destination_lease.release()
            except ConsolidationError:
                pass
            destination_lease = None
        _safe_close(staging_descriptor)
        staging_descriptor = -1
        try:
            _remove_staging(staging_directory, staging_path)
        except ConsolidationError:
            pass
        if isinstance(exc, ConsolidationError):
            raise
        raise ConsolidationError(
            f"{current_stage}_failed"
        ) from None
    finally:
        _safe_close(staging_descriptor)
        if source_anchor is not None:
            source_anchor.close()
        if destination_anchor is not None:
            destination_anchor.close()
        source_database.close()
        if destination_database is not None:
            destination_database.close()
        source.close()
        destination.close()


def _release_installed_copy(
    destination: Path,
    guard: RuntimeTenureGuard,
) -> None:
    """Release the exact maintenance row copied into the installed database."""

    _release_copied_source_lease(destination, guard)


class _ValueFreeParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ConsolidationError("cli_arguments_invalid")


def main(argv: list[str] | None = None) -> int:
    parser = _ValueFreeParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--destination-root", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        config = load_config()
        secrets = load_role_secrets("backup", config=config)
        key_buffer = validate_base64_key(
            "backup_encryption_key",
            secrets.backup_encryption_key,
        )
        inspector = LocalProcessInspector()
        identity = inspector.current()
        try:
            receipt = consolidate_runtime(
                args.source_root,
                args.destination_root,
                backup_key=bytes(key_buffer),
                backup_key_id=config.encryption.backup_key_id,
                process_identity=identity,
                process_inspector=inspector,
            )
        finally:
            for index in range(len(key_buffer)):
                key_buffer[index] = 0
    except ConsolidationError as exc:
        print(exc.stable_code)
        return 1
    print(
        json.dumps(
            {
                "destination_hash": receipt.destination_hash,
                "installed": receipt.installed,
                "source_hash": receipt.source_hash,
                "status": receipt.status,
                "summary_digest": receipt.summary_digest,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
