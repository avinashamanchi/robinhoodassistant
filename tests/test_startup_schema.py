"""Production roots must fail closed until the Alembic schema is current."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

import trading_assistant.logging as app_logging
from trading_assistant.db.migrate import upgrade
from trading_assistant.db.schema import SchemaOutOfDate
from trading_assistant.db.session import create_db_engine


def _revision_0004(tmp_path, name="startup.db"):
    path = tmp_path / name
    url = f"sqlite:///{path}"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "20260724_0004")
    return create_db_engine(url), url


def _patch_common_startup(monkeypatch, module, url):
    from trading_assistant.config import BrokerKind, load_config

    config = load_config(
        Path(__file__).resolve().parent.parent / "config.yaml"
    )
    config = config.model_copy(
        update={
            "trading": config.trading.model_copy(
                update={"broker": BrokerKind.ALPACA}
            )
        }
    )
    monkeypatch.setattr(module, "load_config", lambda: config)
    monkeypatch.setattr(
        module,
        "Secrets",
        lambda: SimpleNamespace(
            database_url=url,
            app_api_token="startup-schema-test-secret",
        ),
    )
    monkeypatch.setattr(app_logging, "register_all_secrets", lambda _secrets: None)


@pytest.mark.parametrize(
    "source",
    [
        "src/trading_assistant/app/main.py",
        "src/trading_assistant/daemon/main.py",
        "src/trading_assistant/mcp_server/server.py",
        "src/trading_assistant/preflight.py",
        "src/trading_assistant/ops/paper_drill.py",
    ],
)
def test_production_roots_never_call_create_all(source):
    assert "create_all" not in Path(source).read_text(encoding="utf-8")


def test_runtime_package_never_calls_create_all():
    offenders = [
        str(path)
        for path in Path("src/trading_assistant").rglob("*.py")
        if "Base.metadata.create_all(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_api_startup_fails_on_0004_without_mutating_schema_and_upgrade_preserves_latches(
    tmp_path,
    monkeypatch,
):
    from trading_assistant.app import main as app_main

    engine, url = _revision_0004(tmp_path, "api-0004.db")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO killswitch_state "
                "(asset_class,tripped,tripped_at,reason,updated_at) VALUES "
                "('equity',1,CURRENT_TIMESTAMP,'equity loss',CURRENT_TIMESTAMP),"
                "('operator_global',1,CURRENT_TIMESTAMP,'panic',CURRENT_TIMESTAMP)"
            )
        )
    before = set(inspect(engine).get_table_names())
    _patch_common_startup(monkeypatch, app_main, url)

    with pytest.raises(SchemaOutOfDate, match="current='20260724_0004'"):
        app_main.build_default_stack()

    assert set(inspect(engine).get_table_names()) == before
    assert "circuit_breaker_state" not in before

    upgrade(engine)

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT scope_key,tripped,reason FROM circuit_breaker_state "
                "WHERE scope_key IN ('loss:equity','operator_global') "
                "ORDER BY scope_key"
            )
        ).mappings().all()
    assert rows == [
        {
            "scope_key": "loss:equity",
            "tripped": 1,
            "reason": "equity loss",
        },
        {
            "scope_key": "operator_global",
            "tripped": 1,
            "reason": "panic",
        },
    ]


def test_daemon_startup_fails_on_0004_before_constructing_monitor(
    tmp_path,
    monkeypatch,
):
    from trading_assistant.daemon import main as daemon_main

    _engine, url = _revision_0004(tmp_path, "daemon-0004.db")
    _patch_common_startup(monkeypatch, daemon_main, url)

    with pytest.raises(SchemaOutOfDate, match="run .*upgrade"):
        daemon_main.build_monitor()


def test_mcp_startup_fails_on_0004_before_constructing_service(
    tmp_path,
    monkeypatch,
):
    from trading_assistant.mcp_server import server

    _engine, url = _revision_0004(tmp_path, "mcp-0004.db")
    _patch_common_startup(monkeypatch, server, url)

    with pytest.raises(SchemaOutOfDate, match="run .*upgrade"):
        server.build_default_service()


def test_preflight_reports_outdated_schema_without_mutating_it(tmp_path):
    from trading_assistant.preflight import FAIL, _db

    engine, url = _revision_0004(tmp_path, "preflight-0004.db")
    before = set(inspect(engine).get_table_names())

    schema, wal, breakers = _db(SimpleNamespace(database_url=url))

    assert schema.status == FAIL
    assert wal.status == FAIL
    assert breakers.status == FAIL
    assert schema.detail == "schema_out_of_date"
    assert wal.detail == "schema_out_of_date"
    assert set(inspect(engine).get_table_names()) == before
    assert "circuit_breaker_state" not in before
