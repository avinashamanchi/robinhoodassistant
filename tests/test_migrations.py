import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import (
    BrokerFill,
    OrderResult,
    OrderStatus,
)
from trading_assistant.db.models import (
    Fill,
    Order,
    ReconciliationCursor,
    Rule,
    RuleGroup,
)
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
    assert (
        "startup_reconciliation_state"
        in inspect(engine).get_table_names()
    )
    assert "auth_sessions" in inspect(engine).get_table_names()
    assert {
        "rate_windows",
        "concurrency_leases",
        "provider_budget_days",
        "provider_reservations",
        "panic_receipts",
        "mutation_interlocks",
    } <= set(inspect(engine).get_table_names())
    assert "alembic_version" in inspect(engine).get_table_names()
    rule_columns = {
        column["name"]
        for column in inspect(engine).get_columns("rules")
    }
    assert {"activation", "terminal_on_trigger"} <= rule_columns
    proposal_columns = {
        column["name"]
        for column in inspect(engine).get_columns("proposals")
    }
    assert {"source_rule_id", "plan_generation"} <= proposal_columns
    plan_columns = {
        column["name"]
        for column in inspect(engine).get_columns("trade_plans")
    }
    assert {
        "entry_filled_qty",
        "exit_filled_qty",
        "residual_generation",
    } <= plan_columns
    order_columns = {
        column["name"]
        for column in inspect(engine).get_columns("orders")
    }
    assert "plan_cancel_state" in order_columns
    order_indexes = {
        index["name"]: index
        for index in inspect(engine).get_indexes("orders")
    }
    assert (
        order_indexes["ix_orders_plan_cancel_state"]["unique"]
        == 0
    )


@pytest.mark.parametrize(
    ("counter_name", "statement"),
    [
        (
            "rate window hits",
            "INSERT INTO rate_windows "
            "(bucket_key,policy_name,window_started_at,expires_at,hits) VALUES "
            "('negative-rate-hits','chat',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,-1)",
        ),
        (
            "rate window version",
            "INSERT INTO rate_windows "
            "(bucket_key,policy_name,window_started_at,expires_at,hits,version) VALUES "
            "('negative-rate-version','chat',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,0,-1)",
        ),
        (
            "lease generation",
            "INSERT INTO concurrency_leases "
            "(resource_key,owner,expires_at,generation) VALUES "
            "('negative-lease-generation','',CURRENT_TIMESTAMP,-1)",
        ),
        (
            "provider calls",
            "INSERT INTO provider_budget_days "
            "(provider,budget_day,calls_used,input_tokens_used,output_tokens_used) VALUES "
            "('negative-calls','2026-07-27',-1,0,0)",
        ),
        (
            "provider input tokens",
            "INSERT INTO provider_budget_days "
            "(provider,budget_day,calls_used,input_tokens_used,output_tokens_used) VALUES "
            "('negative-input-tokens','2026-07-27',0,-1,0)",
        ),
        (
            "provider output tokens",
            "INSERT INTO provider_budget_days "
            "(provider,budget_day,calls_used,input_tokens_used,output_tokens_used) VALUES "
            "('negative-output-tokens','2026-07-27',0,0,-1)",
        ),
        (
            "reservation input",
            "INSERT INTO provider_reservations "
            "(reservation_id,provider,category,request_id,budget_day,state,"
            "input_reserved,output_reserved,input_actual,output_actual,created_at,expires_at) "
            "VALUES ('negative-reservation-input','gemini','chat','request',"
            "'2026-07-27','reserved',-1,0,NULL,NULL,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
        ),
        (
            "reservation output",
            "INSERT INTO provider_reservations "
            "(reservation_id,provider,category,request_id,budget_day,state,"
            "input_reserved,output_reserved,input_actual,output_actual,created_at,expires_at) "
            "VALUES ('negative-reservation-output','gemini','chat','request',"
            "'2026-07-27','reserved',0,-1,NULL,NULL,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
        ),
        (
            "reservation actual input",
            "INSERT INTO provider_reservations "
            "(reservation_id,provider,category,request_id,budget_day,state,"
            "input_reserved,output_reserved,input_actual,output_actual,created_at,expires_at) "
            "VALUES ('negative-reservation-actual-input','gemini','chat','request',"
            "'2026-07-27','reserved',0,0,-1,NULL,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
        ),
        (
            "reservation actual output",
            "INSERT INTO provider_reservations "
            "(reservation_id,provider,category,request_id,budget_day,state,"
            "input_reserved,output_reserved,input_actual,output_actual,created_at,expires_at) "
            "VALUES ('negative-reservation-actual-output','gemini','chat','request',"
            "'2026-07-27','reserved',0,0,NULL,-1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
        ),
    ],
)
def test_policy_budget_migration_rejects_negative_counters(
    tmp_path, counter_name, statement
):
    engine, _cfg = _engine_at_revision(
        tmp_path / f"negative-{counter_name}.db", "head"
    )

    with engine.connect() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(text(statement))


