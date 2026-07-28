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
    BackupMaintenance,
    EncryptedBackupReceipt,
    create_encrypted_database_backup,
    guarded_backup_maintenance,
)
from trading_assistant.ops.tenure import (
    LocalProcessInspector,
    ProcessIdentity,
    ProcessInspector,
    RuntimeTenureGuard,
    RuntimeTenureService,
    TenureUncertain,
    install_runtime_mutation_barrier,
)

from .schema import SchemaOutOfDate, require_current_schema, schema_status
from .migration_authority import (
    issue_bootstrap_authority,
    issue_maintenance_authority,
)
from .models import utcnow
from .session import create_db_engine, make_session_factory

BASELINE = "20260724_0001"
def _config(engine: Engine) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    return cfg


def _run_bootstrap_upgrade(engine: Engine) -> None:
    with engine.connect() as connection:
        cfg = _config(engine)
        cfg.attributes["connection"] = connection
        cfg.attributes["migration_authority"] = (
            issue_bootstrap_authority(connection)
        )
        command.upgrade(cfg, "head")


def _backup(
    engine: Engine,
    *,
    backup_key: bytes | None,
    backup_key_id: str | None,
    backup_directory: str | Path | None,
    schema_head: str,
    maintenance: BackupMaintenance,
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
        maintenance=maintenance,
    )


def adopt_existing(
    engine: Engine,
    *,
    backup_key: bytes | None = None,
    backup_key_id: str | None = None,
    backup_directory: str | Path | None = None,
) -> EncryptedBackupReceipt:
    del engine, backup_key, backup_key_id, backup_directory
    # A pre-tenure database cannot prove that every legacy process is offline,
    # so an in-place stamp would create an unfenced schema race. Bootstrap it
    # only through a separately reviewed, isolated-copy procedure.
    raise RuntimeError("schema_maintenance_bootstrap_required")


def upgrade(
    engine: Engine,
    *,
    backup_key: bytes | None = None,
    backup_key_id: str | None = None,
    backup_directory: str | Path | None = None,
    process_identity: ProcessIdentity | None = None,
    process_inspector: ProcessInspector | None = None,
    tenure_clock=utcnow,
) -> EncryptedBackupReceipt | None:
    status = schema_status(engine)
    tables = set(inspect(engine).get_table_names())
    if not status.versioned and tables:
        raise RuntimeError("schema_maintenance_bootstrap_required")
    if not tables:
        _run_bootstrap_upgrade(engine)
        require_current_schema(engine)
        return None
    if status.ready:
        require_current_schema(engine)
        return None
    if "runtime_tenures" not in tables:
        raise RuntimeError("schema_maintenance_bootstrap_required")
    if process_identity is None or process_inspector is None:
        raise RuntimeError("schema_maintenance_tenure_required")

    service = RuntimeTenureService(
        make_session_factory(engine),
        process_inspector=process_inspector,
        clock=tenure_clock,
    )
    handle = service.acquire_maintenance(
        process_identity,
        ttl_seconds=30,
    )
    guard = RuntimeTenureGuard(
        handle,
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    maintenance = guarded_backup_maintenance(
        guard,
        ttl_seconds=30,
    )
    barrier = None
    primary_failure = False
    released = False
    try:
        backup = _backup(
            engine,
            backup_key=backup_key,
            backup_key_id=backup_key_id,
            backup_directory=backup_directory,
            schema_head=status.current or BASELINE,
            maintenance=maintenance,
        )
        barrier = install_runtime_mutation_barrier(engine, guard)
        guard.ensure_owned()
        with engine.connect() as connection:
            cfg = _config(engine)
            cfg.attributes["connection"] = connection
            cfg.attributes["migration_authority"] = (
                issue_maintenance_authority(connection)
            )
            cfg.attributes["runtime_tenure_fence_schema"] = (
                barrier.fence_schema_execution_option
            )
            cfg.attributes["runtime_tenure_assert_owned"] = (
                guard.assert_owned_in_transaction
            )
            command.upgrade(cfg, "head")
        guard.ensure_owned()
        require_current_schema(engine)
        if not guard.close():
            raise TenureUncertain()
        released = True
        return backup
    except BaseException:
        primary_failure = True
        raise
    finally:
        if not released and not guard.closed:
            try:
                if not guard.close() and not primary_failure:
                    raise TenureUncertain()
            except BaseException:
                if not primary_failure:
                    raise
        if barrier is not None:
            barrier.close()


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


def main(
    argv: list[str] | None = None,
    *,
    process_identity: ProcessIdentity | None = None,
    process_inspector: ProcessInspector | None = None,
) -> int:
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
    inspector = process_inspector or LocalProcessInspector()
    identity = process_identity

    from trading_assistant.logging import runtime_startup

    with runtime_startup("migration", runtime_secrets):
        try:
            if args.command == "status":
                _print_result("status", engine)
                require_current_schema(engine)
            elif args.command == "adopt-existing":
                identity = identity or inspector.current()
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
                upgrade_backup = upgrade(
                    engine,
                    **backup_args,
                    process_identity=identity,
                    process_inspector=inspector,
                )
                _print_result("upgrade", engine, upgrade_backup)
            else:
                identity = identity or inspector.current()
                backup_buffer = validate_base64_key(
                    "backup_encryption_key",
                    runtime_secrets.backup_encryption_key,
                )
                backup = upgrade(
                    engine,
                    backup_key=bytes(backup_buffer),
                    backup_key_id=config.encryption.backup_key_id,
                    backup_directory=config.encryption.backup_directory,
                    process_identity=identity,
                    process_inspector=inspector,
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
