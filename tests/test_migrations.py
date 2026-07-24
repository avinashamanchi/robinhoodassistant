import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, inspect, select, text

from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import (
    BrokerFill,
    OrderResult,
    OrderStatus,
)
from trading_assistant.db.models import Fill, Order, ReconciliationCursor
from trading_assistant.db.migrate import adopt_existing, upgrade
from trading_assistant.db.schema import SchemaOutOfDate, require_current_schema
from trading_assistant.db.session import (
    create_db_engine,
    make_session_factory,
)
from trading_assistant.orders.reconciliation import ReconciliationService
from trading_assistant.orders.repository import OrderRepository


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


def test_breaker_upgrade_preserves_every_legacy_latch_and_adds_account_risk_state(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "legacy-breakers.db", "20260724_0004"
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO killswitch_state "
                "(asset_class,tripped,tripped_at,reason,updated_at) VALUES "
                "('equity',1,'2026-07-24 10:00:00','equity loss',"
                "'2026-07-24 10:00:00'),"
                "('crypto',0,NULL,'','2026-07-24 10:01:00'),"
                "('operator_global',1,'2026-07-24 10:02:00','panic',"
                "'2026-07-24 10:02:00')"
            )
        )

    command.upgrade(cfg, "20260724_0005")

    with engine.connect() as conn:
        tables = set(inspect(engine).get_table_names())
        rows = conn.execute(
            text(
                "SELECT scope_key,kind,target,tripped,reason,generation "
                "FROM circuit_breaker_state ORDER BY scope_key"
            )
        ).mappings().all()

    assert "killswitch_state" not in tables
    assert "account_risk_state" in tables
    assert rows == [
        {
            "scope_key": "loss:crypto",
            "kind": "loss",
            "target": "crypto",
            "tripped": 0,
            "reason": "",
            "generation": 1,
        },
        {
            "scope_key": "loss:equity",
            "kind": "loss",
            "target": "equity",
            "tripped": 1,
            "reason": "equity loss",
            "generation": 1,
        },
        {
            "scope_key": "operator_global",
            "kind": "operator_global",
            "target": "",
            "tripped": 1,
            "reason": "panic",
            "generation": 1,
        },
    ]


def test_breaker_migration_widens_fill_precision_and_empty_downgrade_restores_it(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "fill-precision.db", "20260724_0004"
    )

    before = {
        column["name"]: column["type"]
        for column in inspect(engine).get_columns("fills")
    }
    assert (before["qty"].precision, before["qty"].scale) == (20, 6)
    assert (before["price"].precision, before["price"].scale) == (20, 6)

    command.upgrade(cfg, "20260724_0005")

    widened = {
        column["name"]: column["type"]
        for column in inspect(engine).get_columns("fills")
    }
    assert (widened["qty"].precision, widened["qty"].scale) == (24, 9)
    assert (widened["price"].precision, widened["price"].scale) == (24, 9)

    command.downgrade(cfg, "20260724_0004")

    restored = {
        column["name"]: column["type"]
        for column in inspect(engine).get_columns("fills")
    }
    assert (restored["qty"].precision, restored["qty"].scale) == (20, 6)
    assert (restored["price"].precision, restored["price"].scale) == (20, 6)


