"""Explicit encrypted persistence for registered sensitive ORM fields."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import re
import secrets
import sqlite3
import weakref
from weakref import WeakKeyDictionary

from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from .crypto import (
    SensitiveDataCipher,
    SensitiveDataInvalid,
    SensitiveFieldRef,
)


SENSITIVE_FIELDS = {
    "orders": {"approval_reason"},
    "audit_events": {"reason", "detail_json"},
    "proposals": {"reasoning"},
    "llm_decisions": {"prompt", "tool_calls_json", "reasoning_summary"},
    "risk_events": {"reason"},
    "analysis_reports": {"report_json"},
    "trade_plans": {"plan_json", "sized_json"},
    "circuit_breaker_state": {"reason"},
    "startup_reconciliation_state": {"reason", "evidence_json"},
    "panic_receipts": {"response_json"},
    "backtest_artifacts": {"payload_json"},
}


class PlaintextSensitiveField(RuntimeError):
    """A registered field was not provably encrypted for its exact row."""

    stable_code = "plaintext_sensitive_field"

    def __init__(self) -> None:
        super().__init__(self.stable_code)


@dataclass(frozen=True)
class _StagedField:
    object_ref: weakref.ReferenceType[object]
    object_id: int
    column: str
    operation_token: str
    pending_ref: SensitiveFieldRef
    final_ref: SensitiveFieldRef | None = None


@dataclass
class _StagingState:
    schema_version: int
    staged: dict[tuple[int, str], _StagedField] = field(
        default_factory=dict
    )
    active_operation: str | None = None


_STAGING_STATES: WeakKeyDictionary[Session, _StagingState] = (
    WeakKeyDictionary()
)
_GUARD_INSTALLATIONS: WeakKeyDictionary[Session, tuple[int, int]] = (
    WeakKeyDictionary()
)
_FACTORY_CIPHERS: WeakKeyDictionary[
    sessionmaker[Session],
    SensitiveDataCipher,
] = WeakKeyDictionary()
_ENGINE_CIPHERS: WeakKeyDictionary[Engine, SensitiveDataCipher] = (
    WeakKeyDictionary()
)
_FACTORY_GUARD_CALLBACKS: WeakKeyDictionary[
    sessionmaker[Session],
    object,
] = WeakKeyDictionary()
_ENGINE_WRITE_CAPABILITIES: WeakKeyDictionary[Engine, object] = (
    WeakKeyDictionary()
)
_ENGINE_BOUNDARY_CALLBACKS: WeakKeyDictionary[
    Engine,
    tuple[object, object, object],
] = WeakKeyDictionary()
_ACTIVE_SENSITIVE_WRITE: ContextVar[object | None] = ContextVar(
    "trading_assistant_sensitive_write",
    default=None,
)


def _sensitive_sql_mutation(statement: object) -> bool:
    if not isinstance(statement, str):
        return True
    for table, fields in SENSITIVE_FIELDS.items():
        quoted_table = rf'(?:"{re.escape(table)}"|`{re.escape(table)}`|\[{re.escape(table)}\]|{re.escape(table)})'
        if re.search(
            rf"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO)"
            rf"\s+{quoted_table}\b",
            statement,
            flags=re.IGNORECASE,
        ):
            return True
        if re.search(
            rf"\bDELETE\s+FROM\s+{quoted_table}\b",
            statement,
            flags=re.IGNORECASE,
        ):
            return True
        update = re.search(
            rf"\bUPDATE\s+(?:OR\s+\w+\s+)?{quoted_table}\b",
            statement,
            flags=re.IGNORECASE,
        )
        if update is None:
            continue
        remainder = statement[update.end():]
        if any(
            re.search(
                rf"\b{re.escape(column)}\b",
                remainder,
                flags=re.IGNORECASE,
            )
            for column in fields
        ):
            return True
    return False


def _install_sensitive_sql_boundary(engine: Engine) -> None:
    if engine in _ENGINE_BOUNDARY_CALLBACKS:
        return
    capability = _ENGINE_WRITE_CAPABILITIES[engine]

    def authorizer(
        action,
        first,
        second,
        _database,
        _trigger,
    ):
        if _ACTIVE_SENSITIVE_WRITE.get() is capability:
            return sqlite3.SQLITE_OK
        table = first if isinstance(first, str) else ""
        column = second if isinstance(second, str) else ""
        if (
            action == sqlite3.SQLITE_INSERT
            and table in SENSITIVE_FIELDS
        ):
            return sqlite3.SQLITE_DENY
        if (
            action == sqlite3.SQLITE_DELETE
            and table in SENSITIVE_FIELDS
        ):
            return sqlite3.SQLITE_DENY
        if (
            action == sqlite3.SQLITE_UPDATE
            and column in SENSITIVE_FIELDS.get(table, set())
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def install_authorizer(
        dbapi_connection,
        _record,
        _proxy=None,
    ) -> None:
        setter = getattr(dbapi_connection, "set_authorizer", None)
        if callable(setter):
            setter(authorizer)

    def before_cursor_execute(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _many,
    ) -> None:
        if _ACTIVE_SENSITIVE_WRITE.get() is capability:
            return
        if _sensitive_sql_mutation(statement):
            raise PlaintextSensitiveField()

    def handle_error(exception_context):
        if _ACTIVE_SENSITIVE_WRITE.get() is capability:
            return None
        original = getattr(exception_context, "original_exception", None)
        if (
            isinstance(original, sqlite3.DatabaseError)
            and "not authorized" in str(original).lower()
        ):
            return PlaintextSensitiveField()
        return None

    event.listen(engine, "connect", install_authorizer)
    event.listen(engine, "checkout", install_authorizer)
    event.listen(
        engine,
        "before_cursor_execute",
        before_cursor_execute,
    )
    event.listen(engine, "handle_error", handle_error, retval=True)
    _ENGINE_BOUNDARY_CALLBACKS[engine] = (
        install_authorizer,
        before_cursor_execute,
        handle_error,
    )


def _bind_sensitive_sql_boundary(engine: Engine) -> None:
    """Install the runtime boundary without binding key material."""
    if not isinstance(engine, Engine):
        raise PlaintextSensitiveField()
    if engine not in _ENGINE_WRITE_CAPABILITIES:
        _ENGINE_WRITE_CAPABILITIES[engine] = object()
    _install_sensitive_sql_boundary(engine)


@contextmanager
def _sensitive_write_authority(engine: Engine):
    """Internal exact-engine capability for store and maintenance writes."""
    capability = _ENGINE_WRITE_CAPABILITIES.get(engine)
    if capability is None:
        raise PlaintextSensitiveField()
    token = _ACTIVE_SENSITIVE_WRITE.set(capability)
    try:
        yield
    finally:
        _ACTIVE_SENSITIVE_WRITE.reset(token)


def bind_sensitive_cipher(
    session_factory: sessionmaker[Session],
    cipher: SensitiveDataCipher,
) -> None:
    """Bind one validated cipher to a session factory without fallback."""
    if not isinstance(cipher, SensitiveDataCipher):
        raise SensitiveDataInvalid()
    existing = _FACTORY_CIPHERS.get(session_factory)
    if existing is not None and existing is not cipher:
        raise SensitiveDataInvalid()
    _FACTORY_CIPHERS[session_factory] = cipher
    engine = session_factory.kw.get("bind")
    if not isinstance(engine, Engine):
        raise SensitiveDataInvalid()
    existing_engine = _ENGINE_CIPHERS.get(engine)
    if existing_engine is not None and existing_engine is not cipher:
        raise SensitiveDataInvalid()
    _ENGINE_CIPHERS[engine] = cipher
    _bind_sensitive_sql_boundary(engine)
    if session_factory not in _FACTORY_GUARD_CALLBACKS:
        def install_factory_guard(
            session: Session,
            transaction,
        ) -> None:
            if transaction.parent is None:
                install_sensitive_field_guards(session, cipher)

        event.listen(
            session_factory,
            "after_transaction_create",
            install_factory_guard,
        )
        _FACTORY_GUARD_CALLBACKS[session_factory] = install_factory_guard


def sensitive_store(
    session: Session,
    session_factory: sessionmaker[Session] | None = None,
) -> "SensitiveFieldStore":
    """Resolve the exact factory-bound cipher for one domain transaction."""
    cipher = (
        _FACTORY_CIPHERS.get(session_factory)
        if session_factory is not None
        else None
    )
    if cipher is None:
        bind = session.get_bind()
        cipher = (
            _ENGINE_CIPHERS.get(bind)
            if isinstance(bind, Engine)
            else None
        )
    if cipher is None:
        raise SensitiveDataInvalid()
    return SensitiveFieldStore(session, cipher)


def persist_sensitive(
    session: Session,
    instance: object,
    values: Mapping[str, str],
    *,
    session_factory: sessionmaker[Session] | None = None,
) -> object:
    """Persist one registered record through staged ciphertext only."""
    return sensitive_store(session, session_factory).write_many(
        instance,
        values,
    )


def _table_name(instance: object) -> str | None:
    table = getattr(instance, "__table__", None)
    return getattr(table, "name", None)


def _row_id(instance: object) -> str | None:
    mapper = sa_inspect(instance).mapper
    if len(mapper.primary_key) != 1:
        return None
    value = getattr(instance, mapper.primary_key[0].key)
    return None if value is None else str(value)


def _verify_registered_values(
    session: Session,
    cipher: object,
    state: _StagingState,
    *,
    allow_staged: bool,
) -> None:
    if state.staged and (
        not allow_staged or state.active_operation is None
    ):
        raise PlaintextSensitiveField()
    if state.active_operation is not None and not state.staged:
        raise PlaintextSensitiveField()

    candidates = set(session.new).union(session.dirty)
    for instance in candidates:
        table = _table_name(instance)
        columns = SENSITIVE_FIELDS.get(table or "")
        if columns is None:
            continue
        for column in columns:
            value = getattr(instance, column)
            staged = state.staged.get((id(instance), column))
            if value is None:
                if staged is not None:
                    raise PlaintextSensitiveField()
                mapped_column = instance.__table__.c[column]
                if (
                    mapped_column.nullable
                    and mapped_column.default is None
                    and mapped_column.server_default is None
                ):
                    continue
                raise PlaintextSensitiveField()
            try:
                if staged is not None:
                    if (
                        not allow_staged
                        or state.active_operation is None
                        or staged.operation_token
                        != state.active_operation
                        or staged.object_id != id(instance)
                        or staged.object_ref() is not instance
                        or staged.column != column
                        or staged.pending_ref.table != table
                        or staged.pending_ref.column != column
                        or staged.pending_ref.schema_version
                        != state.schema_version
                        or not staged.pending_ref.row.startswith("pending:")
                    ):
                        raise SensitiveDataInvalid()
                    verification_ref = staged.pending_ref
                    if staged.final_ref is not None:
                        row_id = _row_id(instance)
                        if (
                            row_id is None
                            or staged.final_ref.table != table
                            or staged.final_ref.row != row_id
                            or staged.final_ref.column != column
                            or staged.final_ref.schema_version
                            != state.schema_version
                        ):
                            raise SensitiveDataInvalid()
                        verification_ref = staged.final_ref
                    cipher.decrypt(value, verification_ref)
                    continue

                row_id = _row_id(instance)
                if row_id is None:
                    raise SensitiveDataInvalid()
                cipher.decrypt(
                    value,
                    SensitiveFieldRef(
                        table=table,
                        row=row_id,
                        column=column,
                        schema_version=state.schema_version,
                    ),
                )
            except Exception:
                raise PlaintextSensitiveField() from None


def install_sensitive_field_guards(
    session: Session,
    cipher: SensitiveDataCipher,
    *,
    schema_version: int = 1,
) -> None:
    """Install fail-closed guards on exactly one Session, once."""
    if (
        not isinstance(session, Session)
        or isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version <= 0
    ):
        raise SensitiveDataInvalid()
    existing = _GUARD_INSTALLATIONS.get(session)
    if existing is not None:
        if existing != (id(cipher), schema_version):
            raise SensitiveDataInvalid()
        return

    state = _StagingState(schema_version=schema_version)
    _STAGING_STATES[session] = state
    _GUARD_INSTALLATIONS[session] = (id(cipher), schema_version)

    def before_flush(
        guarded_session: Session,
        _flush_context,
        _instances,
    ) -> None:
        _verify_registered_values(
            guarded_session,
            cipher,
            state,
            allow_staged=True,
        )

    def before_commit(guarded_session: Session) -> None:
        if state.staged or state.active_operation is not None:
            raise PlaintextSensitiveField()
        _verify_registered_values(
            guarded_session,
            cipher,
            state,
            allow_staged=False,
        )

    def after_transaction_end(
        guarded_session: Session,
        transaction,
    ) -> None:
        try:
            is_outermost = transaction.parent is None
        except Exception:
            return
        if is_outermost and not guarded_session.in_transaction():
            state.staged.clear()
            state.active_operation = None

    event.listen(session, "before_flush", before_flush)
    event.listen(session, "before_commit", before_commit)
    event.listen(
        session,
        "after_transaction_end",
        after_transaction_end,
    )


class SensitiveFieldStore:
    """Encrypt registered values before either generated-PK flush."""

    def __init__(
        self,
        session: Session,
        cipher: SensitiveDataCipher,
        *,
        schema_version: int = 1,
    ) -> None:
        engine = session.get_bind()
        if not isinstance(engine, Engine):
            raise PlaintextSensitiveField()
        # Direct construction is the canonical Task 5 write protocol too.
        # Ensure it receives the same exact-engine SQL boundary as a
        # factory-bound production store before any staged flush can occur.
        _bind_sensitive_sql_boundary(engine)
        install_sensitive_field_guards(
            session,
            cipher,
            schema_version=schema_version,
        )
        self._session = session
        self._cipher = cipher
        self._schema_version = schema_version
        self._guard_state = _STAGING_STATES[session]

    def write(
        self,
        instance: object,
        values: Mapping[str, str],
    ) -> object:
        if (
            self._guard_state.staged
            or self._guard_state.active_operation is not None
        ):
            raise PlaintextSensitiveField()

        table = _table_name(instance)
        registered = SENSITIVE_FIELDS.get(table or "")
        if registered is None or not isinstance(values, Mapping) or not values:
            raise PlaintextSensitiveField()
        field_names = set(values)
        if not field_names <= registered:
            raise PlaintextSensitiveField()

        plaintext_values = dict(values)
        operation_token: str | None = None
        try:
            if any(
                not isinstance(value, str) or not value
                for value in plaintext_values.values()
            ):
                raise SensitiveDataInvalid()
            row_id = _row_id(instance)
            state = sa_inspect(instance)
            generated_pk = row_id is None
            engine = self._session.get_bind()
            if not isinstance(engine, Engine):
                raise PlaintextSensitiveField()
            with (
                _sensitive_write_authority(engine),
                self._session.no_autoflush,
            ):
                if generated_pk:
                    required = {
                        column
                        for column in registered
                        if not instance.__table__.c[column].nullable
                    }
                    if not required <= field_names:
                        raise PlaintextSensitiveField()
                    if any(
                        getattr(instance, column) is not None
                        for column in registered
                    ):
                        raise PlaintextSensitiveField()

                    token = secrets.token_urlsafe(24)
                    pending_refs = {
                        column: SensitiveFieldRef(
                            table=table,
                            row=f"pending:{token}",
                            column=column,
                            schema_version=self._schema_version,
                        )
                        for column in field_names
                    }
                    staged_envelopes = {
                        column: self._cipher.encrypt(
                            plaintext_values[column],
                            pending_refs[column],
                        )
                        for column in field_names
                    }
                    operation_token = token
                    self._guard_state.active_operation = operation_token
                    self._session.add(instance)
                    for column, envelope in staged_envelopes.items():
                        staging_key = (id(instance), column)
                        if staging_key in self._guard_state.staged:
                            raise PlaintextSensitiveField()
                        setattr(instance, column, envelope)
                        self._guard_state.staged[staging_key] = (
                            _StagedField(
                                object_ref=weakref.ref(instance),
                                object_id=id(instance),
                                column=column,
                                operation_token=operation_token,
                                pending_ref=pending_refs[column],
                            )
                        )
                    self._session.flush([instance])
                    row_id = _row_id(instance)
                    if row_id is None:
                        raise PlaintextSensitiveField()

                    final_refs = {
                        column: SensitiveFieldRef(
                            table=table,
                            row=row_id,
                            column=column,
                            schema_version=self._schema_version,
                        )
                        for column in field_names
                    }
                    final_envelopes = {
                        column: self._cipher.encrypt(
                            plaintext_values[column],
                            final_refs[column],
                        )
                        for column in field_names
                    }
                    for column in field_names:
                        staged = self._guard_state.staged[
                            (id(instance), column)
                        ]
                        self._guard_state.staged[(id(instance), column)] = (
                            _StagedField(
                                object_ref=staged.object_ref,
                                object_id=staged.object_id,
                                column=staged.column,
                                operation_token=staged.operation_token,
                                pending_ref=staged.pending_ref,
                                final_ref=final_refs[column],
                            )
                        )
                    for column, envelope in final_envelopes.items():
                        setattr(instance, column, envelope)
                    self._session.flush([instance])
                    for column in field_names:
                        staged = self._guard_state.staged.get(
                            (id(instance), column)
                        )
                        if (
                            staged is None
                            or staged.object_ref() is not instance
                            or staged.operation_token != operation_token
                            or staged.final_ref != final_refs[column]
                        ):
                            raise PlaintextSensitiveField()
                        try:
                            self._cipher.decrypt(
                                getattr(instance, column),
                                final_refs[column],
                            )
                        except Exception:
                            raise PlaintextSensitiveField() from None
                    for column in field_names:
                        del self._guard_state.staged[
                            (id(instance), column)
                        ]
                    self._guard_state.active_operation = None
                    operation_token = None
                    return instance

                final_envelopes = {
                    column: self._cipher.encrypt(
                        plaintext_values[column],
                        SensitiveFieldRef(
                            table=table,
                            row=row_id,
                            column=column,
                            schema_version=self._schema_version,
                        ),
                    )
                    for column in field_names
                }
                for column, envelope in final_envelopes.items():
                    setattr(instance, column, envelope)
                if state.transient:
                    self._session.add(instance)
                self._session.flush([instance])
                return instance
        finally:
            if (
                operation_token is not None
                and self._guard_state.active_operation == operation_token
            ):
                self._guard_state.active_operation = None
            plaintext_values.clear()

    def write_many(
        self,
        instance: object,
        values: Mapping[str, str],
    ) -> object:
        """Canonical multi-field spelling used by domain write sites."""
        return self.write(instance, values)

    def clear(self, instance: object, columns: set[str]) -> object:
        """Clear only nullable registered fields through the guarded store."""
        table = _table_name(instance)
        registered = SENSITIVE_FIELDS.get(table or "")
        row_id = _row_id(instance)
        if (
            registered is None
            or row_id is None
            or not columns
            or not columns <= registered
            or any(
                not instance.__table__.c[column].nullable
                for column in columns
            )
        ):
            raise PlaintextSensitiveField()
        for column in columns:
            setattr(instance, column, None)
        engine = self._session.get_bind()
        if not isinstance(engine, Engine):
            raise PlaintextSensitiveField()
        with _sensitive_write_authority(engine):
            self._session.flush([instance])
        return instance

    def delete(self, instance: object) -> None:
        """Delete one registered row through the exact-engine capability."""
        table = _table_name(instance)
        row_id = _row_id(instance)
        engine = self._session.get_bind()
        if (
            table not in SENSITIVE_FIELDS
            or row_id is None
            or not isinstance(engine, Engine)
        ):
            raise PlaintextSensitiveField()
        with _sensitive_write_authority(engine):
            self._session.delete(instance)
            self._session.flush([instance])

    def read(self, instance: object, column: str) -> str:
        table = _table_name(instance)
        if (
            table not in SENSITIVE_FIELDS
            or column not in SENSITIVE_FIELDS[table]
        ):
            raise SensitiveDataInvalid()
        row_id = _row_id(instance)
        value = getattr(instance, column)
        if row_id is None or value is None:
            raise SensitiveDataInvalid()
        return self._cipher.decrypt(
            value,
            SensitiveFieldRef(
                table=table,
                row=row_id,
                column=column,
                schema_version=self._schema_version,
            ),
        )