@pytest.mark.parametrize(
    "state", ["reserved", "started", "settled", "unknown", "released"]
)
def test_provider_reservation_migration_accepts_authoritative_states(
    tmp_path, state
):
    engine, _cfg = _engine_at_revision(
        tmp_path / f"accepted-reservation-{state}.db", "head"
    )

    reservation_id = f"accepted-reservation-{state}"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO provider_reservations "
                "(reservation_id,provider,category,request_id,budget_day,state,"
                "input_reserved,output_reserved,created_at,expires_at) VALUES "
                "(:reservation_id,'gemini','chat','request','2026-07-27',:state,"
                "0,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ),
            {"reservation_id": reservation_id, "state": state},
        )

    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT state FROM provider_reservations "
                "WHERE reservation_id = :reservation_id"
            ),
            {"reservation_id": reservation_id},
        ) == state


@pytest.mark.parametrize("state", ["canceled", "expired"])
def test_provider_reservation_migration_rejects_outside_states(tmp_path, state):
    engine, _cfg = _engine_at_revision(
        tmp_path / f"rejected-reservation-{state}.db", "head"
    )

    with engine.connect() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO provider_reservations "
                    "(reservation_id,provider,category,request_id,budget_day,state,"
                    "input_reserved,output_reserved,created_at,expires_at) VALUES "
                    "(:reservation_id,'gemini','chat','request','2026-07-27',:state,"
                    "0,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                ),
                {"reservation_id": f"rejected-reservation-{state}", "state": state},
            )


@pytest.mark.parametrize("state", ["started", "completed", "failed"])
def test_panic_receipt_migration_accepts_authoritative_states(tmp_path, state):
    engine, _cfg = _engine_at_revision(
        tmp_path / f"accepted-panic-{state}.db", "head"
    )

    account_scope = f"accepted-panic-{state}"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO panic_receipts "
                "(account_scope,request_id,state,started_at,expires_at) VALUES "
                "(:account_scope,'request',:state,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ),
            {"account_scope": account_scope, "state": state},
        )

    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT state FROM panic_receipts "
                "WHERE account_scope = :account_scope"
            ),
            {"account_scope": account_scope},
        ) == state


@pytest.mark.parametrize("state", ["reserved", "unknown"])
def test_panic_receipt_migration_rejects_outside_states(tmp_path, state):
    engine, _cfg = _engine_at_revision(
        tmp_path / f"rejected-panic-{state}.db", "head"
    )

    with engine.connect() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO panic_receipts "
                    "(account_scope,request_id,state,started_at,expires_at) VALUES "
                    "(:account_scope,'request',:state,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                ),
                {"account_scope": f"rejected-panic-{state}", "state": state},
            )