@pytest.mark.parametrize(
    ("case_name", "broker_fill_id"),
    [
        ("null-id", None),
        ("caller-supplied-id", "pre-0005-caller-supplied-id"),
    ],
)
def test_breaker_upgrade_quarantines_every_preexisting_fill_and_trips_drift(
    tmp_path,
    case_name,
    broker_fill_id,
):
    engine, cfg = _engine_at_revision(
        tmp_path / f"legacy-{case_name}-fill.db",
        "20260724_0004",
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO orders "
                "(id,idempotency_key,ticker,side,order_type,qty,status,"
                "broker_order_id,created_at,updated_at,approval_reason,"
                "submission_kind,submission_payload_json,submission_attempt,"
                "acceptance_state,last_error_code,version) VALUES "
                "(1,'legacy-null-client','AAPL','sell','market',1,'canceled',"
                "'legacy-null-order',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'',"
                "'simple','{}',0,'accepted','',0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO fills "
                "(order_id,ticker,side,qty,price,broker_fill_id,filled_at) "
                "VALUES "
                "(1,'AAPL','sell',1,1,:broker_fill_id,CURRENT_TIMESTAMP)"
            ),
            {"broker_fill_id": broker_fill_id},
        )

    command.upgrade(cfg, "20260724_0005")

    with engine.connect() as conn:
        fill = conn.execute(
            text(
                "SELECT broker_fill_id,reconciliation_state "
                "FROM fills WHERE order_id=1"
            )
        ).mappings().one()
        order = conn.execute(
            text(
                "SELECT acceptance_state,last_error_code "
                "FROM orders WHERE id=1"
            )
        ).mappings().one()
        drift = conn.execute(
            text(
                "SELECT tripped,reason,actor,generation "
                "FROM circuit_breaker_state "
                "WHERE scope_key='broker_drift'"
            )
        ).mappings().one()

    assert fill == {
        "broker_fill_id": broker_fill_id,
        "reconciliation_state": "quarantined",
    }
    assert order == {
        "acceptance_state": "fill_reconcile_required",
        "last_error_code": "legacy_unidentified_fill",
    }
    assert drift["tripped"] == 1
    assert "legacy fill" in drift["reason"]
    assert drift["actor"] == "migration:0005"
    assert drift["generation"] == 1


def test_breaker_upgrade_resets_advanced_fill_cursor_for_full_recovery(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "legacy-advanced-fill-cursor.db",
        "20260724_0004",
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO orders "
                "(id,idempotency_key,ticker,side,order_type,qty,status,"
                "broker_order_id,created_at,updated_at,approval_reason,"
                "submission_kind,submission_payload_json,submission_attempt,"
                "submission_started_at,acceptance_state,last_error_code,"
                "version) VALUES "
                "(1,'cursor-replay-client','AAPL','sell','market',1,'canceled',"
                "'cursor-replay-order','2026-07-20 16:00:00',"
                "'2026-07-20 18:00:00','','simple','{}',0,"
                "'2026-07-20 16:59:00','accepted','',0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO fills "
                "(order_id,ticker,side,qty,price,broker_fill_id,filled_at) "
                "VALUES "
                "(1,'AAPL','sell',1,90,'cursor-replay-fill',"
                "'2026-07-20 17:00:00')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO reconciliation_cursors "
                "(broker,stream,last_activity_id,last_activity_at,version) "
                "VALUES "
                "('migration-replay','fills','cursor-ahead',"
                "'2026-07-25 17:00:00',9)"
            )
        )

    command.upgrade(cfg, "20260724_0005")

    with engine.connect() as conn:
        assert conn.scalar(
            text(
                "SELECT count(*) FROM reconciliation_cursors "
                "WHERE stream='fills'"
            )
        ) == 0

    exact_time = datetime(2026, 7, 20, 17, 0, tzinfo=timezone.utc)
    exact = BrokerFill(
        broker_fill_id="cursor-replay-fill",
        broker_order_id="cursor-replay-order",
        ticker="AAPL",
        side="sell",
        qty=Decimal("1"),
        price=Decimal("90"),
        filled_at=exact_time,
    )
    remote = OrderResult(
        "cursor-replay-client",
        "cursor-replay-order",
        OrderStatus.CANCELED,
        filled_qty=Decimal("1"),
        avg_fill_price=Decimal("90"),
    )

    class CursorAwareReplayBroker(MockBroker):
        reconciliation_key = "migration-replay"

        def __init__(self):
            super().__init__()
            self.after_calls = []
            self._orders_by_id["cursor-replay-order"] = remote
            self._orders_by_key["cursor-replay-client"] = remote

        def get_fill_activities(self, after=None):
            self.after_calls.append(after)
            return [exact] if after is None else []

    broker = CursorAwareReplayBroker()
    factory = make_session_factory(engine)
    reconciliation = ReconciliationService(
        factory,
        broker,
        OrderRepository(factory),
    )

    first = reconciliation.reconcile()
    replay = ReconciliationService(
        factory,
        broker,
        OrderRepository(factory),
    ).reconcile()

    assert first.inserted_fills == 0
    assert replay.inserted_fills == 0
    assert broker.after_calls == [None, exact_time]
    with factory() as session:
        order = session.get(Order, 1)
        recovered = session.scalar(
            select(Fill).where(
                Fill.broker_fill_id == "cursor-replay-fill"
            )
        )
        cursor = session.get(
            ReconciliationCursor,
            ("migration-replay", "fills"),
        )
        assert order is not None
        assert recovered is not None
        assert cursor is not None
        assert order.acceptance_state == "accepted"
        assert recovered.reconciliation_state == "trusted"
        assert recovered.filled_at == exact_time
        assert session.scalar(
            select(func.count()).select_from(Fill)
        ) == 1
        assert cursor.last_activity_id == "cursor-replay-fill"
        assert cursor.last_activity_at == exact_time


