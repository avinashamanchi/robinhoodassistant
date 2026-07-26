"""Exercise the production safety barriers on an explicit SQLite copy."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import ROUND_UP, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.engine import make_url

from ..app.auth import InvalidSession
from ..assets import AssetClass
from ..bootstrap import build_test_container
from ..broker.base import BrokerAcceptanceUnknown, BrokerClient
from ..broker.models import (
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderTimeInForce,
)
from ..config import (
    AppConfig,
    BrokerKind,
    Secrets,
    TradingMode,
    load_config,
)
from ..db.migrate import upgrade
from ..db.models import CircuitBreakerState, Order, Rule, RuleGroup
from ..db.schema import schema_status
from ..db.session import create_db_engine
from ..risk.breakers import BreakerScope
from ..risk.clock import FakeClock


@dataclass(frozen=True)
class SafetyDrillReport:
    schema_current: bool
    auth_fail_closed: bool
    crash_recovered_without_duplicate: bool
    oco_single_terminal: bool
    breakers_persisted: bool
    reconciliation_clean: bool
    safe: bool
    details: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True)


class SafetyDrillError(RuntimeError):
    """Stable refusal that never includes a provider exception or secret."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _AcceptanceUnknownOnceBroker(BrokerClient):
    """Lose one acceptance response after the delegated broker accepts it."""

    def __init__(self, broker: BrokerClient) -> None:
        self._broker = broker
        self.reconciliation_key = broker.reconciliation_key
        self.submit_calls = 0
        self._lose_next_acceptance = True

    def get_quote(self, ticker: str):
        return self._broker.get_quote(ticker)

    def get_account(self):
        return self._broker.get_account()

    def get_positions(self):
        return self._broker.get_positions()

    def get_fill_activities(self, after=None):
        reader = getattr(self._broker, "get_fill_activities", None)
        return [] if reader is None else reader(after)

    def submit_order(self, order: OrderRequest) -> OrderResult:
        self.submit_calls += 1
        result = self._broker.submit_order(order)
        if self._lose_next_acceptance:
            self._lose_next_acceptance = False
            raise BrokerAcceptanceUnknown
        return result

    def get_order_by_client_id(self, client_order_id: str):
        return self._broker.get_order_by_client_id(client_order_id)

    def get_open_orders(self):
        return self._broker.get_open_orders()

    def get_order_status(self, order_id: str):
        return self._broker.get_order_status(order_id)

    def cancel_order(self, order_id: str):
        return self._broker.cancel_order(order_id)


def _database_source(database_url: str) -> Path:
    try:
        url = make_url(database_url)
    except Exception:
        raise SafetyDrillError("unsafe_primary_database") from None
    if (
        url.get_backend_name() != "sqlite"
        or not url.database
        or url.database == ":memory:"
    ):
        raise SafetyDrillError("unsafe_primary_database")
    source = Path(url.database).expanduser().resolve()
    if not source.is_file():
        raise SafetyDrillError("unsafe_primary_database")
    try:
        with source.open("rb") as handle:
            header = handle.read(16)
    except OSError:
        raise SafetyDrillError("unsafe_primary_database") from None
    if header != b"SQLite format 3\x00":
        raise SafetyDrillError("invalid_primary_database")
    return source


