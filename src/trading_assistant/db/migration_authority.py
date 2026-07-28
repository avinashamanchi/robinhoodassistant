"""Connection-bound, single-use authority for one Alembic command."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Literal

from sqlalchemy import inspect
from sqlalchemy.engine import Connection

from trading_assistant.ops.tenure import (
    RuntimeMutationBarrier,
    RuntimeTenureGuard,
    RuntimeTenureHandle,
    RuntimeTenureService,
)


MigrationAuthorityMode = Literal["bootstrap", "maintenance"]
_SEAL = object()
_MAINTENANCE_RESOURCE = "sensitive-migration:global"


class MigrationAuthority:
    """A sealed one-shot authority bound to one exact connection."""

    __slots__ = (
        "_activated",
        "_barrier",
        "_connection",
        "_guard",
        "_mode",
        "_retired",
        "_seal",
    )

    def __init__(
        self,
        seal: object,
        connection: Connection,
        mode: MigrationAuthorityMode,
        *,
        guard: RuntimeTenureGuard | None = None,
        barrier: RuntimeMutationBarrier | None = None,
    ) -> None:
        if seal is not _SEAL:
            raise RuntimeError("schema_migration_authority_required")
        self._seal = seal
        self._connection = connection
        self._mode = mode
        self._guard = guard
        self._barrier = barrier
        self._activated = False
        self._retired = False

    @property
    def mode(self) -> MigrationAuthorityMode:
        return self._mode

    def _validate_maintenance_binding(
        self,
        connection: Connection,
    ) -> None:
        guard = self._guard
        barrier = self._barrier
        handle = (
            guard.handle
            if type(guard) is RuntimeTenureGuard
            else None
        )
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
            raise RuntimeError("schema_migration_authority_required")
        guard.assert_owned_in_transaction(connection)

    def assert_owned(self, connection: Connection) -> None:
        """Re-prove the durable lease on the exact migration connection."""

        if (
            self._seal is not _SEAL
            or self._retired
            or not self._activated
            or self._connection is not connection
        ):
            raise RuntimeError("schema_migration_authority_required")
        if self._mode == "maintenance":
            self._validate_maintenance_binding(connection)

    @contextmanager
    def schema_fence(
        self,
        connection: Connection,
    ) -> Iterator[None]:
        """Narrowly permit rebuilding the table that stores the lease."""

        self.assert_owned(connection)
        if self._mode == "bootstrap":
            yield
            return
        barrier = self._barrier
        if type(barrier) is not RuntimeMutationBarrier:
            raise RuntimeError("schema_migration_authority_required")
        option_name, option_value = barrier.fence_schema_execution_option
        connection.execution_options(**{option_name: option_value})
        try:
            yield
        finally:
            connection.execution_options(**{option_name: None})
        self.assert_owned(connection)


def _rollback_inspection(connection: Connection) -> None:
    if connection.in_transaction():
        connection.rollback()


def issue_bootstrap_authority(
    connection: Connection,
) -> MigrationAuthority:
    """Issue for a currently empty DB; activation re-proves emptiness."""

    if inspect(connection).get_table_names():
        _rollback_inspection(connection)
        raise RuntimeError("schema_bootstrap_authority_refused")
    _rollback_inspection(connection)
    return MigrationAuthority(_SEAL, connection, "bootstrap")


def issue_maintenance_authority(
    connection: Connection,
    *,
    guard: RuntimeTenureGuard | None = None,
    barrier: RuntimeMutationBarrier | None = None,
) -> MigrationAuthority:
    """Issue only from a real held lease and its installed SQL fence."""

    if connection.in_transaction():
        raise RuntimeError("schema_migration_authority_required")
    authority = MigrationAuthority(
        _SEAL,
        connection,
        "maintenance",
        guard=guard,
        barrier=barrier,
    )
    try:
        authority._validate_maintenance_binding(connection)
    finally:
        _rollback_inspection(connection)
    return authority


def activate_migration_authority(
    authority: object,
    connection: Connection,
) -> MigrationAuthorityMode:
    """Consume one authority and validate it before Alembic can run."""

    if (
        not isinstance(authority, MigrationAuthority)
        or authority._seal is not _SEAL
        or authority._activated
        or authority._retired
    ):
        raise RuntimeError("schema_migration_authority_required")
    # Any activation attempt consumes the token, including wrong-connection
    # misuse or failed bootstrap/lease validation.
    authority._activated = True
    if authority._connection is not connection:
        authority._retired = True
        raise RuntimeError("schema_migration_authority_required")
    if authority._mode == "bootstrap":
        tables = inspect(connection).get_table_names()
        _rollback_inspection(connection)
        if tables:
            authority._retired = True
            raise RuntimeError("schema_bootstrap_authority_refused")
    else:
        try:
            authority._validate_maintenance_binding(connection)
        except BaseException:
            authority._retired = True
            raise
        finally:
            _rollback_inspection(connection)
    return authority._mode


def assert_migration_authority(
    authority: object,
    connection: Connection,
    *,
    allowed_modes: frozenset[MigrationAuthorityMode],
) -> MigrationAuthority:
    """Validate active authority and exact durable ownership at DDL time."""

    if (
        not isinstance(authority, MigrationAuthority)
        or authority._seal is not _SEAL
        or authority._connection is not connection
        or not authority._activated
        or authority._retired
        or authority._mode not in allowed_modes
    ):
        raise RuntimeError("schema_migration_authority_required")
    authority.assert_owned(connection)
    return authority


def retire_migration_authority(authority: object) -> None:
    """Prevent reuse after success, refusal, or migration failure."""

    if isinstance(authority, MigrationAuthority):
        authority._retired = True