@pytest.mark.parametrize(
    (
        "durable_state",
        "statement",
        "preserved_query",
        "expected_row",
        "expected_revision",
    ),
    [
        (
            "started provider reservation",
            "INSERT INTO provider_reservations "
            "(reservation_id,provider,category,request_id,budget_day,state,"
            "input_reserved,output_reserved,created_at,expires_at) VALUES "
            "('started-reservation','gemini','chat','request','2026-07-27',"
            "'started',1,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            "SELECT reservation_id, state FROM provider_reservations "
            "WHERE reservation_id = 'started-reservation'",
            ("started-reservation", "started"),
            "20260727_0011",
        ),
        (
            "unknown provider reservation",
            "INSERT INTO provider_reservations "
            "(reservation_id,provider,category,request_id,budget_day,state,"
            "input_reserved,output_reserved,created_at,expires_at) VALUES "
            "('unknown-reservation','gemini','chat','request','2026-07-27',"
            "'unknown',1,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            "SELECT reservation_id, state FROM provider_reservations "
            "WHERE reservation_id = 'unknown-reservation'",
            ("unknown-reservation", "unknown"),
            "20260727_0011",
        ),
        (
            "started panic receipt",
            "INSERT INTO panic_receipts "
            "(account_scope,request_id,state,started_at,expires_at) VALUES "
            "('started-panic','request','started',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            "SELECT account_scope, state FROM panic_receipts "
            "WHERE account_scope = 'started-panic'",
            ("started-panic", "started"),
            "20260727_0012",
        ),
    ],
)
def test_policy_budget_downgrade_refuses_durable_inflight_state(
    tmp_path,
    durable_state,
    statement,
    preserved_query,
    expected_row,
    expected_revision,
):
    engine, cfg = _engine_at_revision(
        tmp_path / f"{durable_state}.db", "head"
    )
    with engine.begin() as connection:
        connection.execute(text(statement))

    with pytest.raises(
        RuntimeError,
        match="inflight policy state|generation-bound panic receipt",
    ):
        command.downgrade(cfg, "20260726_0010")

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == expected_revision
        assert {
            "rate_windows",
            "concurrency_leases",
            "provider_budget_days",
            "provider_reservations",
            "panic_receipts",
        } <= set(inspect(engine).get_table_names())
        assert connection.execute(text(preserved_query)).one() == expected_row


def test_mutation_interlock_upgrade_and_downgrade_preserve_latched_state(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "mutation-interlock.db",
        "20260727_0011",
    )
    assert "mutation_interlocks" not in inspect(engine).get_table_names()

    command.upgrade(cfg, "head")

    assert "mutation_interlocks" in inspect(engine).get_table_names()
    assert "lease_generation" in {
        column["name"]
        for column in inspect(engine).get_columns("panic_receipts")
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mutation_interlocks "
                    "(resource_key,owner,generation,operation,state,outcome_code,"
                    "created_at,updated_at) VALUES "
                    "(:resource_key,'internal-owner',7,"
                    "'order_approve','uncertain','lease_renewal_unproven',"
                    "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ),
            {"resource_key": "route:" + "a" * 64 + ":0"},
        )

    with pytest.raises(
        RuntimeError,
        match="durable mutation interlock",
    ):
        command.downgrade(cfg, "20260727_0011")

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260727_0012"
        assert connection.scalar(
            text("SELECT count(*) FROM mutation_interlocks")
        ) == 1

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM mutation_interlocks"))
    command.downgrade(cfg, "20260727_0011")

    assert "mutation_interlocks" not in inspect(engine).get_table_names()
    assert "lease_generation" not in {
        column["name"]
        for column in inspect(engine).get_columns("panic_receipts")
    }
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260727_0011"


@pytest.mark.parametrize(
    (
        "operation",
        "generation",
        "state",
        "outcome_code",
        "worker_finished_at",
    ),
    [
        (
            "order_approve",
            1,
            "active",
            "handler_completed",
            None,
        ),
        (
            "order_approve",
            1,
            "active",
            "",
            datetime(2026, 7, 27, tzinfo=timezone.utc),
        ),
        ("order_approve", 1, "settled", "", None),
        ("order_approve", 1, "uncertain", "", None),
        ("order_approve", 1, "invalid", "", None),
        ("user supplied operation", 1, "active", "", None),
        ("order_approve", -1, "active", "", None),
    ],
)
def test_mutation_interlock_constraints_reject_invalid_combinations(
    tmp_path,
    operation,
    generation,
    state,
    outcome_code,
    worker_finished_at,
):
    engine, _cfg = _engine_at_revision(
        tmp_path
        / (
            "invalid-interlock-"
            f"{abs(hash((operation, generation, state, outcome_code)))}.db"
        ),
        "head",
    )

    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO mutation_interlocks "
                    "(resource_key,owner,generation,operation,state,"
                    "outcome_code,worker_finished_at,created_at,updated_at) "
                    "VALUES (:resource_key,'internal-owner',:generation,"
                    ":operation,:state,:outcome_code,:worker_finished_at,"
                    "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                ),
                {
                    "resource_key": "route:" + "b" * 64 + ":0",
                    "generation": generation,
                    "operation": operation,
                    "state": state,
                    "outcome_code": outcome_code,
                    "worker_finished_at": worker_finished_at,
                },
            )


