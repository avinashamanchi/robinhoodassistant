"""Offline, resumable encryption and rotation for registered database fields."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Literal

from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.orm import Session

from ..config import AppConfig, load_config
from ..db.models import SensitiveMigrationState, utcnow
from ..db.schema import require_current_schema, schema_status
from ..db.session import create_db_engine, make_session_factory
from ..security.crypto import (
    SensitiveDataCipher,
    SensitiveDataInvalid,
    SensitiveFieldRef,
    build_sensitive_data_cipher,
)
from ..security.secrets import (
    RuntimeSecrets,
    load_role_secrets,
    validate_base64_key,
)
from ..security.sensitive_fields import SENSITIVE_FIELDS
from .backup import create_encrypted_database_backup
from .tenure import (
    ProcessIdentity,
    ProcessInspector,
    LocalProcessInspector,
    RuntimeTenureGuard,
    RuntimeTenureService,
    TenureUnavailable,
    TenureUncertain,
)


_ENVELOPE_KEY = re.compile(
    r"enc:v1:([A-Za-z0-9][A-Za-z0-9._-]{7,63}):"
)
_HASH = re.compile(r"[0-9a-f]{64}")
_MAINTENANCE_TTL_SECONDS = 30
_MAX_BATCH_ROWS = 100


class SensitiveMigrationError(RuntimeError):
    """Stable, value-free failure for migration, verification, or rotation."""

    def __init__(self, stable_code: str) -> None:
        self.stable_code = stable_code
        super().__init__(stable_code)


@dataclass(frozen=True)
class SensitiveMigrationReceipt:
    operation: Literal["migrate", "rotate", "verify"]
    status: Literal["complete", "verified", "verified_noop"]
    active_key_id: str
    rows_total: int
    backup_path_hash: str | None
    old_key_id: str | None = None
    old_key_status: Literal["retained"] | None = None


@dataclass(frozen=True)
class _TableSpec:
    table: str
    primary_key: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class _RowRef:
    spec: _TableSpec
    primary_value: object
    row_id: str


@dataclass(frozen=True)
class _Scan:
    rows_total: int
    rows_completed: int
    pending: tuple[_RowRef, ...]


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _timestamp(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise SensitiveMigrationError("sensitive_migration_clock_invalid")
    return value.astimezone(timezone.utc)


def _database_path(engine: Engine, supplied: str | Path) -> Path:
    configured = engine.url.database
    if (
        engine.url.get_backend_name() != "sqlite"
        or not configured
        or configured == ":memory:"
    ):
        raise SensitiveMigrationError("sensitive_database_unsupported")
    try:
        actual = Path(configured).expanduser().resolve(strict=True)
        requested = Path(supplied).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise SensitiveMigrationError("sensitive_database_invalid") from None
    if actual != requested or not actual.is_file():
        raise SensitiveMigrationError("sensitive_database_mismatch")
    return actual


def _registry(engine: Engine) -> tuple[_TableSpec, ...]:
    try:
        database = inspect(engine)
        tables = set(database.get_table_names())
        if not set(SENSITIVE_FIELDS) <= tables:
            raise SensitiveMigrationError(
                "sensitive_registry_schema_invalid"
            )
        specs: list[_TableSpec] = []
        for table in sorted(SENSITIVE_FIELDS):
            primary = database.get_pk_constraint(table).get(
                "constrained_columns"
            )
            columns = {
                column["name"] for column in database.get_columns(table)
            }
            registered = tuple(sorted(SENSITIVE_FIELDS[table]))
            if (
                not isinstance(primary, list)
                or len(primary) != 1
                or primary[0] not in columns
                or not set(registered) <= columns
            ):
                raise SensitiveMigrationError(
                    "sensitive_registry_schema_invalid"
                )
            specs.append(
                _TableSpec(
                    table=table,
                    primary_key=str(primary[0]),
                    columns=registered,
                )
            )
        return tuple(specs)
    except SensitiveMigrationError:
        raise
    except Exception:
        raise SensitiveMigrationError(
            "sensitive_registry_schema_invalid"
        ) from None


def _state(engine: Engine) -> SensitiveMigrationState:
    try:
        with Session(engine) as session:
            rows = session.scalars(select(SensitiveMigrationState)).all()
            if len(rows) != 1 or rows[0].singleton_id != 1:
                raise SensitiveMigrationError(
                    "sensitive_migration_state_invalid"
                )
            session.expunge(rows[0])
            return rows[0]
    except SensitiveMigrationError:
        raise
    except Exception:
        raise SensitiveMigrationError(
            "sensitive_migration_state_invalid"
        ) from None


def _envelope_key(value: str) -> str | None:
    match = _ENVELOPE_KEY.match(value)
    return match.group(1) if match is not None else None


def _verify_envelope(
    cipher: SensitiveDataCipher,
    value: object,
    ref: SensitiveFieldRef,
    *,
    allowed_key_ids: frozenset[str],
) -> str:
    if not isinstance(value, str) or not value.startswith("enc:"):
        raise SensitiveMigrationError("sensitive_migration_data_invalid")
    key_id = _envelope_key(value)
    if key_id not in allowed_key_ids:
        raise SensitiveMigrationError("sensitive_migration_data_invalid")
    try:
        return cipher.decrypt(value, ref)
    except SensitiveDataInvalid:
        raise SensitiveMigrationError(
            "sensitive_migration_data_invalid"
        ) from None


def _rows(
    engine: Engine,
    specs: Sequence[_TableSpec],
    *,
    ensure_maintenance: Callable[[], None] | None = None,
) -> Iterable[
    tuple[_TableSpec, object, str, tuple[object, ...]]
]:
    scanned = 0
    with engine.connect() as connection:
        for spec in specs:
            selected = ", ".join(
                [_quote(spec.primary_key)]
                + [_quote(column) for column in spec.columns]
            )
            statement = text(
                f"SELECT {selected} FROM {_quote(spec.table)} "
                f"ORDER BY {_quote(spec.primary_key)}"
            )
            for row in connection.execute(statement):
                scanned += 1
                if (
                    ensure_maintenance is not None
                    and scanned % _MAX_BATCH_ROWS == 0
                ):
                    ensure_maintenance()
                yield spec, row[0], str(row[0]), tuple(row[1:])


def _scan(
    engine: Engine,
    cipher: SensitiveDataCipher,
    specs: Sequence[_TableSpec],
    *,
    mode: Literal["migrate", "rotate", "verify"],
    old_key_id: str,
    new_key_id: str | None = None,
    ensure_maintenance: Callable[[], None] | None = None,
) -> _Scan:
    total = 0
    completed = 0
    pending: list[_RowRef] = []
    allowed = (
        frozenset({old_key_id})
        if new_key_id is None
        else frozenset({old_key_id, new_key_id})
    )
    target_key_id = new_key_id or old_key_id
    for spec, primary_value, row_id, values in _rows(
        engine,
        specs,
        ensure_maintenance=ensure_maintenance,
    ):
        if all(value is None for value in values):
            continue
        total += 1
        row_pending = False
        for column, value in zip(spec.columns, values, strict=True):
            if value is None:
                continue
            ref = SensitiveFieldRef(
                spec.table,
                row_id,
                column,
                1,
            )
            if mode == "migrate" and (
                not isinstance(value, str) or not value.startswith("enc:")
            ):
                if not isinstance(value, str) or not value:
                    raise SensitiveMigrationError(
                        "sensitive_migration_data_invalid"
                    )
                row_pending = True
                continue
            key_id = _envelope_key(value) if isinstance(value, str) else None
            _verify_envelope(
                cipher,
                value,
                ref,
                allowed_key_ids=allowed,
            )
            if key_id != target_key_id:
                if mode == "verify":
                    raise SensitiveMigrationError(
                        "sensitive_migration_mixed_key"
                    )
                row_pending = True
        if row_pending:
            pending.append(
                _RowRef(
                    spec=spec,
                    primary_value=primary_value,
                    row_id=row_id,
                )
            )
        else:
            completed += 1
    return _Scan(total, completed, tuple(pending))


def _require_complete_state(
    state: SensitiveMigrationState,
    *,
    configured_active_key_id: str,
) -> None:
    if state.state != "complete":
        raise SensitiveMigrationError(
            f"sensitive_migration_{state.state}"
            if state.state
            in {"required", "migrating", "rotating", "failed"}
            else "sensitive_migration_state_invalid"
        )
    if state.schema_version != 1:
        raise SensitiveMigrationError("sensitive_schema_mismatch")
    if state.active_key_id != configured_active_key_id:
        raise SensitiveMigrationError("sensitive_active_key_mismatch")
    if (
        not isinstance(state.rows_total, int)
        or isinstance(state.rows_total, bool)
        or state.rows_total < 0
        or state.rows_completed != state.rows_total
        or not isinstance(state.backup_path_hash, str)
        or _HASH.fullmatch(state.backup_path_hash) is None
        or state.started_at is None
        or state.completed_at is None
        or state.updated_at is None
        or not (
            state.started_at <= state.completed_at <= state.updated_at
        )
    ):
        raise SensitiveMigrationError(
            "sensitive_migration_state_invalid"
        )


def verify_sensitive_fields(
    engine: Engine,
    cipher: SensitiveDataCipher,
    *,
    configured_active_key_id: str,
) -> SensitiveMigrationReceipt:
    """Read-only authoritative state and cryptographic verification."""

    require_current_schema(engine)
    state = _state(engine)
    _require_complete_state(
        state,
        configured_active_key_id=configured_active_key_id,
    )
    if cipher.active_key_id != configured_active_key_id:
        raise SensitiveMigrationError("sensitive_active_key_mismatch")
    scan = _scan(
        engine,
        cipher,
        _registry(engine),
        mode="verify",
        old_key_id=configured_active_key_id,
    )
    if (
        scan.pending
        or scan.rows_completed != scan.rows_total
        or scan.rows_total != state.rows_total
    ):
        raise SensitiveMigrationError(
            "sensitive_migration_evidence_invalid"
        )
    return SensitiveMigrationReceipt(
        operation="verify",
        status="verified",
        active_key_id=configured_active_key_id,
        rows_total=scan.rows_total,
        backup_path_hash=state.backup_path_hash,
    )


def inspect_sensitive_envelopes(
    engine: Engine,
    cipher: SensitiveDataCipher,
    *,
    active_key_id: str,
    schema_version: int,
) -> int:
    """Cryptographically scan every non-null registered field, read-only."""

    if (
        active_key_id not in cipher.key_ids
        or isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version <= 0
    ):
        raise SensitiveMigrationError("sensitive_key_unavailable")
    total = 0
    for spec, _primary_value, row_id, values in _rows(
        engine,
        _registry(engine),
    ):
        if all(value is None for value in values):
            continue
        total += 1
        for column, value in zip(spec.columns, values, strict=True):
            if value is None:
                continue
            if not isinstance(value, str) or not value.startswith("enc:"):
                raise SensitiveMigrationError(
                    "sensitive_plaintext_detected"
                )
            key_id = _envelope_key(value)
            if key_id is None:
                raise SensitiveMigrationError(
                    "sensitive_envelope_invalid"
                )
            if key_id not in cipher.key_ids:
                raise SensitiveMigrationError(
                    "sensitive_key_unavailable"
                )
            if key_id != active_key_id:
                raise SensitiveMigrationError("sensitive_mixed_key")
            try:
                cipher.decrypt(
                    value,
                    SensitiveFieldRef(
                        spec.table,
                        row_id,
                        column,
                        schema_version,
                    ),
                )
            except SensitiveDataInvalid:
                raise SensitiveMigrationError(
                    "sensitive_envelope_invalid"
                ) from None
    return total


def _set_operation_state(
    engine: Engine,
    *,
    operation: Literal["migrating", "rotating"],
    active_key_id: str,
    scan: _Scan,
    backup_path_hash: str,
    now: datetime,
) -> None:
    with Session(engine) as session:
        session.execute(text("BEGIN IMMEDIATE"))
        row = session.get(SensitiveMigrationState, 1)
        if row is None:
            raise SensitiveMigrationError(
                "sensitive_migration_state_invalid"
            )
        if row.state not in {
            "required",
            "migrating",
            "complete",
            "rotating",
        }:
            raise SensitiveMigrationError(
                "sensitive_migration_state_invalid"
            )
        if row.state in {"required", "complete"}:
            row.started_at = now
        elif row.started_at is None:
            raise SensitiveMigrationError(
                "sensitive_migration_state_invalid"
            )
        row.state = operation
        row.active_key_id = active_key_id
        row.rows_total = scan.rows_total
        row.rows_completed = scan.rows_completed
        row.backup_path_hash = backup_path_hash
        row.completed_at = None
        row.updated_at = now
        session.commit()


def _set_progress(
    engine: Engine,
    scan: _Scan,
    *,
    operation: Literal["migrating", "rotating"],
    now: datetime,
) -> None:
    with Session(engine) as session:
        session.execute(text("BEGIN IMMEDIATE"))
        row = session.get(SensitiveMigrationState, 1)
        if row is None or row.state != operation:
            raise SensitiveMigrationError(
                "sensitive_migration_state_invalid"
            )
        row.rows_total = scan.rows_total
        row.rows_completed = scan.rows_completed
        row.updated_at = now
        session.commit()


def _set_complete(
    engine: Engine,
    scan: _Scan,
    *,
    operation: Literal["migrating", "rotating"],
    active_key_id: str,
    now: datetime,
) -> None:
    if scan.pending or scan.rows_completed != scan.rows_total:
        raise SensitiveMigrationError(
            "sensitive_migration_evidence_invalid"
        )
    with Session(engine) as session:
        session.execute(text("BEGIN IMMEDIATE"))
        row = session.get(SensitiveMigrationState, 1)
        if (
            row is None
            or row.state != operation
            or row.started_at is None
            or row.backup_path_hash is None
        ):
            raise SensitiveMigrationError(
                "sensitive_migration_state_invalid"
            )
        row.state = "complete"
        row.active_key_id = active_key_id
        row.rows_total = scan.rows_total
        row.rows_completed = scan.rows_completed
        row.completed_at = now
        row.updated_at = now
        session.commit()


def _mark_failed(
    engine: Engine,
    *,
    active_key_id: str,
    clock: Callable[[], datetime],
) -> None:
    try:
        now = _timestamp(clock)
        with Session(engine) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(SensitiveMigrationState, 1)
            if row is None:
                session.rollback()
                return
            row.state = "failed"
            row.active_key_id = active_key_id
            row.started_at = row.started_at or now
            row.completed_at = None
            row.updated_at = now
            if row.rows_completed > row.rows_total:
                row.rows_completed = 0
                row.rows_total = 0
            session.commit()
    except Exception:
        pass


def _renew(guard: RuntimeTenureGuard) -> None:
    if not guard.renew_once():
        raise SensitiveMigrationError(
            "sensitive_migration_tenure_lost"
        )


def _rewrite_batch(
    engine: Engine,
    batch: Sequence[_RowRef],
    *,
    mode: Literal["migrate", "rotate"],
    cipher: SensitiveDataCipher,
    old_key_id: str,
    new_key_id: str | None,
) -> None:
    allowed = (
        frozenset({old_key_id})
        if new_key_id is None
        else frozenset({old_key_id, new_key_id})
    )
    target_key_id = new_key_id or old_key_id
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            for row_ref in batch:
                spec = row_ref.spec
                selected = ", ".join(
                    _quote(column) for column in spec.columns
                )
                values = connection.execute(
                    text(
                        f"SELECT {selected} FROM {_quote(spec.table)} "
                        f"WHERE {_quote(spec.primary_key)} = :primary"
                    ),
                    {"primary": row_ref.primary_value},
                ).one_or_none()
                if values is None:
                    raise SensitiveMigrationError(
                        "sensitive_migration_data_changed"
                    )
                changed: dict[str, str] = {}
                for column, value in zip(
                    spec.columns,
                    values,
                    strict=True,
                ):
                    if value is None:
                        continue
                    ref = SensitiveFieldRef(
                        spec.table,
                        row_ref.row_id,
                        column,
                        1,
                    )
                    if mode == "migrate" and (
                        not isinstance(value, str)
                        or not value.startswith("enc:")
                    ):
                        if not isinstance(value, str) or not value:
                            raise SensitiveMigrationError(
                                "sensitive_migration_data_invalid"
                            )
                        plaintext = value
                    else:
                        key_id = (
                            _envelope_key(value)
                            if isinstance(value, str)
                            else None
                        )
                        plaintext = _verify_envelope(
                            cipher,
                            value,
                            ref,
                            allowed_key_ids=allowed,
                        )
                        if key_id == target_key_id:
                            continue
                    envelope = cipher.encrypt(plaintext, ref)
                    _verify_envelope(
                        cipher,
                        envelope,
                        ref,
                        allowed_key_ids=frozenset({target_key_id}),
                    )
                    changed[column] = envelope
                if changed:
                    assignments = ", ".join(
                        f"{_quote(column)} = :{column}"
                        for column in changed
                    )
                    connection.execute(
                        text(
                            f"UPDATE {_quote(spec.table)} SET {assignments} "
                            f"WHERE {_quote(spec.primary_key)} = :primary"
                        ),
                        {**changed, "primary": row_ref.primary_value},
                    )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def _run_batches(
    engine: Engine,
    *,
    operation: Literal["migrating", "rotating"],
    scan_mode: Literal["migrate", "rotate"],
    cipher: SensitiveDataCipher,
    specs: Sequence[_TableSpec],
    old_key_id: str,
    new_key_id: str | None,
    handle: RuntimeTenureGuard,
    clock: Callable[[], datetime],
    stage_hook: Callable[[str], None],
) -> _Scan:
    first = True
    while True:
        _renew(handle)
        scan = _scan(
            engine,
            cipher,
            specs,
            mode=scan_mode,
            old_key_id=old_key_id,
            new_key_id=new_key_id,
            ensure_maintenance=lambda: _renew(handle),
        )
        _set_progress(
            engine,
            scan,
            operation=operation,
            now=_timestamp(clock),
        )
        if not scan.pending:
            return scan
        batch = scan.pending[:_MAX_BATCH_ROWS]
        if first:
            stage_hook("before_first_row_mutation")
            first = False
        _renew(handle)
        _rewrite_batch(
            engine,
            batch,
            mode=scan_mode,
            cipher=cipher,
            old_key_id=old_key_id,
            new_key_id=new_key_id,
        )
        _renew(handle)
        refreshed = _scan(
            engine,
            cipher,
            specs,
            mode=scan_mode,
            old_key_id=old_key_id,
            new_key_id=new_key_id,
            ensure_maintenance=lambda: _renew(handle),
        )
        _set_progress(
            engine,
            refreshed,
            operation=operation,
            now=_timestamp(clock),
        )
        stage_hook(f"batch_committed:{len(batch)}")


def _acquire_maintenance(
    engine: Engine,
    *,
    process_identity: ProcessIdentity,
    process_inspector: ProcessInspector,
    tenure_clock: Callable[[], datetime],
) -> RuntimeTenureGuard:
    try:
        service = RuntimeTenureService(
            make_session_factory(engine),
            process_inspector=process_inspector,
            clock=tenure_clock,
        )
        handle = service.acquire_maintenance(
            process_identity,
            ttl_seconds=_MAINTENANCE_TTL_SECONDS,
        )
        guard = RuntimeTenureGuard(
            handle,
            ttl_seconds=_MAINTENANCE_TTL_SECONDS,
            renewal_interval_seconds=5,
        )
        guard.start()
        return guard
    except (TenureUnavailable, TenureUncertain) as exc:
        code = getattr(exc, "stable_code", "tenure_uncertain")
        raise SensitiveMigrationError(
            f"sensitive_migration_{code}"
        ) from None


def _release_or_fail(
    engine: Engine,
    handle: RuntimeTenureGuard,
    *,
    active_key_id: str,
    clock: Callable[[], datetime],
) -> None:
    try:
        if not handle.close():
            raise TenureUncertain()
    except Exception:
        _mark_failed(
            engine,
            active_key_id=active_key_id,
            clock=clock,
        )
        raise SensitiveMigrationError(
            "sensitive_migration_release_uncertain"
        ) from None


def migrate_sensitive_fields(
    engine: Engine,
    cipher: SensitiveDataCipher,
    *,
    backup_key: bytes,
    backup_key_id: str,
    backup_directory: str | Path,
    database_path: str | Path,
    process_identity: ProcessIdentity,
    process_inspector: ProcessInspector,
    now: Callable[[], datetime] = utcnow,
    tenure_clock: Callable[[], datetime] = utcnow,
    stage_hook: Callable[[str], None] | None = None,
) -> SensitiveMigrationReceipt:
    """Encrypt legacy plaintext in bounded transactions under maintenance."""

    require_current_schema(engine)
    source = _database_path(engine, database_path)
    current = _state(engine)
    if current.state == "complete":
        verified = verify_sensitive_fields(
            engine,
            cipher,
            configured_active_key_id=cipher.active_key_id,
        )
        return SensitiveMigrationReceipt(
            operation="migrate",
            status="verified_noop",
            active_key_id=verified.active_key_id,
            rows_total=verified.rows_total,
            backup_path_hash=verified.backup_path_hash,
        )
    if current.state not in {"required", "migrating"}:
        raise SensitiveMigrationError(
            f"sensitive_migration_{current.state}"
            if current.state in {"rotating", "failed"}
            else "sensitive_migration_state_invalid"
        )
    if (
        current.state == "migrating"
        and current.active_key_id != cipher.active_key_id
    ):
        raise SensitiveMigrationError("sensitive_active_key_mismatch")

    hook = stage_hook or (lambda _stage: None)
    handle = _acquire_maintenance(
        engine,
        process_identity=process_identity,
        process_inspector=process_inspector,
        tenure_clock=tenure_clock,
    )
    released = False
    try:
        specs = _registry(engine)
        backup = create_encrypted_database_backup(
            source,
            backup_directory,
            backup_key=backup_key,
            backup_key_id=backup_key_id,
            schema_head=schema_status(engine).head,
            now=now,
            ensure_maintenance=lambda: _renew(handle),
        )
        hook("backup_verified")
        initial = _scan(
            engine,
            cipher,
            specs,
            mode="migrate",
            old_key_id=cipher.active_key_id,
            ensure_maintenance=lambda: _renew(handle),
        )
        _set_operation_state(
            engine,
            operation="migrating",
            active_key_id=cipher.active_key_id,
            scan=initial,
            backup_path_hash=backup.path_hash,
            now=_timestamp(now),
        )
        final = _run_batches(
            engine,
            operation="migrating",
            scan_mode="migrate",
            cipher=cipher,
            specs=specs,
            old_key_id=cipher.active_key_id,
            new_key_id=None,
            handle=handle,
            clock=now,
            stage_hook=hook,
        )
        _renew(handle)
        verified = _scan(
            engine,
            cipher,
            specs,
            mode="verify",
            old_key_id=cipher.active_key_id,
            ensure_maintenance=lambda: _renew(handle),
        )
        if verified != final:
            raise SensitiveMigrationError(
                "sensitive_migration_evidence_invalid"
            )
        _set_complete(
            engine,
            verified,
            operation="migrating",
            active_key_id=cipher.active_key_id,
            now=_timestamp(now),
        )
        _renew(handle)
        _release_or_fail(
            engine,
            handle,
            active_key_id=cipher.active_key_id,
            clock=now,
        )
        released = True
        return SensitiveMigrationReceipt(
            operation="migrate",
            status="complete",
            active_key_id=cipher.active_key_id,
            rows_total=verified.rows_total,
            backup_path_hash=backup.path_hash,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        if isinstance(exc, Exception):
            _mark_failed(
                engine,
                active_key_id=cipher.active_key_id,
                clock=now,
            )
            if isinstance(exc, SensitiveMigrationError):
                raise
            raise SensitiveMigrationError(
                "sensitive_migration_failed"
            ) from None
        raise
    finally:
        if not released:
            _release_or_fail(
                engine,
                handle,
                active_key_id=cipher.active_key_id,
                clock=now,
            )


def rotate_sensitive_fields(
    engine: Engine,
    *,
    old_cipher: SensitiveDataCipher,
    new_cipher: SensitiveDataCipher,
    new_key_id: str,
    retained_key_ids: Sequence[str],
    backup_key: bytes,
    backup_key_id: str,
    backup_directory: str | Path,
    database_path: str | Path,
    process_identity: ProcessIdentity,
    process_inspector: ProcessInspector,
    now: Callable[[], datetime] = utcnow,
    tenure_clock: Callable[[], datetime] = utcnow,
    stage_hook: Callable[[str], None] | None = None,
) -> SensitiveMigrationReceipt:
    """Rotate old/new mixed envelopes, retaining all configured key material."""

    require_current_schema(engine)
    source = _database_path(engine, database_path)
    current = _state(engine)
    old_key_id = old_cipher.active_key_id
    if (
        current.state not in {"complete", "rotating"}
        or current.active_key_id != old_key_id
    ):
        raise SensitiveMigrationError(
            "sensitive_rotation_state_invalid"
        )
    if (
        new_key_id == old_key_id
        or new_cipher.active_key_id != new_key_id
        or new_key_id not in retained_key_ids
        or old_key_id not in old_cipher.key_ids
        or old_key_id not in new_cipher.key_ids
        or new_key_id not in new_cipher.key_ids
    ):
        raise SensitiveMigrationError(
            "sensitive_rotation_key_invalid"
        )

    hook = stage_hook or (lambda _stage: None)
    handle = _acquire_maintenance(
        engine,
        process_identity=process_identity,
        process_inspector=process_inspector,
        tenure_clock=tenure_clock,
    )
    released = False
    try:
        specs = _registry(engine)
        backup = create_encrypted_database_backup(
            source,
            backup_directory,
            backup_key=backup_key,
            backup_key_id=backup_key_id,
            schema_head=schema_status(engine).head,
            now=now,
            ensure_maintenance=lambda: _renew(handle),
        )
        hook("backup_verified")
        initial = _scan(
            engine,
            new_cipher,
            specs,
            mode="rotate",
            old_key_id=old_key_id,
            new_key_id=new_key_id,
            ensure_maintenance=lambda: _renew(handle),
        )
        _set_operation_state(
            engine,
            operation="rotating",
            active_key_id=old_key_id,
            scan=initial,
            backup_path_hash=backup.path_hash,
            now=_timestamp(now),
        )
        final = _run_batches(
            engine,
            operation="rotating",
            scan_mode="rotate",
            cipher=new_cipher,
            specs=specs,
            old_key_id=old_key_id,
            new_key_id=new_key_id,
            handle=handle,
            clock=now,
            stage_hook=hook,
        )
        _renew(handle)
        verified = _scan(
            engine,
            new_cipher,
            specs,
            mode="verify",
            old_key_id=new_key_id,
            ensure_maintenance=lambda: _renew(handle),
        )
        if verified != final:
            raise SensitiveMigrationError(
                "sensitive_migration_evidence_invalid"
            )
        _set_complete(
            engine,
            verified,
            operation="rotating",
            active_key_id=new_key_id,
            now=_timestamp(now),
        )
        _renew(handle)
        _release_or_fail(
            engine,
            handle,
            active_key_id=new_key_id,
            clock=now,
        )
        released = True
        return SensitiveMigrationReceipt(
            operation="rotate",
            status="complete",
            active_key_id=new_key_id,
            rows_total=verified.rows_total,
            backup_path_hash=backup.path_hash,
            old_key_id=old_key_id,
            old_key_status="retained",
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        if isinstance(exc, Exception):
            _mark_failed(
                engine,
                active_key_id=old_key_id,
                clock=now,
            )
            if isinstance(exc, SensitiveMigrationError):
                raise
            raise SensitiveMigrationError(
                "sensitive_rotation_failed"
            ) from None
        raise
    finally:
        if not released:
            state = _state(engine)
            _release_or_fail(
                engine,
                handle,
                active_key_id=state.active_key_id,
                clock=now,
            )


def _rotation_cipher(
    config: AppConfig,
    runtime_secrets: RuntimeSecrets,
    new_key_id: str,
) -> SensitiveDataCipher:
    old_key_id = config.encryption.active_key_id
    retained = list(config.encryption.retained_key_ids)
    if new_key_id not in retained:
        raise SensitiveMigrationError("sensitive_rotation_key_invalid")
    reordered_ids = [
        new_key_id,
        old_key_id,
        *(key_id for key_id in retained if key_id != new_key_id),
    ]
    if (
        len(reordered_ids) != len(set(reordered_ids))
        or any(
            key_id not in runtime_secrets.field_encryption_keys
            for key_id in reordered_ids
        )
    ):
        raise SensitiveMigrationError("sensitive_rotation_key_invalid")
    rotation_config = config.encryption.model_copy(
        update={
            "active_key_id": new_key_id,
            "retained_key_ids": reordered_ids[1:],
        }
    )
    rotation_secrets = runtime_secrets.model_copy(
        update={
            "field_encryption_keys": {
                key_id: runtime_secrets.field_encryption_keys[key_id]
                for key_id in reordered_ids
            }
        }
    )
    return build_sensitive_data_cipher(
        rotation_config,
        rotation_secrets,
    )


def _receipt_payload(
    receipt: SensitiveMigrationReceipt,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "active_key_id": receipt.active_key_id,
        "backup_path_hash": receipt.backup_path_hash,
        "operation": receipt.operation,
        "rows_total": receipt.rows_total,
        "status": receipt.status,
    }
    if receipt.old_key_id is not None:
        payload["old_key_id"] = receipt.old_key_id
    if receipt.old_key_status is not None:
        payload["old_key_status"] = receipt.old_key_status
    return payload


def main(
    argv: list[str] | None = None,
    *,
    config_loader: Callable[[], AppConfig] = load_config,
    secrets_loader: Callable[..., RuntimeSecrets] = load_role_secrets,
    engine_factory: Callable[..., Engine] = create_db_engine,
    process_inspector: ProcessInspector | None = None,
    process_identity: ProcessIdentity | None = None,
) -> int:
    """Run one explicit offline operation without printing secret values."""

    parser = argparse.ArgumentParser(
        description="Migrate or rotate registered sensitive database fields."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("migrate")
    rotate = subparsers.add_parser("rotate")
    rotate.add_argument("--new-key-id", required=True)
    subparsers.add_parser("verify")
    args = parser.parse_args(argv)

    backup_buffer: bytearray | None = None
    try:
        config = config_loader()
        runtime_secrets = secrets_loader(
            "migration",
            config=config,
        )
        engine = engine_factory(runtime_secrets.database_url)
        cipher = build_sensitive_data_cipher(
            config.encryption,
            runtime_secrets,
        )
        if args.command == "verify":
            receipt = verify_sensitive_fields(
                engine,
                cipher,
                configured_active_key_id=(
                    config.encryption.active_key_id
                ),
            )
        else:
            selected_inspector = (
                process_inspector or LocalProcessInspector()
            )
            identity = process_identity
            if identity is None:
                current = getattr(selected_inspector, "current", None)
                if not callable(current):
                    raise SensitiveMigrationError(
                        "current_process_identity_unknown"
                    )
                identity = current()
            identity.validate()
            backup_buffer = validate_base64_key(
                "backup_encryption_key",
                runtime_secrets.backup_encryption_key,
            )
            backup_key = bytes(backup_buffer)
            database_path = engine.url.database
            if not database_path:
                raise SensitiveMigrationError(
                    "sensitive_database_invalid"
                )
            common = {
                "backup_key": backup_key,
                "backup_key_id": config.encryption.backup_key_id,
                "backup_directory": (
                    config.encryption.backup_directory
                ),
                "database_path": database_path,
                "process_identity": identity,
                "process_inspector": selected_inspector,
            }
            if args.command == "migrate":
                receipt = migrate_sensitive_fields(
                    engine,
                    cipher,
                    **common,
                )
            else:
                new_key_id = str(args.new_key_id)
                receipt = rotate_sensitive_fields(
                    engine,
                    old_cipher=cipher,
                    new_cipher=_rotation_cipher(
                        config,
                        runtime_secrets,
                        new_key_id,
                    ),
                    new_key_id=new_key_id,
                    retained_key_ids=list(
                        config.encryption.retained_key_ids
                    ),
                    **common,
                )
        print(
            json.dumps(
                _receipt_payload(receipt),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    except SensitiveMigrationError as exc:
        print(exc.stable_code, file=sys.stderr)
        return 1
    except Exception:
        print("sensitive_command_failed", file=sys.stderr)
        return 1
    finally:
        if backup_buffer is not None:
            for index in range(len(backup_buffer)):
                backup_buffer[index] = 0


if __name__ == "__main__":
    raise SystemExit(main())