@pytest.mark.parametrize(
    ("case_name", "fill_rows"),
    [
        (
            "recovered",
            [
                {
                    "broker_fill_id": "recovered-exact",
                    "reconciliation_state": "trusted",
                },
                {
                    "broker_fill_id": "recovered-legacy",
                    "reconciliation_state": "superseded",
                },
            ],
        ),
        (
            "unresolved",
            [
                {
                    "broker_fill_id": None,
                    "reconciliation_state": "quarantined",
                }
            ],
        ),
    ],
)
def test_breaker_downgrade_refuses_nonempty_fill_trust_ledger(
    tmp_path,
    case_name,
    fill_rows,
):
    engine, cfg = _engine_at_revision(
        tmp_path / f"{case_name}-fill-ledger.db",
        "20260724_0005",
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO fills "
                "(order_id,ticker,side,qty,price,broker_fill_id,filled_at,"
                "reconciliation_state) VALUES "
                "(NULL,'AAPL','sell',1,90,:broker_fill_id,"
                "'2026-07-24 17:00:00',:reconciliation_state)"
            ),
            fill_rows,
        )

    with pytest.raises(RuntimeError, match="verified pre-upgrade backup"):
        command.downgrade(cfg, "20260724_0004")

    with engine.connect() as conn:
        assert conn.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260724_0005"
        assert conn.execute(
            text(
                "SELECT broker_fill_id,reconciliation_state "
                "FROM fills ORDER BY id"
            )
        ).mappings().all() == fill_rows
        tables = set(inspect(engine).get_table_names())
        assert "circuit_breaker_state" in tables
        assert "killswitch_state" not in tables