def test_fill_activated_upgrade_refuses_active_legacy_plan(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "active-legacy-plan.db",
        "20260724_0008",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO trade_plans "
                "(id,symbol,action,status,paper_only,shadow,plan_json,"
                "sized_json,created_at) VALUES "
                "(1,'AAPL','buy','approved',1,0,'{}','{}',"
                "CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO rule_groups "
                "(id,group_key,state,version,reconciliation_required,"
                "created_at,updated_at) VALUES "
                "(1,'legacy-plan-1','active',0,0,"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO rules "
                "(id,group_id,payload_version,ticker,condition_json,"
                "action_json,state,created_at,plan_id,kind,"
                "pre_approved) VALUES "
                "(1,1,1,'AAPL','{\"type\":\"price\","
                "\"direction\":\"below\",\"price\":\"99\"}',"
                "'{\"side\":\"buy\",\"order_type\":\"market\","
                "\"qty\":\"1\"}','active',CURRENT_TIMESTAMP,1,"
                "'entry',0)"
            )
        )

    with pytest.raises(
        RuntimeError,
        match="active legacy plans",
    ):
        command.upgrade(cfg, "head")

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260724_0008"
    assert "activation" not in {
        column["name"]
        for column in inspect(engine).get_columns("rules")
    }


def test_fill_activated_upgrade_refuses_terminalized_legacy_plan_order(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "terminalized-legacy-plan-order.db",
        "20260724_0008",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO trade_plans "
                "(id,symbol,action,status,paper_only,shadow,plan_json,"
                "sized_json,created_at) VALUES "
                "(1,'AAPL','buy','approved',1,0,'{}','{}',"
                "CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO rule_groups "
                "(id,group_key,state,version,reconciliation_required,"
                "created_at,updated_at) VALUES "
                "(1,'legacy-terminalized-plan','triggered',0,0,"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO rules "
                "(id,group_id,payload_version,ticker,condition_json,"
                "action_json,state,created_at,plan_id,kind,"
                "pre_approved) VALUES "
                "(1,1,1,'AAPL','{\"type\":\"price\","
                "\"direction\":\"below\",\"price\":\"99\"}',"
                "'{\"side\":\"buy\",\"order_type\":\"market\","
                "\"qty\":\"1\"}','triggered',CURRENT_TIMESTAMP,1,"
                "'entry',0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO orders "
                "(id,idempotency_key,ticker,side,order_type,qty,status,"
                "broker_order_id,created_at,updated_at,approval_reason,"
                "submission_kind,submission_payload_json,"
                "submission_attempt,acceptance_state,last_error_code,"
                "version) VALUES "
                "(1,'legacy-terminalized-order','AAPL','buy','market',"
                "1,'submitted','legacy-broker-order',"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'','simple','{}',"
                "1,'submitted','',0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO proposals "
                "(order_id,source_rule_group_id,reasoning,ttl_minutes,"
                "created_at,expires_at) VALUES "
                "(1,1,'legacy plan proposal',15,CURRENT_TIMESTAMP,"
                "'2026-08-01 12:00:00')"
            )
        )

    with pytest.raises(
        RuntimeError,
        match="legacy plans",
    ):
        command.upgrade(cfg, "head")

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260724_0008"


