"""One fail-closed production composition root and runtime safety helpers."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
from types import SimpleNamespace
from decimal import Decimal

import pytest
from sqlalchemy import text

from trading_assistant.app.auth import SessionAuth
from trading_assistant.app.limits import (
    ConcurrencyLeaseService,
    DurableRateLimiter,
    LimitSpec,
    LimitStoreUnavailable,
)
from trading_assistant.app.main import create_app
from trading_assistant.broker.alpaca import AlpacaBroker
from trading_assistant.broker.base import BrokerSubmissionRejected
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import (
    OrderRequest,
    OrderSide,
    OrderType,
)
from trading_assistant.config import BrokerKind, Secrets, TradingMode
from trading_assistant.db.migrate import upgrade
from trading_assistant.db.models import StartupReconciliationState
from trading_assistant.db.schema import SchemaOutOfDate
from trading_assistant.db.session import (
    create_db_engine,
    make_session_factory,
)
from trading_assistant.llm.budget import BudgetLimits, ProviderBudgetService
from trading_assistant.ops.tenure import (
    ProcessIdentity,
    ProcessProof,
    RuntimeTenureGuard,
    RuntimeTenureService,
    TenureUncertain,
    TenureUnavailable,
)
from trading_assistant.operations import AuditRecorder, OperationsService
from trading_assistant.orders.startup import StartupReconciliationFailed
from trading_assistant.risk.clock import FakeClock


def _migrated_secrets(tmp_path: Path) -> Secrets:
    database_url = f"sqlite:///{tmp_path}/runtime.db"
    engine = create_db_engine(database_url)
    upgrade(engine)
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE sensitive_migration_state SET "
                "state='complete',active_key_id='local-primary-2026-07',"
                "rows_total=0,rows_completed=0,"
                "backup_path_hash=:backup_hash,started_at=:started_at,"
                "completed_at=:completed_at,updated_at=:updated_at"
            ),
            {
                "backup_hash": "a" * 64,
                "started_at": now - timedelta(minutes=2),
                "completed_at": now - timedelta(minutes=1),
                "updated_at": now,
            },
        )
    return Secrets(
        database_url=database_url,
        app_api_token="operator-secret-for-bootstrap-tests",
        field_encryption_keys={
            "local-primary-2026-07": base64.b64encode(
                b"f" * 32
            ).decode("ascii")
        },
    )


def _alpaca_config(config):
    return config.model_copy(
        update={
            "trading": config.trading.model_copy(
                update={"broker": BrokerKind.ALPACA}
            )
        }
    )


def _injected_container(service, secrets):
    audit = AuditRecorder(service.session_factory)
    configured = service.config.security.provider_budget
    provider_budget = ProviderBudgetService(
        service.session_factory,
        BudgetLimits(
            calls=configured.daily_calls,
            input_tokens=configured.daily_input_tokens,
            output_tokens=configured.daily_output_tokens,
            reservation_ttl_seconds=configured.reservation_ttl_seconds,
        ),
        prices=configured.prices,
    )
    return SimpleNamespace(
        config=service.config,
        secrets=secrets,
        service=service,
        session_factory=service.session_factory,
        rate_limiter=DurableRateLimiter(service.session_factory),
        leases=ConcurrencyLeaseService(service.session_factory),
        provider_budget=provider_budget,
        session_auth=SessionAuth(
            service.session_factory,
            application_secret=secrets.app_api_token,
            cookie_secure=False,
        ),
        audit=audit,
        operations=OperationsService(service, audit),
    )


class _StubAgent:
    def chat(self, message, **context):
        return {"reply": message, "context": context}


class _MutationTradingClient:
    def __init__(self) -> None:
        self._sandbox = True
        self._base_url = "https://paper-api.alpaca.markets"
        self.submit_calls = 0
        self.cancel_calls = 0
        self._orders: dict[str, SimpleNamespace] = {}
        self.open_orders: list[SimpleNamespace] = []

    def submit_order(self, order_data):
        self.submit_calls += 1
        order = SimpleNamespace(
            id=f"broker-{self.submit_calls}",
            client_order_id=order_data.client_order_id,
            status=SimpleNamespace(value="new"),
            filled_qty="0",
            filled_avg_price=None,
            symbol=order_data.symbol,
            asset_class=SimpleNamespace(value="us_equity"),
        )
        self._orders[order.id] = order
        return order

    def cancel_order_by_id(self, order_id):
        self.cancel_calls += 1
        order = self._orders[order_id]
        order.status = SimpleNamespace(value="canceled")

    def get_order_by_id(self, order_id):
        return self._orders[order_id]

    def get(self, _path, _data=None):
        return []

    def get_orders(self, filter=None):
        return self.open_orders

    def get_all_positions(self):
        return []


def _paper_alpaca_broker() -> tuple[AlpacaBroker, _MutationTradingClient]:
    trading = _MutationTradingClient()
    broker = AlpacaBroker(trading, SimpleNamespace())
    broker.get_order_by_client_id = lambda _client_id: None
    return broker, trading


@contextmanager
def _closed_runtime_container(container):
    """Give direct-container tests exact ownership of their renewal worker."""
    guard = container.runtime_tenure_guard
    assert guard is not None
    worker = guard._thread
    assert worker is not None
    try:
        yield container
    finally:
        guard.close()
        assert guard.closed
        assert not worker.is_alive()


def _market_order(key: str) -> OrderRequest:
    return OrderRequest(
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        idempotency_key=key,
        notional=Decimal("10"),
    )


def _bracket_order(key: str) -> OrderRequest:
    return OrderRequest(
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        idempotency_key=key,
        qty=Decimal("1"),
        limit_price=Decimal("100"),
    )


def test_production_container_arms_exact_dynamic_alpaca_paper_guard(
    tmp_path,
    app_config,
    monkeypatch,
):
    from trading_assistant import bootstrap

    broker, trading = _paper_alpaca_broker()
    monkeypatch.setattr(
        bootstrap,
        "build_broker",
        lambda *_args, **_kwargs: broker,
    )
    monkeypatch.setattr(
        bootstrap,
        "build_clock",
        lambda *_args, **_kwargs: FakeClock(is_open=True),
    )

    with _closed_runtime_container(
        bootstrap.build_container(
            _alpaca_config(app_config),
            _migrated_secrets(tmp_path),
        )
    ) as container:
        with container.session_factory() as session:
            startup = session.get(
                StartupReconciliationState,
                broker.reconciliation_key,
            )
            assert startup is not None
            assert startup.status == "current"
            assert startup.completed_generation == startup.generation

        submitted = container.broker.submit_order(
            _market_order("paper-submit")
        )
        bracket = container.broker.submit_bracket(
            _bracket_order("paper-bracket"),
            Decimal("110"),
            Decimal("95"),
        )
        container.broker.cancel_order(submitted.broker_order_id)
        assert bracket.broker_order_id is not None
        assert trading.submit_calls == 2
        assert trading.cancel_calls == 1

        trading._sandbox = False
        trading._base_url = "https://api.alpaca.markets"
        writes_before = (trading.submit_calls, trading.cancel_calls)

        with pytest.raises(
            BrokerSubmissionRejected,
            match="not official Alpaca paper",
        ):
            container.broker.submit_order(
                _market_order("blocked-submit")
            )
        with pytest.raises(
            BrokerSubmissionRejected,
            match="not official Alpaca paper",
        ):
            container.broker.submit_bracket(
                _bracket_order("blocked-bracket"),
                Decimal("110"),
                Decimal("95"),
            )
        with pytest.raises(
            BrokerSubmissionRejected,
            match="not official Alpaca paper",
        ):
            container.broker.cancel_order(bracket.broker_order_id)

        assert (trading.submit_calls, trading.cancel_calls) == writes_before


def test_production_container_rejects_non_alpaca_or_unsafe_target(
    tmp_path,
    app_config,
    monkeypatch,
):
    from trading_assistant import bootstrap

    monkeypatch.setattr(
        bootstrap,
        "build_clock",
        lambda *_args, **_kwargs: FakeClock(is_open=True),
    )
    monkeypatch.setattr(
        bootstrap,
        "build_broker",
        lambda *_args, **_kwargs: MockBroker(),
    )
    with pytest.raises(RuntimeError, match="exact AlpacaBroker"):
        bootstrap.build_container(
            _alpaca_config(app_config),
            _migrated_secrets(tmp_path),
        )

    broker, trading = _paper_alpaca_broker()
    trading._sandbox = False
    trading._base_url = "https://api.alpaca.markets"
    monkeypatch.setattr(
        bootstrap,
        "build_broker",
        lambda *_args, **_kwargs: broker,
    )
    with pytest.raises(
        BrokerSubmissionRejected,
        match="not official Alpaca paper",
    ):
        bootstrap.build_container(
            _alpaca_config(app_config),
            _migrated_secrets(tmp_path),
        )


def test_production_container_refuses_to_serve_unknown_remote_open_order(
    tmp_path,
    app_config,
    monkeypatch,
):
    from trading_assistant import bootstrap

    broker, trading = _paper_alpaca_broker()
    trading.open_orders.append(
        SimpleNamespace(
            id="remote-without-local-truth",
            client_order_id="external-client-order",
            status=SimpleNamespace(value="new"),
            filled_qty="0",
            filled_avg_price=None,
            symbol="AAPL",
            asset_class=SimpleNamespace(value="us_equity"),
        )
    )
    monkeypatch.setattr(
        bootstrap,
        "build_broker",
        lambda *_args, **_kwargs: broker,
    )
    monkeypatch.setattr(
        bootstrap,
        "build_clock",
        lambda *_args, **_kwargs: FakeClock(is_open=True),
    )

    with pytest.raises(
        StartupReconciliationFailed,
        match="broker_reconciliation_failed",
    ):
        bootstrap.build_container(
            _alpaca_config(app_config),
            _migrated_secrets(tmp_path),
            runtime_role="daemon",
        )

    assert trading.submit_calls == 0
    assert trading.cancel_calls == 0


def test_app_container_serves_console_with_failed_startup_reconciliation(
    tmp_path,
    app_config,
    monkeypatch,
):
    """Treating a broker failure as a structural failure would hide degraded safety state."""
    from trading_assistant import bootstrap

    broker, trading = _paper_alpaca_broker()
    trading.open_orders.append(
        SimpleNamespace(
            id="remote-without-local-truth",
            client_order_id="external-client-order",
            status=SimpleNamespace(value="new"),
            filled_qty="0",
            filled_avg_price=None,
            symbol="AAPL",
            asset_class=SimpleNamespace(value="us_equity"),
        )
    )
    monkeypatch.setattr(
        bootstrap,
        "build_broker",
        lambda *_args, **_kwargs: broker,
    )
    monkeypatch.setattr(
        bootstrap,
        "build_clock",
        lambda *_args, **_kwargs: FakeClock(is_open=True),
    )

    with _closed_runtime_container(
        bootstrap.build_container(
            _alpaca_config(app_config),
            _migrated_secrets(tmp_path),
            runtime_role="app",
        )
    ) as container:
        with container.session_factory() as session:
            state = session.get(
                StartupReconciliationState,
                broker.reconciliation_key,
            )
            assert state is not None
            assert state.status == "failed"
            assert state.completed_generation < state.generation
        assert container.operations.health().as_dict()[
            "startup_reconciliation"
        ]["status"] == "failed"
        assert trading.submit_calls == 0
        assert trading.cancel_calls == 0


def test_daemon_container_remains_fail_closed_on_startup_reconciliation_failure(
    tmp_path,
    app_config,
    monkeypatch,
):
    """Letting the daemon continue after missing broker truth could resume automation."""
    from trading_assistant import bootstrap

    broker, trading = _paper_alpaca_broker()
    trading.open_orders.append(
        SimpleNamespace(
            id="remote-without-local-truth",
            client_order_id="external-client-order",
            status=SimpleNamespace(value="new"),
            filled_qty="0",
            filled_avg_price=None,
            symbol="AAPL",
            asset_class=SimpleNamespace(value="us_equity"),
        )
    )
    monkeypatch.setattr(
        bootstrap,
        "build_broker",
        lambda *_args, **_kwargs: broker,
    )
    monkeypatch.setattr(
        bootstrap,
        "build_clock",
        lambda *_args, **_kwargs: FakeClock(is_open=True),
    )

    with pytest.raises(StartupReconciliationFailed):
        bootstrap.build_container(
            _alpaca_config(app_config),
            _migrated_secrets(tmp_path),
            runtime_role="daemon",
        )

    assert trading.submit_calls == 0
    assert trading.cancel_calls == 0


def test_create_app_builds_missing_agent_from_exact_injected_container(
    make_service,
    monkeypatch,
):
    import trading_assistant.app.main as app_main

    service = make_service()
    secrets = Secrets(
        app_api_token="exact-injected-operator-secret",
        database_url="sqlite:///must-not-be-read-from-ambient.db",
    )
    container = _injected_container(service, secrets)
    agent = _StubAgent()
    seen = []

    monkeypatch.setattr(
        app_main,
        "_build_agent",
        lambda supplied: seen.append(supplied) or agent,
    )
    monkeypatch.setattr(
        app_main,
        "build_default_stack",
        lambda: (_ for _ in ()).throw(
            AssertionError("injected container must not build a second stack")
        ),
    )

    app = create_app(
        container=container,
        planning=None,
    )

    assert seen == [container]
    assert app.state.container is container
    assert app.state.trading_service is container.service
    assert app.state.agent is agent
    assert app.state.runtime_secrets is container.secrets
    assert app.state.session_auth is container.session_auth
    assert app.state.audit is container.audit
    assert app.state.operations is container.operations
    assert app.state.rate_limiter is container.rate_limiter
    assert app.state.leases is container.leases
    assert app.state.provider_budget is container.provider_budget


def test_public_create_app_rejects_raw_startup_evidence_and_injection_is_unknown(
    make_service,
):
    from trading_assistant.operations.security_posture import (
        StartupCheckName,
        StartupDetailCode,
        StartupPostureEvidence,
        StartupStructuralCheck,
    )

    service = make_service()
    secrets = Secrets(
        app_api_token="startup-evidence-identity-secret",
    )
    evidence = StartupPostureEvidence(
        observed_at=datetime.now(timezone.utc),
        structural_checks=(
            StartupStructuralCheck(
                name=StartupCheckName.LOOPBACK_HTTPS,
                status="pass",
                detail_code=StartupDetailCode.OK,
            ),
        ),
        secret_provider="macos_keychain",
        secret_load_status="pass",
        secret_loaded_at=datetime.now(timezone.utc),
    )
    container = _injected_container(service, secrets)

    with pytest.raises(TypeError):
        create_app(
            container=container,
            agent=_StubAgent(),
            planning=None,
            startup_evidence=evidence,
        )
    app = create_app(
        container=container,
        agent=_StubAgent(),
        planning=None,
    )
    report = app.state.operations.security_posture(
        limit_principal="session:1:operator",
    )

    assert app.state.startup_evidence is None
    secret_check = next(
        check
        for check in report.checks
        if check.name.value == "secret_provider"
    )
    assert secret_check.status.value == "unknown"


def test_fabricated_startup_guard_receipt_is_rejected_before_composition(
    make_service,
    monkeypatch,
):
    from trading_assistant import bootstrap
    from trading_assistant.operations.security_posture import (
        StartupGuardReceipt,
    )

    service = make_service()
    secrets = Secrets(
        app_api_token="startup-receipt-fabrication-secret",
    )
    composed = []
    monkeypatch.setattr(
        bootstrap,
        "_build_container",
        lambda *_args, **_kwargs: composed.append("built"),
    )
    fabricated = object.__new__(StartupGuardReceipt)

    with pytest.raises(
        RuntimeError,
        match="startup_guard_receipt_invalid",
    ):
        bootstrap._build_guarded_container(
            service.config,
            secrets,
            runtime_role="app",
            startup_guard_receipt=fabricated,
        )

    assert composed == []


def test_startup_guard_receipt_rejects_a_broken_launch_chain(
    make_service,
    monkeypatch,
):
    from trading_assistant import bootstrap
    from trading_assistant.operations import security_posture as posture

    service = make_service()
    secrets = Secrets(app_api_token="startup-receipt-chain-secret")
    receipt = _issue_guard_receipt(posture, service.config, secrets)
    object.__setattr__(receipt, "_launch_chain", object())
    built: list[str] = []
    monkeypatch.setattr(
        bootstrap,
        "_build_container",
        lambda *_args, **_kwargs: built.append("built"),
    )

    with pytest.raises(
        RuntimeError,
        match="startup_guard_receipt_invalid",
    ):
        bootstrap._build_guarded_container(
            service.config,
            secrets,
            runtime_role="app",
            startup_guard_receipt=receipt,
        )

    assert built == []


def _issue_guard_receipt(posture, config, secrets, *, runtime_role="app"):
    return posture._issue_startup_guard_receipt(
        config=config,
        secrets=secrets,
        checks=(
            SimpleNamespace(
                name="runtime_configuration",
                passed=True,
                code="ok",
            ),
            SimpleNamespace(
                name="loopback_https",
                passed=True,
                code="ok",
            ),
            SimpleNamespace(name="tls", passed=True, code="ok"),
            SimpleNamespace(name="database", passed=True, code="ok"),
            SimpleNamespace(name="encryption", passed=True, code="ok"),
        ),
        observed_at=datetime.now(timezone.utc),
        secret_loaded_at=datetime.now(timezone.utc)
        - timedelta(seconds=1),
        runtime_role=runtime_role,
    )


def test_startup_guard_receipt_is_role_bound_and_wrong_role_does_not_consume(
    make_service,
    monkeypatch,
):
    from trading_assistant import bootstrap
    from trading_assistant.operations import security_posture as posture

    service = make_service()
    secrets = Secrets(app_api_token="startup-receipt-role-secret")
    receipt = _issue_guard_receipt(
        posture,
        service.config,
        secrets,
        runtime_role="app",
    )
    built: list[str] = []
    monkeypatch.setattr(
        bootstrap,
        "_build_container",
        lambda *_args, **_kwargs: built.append("built") or "container",
    )

    with pytest.raises(
        RuntimeError,
        match="startup_guard_receipt_role_mismatch",
    ):
        bootstrap._build_guarded_container(
            service.config,
            secrets,
            runtime_role="daemon",
            startup_guard_receipt=receipt,
        )
    assert bootstrap._build_guarded_container(
        service.config,
        secrets,
        runtime_role="app",
        startup_guard_receipt=receipt,
    ) == "container"
    assert built == ["built"]


def test_startup_guard_receipt_is_consumed_before_sequential_reuse(
    make_service,
    monkeypatch,
):
    from trading_assistant import bootstrap
    from trading_assistant.operations import security_posture as posture

    service = make_service()
    secrets = Secrets(app_api_token="startup-receipt-sequential-secret")
    receipt = _issue_guard_receipt(posture, service.config, secrets)
    captured: list[dict[str, object]] = []

    def fake_build(*_args, **kwargs):
        captured.append(kwargs)
        return "container"

    monkeypatch.setattr(bootstrap, "_build_container", fake_build)

    assert bootstrap._build_guarded_container(
        service.config,
        secrets,
        runtime_role="app",
        startup_guard_receipt=receipt,
    ) == "container"
    with pytest.raises(
        RuntimeError,
        match="startup_guard_receipt_consumed",
    ):
        bootstrap._build_guarded_container(
            service.config,
            secrets,
            runtime_role="app",
            startup_guard_receipt=receipt,
        )

    assert len(captured) == 1
    assert "startup_guard_receipt" not in captured[0]
    assert "_consumed_startup_guard" in captured[0]


def test_startup_guard_receipt_has_exactly_one_concurrent_consumer(
    make_service,
    monkeypatch,
):
    from trading_assistant import bootstrap
    from trading_assistant.operations import security_posture as posture

    service = make_service()
    secrets = Secrets(app_api_token="startup-receipt-concurrent-secret")
    receipt = _issue_guard_receipt(posture, service.config, secrets)
    barrier = threading.Barrier(32)
    build_calls: list[int] = []

    def fake_build(*_args, **_kwargs):
        build_calls.append(1)
        return "container"

    monkeypatch.setattr(bootstrap, "_build_container", fake_build)

    def consume(_index):
        barrier.wait()
        try:
            return bootstrap._build_guarded_container(
                service.config,
                secrets,
                runtime_role="app",
                startup_guard_receipt=receipt,
            )
        except RuntimeError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(consume, range(32)))

    assert results.count("container") == 1
    assert results.count("startup_guard_receipt_consumed") == 31
    assert build_calls == [1]


def test_failed_guarded_construction_still_consumes_receipt(
    make_service,
    monkeypatch,
):
    from trading_assistant import bootstrap
    from trading_assistant.operations import security_posture as posture

    service = make_service()
    secrets = Secrets(app_api_token="startup-receipt-failed-build-secret")
    receipt = _issue_guard_receipt(posture, service.config, secrets)
    build_calls = 0

    def fail_build(*_args, **_kwargs):
        nonlocal build_calls
        build_calls += 1
        raise RuntimeError("construction_failed")

    monkeypatch.setattr(bootstrap, "_build_container", fail_build)

    with pytest.raises(RuntimeError, match="construction_failed"):
        bootstrap._build_guarded_container(
            service.config,
            secrets,
            runtime_role="app",
            startup_guard_receipt=receipt,
        )
    with pytest.raises(
        RuntimeError,
        match="startup_guard_receipt_consumed",
    ):
        bootstrap._build_guarded_container(
            service.config,
            secrets,
            runtime_role="app",
            startup_guard_receipt=receipt,
        )

    assert build_calls == 1


def test_application_container_retains_evidence_not_reusable_receipt():
    from trading_assistant.bootstrap import ApplicationContainer

    fields = ApplicationContainer.__dataclass_fields__

    assert "startup_evidence" in fields
    assert "startup_guard_receipt" not in fields


def test_canonical_startup_receipt_is_identity_bound_and_private_app_accepts_it(
    make_service,
    monkeypatch,
):
    from trading_assistant import bootstrap
    from trading_assistant.app import main as app_main
    from trading_assistant.operations import security_posture as posture

    service = make_service()
    secrets = Secrets(
        app_api_token="startup-receipt-canonical-secret",
    )
    receipt = _issue_guard_receipt(posture, service.config, secrets)
    evidence = posture._validate_startup_guard_receipt(
        receipt,
        config=service.config,
        secrets=secrets,
        runtime_role="app",
    )
    captured = []

    def fake_build(config, loaded, **kwargs):
        captured.append((config, loaded, kwargs))
        return "guarded-container"

    monkeypatch.setattr(bootstrap, "_build_container", fake_build)
    with pytest.raises(TypeError):
        bootstrap.build_container(
            service.config,
            secrets,
            runtime_role="app",
            startup_evidence=evidence,
        )
    assert (
        bootstrap._build_guarded_container(
            service.config,
            secrets,
            runtime_role="app",
            startup_guard_receipt=receipt,
        )
        == "guarded-container"
    )
    assert captured[0][0] is service.config
    assert captured[0][1] is secrets
    assert "startup_guard_receipt" not in captured[0][2]
    context = captured[0][2]["_consumed_startup_guard"]
    assert (
        posture._validate_consumed_startup_guard(
            context,
            config=service.config,
            secrets=secrets,
            runtime_role="app",
        )
        is evidence
    )
    with pytest.raises(
        RuntimeError,
        match="startup_guard_receipt_mismatch",
    ):
        bootstrap._build_guarded_container(
            service.config.model_copy(),
            secrets,
            runtime_role="app",
            startup_guard_receipt=receipt,
        )

    container = _injected_container(service, secrets)
    container.startup_evidence = evidence
    container.operations = OperationsService(
        service,
        container.audit,
        rate_limiter=container.rate_limiter,
        provider_budget=container.provider_budget,
        _consumed_startup_guard=context,
        _startup_secrets=secrets,
        _startup_runtime_role="app",
    )
    with pytest.raises(
        RuntimeError,
        match="guarded container requires guarded app composition",
    ):
        create_app(
            container=container,
            agent=_StubAgent(),
            planning=None,
        )
    app = app_main._create_guarded_app(
        container=container,
        agent=_StubAgent(),
        planning=None,
    )
    report = app.state.operations.security_posture(
        limit_principal="session:1:operator",
    )
    secret_check = next(
        check
        for check in report.checks
        if check.name.value == "secret_provider"
    )

    assert app.state.startup_evidence is evidence
    assert secret_check.status.value == "pass"


def test_automatic_planning_and_screen_use_exact_injected_secrets(
    make_service,
    monkeypatch,
):
    from fastapi.testclient import TestClient

    from trading_assistant.analyst import analyst as analyst_module
    from trading_assistant.analyst import live_features, planning as planning_module
    from trading_assistant.analyst import screener
    from trading_assistant.llm import factory as llm_factory

    service = make_service()
    secrets = Secrets(
        app_api_token="planning-injected-operator-secret",
        alpaca_api_key="planning-injected-alpaca-key",
        alpaca_secret_key="planning-injected-alpaca-secret",
    )
    container = _injected_container(service, secrets)
    seen: list[tuple[str, object]] = []

    class StubAnalyst:
        def __init__(self, backend, **kwargs):
            seen.append(("analyst_backend", backend))
            seen.append(("analyst_max_attempts", kwargs["max_attempts"]))

    class StubPlanning:
        def __init__(self, supplied_service, analyst, provider, supplied_secrets):
            seen.extend(
                [
                    ("planning_service", supplied_service),
                    ("planning_provider", provider),
                    ("planning_secrets", supplied_secrets),
                ]
            )

    monkeypatch.setattr(
        llm_factory,
        "build_llm_backend",
        lambda config, supplied, *, provider_budget, category: seen.extend(
            [
                ("backend_secrets", supplied),
                ("backend_budget", provider_budget),
                ("backend_category", category),
            ]
        ) or object(),
    )
    monkeypatch.setattr(analyst_module, "Analyst", StubAnalyst)
    monkeypatch.setattr(planning_module, "PlanningService", StubPlanning)
    monkeypatch.setattr(
        live_features,
        "build_live_feature_provider",
        lambda config, supplied: seen.append(
            ("feature_secrets", supplied)
        )
        or object(),
    )
    monkeypatch.setattr(
        live_features,
        "build_screen_source",
        lambda symbols, supplied: seen.append(
            ("screen_secrets", supplied)
        )
        or object(),
    )
    monkeypatch.setattr(
        screener,
        "screen_source",
        lambda *args, **kwargs: [],
    )

    app = create_app(
        container=container,
        agent=_StubAgent(),
    )
    client = TestClient(app)
    login = client.post(
        "/auth/login",
        json={"secret": secrets.app_api_token.get_secret_value()},
    )
    assert login.status_code == 200
    csrf = client.get("/auth/session").json()["csrf_token"]
    response = client.post(
        "/screen",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    assert app.state.runtime_secrets is secrets
    assert app.state.planning is not None
    assert ("planning_service", service) in seen
    for label in (
        "backend_secrets",
        "feature_secrets",
        "planning_secrets",
        "screen_secrets",
    ):
        assert (label, secrets) in seen
    assert ("backend_budget", container.provider_budget) in seen
    assert ("backend_category", "analysis") in seen
    assert (
        "analyst_max_attempts",
        service.config.security.provider_budget.max_structured_attempts,
    ) in seen


def test_app_daemon_and_mcp_default_roots_pass_distinct_runtime_roles(
    app_config,
    make_service,
    monkeypatch,
):
    import trading_assistant.app.main as app_main
    import trading_assistant.daemon.main as daemon_main
    import trading_assistant.mcp_server.server as mcp_server
    from trading_assistant import bootstrap

    service = make_service()
    config = app_config.model_copy(
        update={
            "features": app_config.features.model_copy(
                update={"shadow_mode": False}
            )
        }
    )
    secrets = Secrets(app_api_token="runtime-role-secret")
    observed: list[tuple[object, object, str | None]] = []
    secret_reads = 0
    policy_container = _injected_container(service, secrets)
    container = SimpleNamespace(
        service=service,
        rule_worker=SimpleNamespace(notifier=None),
        audit=AuditRecorder(service.session_factory),
        rate_limiter=policy_container.rate_limiter,
        leases=policy_container.leases,
        provider_budget=policy_container.provider_budget,
    )

    def capture_container(*args, **kwargs):
        observed.append(
            (args[0], args[1], kwargs.get("runtime_role"))
        )
        return container

    def one_secrets(role, *, config):
        nonlocal secret_reads
        secret_reads += 1
        assert role in {"app", "daemon", "mcp"}
        assert config is not None
        return secrets

    monkeypatch.setattr(bootstrap, "build_container", capture_container)
    monkeypatch.setattr(app_main, "load_config", lambda: config)
    monkeypatch.setattr(app_main, "load_role_secrets", one_secrets)
    monkeypatch.setattr(daemon_main, "load_config", lambda: config)
    monkeypatch.setattr(daemon_main, "load_role_secrets", one_secrets)
    monkeypatch.setattr(
        daemon_main,
        "build_notifier",
        lambda supplied_config, supplied_secrets, **_kwargs: object(),
    )
    monkeypatch.setattr(mcp_server, "load_config", lambda: config)
    monkeypatch.setattr(mcp_server, "load_role_secrets", one_secrets)

    assert app_main.build_default_container() is container
    assert daemon_main.build_monitor().service is service
    assert mcp_server.build_default_container() is container
    assert secret_reads == 3
    assert observed == [
        (config, secrets, "app"),
        (config, secrets, "daemon"),
        (config, secrets, "mcp"),
    ]


def test_mcp_normal_exit_fails_closed_when_exact_release_is_uncertain(
    monkeypatch,
):
    import trading_assistant.logging as runtime_logging
    import trading_assistant.mcp_server.server as mcp_server

    class Guard:
        lost = False

        def set_on_lost(self, callback):
            self.callback = callback

        def close(self):
            # Match RuntimeTenureGuard.close(): uncertain release latches loss.
            self.lost = True
            return False

    async def run_stdio_async():
        return None

    guard = Guard()
    container = SimpleNamespace(
        secrets=Secrets(app_api_token="mcp-lifecycle-secret"),
        service=object(),
        audit=object(),
        runtime_tenure_guard=guard,
    )
    monkeypatch.setattr(
        mcp_server,
        "build_default_container",
        lambda: container,
    )
    monkeypatch.setattr(
        mcp_server,
        "mcp",
        SimpleNamespace(run_stdio_async=run_stdio_async),
    )
    monkeypatch.setattr(
        runtime_logging,
        "runtime_startup",
        lambda *_args, **_kwargs: nullcontext(),
    )

    with pytest.raises(TenureUncertain) as exc:
        mcp_server.main()

    assert exc.value.stable_code == "runtime_tenure_uncertain"


def test_mcp_server_failure_preserves_primary_error_during_uncertain_cleanup(
    monkeypatch,
):
    import trading_assistant.logging as runtime_logging
    import trading_assistant.mcp_server.server as mcp_server

    class Guard:
        lost = False
        close_calls = 0

        def set_on_lost(self, callback):
            self.callback = callback

        def close(self):
            self.close_calls += 1
            self.lost = True
            return False

    async def run_stdio_async():
        raise RuntimeError("mcp-server-failed")

    guard = Guard()
    container = SimpleNamespace(
        secrets=Secrets(app_api_token="mcp-primary-error-secret"),
        service=object(),
        audit=object(),
        runtime_tenure_guard=guard,
    )
    monkeypatch.setattr(
        mcp_server,
        "build_default_container",
        lambda: container,
    )
    monkeypatch.setattr(
        mcp_server,
        "mcp",
        SimpleNamespace(run_stdio_async=run_stdio_async),
    )
    monkeypatch.setattr(
        runtime_logging,
        "runtime_startup",
        lambda *_args, **_kwargs: nullcontext(),
    )

    with pytest.raises(RuntimeError, match="mcp-server-failed"):
        mcp_server.main()

    assert guard.close_calls == 1


def test_mcp_tenure_loss_cancels_server_and_attempts_exact_cleanup(
    monkeypatch,
):
    import trading_assistant.logging as runtime_logging
    import trading_assistant.mcp_server.server as mcp_server
    from trading_assistant.ops.tenure import TenureLost

    class Guard:
        lost = False
        close_calls = 0

        def set_on_lost(self, callback):
            self.callback = callback

        def lose(self):
            self.lost = True
            self.callback()

        def close(self):
            self.close_calls += 1
            return False

    cancelled: list[bool] = []
    guard = Guard()

    async def run_stdio_async():
        import asyncio

        guard.lose()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    container = SimpleNamespace(
        secrets=Secrets(app_api_token="mcp-loss-secret"),
        service=object(),
        audit=object(),
        runtime_tenure_guard=guard,
    )
    monkeypatch.setattr(
        mcp_server,
        "build_default_container",
        lambda: container,
    )
    monkeypatch.setattr(
        mcp_server,
        "mcp",
        SimpleNamespace(run_stdio_async=run_stdio_async),
    )
    monkeypatch.setattr(
        runtime_logging,
        "runtime_startup",
        lambda *_args, **_kwargs: nullcontext(),
    )

    with pytest.raises(TenureLost):
        mcp_server.main()

    assert cancelled == [True]
    assert guard.close_calls == 1


def test_application_container_reuses_exact_trading_service_components(
    tmp_path,
    app_config,
):
    from trading_assistant.bootstrap import build_test_container

    container = build_test_container(
        _alpaca_config(app_config),
        _migrated_secrets(tmp_path),
        broker=MockBroker(),
        clock=FakeClock(is_open=True),
    )

    assert container.snapshot_service is container.service.snapshot_service
    assert container.order_application is container.service.order_application
    assert container.order_submission is container.service.order_submission
    assert container.reconciliation is container.service.reconciliation
    assert container.breakers is container.service.breakers
    assert container.rule_worker.service is container.service
    assert container.rule_worker.repository is container.service.rule_repository
    assert container.session_auth.session_factory is container.session_factory


def test_application_container_shares_exact_policy_services_and_config(
    tmp_path,
    app_config,
):
    from trading_assistant.app.limits import (
        ConcurrencyLeaseService,
        DurableRateLimiter,
    )
    from trading_assistant.bootstrap import build_test_container
    from trading_assistant.llm.budget import ProviderBudgetService

    container = build_test_container(
        _alpaca_config(app_config),
        _migrated_secrets(tmp_path),
        broker=MockBroker(),
        clock=FakeClock(is_open=True),
    )
    configured = app_config.security.provider_budget

    assert isinstance(container.rate_limiter, DurableRateLimiter)
    assert isinstance(container.leases, ConcurrencyLeaseService)
    assert isinstance(container.provider_budget, ProviderBudgetService)
    assert container.rate_limiter.session_factory is container.session_factory
    assert container.leases.session_factory is container.session_factory
    assert (
        container.provider_budget.session_factory
        is container.session_factory
    )
    assert container.provider_budget.limits == BudgetLimits(
        calls=configured.daily_calls,
        input_tokens=configured.daily_input_tokens,
        output_tokens=configured.daily_output_tokens,
        reservation_ttl_seconds=configured.reservation_ttl_seconds,
    )
    price_status = container.provider_budget.status(
        app_config.llm.provider,
        model=app_config.llm.gemini_model,
    )
    assert price_status.price_model == app_config.llm.gemini_model
    assert container.operations.rate_limiter is container.rate_limiter
    assert container.operations.leases is container.leases
    assert container.operations.provider_budget is container.provider_budget
    assert container.rule_worker.rate_limiter is container.rate_limiter
    assert container.rule_worker.leases is container.leases
    assert (
        container.rule_worker.provider_budget
        is container.provider_budget
    )


def test_container_runs_bounded_startup_pruning_and_exposes_posture(
    tmp_path,
    app_config,
    monkeypatch,
):
    from trading_assistant import bootstrap

    calls: list[tuple[str, int, object]] = []

    class RecordingRateLimiter(DurableRateLimiter):
        def prune_expired(self, now, limit=500):
            calls.append(("rate_windows", limit, now))
            return 3

    class RecordingLeases(ConcurrencyLeaseService):
        def prune_expired(self, now, limit=500):
            calls.append(("leases", limit, now))
            return 2

    monkeypatch.setattr(
        bootstrap,
        "DurableRateLimiter",
        RecordingRateLimiter,
    )
    monkeypatch.setattr(
        bootstrap,
        "ConcurrencyLeaseService",
        RecordingLeases,
    )

    container = bootstrap.build_test_container(
        _alpaca_config(app_config),
        _migrated_secrets(tmp_path),
        broker=MockBroker(),
        clock=FakeClock(is_open=True),
    )

    assert [call[:2] for call in calls] == [
        ("rate_windows", 500),
        ("leases", 500),
    ]
    assert calls[0][2] == calls[1][2]
    posture = container.policy_store_maintenance.posture().as_dict()
    assert posture["status"] == "current"
    assert posture["source"] == "startup"
    assert posture["limit"] == 500
    assert posture["rate_windows_deleted"] == 3
    assert posture["leases_deleted"] == 2
    assert posture["failed_stores"] == []
    assert (
        container.operations.health().as_dict()[
            "policy_store_pruning"
        ]
        == posture
    )


def test_startup_pruning_failure_is_observable_without_policy_fallback(
    tmp_path,
    app_config,
    monkeypatch,
):
    from trading_assistant import bootstrap

    class UnavailableRatePruning(DurableRateLimiter):
        def prune_expired(self, now, limit=500):
            raise LimitStoreUnavailable("private prune failure")

    monkeypatch.setattr(
        bootstrap,
        "DurableRateLimiter",
        UnavailableRatePruning,
    )
    container = bootstrap.build_test_container(
        _alpaca_config(app_config),
        _migrated_secrets(tmp_path),
        broker=MockBroker(),
        clock=FakeClock(is_open=True),
    )

    posture = container.policy_store_maintenance.posture().as_dict()
    assert posture["status"] == "degraded"
    assert posture["failed_stores"] == ["rate_windows"]
    assert "private prune failure" not in str(posture)

    spec = LimitSpec(
        "startup-prune-failure",
        principal_requests=1,
        global_requests=1,
        window_seconds=60,
    )
    first = container.rate_limiter.consume_pair(
        spec,
        principal="operator:test",
    )
    second = container.rate_limiter.consume_pair(
        spec,
        principal="operator:test",
    )
    assert first.allowed is True
    assert second.allowed is False


def test_container_constructs_each_policy_service_once_and_app_reuses_it(
    tmp_path,
    app_config,
    monkeypatch,
):
    from trading_assistant import bootstrap

    constructions = {
        "rate_limiter": 0,
        "leases": 0,
        "provider_budget": 0,
    }

    class CountingRateLimiter(DurableRateLimiter):
        def __init__(self, session_factory):
            constructions["rate_limiter"] += 1
            super().__init__(session_factory)

    class CountingLeases(ConcurrencyLeaseService):
        def __init__(self, session_factory):
            constructions["leases"] += 1
            super().__init__(session_factory)

    class CountingProviderBudget(ProviderBudgetService):
        def __init__(self, *args, **kwargs):
            constructions["provider_budget"] += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(
        bootstrap,
        "DurableRateLimiter",
        CountingRateLimiter,
    )
    monkeypatch.setattr(
        bootstrap,
        "ConcurrencyLeaseService",
        CountingLeases,
    )
    monkeypatch.setattr(
        bootstrap,
        "ProviderBudgetService",
        CountingProviderBudget,
    )
    container = bootstrap.build_test_container(
        _alpaca_config(app_config),
        _migrated_secrets(tmp_path),
        broker=MockBroker(),
        clock=FakeClock(is_open=True),
    )

    app = create_app(
        container=container,
        agent=_StubAgent(),
        planning=None,
    )

    assert constructions == {
        "rate_limiter": 1,
        "leases": 1,
        "provider_budget": 1,
    }
    assert app.state.rate_limiter is container.rate_limiter
    assert app.state.leases is container.leases
    assert app.state.provider_budget is container.provider_budget


def test_app_agent_uses_shared_chat_budget_and_configured_turn_ceiling(
    make_service,
    monkeypatch,
):
    import trading_assistant.app.main as app_main
    from trading_assistant.llm import factory as llm_factory

    service = make_service()
    container = _injected_container(
        service,
        Secrets(app_api_token="agent-budget-secret"),
    )
    backend = object()
    seen = []
    monkeypatch.setattr(
        llm_factory,
        "build_llm_backend",
        lambda config, secrets, *, provider_budget, category: seen.append(
            (config, secrets, provider_budget, category)
        ) or backend,
    )

    agent = app_main._build_agent(container)

    assert agent.backend is backend
    assert agent.max_turns == (
        service.config.security.provider_budget.max_chat_tool_turns
    )
    assert seen == [
        (
            service.config,
            container.secrets,
            container.provider_budget,
            "chat",
        )
    ]


def test_automatic_planning_requires_shared_container_before_backend_build(
    make_service,
    monkeypatch,
):
    from trading_assistant.llm import factory as llm_factory

    constructed = []
    monkeypatch.setattr(
        llm_factory,
        "build_llm_backend",
        lambda *_args, **_kwargs: constructed.append(True) or object(),
    )
    secrets = Secrets(app_api_token="no-container-budget-secret")

    with pytest.raises(RuntimeError, match="shared ApplicationContainer"):
        create_app(
            service=make_service(),
            agent=_StubAgent(),
            runtime_secrets=secrets,
            api_token=secrets.app_api_token,
        )

    assert constructed == []


def test_daemon_shadow_uses_shared_analysis_budget_and_attempt_ceiling(
    app_config,
    make_service,
    monkeypatch,
):
    import trading_assistant.daemon.main as daemon_main
    from trading_assistant import bootstrap
    from trading_assistant.analyst import analyst as analyst_module
    from trading_assistant.analyst import live_features
    from trading_assistant.analyst import planning as planning_module
    from trading_assistant.llm import factory as llm_factory

    service = make_service()
    config = app_config.model_copy(
        update={
            "features": app_config.features.model_copy(
                update={"shadow_mode": True}
            )
        }
    )
    secrets = Secrets(app_api_token="daemon-budget-secret")
    container = _injected_container(service, secrets)
    container.rule_worker = SimpleNamespace(notifier=None)
    seen = []

    class StubAnalyst:
        def __init__(self, backend, **kwargs):
            seen.extend(
                [
                    ("analyst_backend", backend),
                    ("analyst_max_attempts", kwargs["max_attempts"]),
                ]
            )

    class StubPlanning:
        def __init__(self, *args):
            seen.append(("planning_args", args))

    monkeypatch.setattr(
        bootstrap,
        "build_container",
        lambda *_args, **_kwargs: container,
    )
    monkeypatch.setattr(
        daemon_main,
        "build_notifier",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        llm_factory,
        "build_llm_backend",
        lambda cfg, supplied, *, provider_budget, category, runtime_role: seen.append(
            (
                "backend",
                cfg,
                supplied,
                provider_budget,
                category,
            )
        ) or object(),
    )
    monkeypatch.setattr(analyst_module, "Analyst", StubAnalyst)
    monkeypatch.setattr(planning_module, "PlanningService", StubPlanning)
    monkeypatch.setattr(
        live_features,
        "build_live_feature_provider",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        live_features,
        "build_screen_source",
        lambda *_args, **_kwargs: object(),
    )

    monitor = daemon_main._build_monitor(config, secrets)

    assert (
        "backend",
        config,
        secrets,
        container.provider_budget,
        "analysis",
    ) in seen
    assert (
        "analyst_max_attempts",
        config.security.provider_budget.max_structured_attempts,
    ) in seen
    assert monitor.rate_limiter is container.rate_limiter
    assert monitor.leases is container.leases
    assert monitor.provider_budget is container.provider_budget


def test_validation_analyst_default_off_uses_disabled_backtest_without_construction(
    app_config,
    session_factory,
    monkeypatch,
    patch_selected_llm_backend,
):
    import trading_assistant.validate_analyst as validation
    from trading_assistant.llm import factory
    from trading_assistant.llm.budget import ProviderBudgetExceeded

    secrets = Secrets(app_api_token="validation-budget-secret")
    provider_budget = ProviderBudgetService(
        session_factory,
        BudgetLimits(
            calls=100,
            input_tokens=1_000_000,
            output_tokens=200_000,
        ),
        prices=app_config.security.provider_budget.prices,
    )
    estimator_calls = []
    raw_calls = []
    monkeypatch.setattr(
        factory,
        "resolve_input_estimator",
        lambda provider: estimator_calls.append(provider) or object(),
    )
    patch_selected_llm_backend(
        app_config,
        lambda *_args, **_kwargs: (
            raw_calls.append(app_config.llm.provider) or object()
        ),
    )

    analyst = validation._build_analyst(
        app_config,
        secrets,
        provider_budget,
    )

    assert analyst.max_attempts == (
        app_config.security.provider_budget.max_structured_attempts
    )
    assert not hasattr(analyst.backend, "delegate")
    with pytest.raises(ProviderBudgetExceeded, match="disabled"):
        analyst.backend.create(
            system="system",
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            request_id="validation-disabled",
        )
    assert estimator_calls == []
    assert raw_calls == []


def test_validation_analyst_enabled_wraps_backtest_exactly_once(
    app_config,
    session_factory,
    patch_selected_llm_backend,
):
    import trading_assistant.validate_analyst as validation
    from trading_assistant.llm.base import BudgetedLLMBackend

    configured = app_config.security.provider_budget
    enabled_budget_config = configured.model_copy(
        update={"backtest_llm_enabled": True}
    )
    enabled_config = app_config.model_copy(
        update={
            "security": app_config.security.model_copy(
                update={"provider_budget": enabled_budget_config}
            )
        }
    )
    provider_budget = ProviderBudgetService(
        session_factory,
        BudgetLimits(
            calls=configured.daily_calls,
            input_tokens=configured.daily_input_tokens,
            output_tokens=configured.daily_output_tokens,
        ),
        prices=configured.prices,
    )
    raw_backend = object()
    raw_calls = []
    patch_selected_llm_backend(
        enabled_config,
        lambda *_args, **_kwargs: (
            raw_calls.append(enabled_config.llm.provider) or raw_backend
        ),
    )

    analyst = validation._build_analyst(
        enabled_config,
        Secrets(app_api_token="validation-budget-secret"),
        provider_budget,
    )

    assert isinstance(analyst.backend, BudgetedLLMBackend)
    assert not hasattr(analyst.backend, "delegate")
    assert not hasattr(analyst.backend, "_delegate")
    assert analyst.backend.budgets is provider_budget
    assert analyst.backend.category == "backtest"
    assert raw_calls == [enabled_config.llm.provider]


def test_validation_acquires_writer_tenure_before_provider_and_closes(
    app_config,
    session_factory,
    monkeypatch,
):
    from trading_assistant import bootstrap
    import trading_assistant.validate_analyst as validation

    timeline = [
        datetime(2022, 1, 1, tzinfo=timezone.utc),
        datetime(2022, 12, 31, tzinfo=timezone.utc),
    ]
    events: list[str] = []

    class Source:
        def timeline(self, _symbols):
            return timeline

    class Guard:
        def split(self, _timeline):
            return [], timeline

    class Tenure:
        def ensure_owned(self):
            events.append("ensure")

        def close(self):
            events.append("close")
            return True

    runtime = SimpleNamespace(
        engine=session_factory.kw["bind"],
        session_factory=session_factory,
    )
    monkeypatch.setattr(validation, "load_config", lambda _path: app_config)
    monkeypatch.setattr(
        validation,
        "load_role_secrets",
        lambda *_args, **_kwargs: Secrets(
            app_api_token="validation-tenure-secret"
        ),
    )
    monkeypatch.setattr(validation, "load_parquet", lambda _path: object())
    monkeypatch.setattr(validation, "DataSource", lambda _frames: Source())
    monkeypatch.setattr(
        validation,
        "HoldoutGuard",
        lambda *_args, **_kwargs: Guard(),
    )
    monkeypatch.setattr(
        validation,
        "estimate_llm_calls",
        lambda *_args, **_kwargs: {"estimated_calls": 0},
    )
    monkeypatch.setattr(
        bootstrap,
        "prepare_database_runtime",
        lambda *_args, **_kwargs: (
            events.append("database") or runtime
        ),
    )
    monkeypatch.setattr(
        bootstrap,
        "acquire_runtime_guard",
        lambda supplied, role, **_kwargs: (
            events.append(f"tenure:{role}") or Tenure()
        ),
        raising=False,
    )

    def build_budget(*_args, **_kwargs):
        assert "tenure:validation" in events
        events.append("provider-budget")
        return object()

    def build_analyst(*_args, **_kwargs):
        assert "tenure:validation" in events
        events.append("provider")
        return object()

    monkeypatch.setattr(
        bootstrap,
        "build_provider_budget_service",
        build_budget,
    )
    monkeypatch.setattr(validation, "_build_analyst", build_analyst)
    monkeypatch.setattr(
        validation,
        "analyst_accuracy",
        lambda *_args, **_kwargs: {
            "verdict": "validation complete"
        },
    )

    assert validation.run(["--symbols", "AAPL", "--yes"]) == 0
    assert events.index("tenure:validation") < events.index(
        "provider-budget"
    )
    assert events.index("tenure:validation") < events.index("provider")
    assert events[-1] == "close"


def _capture_validation_runs(
    *,
    monkeypatch,
    session_factory,
    configs,
    timelines,
    argvs,
    run_configs=None,
):
    from trading_assistant import bootstrap
    import trading_assistant.validate_analyst as validation

    config_iter = iter(configs)
    timeline_iter = iter(timelines)

    class StubSource:
        def __init__(self, timeline):
            self._timeline = timeline

        def timeline(self, _symbols):
            return self._timeline

    class StubGuard:
        def __init__(self, timeline, *, holdout_months):
            assert holdout_months == 12
            self._timeline = timeline

        def split(self, _timeline):
            return [], self._timeline

    runtime = SimpleNamespace(
        engine=session_factory.kw["bind"],
        session_factory=session_factory,
    )
    monkeypatch.setattr(
        validation,
        "load_config",
        lambda _path: next(config_iter),
    )
    monkeypatch.setattr(
        validation,
        "load_role_secrets",
        lambda role, *, config: (
            Secrets(app_api_token="validation-run-secret")
            if role == "validate-analyst"
            else (_ for _ in ()).throw(
                AssertionError("unexpected validation role")
            )
        ),
        raising=False,
    )
    monkeypatch.setattr(validation, "load_parquet", lambda _path: object())
    monkeypatch.setattr(
        validation,
        "DataSource",
        lambda _frames: StubSource(next(timeline_iter)),
    )
    monkeypatch.setattr(validation, "HoldoutGuard", StubGuard)
    monkeypatch.setattr(
        validation,
        "estimate_llm_calls",
        lambda *_args, **_kwargs: {"estimated_calls": 0},
    )
    monkeypatch.setattr(
        bootstrap,
        "prepare_database_runtime",
        lambda *_args, **_kwargs: runtime,
    )
    monkeypatch.setattr(
        bootstrap,
        "acquire_runtime_guard",
        lambda *_args, **_kwargs: SimpleNamespace(
            ensure_owned=lambda: None,
            close=lambda: True,
        ),
    )
    monkeypatch.setattr(
        bootstrap,
        "build_provider_budget_service",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        validation,
        "_build_analyst",
        lambda *_args, **_kwargs: object(),
    )
    if run_configs is not None:
        run_config_iter = iter(run_configs)
        monkeypatch.setattr(
            validation,
            "LLMRunConfig",
            lambda **_kwargs: next(run_config_iter),
        )

    captured = []

    def accuracy(_source, symbols, _analyst, run_config, **kwargs):
        captured.append(
            {
                "symbols": symbols,
                "run_config": run_config,
                **kwargs,
            }
        )
        return {"verdict": "validation complete"}

    monkeypatch.setattr(validation, "analyst_accuracy", accuracy)
    for argv in argvs:
        assert validation.run(argv) == 0
    return captured


def test_validation_run_canonicalizes_equivalent_runtime_identity(
    app_config,
    session_factory,
    monkeypatch,
):
    from datetime import datetime, timedelta, timezone

    first_config = app_config.model_copy(
        update={
            "analyst": app_config.analyst.model_copy(
                update={"version": " V2 "}
            )
        }
    )
    equivalent_config = app_config.model_copy(
        update={
            "analyst": app_config.analyst.model_copy(
                update={"version": "v2"}
            )
        }
    )
    pacific = timezone(-timedelta(hours=7))
    timelines = [
        [
            datetime(2022, 1, 1, 1, 2, 3, 456789, tzinfo=pacific),
            datetime(2022, 12, 31, 1, 2, 3, 456789, tzinfo=pacific),
        ],
        [
            datetime(2022, 1, 1, 8, 2, 3, 456789, tzinfo=timezone.utc),
            datetime(2022, 12, 31, 8, 2, 3, 456789, tzinfo=timezone.utc),
        ],
    ]

    captured = _capture_validation_runs(
        monkeypatch=monkeypatch,
        session_factory=session_factory,
        configs=[first_config, equivalent_config],
        timelines=timelines,
        argvs=[
            ["--symbols", " aapl , msft ", "--yes"],
            ["--symbols", "MSFT,AAPL", "--yes"],
        ],
    )

    assert captured[0]["run_id"] == captured[1]["run_id"]
    assert captured[0]["run_id"].startswith("validation:")
    assert len(captured[0]["run_id"]) <= 64
    assert captured[0]["symbols"] == captured[1]["symbols"] == [
        "AAPL",
        "MSFT",
    ]
    assert captured[0]["start"] == captured[1]["start"]
    assert captured[0]["end"] == captured[1]["end"]
    assert captured[0]["start"].tzinfo == timezone.utc
    assert captured[0]["end"].tzinfo == timezone.utc


def test_validation_run_identity_distinguishes_logical_runtime_changes(
    app_config,
    session_factory,
    monkeypatch,
):
    from datetime import datetime, timedelta, timezone

    baseline = [
        datetime(2022, 1, 1, tzinfo=timezone.utc),
        datetime(2022, 12, 31, tzinfo=timezone.utc),
    ]
    changed_time = [baseline[0], baseline[1] + timedelta(microseconds=1)]
    changed_version = app_config.model_copy(
        update={
            "analyst": app_config.analyst.model_copy(
                update={"version": "v3"}
            )
        }
    )
    captured = _capture_validation_runs(
        monkeypatch=monkeypatch,
        session_factory=session_factory,
        configs=[
            app_config,
            app_config,
            changed_version,
            app_config,
        ],
        timelines=[baseline, baseline, baseline, changed_time],
        argvs=[
            ["--symbols", "AAPL", "--yes"],
            ["--symbols", "MSFT", "--yes"],
            ["--symbols", "AAPL", "--yes"],
            ["--symbols", "AAPL", "--yes"],
        ],
    )

    identities = [item["run_id"] for item in captured]
    assert len(set(identities)) == len(identities)


def test_validation_run_rejects_naive_holdout_before_runtime_construction(
    app_config,
    session_factory,
    monkeypatch,
):
    from datetime import datetime

    with pytest.raises(ValueError, match="timezone-aware"):
        _capture_validation_runs(
            monkeypatch=monkeypatch,
            session_factory=session_factory,
            configs=[app_config],
            timelines=[
                [
                    datetime(2022, 1, 1),
                    datetime(2022, 12, 31),
                ]
            ],
            argvs=[["--symbols", "AAPL", "--yes"]],
        )


@pytest.mark.parametrize(
    ("symbols", "version"),
    [
        ("AAPL X", "v2"),
        ("AAPL\nX", "v2"),
        ("A" * 17, "v2"),
        ("é", "v2"),
        ("e\u0301", "v2"),
        ("AAPL", "version two"),
        ("AAPL", "v" * 17),
        ("AAPL", "vérsion"),
        ("AAPL", "ve\u0301rsion"),
    ],
)
def test_validation_run_rejects_invalid_canonical_provenance(
    app_config,
    session_factory,
    monkeypatch,
    symbols,
    version,
):
    from datetime import datetime, timezone

    configured = app_config.model_copy(
        update={
            "analyst": app_config.analyst.model_copy(
                update={"version": version}
            )
        }
    )
    timeline = [
        datetime(2022, 1, 1, tzinfo=timezone.utc),
        datetime(2022, 12, 31, tzinfo=timezone.utc),
    ]

    with pytest.raises(ValueError):
        _capture_validation_runs(
            monkeypatch=monkeypatch,
            session_factory=session_factory,
            configs=[configured],
            timelines=[timeline],
            argvs=[["--symbols", symbols, "--yes"]],
        )


def test_validation_run_identity_uses_actual_provider_and_selected_model(
    app_config,
    session_factory,
    monkeypatch,
):
    from datetime import datetime, timezone

    timeline = [
        datetime(2022, 1, 1, tzinfo=timezone.utc),
        datetime(2022, 12, 31, tzinfo=timezone.utc),
    ]
    changed_actual_model = app_config.model_copy(
        update={
            "llm": app_config.llm.model_copy(
                update={"gemini_model": "gemini-review-model"}
            )
        }
    )
    changed_provider = app_config.model_copy(
        update={
            "llm": app_config.llm.model_copy(
                update={"provider": "anthropic"}
            )
        }
    )
    changed_inactive_model = app_config.model_copy(
        update={
            "llm": app_config.llm.model_copy(
                update={"model": "inactive-while-gemini-selected"}
            )
        }
    )
    captured = _capture_validation_runs(
        monkeypatch=monkeypatch,
        session_factory=session_factory,
        configs=[
            app_config,
            changed_actual_model,
            changed_provider,
            changed_inactive_model,
        ],
        timelines=[timeline] * 4,
        argvs=[["--symbols", "AAPL,MSFT", "--yes"]] * 4,
    )

    baseline = captured[0]["run_id"]
    assert captured[1]["run_id"] != baseline
    assert captured[2]["run_id"] != baseline
    assert captured[3]["run_id"] == baseline


def test_validation_run_identity_ignores_unused_model_placeholders(
    app_config,
    session_factory,
    monkeypatch,
):
    from datetime import datetime, timezone

    from trading_assistant.backtest.llm_runner import LLMRunConfig

    timeline = [
        datetime(2022, 1, 1, tzinfo=timezone.utc),
        datetime(2022, 12, 31, tzinfo=timezone.utc),
    ]
    captured = _capture_validation_runs(
        monkeypatch=monkeypatch,
        session_factory=session_factory,
        configs=[app_config, app_config],
        timelines=[timeline, timeline],
        argvs=[["--symbols", "AAPL,MSFT", "--yes"]] * 2,
        run_configs=[
            LLMRunConfig(max_llm_calls=25, horizon_bars=7),
            LLMRunConfig(
                max_llm_calls=25,
                horizon_bars=7,
                cheap_model="unused-cheap-placeholder",
                full_model="unused-full-placeholder",
            ),
        ],
    )

    assert captured[0]["run_id"] == captured[1]["run_id"]


@pytest.mark.parametrize(
    ("config_update", "message"),
    [
        (
            lambda cfg: {
                "trading": cfg.trading.model_copy(
                    update={
                        "mode": TradingMode.LIVE,
                        "broker": BrokerKind.ALPACA,
                    }
                )
            },
            "live trading is locked out",
        ),
        (
            lambda cfg: {
                "features": cfg.features.model_copy(
                    update={"auto_execute_preapproved_rules": True}
                )
            },
            "auto-execution",
        ),
        (
            lambda cfg: {
                "execution": cfg.execution.model_copy(
                    update={"prefer_bracket_orders": True}
                )
            },
            "automatic bracket",
        ),
        (
            lambda cfg: {
                "llm": cfg.llm.model_copy(
                    update={"fallback_provider": "groq"}
                )
            },
            "cross-provider",
        ),
    ],
)
def test_bootstrap_rejects_every_dangerous_runtime_switch(
    tmp_path,
    app_config,
    config_update,
    message,
):
    from trading_assistant.bootstrap import build_container

    safe = _alpaca_config(app_config)
    unsafe = safe.model_copy(update=config_update(safe))

    with pytest.raises(RuntimeError, match=message):
        build_container(unsafe, _migrated_secrets(tmp_path))


def test_bootstrap_rejects_outdated_schema_before_provider_construction(
    tmp_path,
    app_config,
    monkeypatch,
):
    from trading_assistant import bootstrap

    database_url = f"sqlite:///{tmp_path}/outdated.db"
    create_db_engine(database_url)
    broker_built = False

    def forbidden_broker(*_args, **_kwargs):
        nonlocal broker_built
        broker_built = True
        raise AssertionError("provider construction must follow schema gate")

    monkeypatch.setattr(bootstrap, "build_broker", forbidden_broker)

    with pytest.raises(SchemaOutOfDate):
        bootstrap.build_container(
            _alpaca_config(app_config),
            Secrets(
                database_url=database_url,
                app_api_token="operator-secret-for-bootstrap-tests",
            ),
        )

    assert broker_built is False


def test_heartbeat_upserts_one_row_per_source(make_service):
    from sqlalchemy import select

    from trading_assistant.db.models import Heartbeat

    service = make_service()
    for _ in range(5):
        service.write_heartbeat("daemon")
    service.write_heartbeat("app")

    with service.session_factory() as session:
        daemon = session.scalars(
            select(Heartbeat).where(Heartbeat.source == "daemon")
        ).all()
        app = session.scalars(
            select(Heartbeat).where(Heartbeat.source == "app")
        ).all()

    assert len(daemon) == 1
    assert len(app) == 1


class _UnknownProcessInspector:
    def inspect(self, _identity):
        return ProcessProof.UNKNOWN


def test_active_maintenance_blocks_bootstrap_before_broker_construction(
    tmp_path,
    app_config,
    monkeypatch,
):
    from trading_assistant import bootstrap

    secrets = _migrated_secrets(tmp_path)
    engine = create_db_engine(secrets.database_url)
    factory = make_session_factory(engine)
    inspector = _UnknownProcessInspector()
    maintenance = RuntimeTenureService(
        factory,
        process_inspector=inspector,
    ).acquire_maintenance(
        ProcessIdentity(5101, "maintenance-start"),
        ttl_seconds=30,
    )
    broker_built = False

    def forbidden_broker(*_args, **_kwargs):
        nonlocal broker_built
        broker_built = True
        raise AssertionError("broker construction must follow runtime tenure")

    monkeypatch.setattr(bootstrap, "build_broker", forbidden_broker)

    with pytest.raises(TenureUnavailable) as exc:
        bootstrap.build_container(
            _alpaca_config(app_config),
            secrets,
            runtime_role="app",
            process_identity=ProcessIdentity(5102, "app-start"),
            process_inspector=inspector,
        )

    assert exc.value.stable_code == "maintenance_tenure_active"
    assert broker_built is False
    assert maintenance.release() is True


def test_plaintext_sensitive_field_blocks_before_broker_construction(
    tmp_path,
    app_config,
    monkeypatch,
):
    from trading_assistant import bootstrap

    secrets = _migrated_secrets(tmp_path)
    engine = create_db_engine(secrets.database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO risk_events (event_type,reason,created_at) "
                "VALUES ('rejection','legacy-plaintext',CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "UPDATE sensitive_migration_state "
                "SET rows_total=1,rows_completed=1"
            )
        )
    broker_built = False

    def forbidden_broker(*_args, **_kwargs):
        nonlocal broker_built
        broker_built = True
        raise AssertionError("broker must follow full crypto scan")

    monkeypatch.setattr(bootstrap, "build_broker", forbidden_broker)

    with pytest.raises(bootstrap.StartupEncryptionBlocked) as exc:
        bootstrap.build_container(
            _alpaca_config(app_config),
            secrets,
            runtime_role="app",
            process_identity=ProcessIdentity(5110, "app-start"),
            process_inspector=_UnknownProcessInspector(),
        )

    assert exc.value.stable_code == "sensitive_plaintext_detected"
    assert "legacy-plaintext" not in str(exc.value)
    assert broker_built is False


def test_broker_constructor_observes_runtime_tenure_and_failure_releases_it(
    tmp_path,
    app_config,
    monkeypatch,
):
    from sqlalchemy import select

    from trading_assistant import bootstrap
    from trading_assistant.db.models import RuntimeTenure

    secrets = _migrated_secrets(tmp_path)
    engine = create_db_engine(secrets.database_url)
    factory = make_session_factory(engine)
    inspector = _UnknownProcessInspector()
    guard_started = False
    real_guard = bootstrap.RuntimeTenureGuard

    class ObservedGuard(real_guard):
        def start(self):
            nonlocal guard_started
            guard_started = True
            super().start()

    monkeypatch.setattr(bootstrap, "RuntimeTenureGuard", ObservedGuard)

    class ProviderProbe(RuntimeError):
        pass

    def observe_then_fail(*_args, **_kwargs):
        assert guard_started is True
        with factory() as session:
            row = session.scalar(
                select(RuntimeTenure).where(
                    RuntimeTenure.resource_key == "runtime:daemon"
                )
            )
            assert row is not None
            assert row.state == "held"
            assert row.role == "daemon"
        raise ProviderProbe("provider-probe")

    monkeypatch.setattr(bootstrap, "build_broker", observe_then_fail)

    with pytest.raises(ProviderProbe, match="provider-probe"):
        bootstrap.build_container(
            _alpaca_config(app_config),
            secrets,
            runtime_role="daemon",
            process_identity=ProcessIdentity(5103, "daemon-start"),
            process_inspector=inspector,
        )

    with factory() as session:
        row = session.scalar(
            select(RuntimeTenure).where(
                RuntimeTenure.resource_key == "runtime:daemon"
            )
        )
        assert row is not None
        assert row.state == "released"


@pytest.mark.parametrize("failure_stage", ["start", "barrier"])
def test_runtime_guard_setup_failure_closes_acquired_tenure(
    session_factory,
    monkeypatch,
    failure_stage,
):
    from trading_assistant import bootstrap

    events: list[str] = []

    class Handle:
        role = "app"

    class TenureService:
        def __init__(self, *_args, **_kwargs):
            pass

        def acquire_runtime(self, role, _identity, *, ttl_seconds):
            assert role == "app"
            assert ttl_seconds == 30
            events.append("acquired")
            return Handle()

    class Guard:
        def __init__(self, _handle, **_kwargs):
            pass

        def start(self):
            events.append("started")
            if failure_stage == "start":
                raise RuntimeError("guard-start-failed")

        def close(self):
            events.append("closed")
            return True

    runtime = SimpleNamespace(
        engine=session_factory.kw["bind"],
        session_factory=session_factory,
    )
    monkeypatch.setattr(bootstrap, "RuntimeTenureService", TenureService)
    monkeypatch.setattr(bootstrap, "RuntimeTenureGuard", Guard)
    monkeypatch.setattr(
        bootstrap,
        "install_runtime_mutation_barrier",
        (
            lambda *_args: (_ for _ in ()).throw(
                RuntimeError("barrier-install-failed")
            )
            if failure_stage == "barrier"
            else None
        ),
    )

    expected = (
        "guard-start-failed"
        if failure_stage == "start"
        else "barrier-install-failed"
    )
    with pytest.raises(RuntimeError, match=expected):
        bootstrap.acquire_runtime_guard(
            runtime,
            "app",
            process_identity=ProcessIdentity(
                8300,
                "guard-install-failure",
            ),
            process_inspector=SimpleNamespace(),
        )

    assert events == ["acquired", "started", "closed"]


def test_app_wires_runtime_renewal_loss_to_controlled_shutdown(
    make_service,
):
    service = make_service()
    secrets = Secrets(
        database_url=str(service.session_factory.kw["bind"].url),
        app_api_token="operator-secret-for-bootstrap-tests",
    )
    container = _injected_container(service, secrets)

    class FailingHandle:
        role = "app"

        def renew(self, *, ttl_seconds):
            raise TenureUncertain()

        def release(self):
            return False

    guard = RuntimeTenureGuard(
        FailingHandle(),
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    guard.start()
    container.runtime_tenure_guard = guard
    with _closed_runtime_container(container):
        app = create_app(
            container=container,
            agent=_StubAgent(),
            planning=None,
        )
        shutdowns: list[str] = []
        app.state.install_controlled_shutdown(
            lambda: shutdowns.append("requested")
        )

        assert app.state.runtime_tenure_guard.renew_once() is False
        assert app.state.runtime_tenure_guard.lost is True
        assert shutdowns == ["requested"]


def test_daemon_monitor_renews_the_container_runtime_tenure(
    make_service,
    monkeypatch,
):
    from trading_assistant import bootstrap
    from trading_assistant.daemon import main as daemon_main

    service = make_service()
    secrets = Secrets(
        database_url=str(service.session_factory.kw["bind"].url),
        app_api_token="operator-secret-for-bootstrap-tests",
    )
    container = _injected_container(service, secrets)
    container.rule_worker = SimpleNamespace(notifier=None)
    container.policy_store_maintenance = None
    handle = SimpleNamespace(role="daemon")
    guard = SimpleNamespace(handle=handle)
    container.runtime_tenure_guard = guard
    monkeypatch.setattr(
        bootstrap,
        "build_container",
        lambda *_args, **_kwargs: container,
    )
    monkeypatch.setattr(
        daemon_main,
        "build_notifier",
        lambda *_args, **_kwargs: object(),
    )
    config = service.config.model_copy(
        update={
            "features": service.config.features.model_copy(
                update={"shadow_mode": False}
            )
        }
    )

    monitor = daemon_main._build_monitor(config, secrets)

    assert monitor.runtime_tenure_guard is guard


def test_private_logging_is_idempotent_rotating_and_redacted(tmp_path):
    import logging
    import stat

    from trading_assistant.logging import (
        configure_logging,
        register_secret,
    )

    path = tmp_path / "private" / "runtime.log"
    marker = "secret-for-runtime-log-test"
    register_secret(marker)

    configure_logging(
        log_path=path,
        max_bytes=64,
        backup_count=1,
    )
    configure_logging(
        log_path=path,
        max_bytes=64,
        backup_count=1,
    )
    logging.getLogger("task9").warning("value=%s", marker)
    logging.getLogger("task9").warning("rotation=%s", "x" * 128)

    handlers = [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_trading_assistant_path", None) == str(path)
    ]
    assert len(handlers) == 1
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    rotated = path.with_name(f"{path.name}.1")
    assert rotated.exists()
    assert stat.S_IMODE(rotated.stat().st_mode) == 0o600
    combined = (
        path.read_text(encoding="utf-8")
        + rotated.read_text(encoding="utf-8")
    )
    assert marker not in combined
    assert "REDACTED" in combined


@pytest.mark.parametrize("runtime_role", ["app", "daemon", "mcp"])
def test_production_runtime_role_installs_private_bounded_log(
    tmp_path,
    monkeypatch,
    runtime_role,
):
    import logging
    from logging.handlers import RotatingFileHandler

    from trading_assistant import bootstrap

    secrets = _migrated_secrets(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        bootstrap,
        "require_current_schema",
        lambda engine: None,
    )

    bootstrap.prepare_database_runtime(
        secrets,
        runtime_role=runtime_role,
    )

    expected = tmp_path / "logs" / f"{runtime_role}.runtime.log"
    handlers = [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_trading_assistant_path", None)
        == str(expected)
    ]
    assert len(handlers) == 1
    assert isinstance(handlers[0], RotatingFileHandler)
    assert handlers[0].maxBytes > 0
    assert handlers[0].backupCount > 0
    assert expected.exists()
    assert (expected.stat().st_mode & 0o777) == 0o600
    assert (expected.parent.stat().st_mode & 0o777) == 0o700
