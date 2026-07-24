from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from trading_assistant.db.migrate import adopt_existing, upgrade
from trading_assistant.db.schema import SchemaOutOfDate, require_current_schema
from trading_assistant.db.session import create_db_engine


def _url(path: Path) -> str:
    return f"sqlite:///{path}"


def _legacy_engine(path: Path):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _url(path))
    command.upgrade(cfg, "20260724_0001")
    engine = create_db_engine(_url(path))
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
    return engine


def test_fresh_database_upgrades_to_head(tmp_path):
    engine = create_db_engine(_url(tmp_path / "fresh.db"))
    upgrade(engine)
    require_current_schema(engine)
    assert "orders" in inspect(engine).get_table_names()
    assert "alembic_version" in inspect(engine).get_table_names()


def test_existing_unversioned_database_must_be_adopted(tmp_path):
    path = tmp_path / "legacy.db"
    engine = _legacy_engine(path)
    with pytest.raises(SchemaOutOfDate, match="adopt-existing"):
        require_current_schema(engine)
    backup = adopt_existing(engine)
    assert backup.exists()
    upgrade(engine)
    require_current_schema(engine)


def test_adoption_backup_contains_committed_wal_rows(tmp_path):
    path = tmp_path / "legacy.db"
    engine = _legacy_engine(path)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO orders "
                          "(idempotency_key,ticker,side,order_type,status,created_at,updated_at) "
                          "VALUES ('keep-me','AAPL','buy','market','proposed',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    backup = adopt_existing(engine)
    with create_db_engine(_url(backup)).connect() as conn:
        assert conn.scalar(
            text("SELECT count(*) FROM orders WHERE idempotency_key='keep-me'")
        ) == 1


def test_unknown_revision_is_rejected_at_startup(tmp_path):
    engine = create_db_engine(_url(tmp_path / "unknown.db"))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        conn.execute(text(
            "INSERT INTO alembic_version(version_num) VALUES ('unknown_revision')"
        ))
    with pytest.raises(SchemaOutOfDate, match="current='unknown_revision'"):
        require_current_schema(engine)


def test_order_outbox_upgrade_preserves_and_maps_legacy_order(tmp_path):
    path = tmp_path / "legacy-order.db"
    engine = _legacy_engine(path)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO orders "
            "(idempotency_key,ticker,side,order_type,status,created_at,updated_at) "
            "VALUES ('legacy-approved','AAPL','buy','market','approved',"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        ))
    adopt_existing(engine)
    upgrade_backup = upgrade(engine)
    assert upgrade_backup is not None
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT status, approval_actor FROM orders "
            "WHERE idempotency_key='legacy-approved'"
        )).one()
    assert row.status == "approval_recorded"
    assert row.approval_actor is None