def _online_copy(source: Path, destination: Path) -> Path:
    if not destination.is_absolute():
        raise SafetyDrillError("unsafe_database_copy")
    if destination.exists() or destination.is_symlink():
        raise SafetyDrillError("unsafe_database_copy")
    existing_parent = destination.parent
    while not existing_parent.exists():
        if existing_parent.is_symlink():
            raise SafetyDrillError("unsafe_database_copy")
        next_parent = existing_parent.parent
        if next_parent == existing_parent:
            raise SafetyDrillError("unsafe_database_copy")
        existing_parent = next_parent
    if existing_parent.is_symlink():
        raise SafetyDrillError("unsafe_database_copy")
    target = destination.resolve(strict=False)
    if target == source:
        raise SafetyDrillError("unsafe_database_copy")

    missing_parents: list[Path] = []
    candidate = target.parent
    while not candidate.exists():
        missing_parents.append(candidate)
        candidate = candidate.parent
    for parent in reversed(missing_parents):
        parent.mkdir(mode=0o700)
        parent.chmod(0o700)

    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        with (
            sqlite3.connect(source) as source_connection,
            sqlite3.connect(temporary) as target_connection,
        ):
            source_connection.backup(target_connection)
            integrity = target_connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()
            if integrity != ("ok",):
                raise SafetyDrillError("database_copy_failed")
            target_connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        temporary.chmod(0o600)
        try:
            # Publishing with a hard link is atomic and fails if another actor
            # creates the explicit destination after validation. os.replace()
            # would silently overwrite that evidence.
            os.link(temporary, target)
        except FileExistsError:
            raise SafetyDrillError("unsafe_database_copy") from None
        target.chmod(0o600)
    except SafetyDrillError:
        raise
    except Exception:
        raise SafetyDrillError("database_copy_failed") from None
    finally:
        temporary.unlink(missing_ok=True)
        temporary.with_name(f"{temporary.name}-wal").unlink(missing_ok=True)
        temporary.with_name(f"{temporary.name}-shm").unlink(missing_ok=True)
    return target


def _validate_safe_config(config: AppConfig) -> None:
    if (
        config.trading.mode is not TradingMode.PAPER
        or config.trading.broker is not BrokerKind.ALPACA
        or config.features.auto_execute_preapproved_rules
        or config.execution.prefer_bracket_orders
        or config.llm.fallback_provider is not None
    ):
        raise SafetyDrillError("unsafe_configuration")


def _validate_credentialed_paper(
    broker: BrokerClient,
    secrets: Secrets,
) -> None:
    from ..broker.alpaca import AlpacaBroker

    if not isinstance(broker, AlpacaBroker):
        raise SafetyDrillError("unsafe_configuration")
    if not (secrets.alpaca_api_key and secrets.alpaca_secret_key):
        raise SafetyDrillError("credentials_unavailable")
    if secrets.live_trading_confirm:
        raise SafetyDrillError("unsafe_configuration")
    endpoint = urlsplit(secrets.alpaca_paper_base_url)
    if (
        endpoint.scheme != "https"
        or endpoint.hostname != "paper-api.alpaca.markets"
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.port is not None
        or endpoint.path not in {"", "/"}
        or endpoint.query
        or endpoint.fragment
    ):
        raise SafetyDrillError("unsafe_configuration")


def _open_order_manifest(broker: BrokerClient) -> frozenset[str]:
    manifest: set[str] = set()
    for order in broker.get_open_orders():
        if (
            not isinstance(order.broker_order_id, str)
            or not order.broker_order_id.strip()
        ):
            raise SafetyDrillError("paper_manifest_unconfirmed")
        manifest.add(order.broker_order_id)
    return frozenset(manifest)


def _position_manifest(broker: BrokerClient) -> dict[str, Decimal]:
    return {
        position.ticker.upper(): position.qty
        for position in broker.get_positions()
    }


def _rule_command(group_key: str, direction: str, price: str) -> dict[str, Any]:
    return {
        "ticker": "AAPL",
        "kind": "price",
        "condition": {
            "type": "price",
            "direction": direction,
            "price": price,
        },
        "action": {
            "side": "buy",
            "order_type": "limit",
            "qty": "0.010000",
            "limit_price": "96.000000",
        },
        "group_key": group_key,
        "pre_approved": False,
    }


def _reconcile_drill_orders(container, tag: str, stage: str) -> dict[str, Any]:
    return container.service.sync_open_orders(
        actor="operator:safety-drill",
        reason=f"safety drill {stage} order reconciliation",
        request_id=f"{tag}-{stage}-orders",
    )


