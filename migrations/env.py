from logging.config import fileConfig

from alembic import context

from trading_assistant.db.models import Base
from trading_assistant.db.migration_authority import (
    activate_migration_authority,
    retire_migration_authority,
)


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    raise RuntimeError("schema_migration_offline_refused")


def run_migrations_online() -> None:
    supplied_connection = config.attributes.get("connection")
    authority = config.attributes.get("migration_authority")
    if supplied_connection is None or authority is None:
        raise RuntimeError("schema_migration_authority_required")
    try:
        activate_migration_authority(authority, supplied_connection)
        context.configure(
            connection=supplied_connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()
    finally:
        retire_migration_authority(authority)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
