"""Engine/session factory. SQLite runs in WAL mode (A5): the host app and the
monitoring daemon both write, and WAL allows concurrent readers with a writer.
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, event
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
        event.listen(engine, "connect", _enable_sqlite_pragmas)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