def _compensate_drill_delta(
    container,
    *,
    before_positions: dict[str, Decimal],
    tag: str,
    symbol: str,
) -> bool:
    """Flatten only a position delta bounded by the drill order's exact fill."""
    order_sync = _reconcile_drill_orders(container, tag, "pre-compensation")
    if order_sync["failed"]:
        return False
    positions = _position_manifest(container.broker)
    delta = positions.get(symbol, Decimal("0")) - before_positions.get(
        symbol,
        Decimal("0"),
    )
    if delta == 0:
        return True
    initial = container.broker.get_order_by_client_id(f"{tag}-crash")
    if (
        initial is None
        or initial.filled_qty <= 0
        or abs(delta) > initial.filled_qty
    ):
        return False
    side = OrderSide.SELL if delta > 0 else OrderSide.BUY
    proposal = container.service.propose_order(
        symbol,
        side.value,
        "market",
        qty=str(abs(delta)),
        idempotency_key=f"{tag}-compensate",
        actor="operator:safety-drill",
        reason="compensate only the safety drill position delta",
        request_id=f"{tag}-compensate-propose",
    )
    if proposal["status"] != OrderStatus.PROPOSED.value:
        return False
    approved = container.service.approve_order(
        proposal["order_id"],
        actor="operator:safety-drill",
        reason="human safety drill compensation approval",
        request_id=f"{tag}-compensate-approve",
    )
    if approved["status"] == OrderStatus.ACCEPTANCE_UNKNOWN.value:
        container.reconciliation.reconcile_unknown(
            actor="operator:safety-drill",
            reason="resolve safety drill compensation by client identity",
            request_id=f"{tag}-compensate-resolve",
        )
    final_sync = _reconcile_drill_orders(container, tag, "post-compensation")
    return (
        final_sync["failed"] == 0
        and _position_manifest(container.broker) == before_positions
    )


def _best_effort_cleanup(
    container,
    *,
    before_positions: dict[str, Decimal],
    tag: str,
    symbol: str,
) -> bool:
    """Cancel/flatten only tagged state; never expose a provider exception."""
    try:
        container.reconciliation.reconcile_unknown(
            actor="operator:safety-drill",
            reason="safety drill exception cleanup identity resolution",
            request_id=f"{tag}-cleanup-resolve",
        )
    except Exception:
        pass
    try:
        tagged_open = [
            order
            for order in container.broker.get_open_orders()
            if order.idempotency_key.startswith(tag)
        ]
        for remote in tagged_open:
            with container.session_factory() as session:
                local = session.scalar(
                    select(Order).where(
                        Order.idempotency_key == remote.idempotency_key
                    )
                )
            if local is not None and OrderStatus(local.status) in {
                OrderStatus.SUBMITTED,
                OrderStatus.PARTIALLY_FILLED,
            }:
                container.service.cancel_live_order(
                    local.id,
                    actor="operator:safety-drill",
                    reason="cancel tagged safety drill state",
                    request_id=f"{tag}-cleanup-cancel",
                )
        return _compensate_drill_delta(
            container,
            before_positions=before_positions,
            tag=tag,
            symbol=symbol,
        )
    except Exception:
        return False


