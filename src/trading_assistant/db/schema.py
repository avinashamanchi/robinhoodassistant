from dataclasses import dataclass

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect


class SchemaOutOfDate(RuntimeError):
    pass


@dataclass(frozen=True)
class SchemaStatus:
    current: str | None
    head: str
    versioned: bool

    @property
    def ready(self) -> bool:
        return self.versioned and self.current == self.head


def _config() -> Config:
    return Config("alembic.ini")


def schema_status(engine: Engine) -> SchemaStatus:
    head = ScriptDirectory.from_config(_config()).get_current_head()
    versioned = "alembic_version" in inspect(engine).get_table_names()
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision() if versioned else None
    return SchemaStatus(current=current, head=head, versioned=versioned)


def require_current_schema(engine: Engine) -> None:
    status = schema_status(engine)
    if status.ready:
        return
    action = "adopt-existing" if not status.versioned else "upgrade"
    raise SchemaOutOfDate(
        f"database schema is not current: current={status.current!r}, "
        f"head={status.head!r}; run `python -m trading_assistant.db.migrate {action}`"
    )
