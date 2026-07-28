"""Small deterministic helpers for safety-boundary regression tests."""

from __future__ import annotations

from contextlib import contextmanager
import signal

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

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
            config.attributes["migration_authority"] = (
                issue_bootstrap_authority(connection)
            )
            command.upgrade(config, revision)
    finally:
        engine.dispose()
