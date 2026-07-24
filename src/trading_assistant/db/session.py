"""Engine/session factory. SQLite runs in WAL mode (A5): the host app and the
monitoring daemon both write, and WAL allows concurrent readers with a writer.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


def _enable_sqlite_pragmas(dbapi_conn, _record) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")     # concurrent readers + 1 writer
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")    # wait, don't fail, on contention
    cursor.close()


def create_db_engine(url: str = "sqlite:///./trading_assistant.db") -> Engine:
    connect_args = {}
    if url.startswith("sqlite"):
        # Allow the engine to be shared across threads (app + daemon).
        connect_args = {"check_same_thread": False}
    engine = create_engine(url, connect_args=connect_args, future=True)
    if url.startswith("sqlite"):
        parsed = make_url(url)
        database_path = (
            Path(parsed.database).expanduser().resolve()
            if parsed.database and parsed.database != ":memory:"
            else None
        )

        def configure_and_secure(dbapi_conn, record) -> None:
            _enable_sqlite_pragmas(dbapi_conn, record)
            if database_path is not None:
                for candidate in (
                    database_path,
                    database_path.with_name(f"{database_path.name}-wal"),
                    database_path.with_name(f"{database_path.name}-shm"),
                ):
                    if candidate.exists():
                        os.chmod(candidate, 0o600)

        event.listen(engine, "connect", configure_and_secure)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