@pytest.mark.parametrize(
    "safety_state",
    [
        "account-risk",
        "fill-latch",
        "data-breaker",
        "drawdown-breaker",
        "liquidity-breaker",
        "broker-drift-breaker",
    ],
)
def test_breaker_downgrade_refuses_nonrepresentable_safety_state_before_ddl(
    tmp_path,
    safety_state,
):
    engine, cfg = _engine_at_revision(
        tmp_path / f"{safety_state}.db",
        "20260724_0005",
    )
    with engine.begin() as conn:
        if safety_state == "account-risk":
            conn.execute(
                text(
                    "INSERT INTO account_risk_state "
                    "(asset_class,high_water_mark,last_equity,updated_at) "
                    "VALUES ('equity',100000,95000,CURRENT_TIMESTAMP)"
                )
            )
        elif safety_state == "fill-latch":
            conn.execute(
                text(
                    "INSERT INTO orders "
                    "(id,idempotency_key,ticker,side,order_type,qty,status,"
                    "created_at,updated_at,approval_reason,submission_kind,"
                    "submission_payload_json,submission_attempt,"
                    "acceptance_state,last_error_code,version) VALUES "
                    "(1,'downgrade-latched-order','AAPL','buy','market',1,"
                    "'canceled',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'',"
                    "'simple','{}',0,'fill_reconcile_required',"
                    "'invalid_fill_activity',0)"
                )
            )
        else:
            kind, target = {
                "data-breaker": ("data", "equity"),
                "drawdown-breaker": ("drawdown", "equity"),
                "liquidity-breaker": ("liquidity", "AAPL"),
                "broker-drift-breaker": ("broker_drift", ""),
            }[safety_state]
            scope_key = f"{kind}:{target}" if target else kind
            conn.execute(
                text(
                    "INSERT INTO circuit_breaker_state "
                    "(scope_key,kind,target,tripped,reason,actor,generation,"
                    "updated_at) VALUES "
                    "(:scope_key,:kind,:target,1,'active safety state',"
                    "'test:review19',3,CURRENT_TIMESTAMP)"
                ),
                {
                    "scope_key": scope_key,
                    "kind": kind,
                    "target": target,
                },
            )

    with pytest.raises(RuntimeError, match="verified pre-upgrade backup"):
        command.downgrade(cfg, "20260724_0004")

    with engine.connect() as conn:
        assert conn.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260724_0005"
        assert conn.scalar(text("SELECT count(*) FROM fills")) == 0
        tables = set(inspect(engine).get_table_names())
        assert "account_risk_state" in tables
        assert "circuit_breaker_state" in tables
        assert "killswitch_state" not in tables
        assert "reconciliation_state" in {
            column["name"] for column in inspect(engine).get_columns("fills")
        }
        if safety_state == "account-risk":
            assert conn.execute(
                text(
                    "SELECT high_water_mark,last_equity "
                    "FROM account_risk_state WHERE asset_class='equity'"
                )
            ).one() == (100000, 95000)
        elif safety_state == "fill-latch":
            assert conn.execute(
                text(
                    "SELECT acceptance_state,last_error_code "
                    "FROM orders WHERE id=1"
                )
            ).one() == (
                "fill_reconcile_required",
                "invalid_fill_activity",
            )
        else:
            assert conn.execute(
                text(
                    "SELECT tripped,reason,actor,generation "
                    "FROM circuit_breaker_state"
                )
            ).one() == (
                1,
                "active safety state",
                "test:review19",
                3,
            )


def test_breaker_downgrade_allows_representable_empty_safety_state(tmp_path):
    engine, cfg = _engine_at_revision(
        tmp_path / "empty-fill-ledger.db",
        "20260724_0005",
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO circuit_breaker_state "
                "(scope_key,kind,target,tripped,reason,actor,generation,"
                "updated_at) VALUES "
                "('loss:equity','loss','equity',1,'loss limit','test',1,"
                "CURRENT_TIMESTAMP),"
                "('operator_global','operator_global','',1,'operator panic',"
                "'test',2,CURRENT_TIMESTAMP)"
            )
        )

    command.downgrade(cfg, "20260724_0004")

    with engine.connect() as conn:
        assert conn.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260724_0004"
        assert "reconciliation_state" not in {
            column["name"] for column in inspect(engine).get_columns("fills")
        }
        tables = set(inspect(engine).get_table_names())
        assert "killswitch_state" in tables
        assert "circuit_breaker_state" not in tables
        assert conn.execute(
            text(
                "SELECT asset_class,tripped,reason "
                "FROM killswitch_state ORDER BY asset_class"
            )
        ).all() == [
            ("equity", 1, "loss limit"),
            ("operator_global", 1, "operator panic"),
        ]
