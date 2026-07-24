import json
from decimal import Decimal
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


def _engine_at_revision(path: Path, revision: str):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _url(path))
    command.upgrade(cfg, revision)
    return create_db_engine(_url(path)), cfg


def _insert_legacy_rule(
    conn,
    *,
    rule_id: int,
    plan_id: int | None,
    state: str,
    pre_approved: bool = False,
    ticker: str = "AAPL",
    fraction=None,
    hwm=None,
    action: dict | None = None,
) -> None:
    conn.execute(
        text(
            "INSERT INTO rules "
            "(id,ticker,condition_json,action_json,state,created_at,plan_id,kind,"
            "fraction,hwm,deadline,pre_approved) "
            "VALUES (:id,:ticker,:condition,:action,:state,CURRENT_TIMESTAMP,"
            ":plan_id,'price',:fraction,:hwm,NULL,:pre_approved)"
        ),
        {
            "id": rule_id,
            "ticker": ticker,
            "condition": json.dumps({"price_below": 100 + rule_id}),
            "action": json.dumps(
                action
                if action is not None
                else {"side": "buy", "notional": "50"}
            ),
            "state": state,
            "plan_id": plan_id,
            "pre_approved": pre_approved,
            "fraction": fraction,
            "hwm": hwm,
        },
    )


def test_fresh_database_upgrades_to_head(tmp_path):
    engine = create_db_engine(_url(tmp_path / "fresh.db"))
    upgrade(engine)
    require_current_schema(engine)
    assert "orders" in inspect(engine).get_table_names()
    assert "reconciliation_cursors" in inspect(engine).get_table_names()
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