def test_fill_activated_upgrade_refuses_canceled_plan_with_legacy_fill(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "canceled-legacy-plan-fill.db",
        "20260724_0008",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO trade_plans "
                "(id,symbol,action,status,paper_only,shadow,plan_json,"
                "sized_json,created_at) VALUES "
                "(1,'AAPL','buy','canceled',1,0,'{}','{}',"
                "CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO rule_groups "
                "(id,group_key,state,version,reconciliation_required,"
                "created_at,updated_at) VALUES "
                "(1,'legacy-canceled-plan','canceled',0,0,"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO rules "
                "(id,group_id,payload_version,ticker,condition_json,"
                "action_json,state,created_at,plan_id,kind,"
                "pre_approved) VALUES "
                "(1,1,1,'AAPL','{\"type\":\"price\","
                "\"direction\":\"below\",\"price\":\"99\"}',"
                "'{\"side\":\"buy\",\"order_type\":\"market\","
                "\"qty\":\"1\"}','canceled',CURRENT_TIMESTAMP,1,"
                "'entry',0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO orders "
                "(id,idempotency_key,ticker,side,order_type,qty,status,"
                "broker_order_id,created_at,updated_at,approval_reason,"
                "submission_kind,submission_payload_json,"
                "submission_attempt,acceptance_state,last_error_code,"
                "version) VALUES "
                "(1,'legacy-canceled-filled-entry','AAPL','buy',"
                "'market',1,'filled','legacy-canceled-broker-order',"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'','simple','{}',"
                "1,'accepted','',0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO proposals "
                "(order_id,source_rule_group_id,reasoning,ttl_minutes,"
                "created_at,expires_at) VALUES "
                "(1,1,'legacy canceled plan fill',15,"
                "CURRENT_TIMESTAMP,'2026-08-01 12:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO fills "
                "(order_id,ticker,side,qty,price,broker_fill_id,"
                "filled_at) VALUES "
                "(1,'AAPL','buy',1,98,'legacy-canceled-fill',"
                "CURRENT_TIMESTAMP)"
            )
        )

    with pytest.raises(RuntimeError, match="legacy plans"):
        command.upgrade(cfg, "head")

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260724_0008"


def test_fill_activated_rule_downgrade_refuses_to_remove_protection(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "fill-activated-rules.db",
        "head",
    )
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        group = RuleGroup(
            group_key="pending-protective-exits",
            state="pending",
        )
        session.add(group)
        session.flush()
        session.add(
            Rule(
                group_id=group.id,
                ticker="AAPL",
                condition_json=json.dumps(
                    {
                        "type": "price",
                        "direction": "below",
                        "price": "90",
                    }
                ),
                action_json=json.dumps(
                    {
                        "side": "sell",
                        "order_type": "market",
                        "qty": "1",
                    }
                ),
                state="pending",
                kind="stop",
                activation="on_entry_fill",
                terminal_on_trigger=True,
            )
        )
        session.commit()

    with pytest.raises(
        RuntimeError,
        match="fill-activated plan protection",
    ):
        command.downgrade(cfg, "20260724_0008")

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260724_0009"
    assert {
        "activation",
        "terminal_on_trigger",
    } <= {
        column["name"]
        for column in inspect(engine).get_columns("rules")
    }


def test_plan_cancel_intent_downgrade_refuses_to_erase_retry_state(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "plan-cancel-intent.db",
        "head",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO orders "
                "(idempotency_key,ticker,side,order_type,status,"
                "plan_cancel_state,created_at,updated_at,"
                "approval_reason,submission_kind,"
                "submission_payload_json,submission_attempt,"
                "acceptance_state,last_error_code,version) VALUES "
                "('durable-plan-cancel','AAPL','buy','market',"
                "'submitted','requested',CURRENT_TIMESTAMP,"
                "CURRENT_TIMESTAMP,'','simple','{}',1,'submitted',"
                "'plan_cancel',0)"
            )
        )

    with pytest.raises(
        RuntimeError,
        match="durable plan cancellation intent",
    ):
        command.downgrade(cfg, "20260724_0009")

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260726_0010"
        assert connection.scalar(
            text(
                "SELECT plan_cancel_state FROM orders "
                "WHERE idempotency_key='durable-plan-cancel'"
            )
        ) == "requested"


