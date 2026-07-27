import argparse
import os
import secrets
import sqlite3
import sys
import stat
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect
from sqlalchemy.engine import make_url

from trading_assistant.config import load_config
from trading_assistant.security.secrets import (
    EnvironmentSecretProvider,
    load_role_secrets,
)

from .schema import SchemaOutOfDate, require_current_schema, schema_status
from .session import create_db_engine

BASELINE = "20260724_0001"
LEGACY_TABLES = {
    "analysis_reports", "backtest_metric_rows", "backtest_runs", "fills",
    "graded_calls", "heartbeats", "holdout_access_log", "killswitch_state",
    "llm_decisions", "orders", "proposals", "risk_events", "rules",
    "shadow_calls", "trade_plans",
}
_BACKUP_NAME_ATTEMPTS = 16


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
    source_uri = f"file:{quote(str(source), safe='/')}?mode=ro"
    # SQLite's online backup API includes committed WAL pages. Materialize into
    # memory first so no attacker-controlled filesystem path is ever opened by
    # SQLite as a writable destination.
    with (
        sqlite3.connect(source_uri, uri=True) as source_db,
        sqlite3.connect(":memory:") as backup_db,
    ):
        source_db.backup(backup_db)
        integrity = backup_db.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise RuntimeError(f"migration backup failed integrity check: {integrity!r}")
        backed_up_tables = {
            row[0]
            for row in backup_db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        source_tables = {
            row[0]
            for row in source_db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        serialized = backup_db.serialize()
    if backed_up_tables != source_tables:
        raise RuntimeError("migration backup table manifest mismatch")

    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_fd = os.open(source.parent, directory_flags)
    staging_name: str | None = None
    staging_fd: int | None = None
    try:
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        file_flags |= getattr(os, "O_CLOEXEC", 0)
        for _ in range(_BACKUP_NAME_ATTEMPTS):
            candidate = (
                f".{source.name}.migration-backup-"
                f"{secrets.token_hex(16)}"
            )
            try:
                staging_fd = os.open(
                    candidate,
                    file_flags,
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                continue
            staging_name = candidate
            break
        if staging_fd is None or staging_name is None:
            raise RuntimeError("could not allocate private migration backup")

        os.fchmod(staging_fd, 0o600)
        remaining = memoryview(serialized)
        while remaining:
            written = os.write(staging_fd, remaining)
            if written <= 0:
                raise OSError("short write creating migration backup")
            remaining = remaining[written:]
        os.fsync(staging_fd)

        staged = os.fstat(staging_fd)
        linked = os.stat(
            staging_name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(linked.st_mode)
            or (staged.st_dev, staged.st_ino)
            != (linked.st_dev, linked.st_ino)
        ):
            raise RuntimeError("migration backup staging identity changed")

        target_name: str | None = None
        for _ in range(_BACKUP_NAME_ATTEMPTS):
            candidate = (
                f"{source.name}.{stamp}.{secrets.token_hex(8)}."
                "pre-migration.bak"
            )
            try:
                os.link(
                    staging_name,
                    candidate,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                continue
            target_name = candidate
            break
        if target_name is None:
            raise RuntimeError("could not publish migration backup")

        published = os.stat(
            target_name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(published.st_mode)
            or (staged.st_dev, staged.st_ino)
            != (published.st_dev, published.st_ino)
        ):
            raise RuntimeError("migration backup publication identity changed")
        os.fsync(directory_fd)
        os.unlink(staging_name, dir_fd=directory_fd)
        staging_name = None
        os.fsync(directory_fd)
        return source.parent / target_name
    finally:
        if staging_name is not None and staging_fd is not None:
            try:
                staged = os.fstat(staging_fd)
                linked = os.stat(
                    staging_name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if (staged.st_dev, staged.st_ino) == (
                    linked.st_dev,
                    linked.st_ino,
                ):
                    os.unlink(staging_name, dir_fd=directory_fd)
                    os.fsync(directory_fd)
            except FileNotFoundError:
                pass
        if staging_fd is not None:
            os.close(staging_fd)
        os.close(directory_fd)


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
    parser.add_argument(
        "--development-environment-secrets",
        action="store_true",
    )
    parser.add_argument("command", choices=("status", "adopt-existing", "upgrade"))
    args = parser.parse_args(argv)
    config = load_config()
    if args.development_environment_secrets:
        provider = EnvironmentSecretProvider(environ=os.environ)
        runtime_secrets = load_role_secrets(
            "migration",
            config=config,
            provider=provider,
            allow_environment=True,
        )
    else:
        runtime_secrets = load_role_secrets(
            "migration",
            config=config,
        )
    engine = create_db_engine(runtime_secrets.database_url)

    from trading_assistant.logging import runtime_startup

    with runtime_startup("migration", runtime_secrets):
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