def test_rule_lease_upgrade_from_0003_maps_every_repository_legacy_shape(tmp_path):
    engine, cfg = _engine_at_revision(
        tmp_path / "legacy-rules.db", "20260724_0003"
    )
    rows = [
        (
            1,
            "AAPL",
            {"price_below": 175},
            {"side": "buy", "notional": "50"},
            "active",
            7,
            "entry",
            None,
            None,
            None,
            True,
        ),
        (
            2,
            "AAPL",
            {"price_above": 200},
            {"side": "sell", "qty": "2"},
            "processing",
            7,
            "target",
            "0.5",
            None,
            None,
            True,
        ),
        (
            3,
            "AAPL",
            {"trailing_stop_pct": 8},
            {"side": "sell", "qty": "4"},
            "canceled",
            None,
            "trailing",
            None,
            "212.50",
            None,
            True,
        ),
        (
            4,
            "AAPL",
            {},
            {"side": "sell", "qty": "4"},
            "active",
            None,
            "time",
            None,
            None,
            "2026-08-01 12:00:00",
            False,
        ),
        (
            5,
            "MSFT",
            {"retired_custom_shape": "preserve-me"},
            {"historical_action": "preserve-me"},
            "canceled",
            None,
            "price",
            None,
            None,
            None,
            False,
        ),
    ]
    with engine.begin() as conn:
        for row in rows:
            (
                rule_id,
                ticker,
                condition,
                action,
                state,
                plan_id,
                kind,
                fraction,
                hwm,
                deadline,
                pre_approved,
            ) = row
            conn.execute(
                text(
                    "INSERT INTO rules "
                    "(id,ticker,condition_json,action_json,state,created_at,plan_id,"
                    "kind,fraction,hwm,deadline,pre_approved) "
                    "VALUES (:id,:ticker,:condition,:action,:state,CURRENT_TIMESTAMP,"
                    ":plan_id,:kind,:fraction,:hwm,:deadline,:pre_approved)"
                ),
                {
                    "id": rule_id,
                    "ticker": ticker,
                    "condition": json.dumps(condition),
                    "action": json.dumps(action),
                    "state": state,
                    "plan_id": plan_id,
                    "kind": kind,
                    "fraction": fraction,
                    "hwm": hwm,
                    "deadline": deadline,
                    "pre_approved": pre_approved,
                },
            )
        conn.execute(
            text(
                "INSERT INTO orders "
                    "(id,idempotency_key,ticker,side,order_type,status,created_at,"
                "updated_at,approval_reason,submission_kind,"
                "submission_payload_json,submission_attempt,acceptance_state,"
                "last_error_code,version) "
                    "VALUES (1,'rule-1','AAPL','buy','market','acceptance_unknown',"
                    "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'','simple','{}',0,"
                    "'acceptance_unknown','',0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO proposals "
                "(order_id,reasoning,ttl_minutes,created_at,expires_at) "
                "VALUES (1,'legacy rule trace',15,CURRENT_TIMESTAMP,"
                "'2026-08-01 12:00:00')"
            )
        )

    command.upgrade(cfg, "20260724_0004")

    with engine.connect() as conn:
        migrated = conn.execute(
            text(
                "SELECT id, condition_json, action_json, group_id, payload_version, "
                "plan_id, fraction, hwm, deadline, pre_approved "
                "FROM rules ORDER BY id"
            )
        ).mappings().all()
        groups = conn.execute(
            text("SELECT id, group_key FROM rule_groups ORDER BY group_key")
        ).mappings().all()
        proposal_columns = {
            column["name"] for column in inspect(engine).get_columns("proposals")
        }
        source_rule_group_id = conn.scalar(
            text(
                "SELECT source_rule_group_id FROM proposals WHERE order_id=1"
            )
        )
        source_group_reconciliation_required = conn.scalar(
            text(
                "SELECT reconciliation_required FROM rule_groups "
                "WHERE id=:group_id"
            ),
            {"group_id": source_rule_group_id},
        )

    assert json.loads(migrated[0]["condition_json"]) == {
        "type": "price",
        "direction": "below",
        "price": 175,
    }
    assert json.loads(migrated[1]["condition_json"]) == {
        "type": "price",
        "direction": "above",
        "price": 200,
    }
    assert json.loads(migrated[2]["condition_json"]) == {
        "type": "trailing",
        "percent": 8,
    }
    assert json.loads(migrated[3]["condition_json"]) == {
        "type": "time",
        "deadline": "2026-08-01 12:00:00",
    }
    assert all(
        json.loads(row["action_json"])["order_type"] == "market"
        for row in migrated[:4]
    )
    assert migrated[0]["group_id"] == migrated[1]["group_id"]
    assert migrated[2]["group_id"] != migrated[3]["group_id"]
    assert {row["group_key"] for row in groups} == {
        "legacy-plan-7",
        "legacy-rule-3",
        "legacy-rule-4",
        "legacy-rule-5",
    }
    assert all(row["payload_version"] == 1 for row in migrated[:4])
    assert migrated[4]["payload_version"] == 0
    assert json.loads(migrated[4]["condition_json"]) == {
        "retired_custom_shape": "preserve-me"
    }
    assert json.loads(migrated[4]["action_json"]) == {
        "historical_action": "preserve-me"
    }
    assert migrated[0]["plan_id"] == 7
    assert Decimal(str(migrated[1]["fraction"])) == Decimal("0.5")
    assert Decimal(str(migrated[2]["hwm"])) == Decimal("212.50")
    assert migrated[0]["pre_approved"] == 0
    assert migrated[1]["pre_approved"] == 0
    assert migrated[2]["pre_approved"] == 1
    assert "source_rule_group_id" in proposal_columns
    assert source_rule_group_id == migrated[0]["group_id"]
    assert source_group_reconciliation_required == 1


def test_rule_lease_upgrade_aborts_on_unknown_active_shape(tmp_path):
    engine, cfg = _engine_at_revision(
        tmp_path / "unknown-active-rule.db", "20260724_0003"
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO rules "
                "(ticker,condition_json,action_json,state,created_at,plan_id,kind,"
                "fraction,hwm,deadline,pre_approved) "
                "VALUES ('AAPL',:condition,:action,'active',CURRENT_TIMESTAMP,"
                "NULL,'price',NULL,NULL,NULL,0)"
            ),
            {
                "condition": json.dumps({"mystery": 1}),
                "action": json.dumps({"side": "buy", "notional": "50"}),
            },
        )

    with pytest.raises(RuntimeError, match="unknown active rule"):
        command.upgrade(cfg, "20260724_0004")

    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
            "20260724_0003"
        )
        assert "rule_groups" not in inspect(engine).get_table_names()