def test_plan_cancel_intent_upgrade_backfills_only_plan_linked_markers(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "plan-cancel-backfill.db",
        "20260724_0009",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO trade_plans "
                "(id,symbol,action,status,paper_only,shadow,plan_json,"
                "sized_json,created_at) VALUES "
                "(1,'AAPL','buy','approved',1,0,'{}','{}',"
                "CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO rule_groups "
                "(id,group_key,state,version,reconciliation_required,"
                "created_at,updated_at) VALUES "
                "(1,'cancel-backfill','active',0,1,"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO rules "
                "(id,group_id,payload_version,ticker,condition_json,"
                "action_json,state,created_at,plan_id,kind,"
                "pre_approved,activation,terminal_on_trigger) VALUES "
                "(1,1,1,'AAPL','{\"type\":\"price\","
                "\"direction\":\"below\",\"price\":\"99\"}',"
                "'{\"side\":\"buy\",\"order_type\":\"market\","
                "\"qty\":\"1\"}','processing',CURRENT_TIMESTAMP,1,"
                "'entry',0,'immediate',1)"
            )
        )
        for order_id, marker in enumerate(
            (
                "plan_cancel",
                "plan_exit_entry_cancel",
                "indeterminate_cancel",
            ),
            start=1,
        ):
            connection.execute(
                text(
                    "INSERT INTO orders "
                    "(id,idempotency_key,ticker,side,order_type,qty,"
                    "status,broker_order_id,created_at,updated_at,"
                    "approval_reason,submission_kind,"
                    "submission_payload_json,submission_attempt,"
                    "acceptance_state,last_error_code,version) VALUES "
                    "(:id,:key,'AAPL','buy','market',1,'submitted',"
                    ":broker_id,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'',"
                    "'simple','{}',1,'submitted',:marker,0)"
                ),
                {
                    "id": order_id,
                    "key": f"plan-cancel-backfill-{order_id}",
                    "broker_id": f"plan-cancel-broker-{order_id}",
                    "marker": marker,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO proposals "
                    "(order_id,source_rule_group_id,source_rule_id,"
                    "reasoning,ttl_minutes,plan_generation,created_at,"
                    "expires_at) VALUES "
                    "(:id,1,1,'legacy cancellation intent',15,0,"
                    "CURRENT_TIMESTAMP,'2026-08-01 12:00:00')"
                ),
                {"id": order_id},
            )
        connection.execute(
            text(
                "INSERT INTO orders "
                "(id,idempotency_key,ticker,side,order_type,qty,status,"
                "broker_order_id,created_at,updated_at,approval_reason,"
                "submission_kind,submission_payload_json,"
                "submission_attempt,acceptance_state,last_error_code,"
                "version) VALUES "
                "(4,'generic-indeterminate','AAPL','buy','market',1,"
                "'submitted','generic-broker',CURRENT_TIMESTAMP,"
                "CURRENT_TIMESTAMP,'','simple','{}',1,'submitted',"
                "'indeterminate_cancel',0)"
            )
        )

    command.upgrade(cfg, "head")

    with engine.connect() as connection:
        states = connection.execute(
            text(
                "SELECT id, plan_cancel_state FROM orders "
                "ORDER BY id"
            )
        ).all()
        version = connection.scalar(
            text("SELECT version_num FROM alembic_version")
        )
    assert states == [
        (1, "requested"),
        (2, "requested"),
        (3, "indeterminate"),
        (4, "none"),
    ]
    assert version == "20260727_0012"


def test_auth_session_upgrade_from_0005_adds_only_hashed_session_storage(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "auth-sessions.db",
        "20260724_0005",
    )
    assert "auth_sessions" not in inspect(engine).get_table_names()

    command.upgrade(cfg, "head")

    inspector = inspect(engine)
    assert {
        column["name"] for column in inspector.get_columns("auth_sessions")
    } == {
        "id",
        "token_hash",
        "csrf_hash",
        "actor",
        "created_at",
        "expires_at",
        "authenticated_at",
        "revoked_at",
    }
    indexes = {
        index["name"]: index
        for index in inspector.get_indexes("auth_sessions")
    }
    assert indexes["ix_auth_sessions_token_hash"]["unique"] == 1
    assert indexes["ix_auth_sessions_expires_at"]["unique"] == 0
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260727_0012"

    command.downgrade(cfg, "20260724_0005")
    assert "auth_sessions" not in inspect(engine).get_table_names()
    assert "circuit_breaker_state" in inspect(engine).get_table_names()


