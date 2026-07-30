"""Small deterministic helpers for safety-boundary regression tests."""

from __future__ import annotations

from contextlib import contextmanager
import signal
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from trading_assistant.db import migration_authority as authority_module
from trading_assistant.db.migration_authority import (
    issue_bootstrap_authority,
)


class OperationDeadlineExceeded(RuntimeError):
    """A local-only operation did not complete inside its safety bound."""


@contextmanager
def operation_deadline(seconds: float):
    """Interrupt a main-thread local operation without leaving worker threads."""

    if seconds <= 0:
        raise ValueError("seconds must be positive")
    previous = signal.getsignal(signal.SIGALRM)

    def expired(_signum, _frame):
        raise OperationDeadlineExceeded(
            f"operation exceeded {seconds:.1f} seconds"
        )

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class _HistoricalBootstrapAuthority:
    """Test-only authority for constructing a pre-head migration fixture."""

    def __init__(self, connection, destination: str):
        self.connection = connection
        self.destination = destination
        self.activated = False
        self.observed = False
        self.retired = False


def _activate_historical(
    authority,
    connection,
    *,
    destination_revisions,
):
    normalized = (
        destination_revisions
        if isinstance(destination_revisions, str)
        else tuple(destination_revisions or ())
    )
    if (
        type(authority) is not _HistoricalBootstrapAuthority
        or authority.connection is not connection
        or authority.destination != normalized
        or authority.activated
        or authority.retired
        or inspect(connection).get_table_names()
    ):
        if type(authority) is _HistoricalBootstrapAuthority:
            authority.retired = True
        raise RuntimeError("schema_migration_authority_required")
    if connection.in_transaction():
        connection.rollback()
    authority.activated = True
    return "bootstrap"


def _observe_historical(
    authority,
    connection,
    *,
    step,
    heads,
):
    del heads
    if (
        type(authority) is not _HistoricalBootstrapAuthority
        or authority.connection is not connection
        or not authority.activated
        or authority.retired
        or step.is_stamp
        or not step.is_upgrade
    ):
        raise RuntimeError("schema_migration_authority_required")
    authority.observed = True


def _finish_historical(authority, connection):
    if (
        type(authority) is not _HistoricalBootstrapAuthority
        or authority.connection is not connection
        or not authority.activated
        or not authority.observed
        or authority.retired
    ):
        raise RuntimeError("schema_migration_authority_required")


def _retire_historical(authority):
    if type(authority) is _HistoricalBootstrapAuthority:
        authority.retired = True


def bootstrap_database_to_revision(
    database_url: str,
    revision: str,
) -> None:
    """Build one empty migration fixture through one-shot bootstrap authority."""

    # Match Alembic's historical test-fixture connection semantics. Production
    # engines deliberately enable WAL, but closed WAL databases without their
    # sidecars are invalid inputs to the safety-drill copy boundary.
    engine = create_engine(
        database_url,
        future=True,
        poolclass=NullPool,
    )
    try:
        with engine.connect() as connection:
            config = Config("alembic.ini")
            config.set_main_option("sqlalchemy.url", database_url)
            config.attributes["connection"] = connection
            head = ScriptDirectory.from_config(
                config
            ).get_current_head()
            if revision == "head" or revision == head:
                config.attributes["migration_authority"] = (
                    issue_bootstrap_authority(connection)
                )
                command.upgrade(config, revision)
            else:
                destination = ScriptDirectory.from_config(
                    config
                ).as_revision_number(revision)
                config.attributes["migration_authority"] = (
                    _HistoricalBootstrapAuthority(
                        connection,
                        destination,
                    )
                )
                with (
                    patch.object(
                        authority_module,
                        "activate_migration_authority",
                        _activate_historical,
                    ),
                    patch.object(
                        authority_module,
                        "observe_migration_step",
                        _observe_historical,
                    ),
                    patch.object(
                        authority_module,
                        "finish_migration_authority",
                        _finish_historical,
                    ),
                    patch.object(
                        authority_module,
                        "retire_migration_authority",
                        _retire_historical,
                    ),
                ):
                    command.upgrade(config, revision)
    finally:
        engine.dispose()
