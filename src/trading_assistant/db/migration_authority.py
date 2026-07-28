"""Opaque, connection-bound authority for one Alembic command."""

from __future__ import annotations

from typing import Literal

from sqlalchemy import inspect
from sqlalchemy.engine import Connection


MigrationAuthorityMode = Literal["bootstrap", "maintenance"]
_SEAL = object()


class MigrationAuthority:
    """A single-use authority bound to one exact SQLAlchemy connection."""

    __slots__ = (
        "_activated",
        "_connection",
        "_mode",
        "_retired",
        "_seal",
    )

    def __init__(
        self,
        seal: object,
        connection: Connection,
        mode: MigrationAuthorityMode,
    ) -> None:
        if seal is not _SEAL:
            raise RuntimeError("schema_migration_authority_required")
        self._seal = seal
        self._connection = connection
        self._mode = mode
        self._activated = False
        self._retired = False


def issue_bootstrap_authority(
    connection: Connection,
) -> MigrationAuthority:
    """Issue once only after proving the supplied database is truly empty."""

    if inspect(connection).get_table_names():
        raise RuntimeError("schema_bootstrap_authority_refused")
    if connection.in_transaction():
        connection.rollback()
    return MigrationAuthority(_SEAL, connection, "bootstrap")


def issue_maintenance_authority(
    connection: Connection,
) -> MigrationAuthority:
    """Issue the wrapper's connection-bound maintenance capability."""

    return MigrationAuthority(_SEAL, connection, "maintenance")


def activate_migration_authority(
    authority: object,
    connection: Connection,
) -> MigrationAuthorityMode:
    """Consume the authority for one Alembic environment invocation."""

    if (
        not isinstance(authority, MigrationAuthority)
        or authority._seal is not _SEAL
        or authority._connection is not connection
        or authority._activated
        or authority._retired
    ):
        raise RuntimeError("schema_migration_authority_required")
    authority._activated = True
    return authority._mode


def assert_migration_authority(
    authority: object,
    connection: Connection,
    *,
    allowed_modes: frozenset[MigrationAuthorityMode],
) -> MigrationAuthorityMode:
    """Validate active authority before a revision performs its first write."""

    if (
        not isinstance(authority, MigrationAuthority)
        or authority._seal is not _SEAL
        or authority._connection is not connection
        or not authority._activated
        or authority._retired
        or authority._mode not in allowed_modes
    ):
        raise RuntimeError("schema_migration_authority_required")
    return authority._mode


def retire_migration_authority(authority: object) -> None:
    """Prevent reuse after success or failure."""

    if isinstance(authority, MigrationAuthority):
        authority._retired = True