def test_runtime_health_upgrade_deduplicates_heartbeats_by_time_then_id(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "runtime-health.db",
        "20260724_0006",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO heartbeats (id, source, at) VALUES "
                "(1, 'daemon', '2026-07-24 10:00:00'),"
                "(2, 'daemon', '2026-07-24 11:00:00'),"
                "(3, 'daemon', '2026-07-24 11:00:00'),"
                "(4, 'app', '2026-07-24 09:00:00')"
            )
        )

    command.upgrade(cfg, "head")

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, source FROM heartbeats "
                "ORDER BY source"
            )
        ).mappings().all()
        version = connection.scalar(
            text("SELECT version_num FROM alembic_version")
        )
    assert rows == [
        {"id": 4, "source": "app"},
        {"id": 3, "source": "daemon"},
    ]
    assert version == "20260727_0012"
    heartbeat_indexes = {
        index["name"]: index
        for index in inspect(engine).get_indexes("heartbeats")
    }
    assert heartbeat_indexes["uq_heartbeats_source"]["unique"] == 1


def test_startup_reconciliation_downgrade_refuses_to_drop_durable_state(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "startup-reconciliation.db",
        "head",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO startup_reconciliation_state "
                "(broker,generation,completed_generation,status,actor,"
                "reason,request_id,evidence_json,started_at,completed_at,"
                "updated_at) VALUES "
                "('alpaca',2,1,'required','runtime:test','restart',"
                "'startup-test','{}',CURRENT_TIMESTAMP,NULL,"
                "CURRENT_TIMESTAMP)"
            )
        )

    with pytest.raises(
        RuntimeError,
        match="durable startup reconciliation state",
    ):
        command.downgrade(cfg, "20260724_0007")

    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT generation FROM startup_reconciliation_state "
                "WHERE broker='alpaca'"
            )
        ) == 2
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260724_0008"


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


def test_adoption_backup_does_not_follow_predictable_target_symlink(
    tmp_path,
    monkeypatch,
):
    from trading_assistant.db import migrate

    path = tmp_path / "legacy.db"
    engine = _legacy_engine(path)
    victim = tmp_path / "must-not-be-overwritten.txt"
    victim.write_text("sentinel", encoding="utf-8")
    frozen = datetime(2026, 7, 26, 12, 34, 56, tzinfo=timezone.utc)

    class FrozenDateTime:
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is not None else frozen.replace(tzinfo=None)

    monkeypatch.setattr(migrate, "datetime", FrozenDateTime)
    predictable = path.with_name(
        f"{path.name}.20260726T123456000000Z.pre-migration.bak"
    )
    predictable.symlink_to(victim)

    backup = adopt_existing(engine)

    assert backup != predictable
    assert backup.exists()
    assert not backup.is_symlink()
    assert victim.read_text(encoding="utf-8") == "sentinel"
    assert predictable.is_symlink()
    assert not list(tmp_path.glob(f".{path.name}.migration-backup-*"))


def test_adoption_backup_publication_never_replaces_existing_name(
    tmp_path,
    monkeypatch,
):
    from trading_assistant.db import migrate

    path = tmp_path / "legacy.db"
    engine = _legacy_engine(path)
    frozen = datetime(2026, 7, 26, 12, 34, 56, tzinfo=timezone.utc)

    class FrozenDateTime:
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is not None else frozen.replace(tzinfo=None)

    target_tokens = iter(("collision", "fresh"))

    def token_hex(size):
        if size == 16:
            return "private-staging"
        return next(target_tokens)

    monkeypatch.setattr(migrate, "datetime", FrozenDateTime)
    monkeypatch.setattr(migrate.secrets, "token_hex", token_hex)
    collision = path.with_name(
        f"{path.name}.20260726T123456000000Z.collision."
        "pre-migration.bak"
    )
    collision.write_text("sentinel", encoding="utf-8")

    backup = adopt_existing(engine)

    assert backup.name.endswith(".fresh.pre-migration.bak")
    assert collision.read_text(encoding="utf-8") == "sentinel"
    assert not list(tmp_path.glob(f".{path.name}.migration-backup-*"))


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
    # Runtime code is only supported at the current schema. Preserve the
    # point-in-time 0005 assertion above, then finish the upgrade before using
    # current ORM models and reconciliation services.
    command.upgrade(cfg, "head")

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

    first = reconciliation.reconcile(
        actor="test:migration",
        reason="migration reconciliation",
        request_id="migration-reconciliation",
    )
    replay = ReconciliationService(
        factory,
        broker,
        OrderRepository(factory),
    ).reconcile(
        actor="test:migration",
        reason="migration reconciliation replay",
        request_id="migration-reconciliation-replay",
    )

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
