"""Connection-bound, operation-bound authority for one Alembic command."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Literal, NoReturn

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.runtime.migration import MigrationInfo
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Connection

from trading_assistant.ops.tenure import (
    RuntimeMutationBarrier,
    RuntimeTenureGuard,
    RuntimeTenureHandle,
    RuntimeTenureService,
)


MigrationAuthorityMode = Literal["bootstrap", "maintenance"]
MigrationOperation = Literal["upgrade", "downgrade"]
_SEAL = object()
_MAINTENANCE_RESOURCE = "sensitive-migration:global"
_AUTHORITY_ERROR = "schema_migration_authority_required"


class MigrationAuthority:
    """Sealed passive state for one exact Alembic migration run.

    Security decisions intentionally live in module-level functions. No
    authority instance method participates in validation or schema fencing.
    """

    __slots__ = (
        "_activated",
        "_barrier",
        "_completed",
        "_connection",
        "_destination_revisions",
        "_guard",
        "_mode",
        "_observed_heads",
        "_observed_steps",
        "_operation",
        "_retired",
        "_seal",
        "_start_revisions",
    )

    def __init__(
        self,
        seal: object,
        connection: Connection,
        mode: MigrationAuthorityMode,
        *,
        operation: MigrationOperation,
        destination_revisions: tuple[str, ...],
        guard: RuntimeTenureGuard | None = None,
        barrier: RuntimeMutationBarrier | None = None,
    ) -> None:
        if seal is not _SEAL:
            raise RuntimeError(_AUTHORITY_ERROR)
        self._seal = seal
        self._connection = connection
        self._mode = mode
        self._operation = operation
        self._destination_revisions = destination_revisions
        self._guard = guard
        self._barrier = barrier
        self._activated = False
        self._completed = False
        self._retired = False
        self._start_revisions: tuple[str, ...] = ()
        self._observed_heads: tuple[str, ...] = ()
        self._observed_steps = 0


# Keep a stable reference even if untrusted application code rebinds the
# public module attribute.
_AUTHORITY_TYPE = MigrationAuthority
_SLOTS = {
    name: _AUTHORITY_TYPE.__dict__[name]
    for name in _AUTHORITY_TYPE.__slots__
}


def _read(authority: MigrationAuthority, name: str):
    return _SLOTS[name].__get__(authority, _AUTHORITY_TYPE)


def _write(authority: MigrationAuthority, name: str, value: object) -> None:
    _SLOTS[name].__set__(authority, value)


def _consume(authority: object) -> None:
    if type(authority) is _AUTHORITY_TYPE:
        try:
            _SLOTS["_retired"].__set__(authority, True)
        except BaseException:
            pass


def _refuse(authority: object) -> NoReturn:
    _consume(authority)
    raise RuntimeError(_AUTHORITY_ERROR)


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config("alembic.ini"))


def _normalize_revisions(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values: tuple[object, ...] = (value,)
    elif isinstance(value, (tuple, list, set, frozenset)):
        values = tuple(value)
    else:
        raise RuntimeError(_AUTHORITY_ERROR)
    if any(not isinstance(item, str) or not item for item in values):
        raise RuntimeError(_AUTHORITY_ERROR)
    return tuple(sorted(values))


def _head_revisions() -> tuple[str, ...]:
    heads = tuple(_script().get_heads())
    normalized = _normalize_revisions(heads)
    if not normalized:
        raise RuntimeError(_AUTHORITY_ERROR)
    return normalized


def _resolve_destination(revision: str) -> tuple[str, ...]:
    if not isinstance(revision, str) or not revision:
        raise RuntimeError(_AUTHORITY_ERROR)
    resolved = _script().as_revision_number(revision)
    return _normalize_revisions(resolved)


def _rollback_inspection(connection: Connection) -> None:
    if connection.in_transaction():
        connection.rollback()


def _exact(authority: object) -> MigrationAuthority:
    if type(authority) is not _AUTHORITY_TYPE:
        _refuse(authority)
    return authority


def _validate_maintenance_binding(
    authority: MigrationAuthority,
    connection: Connection,
) -> None:
    guard = _read(authority, "_guard")
    barrier = _read(authority, "_barrier")
    handle = guard.handle if type(guard) is RuntimeTenureGuard else None
    service = (
        handle._service
        if type(handle) is RuntimeTenureHandle
        else None
    )
    session_factory = getattr(service, "_session_factory", None)
    session_bind = getattr(session_factory, "kw", {}).get("bind")
    if (
        type(guard) is not RuntimeTenureGuard
        or type(barrier) is not RuntimeMutationBarrier
        or barrier.guard is not guard
        or barrier.engine is not connection.engine
        or barrier._closed
        or type(handle) is not RuntimeTenureHandle
        or type(service) is not RuntimeTenureService
        or session_bind is not connection.engine
        or handle.role != "maintenance"
        or handle.resource_key != _MAINTENANCE_RESOURCE
    ):
        _refuse(authority)
    try:
        RuntimeTenureGuard.assert_owned_in_transaction(
            guard,
            connection,
        )
    except BaseException:
        _consume(authority)
        raise


def _assert_active(
    authority: object,
    connection: Connection,
    *,
    allowed_modes: frozenset[MigrationAuthorityMode] | None = None,
) -> MigrationAuthority:
    exact = _exact(authority)
    if (
        _read(exact, "_seal") is not _SEAL
        or _read(exact, "_connection") is not connection
        or not _read(exact, "_activated")
        or _read(exact, "_completed")
        or _read(exact, "_retired")
        or (
            allowed_modes is not None
            and _read(exact, "_mode") not in allowed_modes
        )
    ):
        _refuse(exact)
    if _read(exact, "_mode") == "maintenance":
        _validate_maintenance_binding(exact, connection)
    return exact


def issue_bootstrap_authority(
    connection: Connection,
) -> MigrationAuthority:
    """Issue an upgrade-to-current-head token for a currently empty DB."""

    if inspect(connection).get_table_names():
        _rollback_inspection(connection)
        raise RuntimeError("schema_bootstrap_authority_refused")
    _rollback_inspection(connection)
    return _AUTHORITY_TYPE(
        _SEAL,
        connection,
        "bootstrap",
        operation="upgrade",
        destination_revisions=_head_revisions(),
    )


def issue_maintenance_authority(
    connection: Connection,
    *,
    guard: RuntimeTenureGuard | None = None,
    barrier: RuntimeMutationBarrier | None = None,
) -> MigrationAuthority:
    """Issue an upgrade-to-current-head token from a held maintenance lease."""

    return _issue_maintenance_authority(
        connection,
        operation="upgrade",
        destination_revisions=_head_revisions(),
        guard=guard,
        barrier=barrier,
    )


def issue_maintenance_downgrade_authority(
    connection: Connection,
    destination_revision: str,
    *,
    guard: RuntimeTenureGuard | None = None,
    barrier: RuntimeMutationBarrier | None = None,
) -> MigrationAuthority:
    """Issue an exact-destination downgrade token for reviewed tooling."""

    return _issue_maintenance_authority(
        connection,
        operation="downgrade",
        destination_revisions=_resolve_destination(destination_revision),
        guard=guard,
        barrier=barrier,
    )


def _issue_maintenance_authority(
    connection: Connection,
    *,
    operation: MigrationOperation,
    destination_revisions: tuple[str, ...],
    guard: RuntimeTenureGuard | None,
    barrier: RuntimeMutationBarrier | None,
) -> MigrationAuthority:
    if connection.in_transaction():
        raise RuntimeError(_AUTHORITY_ERROR)
    authority = _AUTHORITY_TYPE(
        _SEAL,
        connection,
        "maintenance",
        operation=operation,
        destination_revisions=destination_revisions,
        guard=guard,
        barrier=barrier,
    )
    try:
        _validate_maintenance_binding(authority, connection)
    except BaseException:
        _consume(authority)
        raise
    finally:
        _rollback_inspection(connection)
    return authority


def activate_migration_authority(
    authority: object,
    connection: Connection,
    *,
    destination_revisions: object,
) -> MigrationAuthorityMode:
    """Consume and validate one token before Alembic can configure DDL."""

    exact = _exact(authority)
    if (
        _read(exact, "_seal") is not _SEAL
        or _read(exact, "_activated")
        or _read(exact, "_completed")
        or _read(exact, "_retired")
    ):
        _refuse(exact)
    _write(exact, "_activated", True)
    try:
        actual_destination = _normalize_revisions(
            destination_revisions
        )
    except BaseException:
        _refuse(exact)
    if (
        _read(exact, "_connection") is not connection
        or actual_destination != _read(exact, "_destination_revisions")
    ):
        _refuse(exact)

    if _read(exact, "_mode") == "bootstrap":
        tables = inspect(connection).get_table_names()
        _rollback_inspection(connection)
        if tables:
            _consume(exact)
            raise RuntimeError("schema_bootstrap_authority_refused")
    else:
        try:
            _validate_maintenance_binding(exact, connection)
        finally:
            _rollback_inspection(connection)

    current = MigrationContext.configure(connection).get_current_heads()
    _write(exact, "_start_revisions", _normalize_revisions(current))
    _rollback_inspection(connection)
    return _read(exact, "_mode")


def assert_migration_authority(
    authority: object,
    connection: Connection,
    *,
    allowed_modes: frozenset[MigrationAuthorityMode],
) -> MigrationAuthority:
    """Re-prove exact authority and durable ownership at DDL boundaries."""

    return _assert_active(
        authority,
        connection,
        allowed_modes=allowed_modes,
    )


@contextmanager
def migration_schema_fence(
    authority: object,
    connection: Connection,
) -> Iterator[None]:
    """Narrowly permit rebuilding the table that stores the lease."""

    exact = _assert_active(authority, connection)
    if _read(exact, "_mode") == "bootstrap":
        try:
            yield
        except BaseException:
            _consume(exact)
            raise
        _assert_active(exact, connection)
        return
    barrier = _read(exact, "_barrier")
    if type(barrier) is not RuntimeMutationBarrier:
        _refuse(exact)
    option_name, option_value = (
        RuntimeMutationBarrier.fence_schema_execution_option.fget(barrier)
    )
    connection.execution_options(**{option_name: option_value})
    try:
        yield
    except BaseException:
        _consume(exact)
        raise
    finally:
        try:
            connection.execution_options(**{option_name: None})
        except BaseException:
            _consume(exact)
            raise
    _assert_active(exact, connection)


def observe_migration_step(
    authority: object,
    connection: Connection,
    *,
    step: object,
    heads: object,
) -> None:
    """Accept only real migration steps in the token's bound direction."""

    exact = _assert_active(authority, connection)
    if type(step) is not MigrationInfo:
        _refuse(exact)
    expected_upgrade = _read(exact, "_operation") == "upgrade"
    if step.is_stamp or step.is_upgrade is not expected_upgrade:
        _refuse(exact)
    observed_heads = _normalize_revisions(heads)
    if observed_heads != _normalize_revisions(step.destination_revision_ids):
        _refuse(exact)
    count = _read(exact, "_observed_steps")
    previous_heads = (
        _read(exact, "_start_revisions")
        if count == 0
        else _read(exact, "_observed_heads")
    )
    if _normalize_revisions(step.source_revision_ids) != previous_heads:
        _refuse(exact)
    _write(exact, "_observed_heads", observed_heads)
    _write(exact, "_observed_steps", count + 1)
    _assert_active(exact, connection)


def finish_migration_authority(
    authority: object,
    connection: Connection,
) -> None:
    """Prove a non-empty real migration ended at the bound destination."""

    exact = _assert_active(authority, connection)
    expected = _read(exact, "_destination_revisions")
    if (
        _read(exact, "_observed_steps") <= 0
        or _read(exact, "_observed_heads") != expected
    ):
        _refuse(exact)
    actual = _normalize_revisions(
        MigrationContext.configure(connection).get_current_heads()
    )
    if actual != expected:
        _refuse(exact)
    _assert_active(exact, connection)
    _write(exact, "_completed", True)


def retire_migration_authority(authority: object) -> None:
    """Prevent reuse after success, refusal, or migration failure."""

    _consume(authority)