@pytest.mark.parametrize(
    ("state", "field", "value"),
    [
        ("active", "ticker", "   "),
        ("active", "ticker", "ABCDEFGHIJKLMNOPQ"),
        ("active", "fraction", "0"),
        ("active", "fraction", "1.01"),
        ("active", "fraction", "NaN"),
        ("active", "hwm", "0"),
        ("active", "hwm", "Infinity"),
        ("processing", "hwm", "Infinity"),
        ("active", "fraction", "0.0000001"),
        ("processing", "fraction", "0.1234567"),
        ("active", "hwm", "0.0000001"),
        ("processing", "hwm", "100000000000000"),
    ],
)
def test_rule_lease_upgrade_aborts_invalid_resumable_scalar_before_ddl(
    tmp_path, state, field, value
):
    engine, cfg = _engine_at_revision(
        tmp_path / f"invalid-{field}-rule.db", "20260724_0003"
    )
    values = {"ticker": "AAPL", "fraction": None, "hwm": None}
    values[field] = value
    with engine.begin() as conn:
        _insert_legacy_rule(
            conn,
            rule_id=1,
            plan_id=None,
            state=state,
            **values,
        )

    with pytest.raises(RuntimeError, match="unknown active rule"):
        command.upgrade(cfg, "20260724_0004")

    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
            "20260724_0003"
        )
        assert "rule_groups" not in inspect(engine).get_table_names()


@pytest.mark.parametrize(
    ("state", "field", "value"),
    [
        ("active", "qty", "0.0000001"),
        ("processing", "qty", "0.0000001"),
        ("active", "qty", "100000000000000"),
        ("processing", "qty", "100000000000000"),
        ("active", "notional", "0.0000001"),
        ("processing", "notional", "0.0000001"),
        ("active", "notional", "100000000000000"),
        ("processing", "notional", "100000000000000"),
        ("active", "limit_price", "0.0000001"),
        ("processing", "limit_price", "0.0000001"),
        ("active", "limit_price", "100000000000000"),
        ("processing", "limit_price", "100000000000000"),
    ],
)
def test_rule_lease_upgrade_aborts_unpersistable_action_before_ddl(
    tmp_path, state, field, value
):
    engine, cfg = _engine_at_revision(
        tmp_path / "invalid-action-rule.db", "20260724_0003"
    )
    action = {
        "side": "buy",
        "order_type": "limit" if field == "limit_price" else "market",
        "qty": "1" if field == "limit_price" else value,
    }
    if field == "notional":
        action.pop("qty")
        action["notional"] = value
    elif field == "limit_price":
        action["limit_price"] = value

    with engine.begin() as conn:
        _insert_legacy_rule(
            conn,
            rule_id=1,
            plan_id=None,
            state=state,
            action=action,
        )

    with pytest.raises(RuntimeError, match="unknown active rule"):
        command.upgrade(cfg, "20260724_0004")

    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
            "20260724_0003"
        )
        assert "rule_groups" not in inspect(engine).get_table_names()


def test_rule_lease_upgrade_cancels_resumable_siblings_of_terminal_winner(tmp_path):
    engine, cfg = _engine_at_revision(
        tmp_path / "terminal-rule-group.db", "20260724_0003"
    )
    with engine.begin() as conn:
        _insert_legacy_rule(
            conn, rule_id=1, plan_id=9, state="triggered"
        )
        _insert_legacy_rule(conn, rule_id=2, plan_id=9, state="active")
        _insert_legacy_rule(
            conn, rule_id=3, plan_id=9, state="processing"
        )

    command.upgrade(cfg, "20260724_0004")

    with engine.connect() as conn:
        group = conn.execute(
            text(
                "SELECT state, terminal_rule_id FROM rule_groups "
                "WHERE group_key='legacy-plan-9'"
            )
        ).mappings().one()
        states = dict(
            conn.execute(
                text("SELECT id, state FROM rules ORDER BY id")
            ).all()
        )

    assert group == {"state": "triggered", "terminal_rule_id": 1}
    assert states == {1: "triggered", 2: "canceled", 3: "canceled"}


def test_rule_lease_upgrade_aborts_plan_with_multiple_terminal_winners(tmp_path):
    engine, cfg = _engine_at_revision(
        tmp_path / "ambiguous-terminal-rule-group.db", "20260724_0003"
    )
    with engine.begin() as conn:
        _insert_legacy_rule(
            conn, rule_id=1, plan_id=10, state="triggered"
        )
        _insert_legacy_rule(conn, rule_id=2, plan_id=10, state="failed")

    with pytest.raises(RuntimeError, match="multiple terminal winners"):
        command.upgrade(cfg, "20260724_0004")

    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
            "20260724_0003"
        )
        assert "rule_groups" not in inspect(engine).get_table_names()
