"""Process-safe ordering barrier for submission claims and breaker trips."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


class SubmissionBarrier:
    """Exclusive OS lock shared by every process using one SQLite database.

    The sidecar lock is independent of SQLite transactions, so a submission can
    retain ordering ownership across broker I/O without holding a database
    transaction open. ``flock`` is released automatically if a process exits.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        bind = session_factory.kw.get("bind")
        if not isinstance(bind, Engine) or bind.dialect.name != "sqlite":
            raise ValueError("submission barrier requires a bound SQLite engine")
        database = bind.url.database
        if not database or database == ":memory:":
            raise ValueError(
                "submission barrier requires file-backed SQLite storage"
            )
        database_path = Path(database).expanduser().resolve()
        self.path = database_path.with_name(
            f"{database_path.name}.submission.lock"
        )

    @contextmanager
    def hold(self) -> Iterator[None]:
        descriptor = os.open(
            self.path,
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
