import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from trading_assistant.broker.alpaca import AlpacaBroker
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import (
    Account,
    BrokerFill,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderTimeInForce,
    OrderType,
    Position,
    Quote,
)
from trading_assistant.config import BrokerKind, TradingMode
from trading_assistant.db.models import Order
from trading_assistant.db.session import create_db_engine, make_session_factory
from trading_assistant.ops.safety_drill import (
    SafetyDrillError,
    main,
    run_safety_drill,
)
from trading_assistant.risk.clock import FakeClock


def _upgrade_database(path) -> None:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "head")


def _safe_config(app_config):
    return app_config.model_copy(
        update={
            "trading": app_config.trading.model_copy(
                update={"broker": BrokerKind.ALPACA}
            ),
        }
    )


def _primary_manifest(path: Path) -> tuple[bytes, tuple, tuple]:
    content = path.read_bytes()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        schema = tuple(
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "ORDER BY type,name"
            )
        )
        state = tuple(connection.execute("SELECT version_num FROM alembic_version"))
    return content, schema, state


def test_safety_drill_requires_every_gate(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.setenv("APP_API_TOKEN", "task-10-test-operator-secret")
    broker = MockBroker(prices={"AAPL": Decimal("100")})

    report = run_safety_drill(
        database_copy=tmp_path / "operator-copy.db",
        config=_safe_config(app_config),
        broker=broker,
    )

    assert report.schema_current
    assert report.auth_fail_closed
    assert report.crash_recovered_without_duplicate
    assert report.oco_single_terminal
    assert report.breakers_persisted
    assert report.reconciliation_clean
    assert report.safe


def test_mock_drill_leaves_primary_bytes_schema_and_state_unchanged(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    engine = create_db_engine(f"sqlite:///{primary}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO heartbeats (source, at) "
                "VALUES ('primary-sentinel', CURRENT_TIMESTAMP)"
            )
        )
    engine.dispose()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.setenv("APP_API_TOKEN", "task-10-test-operator-secret")
    before = _primary_manifest(primary)

    report = run_safety_drill(
        database_copy=tmp_path / "copy.db",
        config=_safe_config(app_config),
        broker=MockBroker(prices={"AAPL": Decimal("100")}),
    )

    assert report.safe
    assert _primary_manifest(primary) == before
    assert oct((tmp_path / "copy.db").stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize(
    "unsafe_update",
    [
        {"trading": {"mode": TradingMode.LIVE}},
        {"trading": {"broker": BrokerKind.MOCK}},
        {"features": {"auto_execute_preapproved_rules": True}},
        {"execution": {"prefer_bracket_orders": True}},
        {"llm": {"fallback_provider": "groq"}},
    ],
)
def test_unsafe_config_is_refused_before_copy_or_broker_mutation(
    tmp_path,
    app_config,
    monkeypatch,
    unsafe_update,
):
    primary = tmp_path / "primary.db"
    destination = tmp_path / "must-not-exist.db"
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.setenv("APP_API_TOKEN", "task-10-test-operator-secret")
    config = _safe_config(app_config)
    field, values = next(iter(unsafe_update.items()))
    config = config.model_copy(
        update={
            field: getattr(config, field).model_copy(update=values),
        }
    )
    before = _primary_manifest(primary)

    with pytest.raises(SafetyDrillError) as caught:
        run_safety_drill(
            database_copy=destination,
            config=config,
            broker=MockBroker(prices={"AAPL": Decimal("100")}),
        )

    assert caught.value.code == "unsafe_configuration"
    assert not destination.exists()
    assert _primary_manifest(primary) == before


def test_refuses_relative_destination_without_creating_it(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.chdir(tmp_path)
    before = _primary_manifest(primary)

    with pytest.raises(SafetyDrillError) as caught:
        run_safety_drill(
            database_copy=Path("relative.db"),
            config=_safe_config(app_config),
            broker=MockBroker(),
        )

    assert caught.value.code == "unsafe_database_copy"
    assert not (tmp_path / "relative.db").exists()
    assert _primary_manifest(primary) == before


def test_refuses_primary_aliases_and_existing_destination_without_changes(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    symlink = tmp_path / "primary-symlink.db"
    hardlink = tmp_path / "primary-hardlink.db"
    symlink.symlink_to(primary)
    os.link(primary, hardlink)
    existing = tmp_path / "existing.db"
    existing.write_bytes(b"operator evidence")
    before = _primary_manifest(primary)

    for destination in (primary, symlink, hardlink, existing):
        prior = destination.read_bytes()
        with pytest.raises(SafetyDrillError) as caught:
            run_safety_drill(
                database_copy=destination,
                config=_safe_config(app_config),
                broker=MockBroker(),
            )
        assert caught.value.code == "unsafe_database_copy"
        assert destination.read_bytes() == prior

    assert _primary_manifest(primary) == before


def test_refuses_destination_beneath_symlinked_parent(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    real_parent = tmp_path / "real-parent"
    linked_parent = tmp_path / "linked-parent"
    real_parent.mkdir()
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")

    with pytest.raises(SafetyDrillError) as caught:
        run_safety_drill(
            database_copy=linked_parent / "copy.db",
            config=_safe_config(app_config),
            broker=MockBroker(),
        )

    assert caught.value.code == "unsafe_database_copy"
    assert not (real_parent / "copy.db").exists()


def test_copy_publish_refuses_a_racing_overwrite(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    destination = tmp_path / "racing-evidence.db"
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    before = _primary_manifest(primary)
    original_link = os.link

    def race_destination(source, target):
        destination.write_bytes(b"operator evidence created during copy")
        return original_link(source, target)

    monkeypatch.setattr(os, "link", race_destination)

    with pytest.raises(SafetyDrillError) as caught:
        run_safety_drill(
            database_copy=destination,
            config=_safe_config(app_config),
            broker=MockBroker(),
        )

    assert caught.value.code == "unsafe_database_copy"
    assert destination.read_bytes() == b"operator evidence created during copy"
    assert _primary_manifest(primary) == before


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite:///:memory:",
        "postgresql://localhost/trading",
    ],
)
def test_refuses_non_file_sqlite_primary_before_destination_creation(
    tmp_path,
    app_config,
    monkeypatch,
    database_url,
):
    destination = tmp_path / "must-not-exist" / "copy.db"
    monkeypatch.setenv("DATABASE_URL", database_url)

    with pytest.raises(SafetyDrillError) as caught:
        run_safety_drill(
            database_copy=destination,
            config=_safe_config(app_config),
            broker=MockBroker(),
        )

    assert caught.value.code == "unsafe_primary_database"
    assert not destination.parent.exists()


def test_invalid_sqlite_primary_does_not_publish_a_copy(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "not-sqlite.db"
    destination = tmp_path / "copy.db"
    primary.write_text("not a sqlite database", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    before = primary.read_bytes()

    with pytest.raises(SafetyDrillError) as caught:
        run_safety_drill(
            database_copy=destination,
            config=_safe_config(app_config),
            broker=MockBroker(),
        )

    assert caught.value.code == "invalid_primary_database"
    assert not destination.exists()
    assert primary.read_bytes() == before


def test_gate_failure_is_sanitized_and_never_claimed_safe(
    tmp_path,
    app_config,
    monkeypatch,
):
    secret = "provider-secret-must-not-escape"

    class UnavailableBroker(MockBroker):
        def get_quote(self, ticker):
            raise RuntimeError(secret)

    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.setenv("APP_API_TOKEN", "task-10-test-operator-secret")

    report = run_safety_drill(
        database_copy=tmp_path / "evidence.db",
        config=_safe_config(app_config),
        broker=UnavailableBroker(),
    )

    payload = json.dumps(report.as_dict(), sort_keys=True)
    assert report.safe is False
    assert report.crash_recovered_without_duplicate is False
    assert "crash:dependency_failed" in report.details
    assert secret not in payload


def test_mock_cli_emits_machine_readable_safe_json(
    tmp_path,
    monkeypatch,
    capsys,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.setenv("APP_API_TOKEN", "task-10-test-operator-secret")

    exit_code = main(
        [
            "--database-copy",
            str(tmp_path / "cli-copy.db"),
            "--mock",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["safe"] is True
    assert payload["details"][0] == "mode:mock"


class PaperStateBroker(AlpacaBroker):
    """Offline Alpaca-shaped broker with observable paper-account manifests."""

    reconciliation_key = "alpaca"

    def __init__(self, *, fill_initial: bool = False) -> None:
        self.fill_initial = fill_initial
        self.submit_requests: list[OrderRequest] = []
        self.cancel_ids: list[str] = []
        self._positions: dict[str, Position] = {}
        self._fills: list[BrokerFill] = []
        self._orders_by_id: dict[str, OrderResult] = {
            "paper-preexisting": OrderResult(
                idempotency_key="paper-preexisting-client",
                broker_order_id="paper-preexisting",
                status=OrderStatus.SUBMITTED,
                ticker="AAPL",
            )
        }
        self._orders_by_key = {
            order.idempotency_key: order
            for order in self._orders_by_id.values()
        }

    def get_quote(self, ticker: str) -> Quote:
        now = datetime.now(timezone.utc)
        return Quote(
            ticker=ticker.upper(),
            bid=Decimal("99.95"),
            ask=Decimal("100.05"),
            last=Decimal("100"),
            prev_close=Decimal("100"),
            as_of=now,
            book_as_of=now,
            trade_as_of=now,
        )

    def get_account(self) -> Account:
        return Account(
            buying_power=Decimal("100000"),
            equity=Decimal("100000"),
            cash=Decimal("100000"),
        )

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_fill_activities(self, after=None) -> list[BrokerFill]:
        return list(self._fills)

    def submit_order(self, order: OrderRequest) -> OrderResult:
        existing = self._orders_by_key.get(order.idempotency_key)
        if existing is not None:
            return existing
        self.submit_requests.append(order)
        broker_id = f"paper-drill-{len(self.submit_requests)}"
        filled = self.fill_initial or len(self.submit_requests) > 1
        status = OrderStatus.FILLED if filled else OrderStatus.SUBMITTED
        filled_qty = order.qty if filled else Decimal("0")
        result = OrderResult(
            idempotency_key=order.idempotency_key,
            broker_order_id=broker_id,
            status=status,
            filled_qty=filled_qty or Decimal("0"),
            avg_fill_price=Decimal("100") if filled else None,
            ticker=order.ticker,
        )
        self._orders_by_id[broker_id] = result
        self._orders_by_key[order.idempotency_key] = result
        if filled:
            assert order.qty is not None
            signed = (
                order.qty
                if order.side is OrderSide.BUY
                else -order.qty
            )
            prior = self._positions.get(order.ticker)
            new_qty = (prior.qty if prior is not None else Decimal("0")) + signed
            if new_qty:
                self._positions[order.ticker] = Position(
                    order.ticker,
                    new_qty,
                    Decimal("100"),
                    Decimal("100"),
                    Decimal("0"),
                )
            else:
                self._positions.pop(order.ticker, None)
            self._fills.append(
                BrokerFill(
                    broker_fill_id=f"paper-fill-{len(self._fills) + 1}",
                    broker_order_id=broker_id,
                    ticker=order.ticker,
                    side=order.side.value,
                    qty=order.qty,
                    price=Decimal("100"),
                    filled_at=datetime.now(timezone.utc),
                )
            )
        return result

    def get_order_by_client_id(self, client_order_id: str):
        return self._orders_by_key.get(client_order_id)

    def get_open_orders(self) -> list[OrderResult]:
        return [
            order
            for order in self._orders_by_id.values()
            if order.status
            in {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}
        ]

    def get_order_status(self, order_id: str) -> OrderResult:
        return self._orders_by_id[order_id]

    def cancel_order(self, order_id: str) -> OrderResult:
        assert order_id != "paper-preexisting"
        self.cancel_ids.append(order_id)
        prior = self._orders_by_id[order_id]
        canceled = OrderResult(
            idempotency_key=prior.idempotency_key,
            broker_order_id=prior.broker_order_id,
            status=OrderStatus.CANCELED,
            filled_qty=prior.filled_qty,
            avg_fill_price=prior.avg_fill_price,
            ticker=prior.ticker,
        )
        self._orders_by_id[order_id] = canceled
        self._orders_by_key[prior.idempotency_key] = canceled
        return canceled


def _seed_preexisting_paper_order(primary: Path) -> None:
    engine = create_db_engine(f"sqlite:///{primary}")
    with make_session_factory(engine)() as session:
        session.add(
            Order(
                idempotency_key="paper-preexisting-client",
                ticker="AAPL",
                side="buy",
                order_type="limit",
                qty=Decimal("1"),
                limit_price=Decimal("96"),
                status=OrderStatus.SUBMITTED.value,
                broker_order_id="paper-preexisting",
                acceptance_state="accepted",
                last_error_code="",
            )
        )
        session.commit()
    engine.dispose()


def _credentialed_environment(monkeypatch, primary: Path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.setenv("APP_API_TOKEN", "task-10-test-operator-secret")
    monkeypatch.setenv("ALPACA_API_KEY", "paper-key-present")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "paper-secret-present")
    monkeypatch.setenv(
        "ALPACA_PAPER_BASE_URL",
        "https://paper-api.alpaca.markets",
    )


def test_credentialed_mode_preserves_preexisting_manifest_and_cleans_tagged_order(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    _seed_preexisting_paper_order(primary)
    _credentialed_environment(monkeypatch, primary)
    broker = PaperStateBroker()

    report = run_safety_drill(
        database_copy=tmp_path / "alpaca-copy.db",
        config=_safe_config(app_config),
        broker=broker,
        credentialed_paper=True,
        clock=FakeClock(is_open=True),
    )

    assert report.safe
    assert "alpaca_paper:passed" in report.details
    assert len(broker.submit_requests) == 1
    submitted = broker.submit_requests[0]
    assert submitted.order_type is OrderType.LIMIT
    assert submitted.time_in_force is OrderTimeInForce.GTC
    assert submitted.qty == Decimal("1")
    assert submitted.qty == submitted.qty.to_integral_value()
    assert submitted.limit_price == Decimal("96.00")
    assert submitted.idempotency_key.startswith("safety-drill-")
    copied_engine = create_db_engine(f"sqlite:///{tmp_path / 'alpaca-copy.db'}")
    with make_session_factory(copied_engine)() as session:
        persisted = session.scalar(
            select(Order).where(
                Order.idempotency_key == submitted.idempotency_key
            )
        )
        assert json.loads(persisted.submission_payload_json) == {
            "time_in_force": "gtc"
        }
    copied_engine.dispose()
    assert broker.cancel_ids == ["paper-drill-1"]
    assert {
        order.broker_order_id for order in broker.get_open_orders()
    } == {"paper-preexisting"}
    assert broker.get_positions() == []


def test_credentialed_mode_compensates_only_its_adverse_fill(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    _seed_preexisting_paper_order(primary)
    _credentialed_environment(monkeypatch, primary)
    broker = PaperStateBroker(fill_initial=True)

    report = run_safety_drill(
        database_copy=tmp_path / "alpaca-fill-copy.db",
        config=_safe_config(app_config),
        broker=broker,
        credentialed_paper=True,
        clock=FakeClock(is_open=True),
    )

    assert report.safe
    assert "alpaca_paper:passed" in report.details
    assert len(broker.submit_requests) == 2
    initial, compensation = broker.submit_requests
    assert initial.side is OrderSide.BUY
    assert compensation.side is OrderSide.SELL
    assert compensation.qty == initial.qty
    assert compensation.idempotency_key.startswith(
        f"{initial.idempotency_key.rsplit('-', 1)[0]}-compensate"
    )
    assert broker.cancel_ids == []
    assert broker.get_positions() == []


def test_credentialed_mode_refuses_missing_keys_or_nonpaper_endpoint_before_copy(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.setenv("APP_API_TOKEN", "task-10-test-operator-secret")
    destination = tmp_path / "must-not-exist.db"
    broker = PaperStateBroker()

    with pytest.raises(SafetyDrillError) as missing:
        run_safety_drill(
            database_copy=destination,
            config=_safe_config(app_config),
            broker=broker,
            credentialed_paper=True,
            clock=FakeClock(is_open=True),
        )
    assert missing.value.code == "credentials_unavailable"
    assert not destination.exists()
    assert broker.submit_requests == []

    monkeypatch.setenv("ALPACA_API_KEY", "paper-key-present")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "paper-secret-present")
    monkeypatch.setenv(
        "ALPACA_PAPER_BASE_URL",
        "https://api.alpaca.markets",
    )
    with pytest.raises(SafetyDrillError) as endpoint:
        run_safety_drill(
            database_copy=destination,
            config=_safe_config(app_config),
            broker=broker,
            credentialed_paper=True,
            clock=FakeClock(is_open=True),
        )
    assert endpoint.value.code == "unsafe_configuration"
    assert not destination.exists()
    assert broker.submit_requests == []
