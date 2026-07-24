import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect
from sqlalchemy.engine import make_url

from trading_assistant.config import Secrets

from .schema import SchemaOutOfDate, require_current_schema, schema_status
from .session import create_db_engine

BASELINE = "20260724_0001"
LEGACY_TABLES = {
    "analysis_reports", "backtest_metric_rows", "backtest_runs", "fills",
    "graded_calls", "heartbeats", "holdout_access_log", "killswitch_state",
    "llm_decisions", "orders", "proposals", "risk_events", "rules",
    "shadow_calls", "trade_plans",
}


def _config(engine: Engine) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    return cfg


def _backup(engine: Engine) -> Path | None:
    url = make_url(str(engine.url))
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    source = Path(url.database).resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = source.with_name(f"{source.name}.{stamp}.pre-migration.bak")
    # SQLite's online backup API includes committed WAL pages. A raw file copy
    # can silently omit them.
    with sqlite3.connect(source) as source_db, sqlite3.connect(target) as backup_db:
        source_db.backup(backup_db)
    with sqlite3.connect(target) as backup_db:
        integrity = backup_db.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            target.unlink(missing_ok=True)
            raise RuntimeError(f"migration backup failed integrity check: {integrity!r}")
        backed_up_tables = {
            row[0]
            for row in backup_db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    source_tables = set(inspect(engine).get_table_names())
    if backed_up_tables != source_tables:
        target.unlink(missing_ok=True)
        raise RuntimeError("migration backup table manifest mismatch")
    target.chmod(0o600)
    return target


def adopt_existing(engine: Engine) -> Path:
    tables = set(inspect(engine).get_table_names())
    if tables != LEGACY_TABLES:
        raise RuntimeError(f"legacy schema mismatch: {sorted(tables ^ LEGACY_TABLES)}")
    backup = _backup(engine)
    assert backup is not None
    command.stamp(_config(engine), BASELINE)
    return backup


def upgrade(engine: Engine) -> Path | None:
    status = schema_status(engine)
    if not status.versioned and set(inspect(engine).get_table_names()):
        raise RuntimeError(
            "unversioned non-empty database; run `python -m "
            "trading_assistant.db.migrate adopt-existing` first"
        )
    backup = _backup(engine) if status.versioned and not status.ready else None
    command.upgrade(_config(engine), "head")
    require_current_schema(engine)
    return backup


def _print_result(action: str, engine: Engine, backup: Path | None = None) -> None:
    status = schema_status(engine)
    print(
        f"{action}: current={status.current!r} head={status.head!r} "
        f"backup={backup if backup is not None else 'none'}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the trading assistant schema.")
    parser.add_argument("command", choices=("status", "adopt-existing", "upgrade"))
    args = parser.parse_args(argv)
    engine = create_db_engine(Secrets().database_url)

    try:
        if args.command == "status":
            _print_result("status", engine)
            require_current_schema(engine)
        elif args.command == "adopt-existing":
            backup = adopt_existing(engine)
            _print_result("adopt-existing", engine, backup)
            upgrade_backup = upgrade(engine)
            _print_result("upgrade", engine, upgrade_backup)
        else:
            backup = upgrade(engine)
            _print_result("upgrade", engine, backup)
    except SchemaOutOfDate as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
