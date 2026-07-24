"""Consistent online SQLite backups with bounded retention."""

from __future__ import annotations

import argparse
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url

from ..config import Secrets

_BACKUP_PREFIX = "trading-assistant-"
_BACKUP_SUFFIX = ".sqlite3"


def backup_database(
    source: str | Path,
    destination_dir: str | Path,
    retention_days: int = 14,
) -> Path:
    """Create a transactionally consistent backup and rotate our old backups."""
    if retention_days <= 0:
        raise ValueError("retention_days must be positive")
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    destination = Path(destination_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = destination / f"{_BACKUP_PREFIX}{stamp}{_BACKUP_SUFFIX}"
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    try:
        with (
            sqlite3.connect(source_path) as source_connection,
            sqlite3.connect(temporary) as backup_connection,
        ):
            source_connection.backup(backup_connection)
            check = backup_connection.execute("PRAGMA integrity_check").fetchone()
            if check != ("ok",):
                raise RuntimeError(f"backup integrity check failed: {check}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
        temporary.with_name(f"{temporary.name}-wal").unlink(missing_ok=True)
        temporary.with_name(f"{temporary.name}-shm").unlink(missing_ok=True)

    cutoff = time.time() - retention_days * 86400
    pattern = f"{_BACKUP_PREFIX}*{_BACKUP_SUFFIX}"
    for candidate in destination.glob(pattern):
        if candidate != target and candidate.stat().st_mtime < cutoff:
            candidate.unlink()
    return target


def database_path(database_url: str) -> Path:
    url = make_url(database_url)
    if url.drivername != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("backup supports only file-backed SQLite DATABASE_URL values")
    return Path(url.database)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", default="backups")
    parser.add_argument("--retention-days", type=int, default=14)
    args = parser.parse_args(argv)
    created = backup_database(
        database_path(Secrets().database_url),
        args.destination,
        args.retention_days,
    )
    print(created)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