def run_safety_drill(
    *,
    database_copy: str | Path,
    config: AppConfig,
    broker: BrokerClient,
    credentialed_paper: bool = False,
    clock=None,
) -> SafetyDrillReport:
    """Copy the primary and derive every report gate from production behavior."""
    _validate_safe_config(config)
    primary_secrets = Secrets()
    if credentialed_paper:
        _validate_credentialed_paper(broker, primary_secrets)
        if clock is None:
            raise SafetyDrillError("unsafe_configuration")
    primary = _database_source(primary_secrets.database_url)
    copied = _online_copy(primary, Path(database_copy))
    copy_url = f"sqlite:///{copied}"
    copy_engine = create_db_engine(copy_url)
    try:
        upgrade(copy_engine)
        schema_current = schema_status(copy_engine).ready
    except Exception:
        raise SafetyDrillError("migration_failed") from None
    finally:
        copy_engine.dispose()

    drill_secrets = primary_secrets.model_copy(
        update={
            "database_url": copy_url,
            "app_api_token": (
                primary_secrets.app_api_token
                or "task-10-safety-drill-local-operator"
            ),
        }
    )
    crash_broker = _AcceptanceUnknownOnceBroker(broker)
    container = build_test_container(
        config,
        drill_secrets,
        broker=crash_broker,
        clock=clock or FakeClock(is_open=True),
    )
    tag = f"safety-drill-{uuid4().hex}"
    details: list[str] = [
        "mode:alpaca_paper" if credentialed_paper else "mode:mock"
    ]
    try:
        before_orders = _open_order_manifest(crash_broker)
        before_positions = _position_manifest(crash_broker)
    except Exception:
        raise SafetyDrillError("paper_manifest_unconfirmed") from None

    try:
        container.session_auth.authenticate(f"{tag}-invalid-session")
    except InvalidSession:
        auth_fail_closed = True
        details.append("auth:fail_closed")
    else:
        auth_fail_closed = False
        details.append("auth:unexpected_accept")

    symbol = "AAPL"
    try:
        quote = crash_broker.get_quote(symbol)
        limit_price = (quote.last * Decimal("0.96")).quantize(
            Decimal("0.01"),
            rounding=ROUND_UP,
        )
        quantity = (
            Decimal("1")
            if credentialed_paper
            else (Decimal("1.25") / limit_price).quantize(
                Decimal("0.000001"),
                rounding=ROUND_UP,
            )
        )
        proposal = container.service.propose_order(
            symbol,
            "buy",
            "limit",
            qty=str(quantity),
            limit_price=str(limit_price),
            idempotency_key=f"{tag}-crash",
            time_in_force=(
                OrderTimeInForce.GTC.value
                if credentialed_paper
                else OrderTimeInForce.DAY.value
            ),
            actor="operator:safety-drill",
            reason="exercise acceptance unknown recovery",
            request_id=f"{tag}-crash",
        )
        approved = container.service.approve_order(
            proposal["order_id"],
            actor="operator:safety-drill",
            reason="exercise acceptance unknown recovery",
            request_id=f"{tag}-crash",
        )
        resolved, unresolved = container.reconciliation.reconcile_unknown(
            actor="operator:safety-drill",
            reason="resolve acceptance by client identity",
            request_id=f"{tag}-reconcile",
        )
        replay = container.order_submission.submit(
            proposal["order_id"],
            actor="operator:safety-drill",
            reason="prove accepted order is not resubmitted",
            request_id=f"{tag}-replay",
        )
        terminal_confirmed = replay.status in {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }
        if replay.status in {
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
        }:
            canceled = container.service.cancel_live_order(
                proposal["order_id"],
                actor="operator:safety-drill",
                reason="finish deterministic drill order",
                request_id=f"{tag}-cancel",
            )
            terminal_confirmed = (
                canceled.get("status") == OrderStatus.CANCELED.value
            )
        one_initial_write = crash_broker.submit_calls == 1
        cleanup_confirmed = _best_effort_cleanup(
            container,
            before_positions=before_positions,
            tag=tag,
            symbol=symbol,
        )
        crash_recovered_without_duplicate = (
            approved["status"] == OrderStatus.ACCEPTANCE_UNKNOWN.value
            and resolved == 1
            and unresolved == ()
            and replay.status is not OrderStatus.ACCEPTANCE_UNKNOWN
            and one_initial_write
            and terminal_confirmed
            and cleanup_confirmed
        )
        details.append(
            "crash:recovered_once"
            if crash_recovered_without_duplicate
            else "crash:unconfirmed"
        )
    except Exception:
        _best_effort_cleanup(
            container,
            before_positions=before_positions,
            tag=tag,
            symbol=symbol,
        )
        crash_recovered_without_duplicate = False
        details.append("crash:dependency_failed")

    try:
        group_key = f"{tag}-oco"
        first_rule = container.service.rule_application.create_rule(
            _rule_command(group_key, "below", "99"),
            actor="operator:safety-drill",
            reason="create OCO safety drill group",
            request_id=f"{tag}-oco-create-1",
        )
        container.service.rule_application.create_rule(
            _rule_command(group_key, "above", "101"),
            actor="operator:safety-drill",
            reason="create OCO safety drill group",
            request_id=f"{tag}-oco-create-2",
        )
        with container.session_factory() as session:
            group_id = session.scalar(
                select(RuleGroup.id).where(RuleGroup.group_key == group_key)
            )
        assert group_id is not None
        lease = container.service.rule_repository.lease_group(
            group_id,
            now=datetime.now(timezone.utc),
            actor="daemon:safety-drill",
            reason="exercise OCO terminal claim",
            request_id=f"{tag}-oco-lease",
        )
        claimed = (
            lease is not None
            and container.service.rule_repository.claim_terminal(
                lease,
                first_rule,
                now=datetime.now(timezone.utc),
                actor="daemon:safety-drill",
                reason="exercise OCO terminal claim",
                request_id=f"{tag}-oco-terminal",
            )
        )
        replay_claimed = (
            lease is not None
            and container.service.rule_repository.claim_terminal(
                lease,
                first_rule,
                now=datetime.now(timezone.utc),
                actor="daemon:safety-drill",
                reason="prove OCO claim is single terminal",
                request_id=f"{tag}-oco-replay",
            )
        )
        with container.session_factory() as session:
            rule_states = tuple(
                session.scalars(
                    select(Rule.state)
                    .where(Rule.group_id == group_id)
                    .order_by(Rule.id)
                ).all()
            )
            group = session.get(RuleGroup, group_id)
            group_terminal_rule_id = (
                group.terminal_rule_id if group is not None else None
            )
        oco_single_terminal = (
            claimed
            and not replay_claimed
            and group_terminal_rule_id == first_rule
            and sorted(rule_states) == ["canceled", "triggered"]
        )
        details.append(
            "oco:single_terminal"
            if oco_single_terminal
            else "oco:unconfirmed"
        )
    except Exception:
        oco_single_terminal = False
        details.append("oco:dependency_failed")

    restarted = container
    try:
        data_scope = BreakerScope.data(AssetClass.EQUITY)
        liquidity_scope = BreakerScope.liquidity("AAPL")
        data_trip = container.breakers.trip(
            data_scope,
            "safety drill persisted data breaker",
            "operator:safety-drill",
            request_id=f"{tag}-breaker-data",
        )
        container.breakers.trip(
            liquidity_scope,
            "safety drill persisted liquidity breaker",
            "operator:safety-drill",
            request_id=f"{tag}-breaker-liquidity",
        )
        restarted = build_test_container(
            config,
            drill_secrets,
            broker=crash_broker,
            clock=clock or FakeClock(is_open=True),
        )
        survived_restart = (
            restarted.breakers.is_tripped(data_scope)
            and restarted.breakers.is_tripped(liquidity_scope)
        )
        restarted.breakers.reset(
            data_scope,
            "operator:safety-drill",
            "scoped safety drill reset",
            {"broker_reconciled": True},
            expected_generation=data_trip.generation,
            request_id=f"{tag}-breaker-data-reset",
        )
        scoped_reset = (
            not restarted.breakers.is_tripped(data_scope)
            and restarted.breakers.is_tripped(liquidity_scope)
        )
        liquidity_state = restarted.breakers.get(liquidity_scope)
        if liquidity_state is not None:
            restarted.breakers.reset(
                liquidity_scope,
                "operator:safety-drill",
                "finish safety drill breaker cleanup",
                {"broker_reconciled": True},
                expected_generation=liquidity_state.generation,
                request_id=f"{tag}-breaker-liquidity-reset",
            )
        breakers_persisted = (
            survived_restart
            and scoped_reset
            and not restarted.breakers.is_tripped(liquidity_scope)
        )
        details.append(
            "breakers:persisted_scoped_reset"
            if breakers_persisted
            else "breakers:unconfirmed"
        )
    except Exception:
        breakers_persisted = False
        details.append("breakers:dependency_failed")

    try:
        order_sync = restarted.service.sync_open_orders(
            actor="operator:safety-drill",
            reason="prove final order reconciliation",
            request_id=f"{tag}-final-orders",
        )
        position_sync = restarted.service.reconcile_positions(
            actor="operator:safety-drill",
            reason="prove final position reconciliation",
            request_id=f"{tag}-final-positions",
        )
        with restarted.session_factory() as session:
            unsafe_local_orders = session.scalars(
                select(Order.id).where(
                    Order.idempotency_key.like(f"{tag}%"),
                    Order.status.in_(
                        (
                            OrderStatus.SUBMITTING.value,
                            OrderStatus.ACCEPTANCE_UNKNOWN.value,
                            OrderStatus.SUBMITTED.value,
                            OrderStatus.PARTIALLY_FILLED.value,
                        )
                    ),
                )
            ).all()
            active_breakers = session.scalars(
                select(CircuitBreakerState.scope_key).where(
                    CircuitBreakerState.tripped.is_(True)
                )
            ).all()
        tagged_broker_open = tuple(
            order
            for order in crash_broker.get_open_orders()
            if order.idempotency_key.startswith(tag)
        )
        after_orders = _open_order_manifest(crash_broker)
        after_positions = _position_manifest(crash_broker)
        paper_manifest_clean = (
            after_orders == before_orders
            and after_positions == before_positions
        )
        reconciliation_clean = (
            order_sync["failed"] == 0
            and position_sync["reconciled"]
            and not unsafe_local_orders
            and not tagged_broker_open
            and not active_breakers
            and paper_manifest_clean
        )
        details.append(
            "reconciliation:clean"
            if reconciliation_clean
            else "reconciliation:unconfirmed"
        )
        if credentialed_paper:
            details.append(
                "alpaca_paper:passed"
                if reconciliation_clean
                else "alpaca_paper:unconfirmed"
            )
    except Exception:
        reconciliation_clean = False
        details.append("reconciliation:dependency_failed")
        if credentialed_paper:
            details.append("alpaca_paper:unconfirmed")
    details.insert(
        1,
        "schema:current" if schema_current else "schema:not_current",
    )

    gates = (
        schema_current,
        auth_fail_closed,
        crash_recovered_without_duplicate,
        oco_single_terminal,
        breakers_persisted,
        reconciliation_clean,
    )
    return SafetyDrillReport(
        schema_current=schema_current,
        auth_fail_closed=auth_fail_closed,
        crash_recovered_without_duplicate=crash_recovered_without_duplicate,
        oco_single_terminal=oco_single_terminal,
        breakers_persisted=breakers_persisted,
        reconciliation_clean=reconciliation_clean,
        safe=all(gates),
        details=tuple(details),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-copy", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mock", action="store_true")
    mode.add_argument("--alpaca-paper", action="store_true")
    args = parser.parse_args(argv)

    secrets = Secrets()
    from ..logging import runtime_startup

    try:
        with runtime_startup("safety-drill", secrets):
            config = load_config()
            if args.mock:
                from ..broker.mock import MockBroker

                broker: BrokerClient = MockBroker(
                    prices={"AAPL": Decimal("100")}
                )
                clock = FakeClock(is_open=True)
            else:
                if not (
                    secrets.alpaca_api_key
                    and secrets.alpaca_secret_key
                ):
                    raise SafetyDrillError("credentials_unavailable")
                from ..broker.alpaca import AlpacaBroker, AlpacaClock

                broker = AlpacaBroker.from_credentials(
                    secrets.alpaca_api_key,
                    secrets.alpaca_secret_key,
                    paper=True,
                    timeout_seconds=config.trading.request_timeout_seconds,
                )
                clock = AlpacaClock.from_credentials(
                    secrets.alpaca_api_key,
                    secrets.alpaca_secret_key,
                    paper=True,
                    timeout_seconds=config.trading.request_timeout_seconds,
                )
            report = run_safety_drill(
                database_copy=args.database_copy,
                config=config,
                broker=broker,
                credentialed_paper=args.alpaca_paper,
                clock=clock,
            )
    except SafetyDrillError as exc:
        print(
            json.dumps(
                {"error": exc.code, "safe": False},
                sort_keys=True,
            )
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {"error": "drill_failed", "safe": False},
                sort_keys=True,
            )
        )
        return 2
    print(report.to_json())
    return 0 if report.safe else 1


if __name__ == "__main__":
    raise SystemExit(main())
