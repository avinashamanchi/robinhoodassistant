import argparse
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect
from sqlalchemy.engine import make_url

from trading_assistant.config import load_config
from trading_assistant.security.secrets import (
    EnvironmentSecretProvider,
    load_role_secrets,
    validate_base64_key,
)
from trading_assistant.ops.backup import (
    EncryptedBackupReceipt,
    create_encrypted_database_backup,
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
def _config(engine: Engine) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    return cfg


def _backup(
    engine: Engine,
    *,
    backup_key: bytes | None,
    backup_key_id: str | None,
    backup_directory: str | Path | None,
    schema_head: str,
) -> EncryptedBackupReceipt:
    url = make_url(str(engine.url))
    if (
        url.get_backend_name() != "sqlite"
        or not url.database
        or url.database == ":memory:"
        or not isinstance(backup_key, bytes)
        or len(backup_key) != 32
        or not backup_key_id
        or backup_directory is None
    ):
        raise RuntimeError("encrypted_migration_backup_required")
    return create_encrypted_database_backup(
        Path(url.database),
        backup_directory,
        backup_key=backup_key,
        backup_key_id=backup_key_id,
        schema_head=schema_head,
    )


def adopt_existing(
    engine: Engine,
    *,
    backup_key: bytes | None = None,
    backup_key_id: str | None = None,
    backup_directory: str | Path | None = None,
) -> EncryptedBackupReceipt:
    tables = set(inspect(engine).get_table_names())
    if tables != LEGACY_TABLES:
        raise RuntimeError(f"legacy schema mismatch: {sorted(tables ^ LEGACY_TABLES)}")
    backup = _backup(
        engine,
        backup_key=backup_key,
        backup_key_id=backup_key_id,
        backup_directory=backup_directory,
        schema_head=BASELINE,
    )
    command.stamp(_config(engine), BASELINE)
    return backup


def upgrade(
    engine: Engine,
    *,
    backup_key: bytes | None = None,
    backup_key_id: str | None = None,
    backup_directory: str | Path | None = None,
) -> EncryptedBackupReceipt | None:
    status = schema_status(engine)
    if not status.versioned and set(inspect(engine).get_table_names()):
        raise RuntimeError(
            "unversioned non-empty database; run `python -m "
            "trading_assistant.db.migrate adopt-existing` first"
        )
    backup = (
        _backup(
            engine,
            backup_key=backup_key,
            backup_key_id=backup_key_id,
            backup_directory=backup_directory,
            schema_head=status.current or BASELINE,
        )
        if status.versioned and not status.ready
        else None
    )
    command.upgrade(_config(engine), "head")
    require_current_schema(engine)
    return backup


def _print_result(
    action: str,
    engine: Engine,
    backup: EncryptedBackupReceipt | None = None,
) -> None:
    status = schema_status(engine)
    print(
        f"{action}: current={status.current!r} head={status.head!r} "
        "backup_path_hash="
        f"{backup.path_hash if backup is not None else 'none'}"
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
        provider = EnvironmentSecretProvider(
            environ=os.environ,
            encryption=config.encryption,
        )
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
    backup_buffer: bytearray | None = None

    from trading_assistant.logging import runtime_startup

    with runtime_startup("migration", runtime_secrets):
        try:
            if args.command == "status":
                _print_result("status", engine)
                require_current_schema(engine)
            elif args.command == "adopt-existing":
                backup_buffer = validate_base64_key(
                    "backup_encryption_key",
                    runtime_secrets.backup_encryption_key,
                )
                backup_args = {
                    "backup_key": bytes(backup_buffer),
                    "backup_key_id": config.encryption.backup_key_id,
                    "backup_directory": (
                        config.encryption.backup_directory
                    ),
                }
                backup = adopt_existing(engine, **backup_args)
                _print_result("adopt-existing", engine, backup)
                upgrade_backup = upgrade(engine, **backup_args)
                _print_result("upgrade", engine, upgrade_backup)
            else:
                backup_buffer = validate_base64_key(
                    "backup_encryption_key",
                    runtime_secrets.backup_encryption_key,
                )
                backup = upgrade(
                    engine,
                    backup_key=bytes(backup_buffer),
                    backup_key_id=config.encryption.backup_key_id,
                    backup_directory=config.encryption.backup_directory,
                )
                _print_result("upgrade", engine, backup)
        except SchemaOutOfDate as exc:
            print(exc, file=sys.stderr)
            return 1
        finally:
            if backup_buffer is not None:
                for index in range(len(backup_buffer)):
                    backup_buffer[index] = 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
