"""Single fail-closed production composition root."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .app.auth import SessionAuth
from .app.limits import (
    ConcurrencyLeaseService,
    DurableRateLimiter,
    PolicyStoreMaintenance,
)
from .broker.base import BrokerClient
from .broker.factory import build_broker, build_clock
from .config import (
    AppConfig,
    BrokerKind,
    TradingMode,
    load_config,
)
from .db.schema import require_current_schema
from .db.session import create_db_engine, make_session_factory
from .logging import (
    configure_logging,
    configure_runtime_logging,
    register_all_secrets,
)
from .llm.budget import BudgetLimits, ProviderBudgetService
from .notifications.base import NullNotifier
from .orders.application import OrderApplicationService
from .orders.startup import StartupReconciliationFailed
from .orders.reconciliation import ReconciliationService
from .orders.snapshot import PortfolioSnapshotService
from .orders.submission import OrderSubmissionService
from .operations import AuditRecorder, OperationsService
from .risk.breakers import BreakerService
from .rules.worker import RuleWorker
from .security.secrets import RuntimeSecrets, secret_is_set
from .service import TradingService


@dataclass(frozen=True)
class DatabaseRuntime:
    engine: Engine
    session_factory: sessionmaker[Session]


@dataclass(frozen=True)
class ApplicationContainer:
    config: AppConfig
    secrets: RuntimeSecrets
    engine: Engine
    session_factory: sessionmaker[Session]
    rate_limiter: DurableRateLimiter
    leases: ConcurrencyLeaseService
    policy_store_maintenance: PolicyStoreMaintenance
    provider_budget: ProviderBudgetService
    broker: BrokerClient
    service: TradingService
    snapshot_service: PortfolioSnapshotService
    order_application: OrderApplicationService
    order_submission: OrderSubmissionService
    reconciliation: ReconciliationService
    breakers: BreakerService
    rule_worker: RuleWorker
    session_auth: SessionAuth
    audit: AuditRecorder
    operations: OperationsService


def prepare_database_runtime(
    secrets: RuntimeSecrets,
    *,
    log_path=None,
    runtime_role: str | None = None,
) -> DatabaseRuntime:
    if log_path is not None and runtime_role is not None:
        raise ValueError(
            "log_path and runtime_role are mutually exclusive"
        )
    if runtime_role is not None:
        configure_runtime_logging(runtime_role, secrets)
    else:
        register_all_secrets(secrets)
        configure_logging(log_path=log_path)
    engine = create_db_engine(secrets.database_url)
    require_current_schema(engine)
    return DatabaseRuntime(
        engine=engine,
        session_factory=make_session_factory(engine),
    )


def _guard_runtime(config: AppConfig, secrets: RuntimeSecrets) -> None:
    if not secret_is_set(secrets.app_api_token):
        raise RuntimeError("APP_API_TOKEN is required")
    if config.trading.mode is not TradingMode.PAPER:
        raise RuntimeError(
            "live trading is locked out by the safety foundation"
        )
    if config.trading.broker is not BrokerKind.ALPACA:
        raise RuntimeError(
            "production broker must be Alpaca"
        )
    if config.features.auto_execute_preapproved_rules:
        raise RuntimeError("auto-execution must remain disabled")
    if config.execution.prefer_bracket_orders:
        raise RuntimeError("automatic bracket execution must remain disabled")
    if config.llm.fallback_provider is not None:
        raise RuntimeError(
            "automatic cross-provider LLM fallback is disabled"
        )


def _arm_production_paper_broker(broker: BrokerClient) -> None:
    """Bind every production write path to Alpaca's official paper endpoint."""
    from .broker.alpaca import AlpacaBroker

    if type(broker) is not AlpacaBroker:
        raise RuntimeError(
            "production broker must be the exact AlpacaBroker adapter"
        )
    broker.arm_paper_only_mutations()
    broker.validate_armed_paper_target()


def build_provider_budget_service(
    config: AppConfig,
    session_factory: sessionmaker[Session],
) -> ProviderBudgetService:
    configured = config.security.provider_budget
    return ProviderBudgetService(
        session_factory,
        BudgetLimits(
            calls=configured.daily_calls,
            input_tokens=configured.daily_input_tokens,
            output_tokens=configured.daily_output_tokens,
            reservation_ttl_seconds=configured.reservation_ttl_seconds,
        ),
        prices=configured.prices,
    )


def build_container(
    config: AppConfig | None = None,
    secrets: RuntimeSecrets | None = None,
    *,
    runtime_role: str | None = None,
) -> ApplicationContainer:
    config = config or load_config()
    if secrets is None:
        raise RuntimeError(
            "build_container requires explicit RuntimeSecrets"
        )
    return _build_container(
        config,
        secrets,
        runtime_role=runtime_role,
    )


def build_test_container(
    config: AppConfig,
    secrets: RuntimeSecrets,
    *,
    broker: BrokerClient,
    clock,
) -> ApplicationContainer:
    """Compose with explicit fakes while retaining production-safe config."""
    return _build_container(
        config,
        secrets,
        broker=broker,
        clock=clock,
    )


def _build_container(
    config: AppConfig,
    secrets: RuntimeSecrets,
    *,
    runtime_role: str | None = None,
    broker: BrokerClient | None = None,
    clock=None,
) -> ApplicationContainer:
    _guard_runtime(config, secrets)
    runtime = prepare_database_runtime(
        secrets,
        runtime_role=runtime_role,
    )
    session_factory = runtime.session_factory
    rate_limiter = DurableRateLimiter(session_factory)
    leases = ConcurrencyLeaseService(session_factory)
    policy_store_maintenance = PolicyStoreMaintenance(
        rate_limiter,
        leases,
    )
    policy_store_maintenance.prune_once(
        source="startup",
        limit=500,
    )
    provider_budget = build_provider_budget_service(
        config,
        session_factory,
    )

    production_broker = broker is None
    if broker is None:
        broker = build_broker(config, secrets)
        _arm_production_paper_broker(broker)
    if clock is None:
        clock = build_clock(config, secrets)
    service = TradingService(
        broker,
        session_factory,
        config,
        clock,
        external_source=None,
        require_startup_reconciliation=production_broker,
    )
    if production_broker:
        startup_actor = f"runtime:{runtime_role or 'bootstrap'}"
        generation = service.require_startup_reconciliation(
            actor=startup_actor,
            reason="production process startup requires fresh broker truth",
            request_id=uuid4().hex,
        )
        try:
            service.reconcile_startup_epoch(
                generation,
                actor=startup_actor,
                reason="production process startup broker reconciliation",
                request_id=uuid4().hex,
            )
        except StartupReconciliationFailed:
            if runtime_role != "app":
                raise
    rule_worker = RuleWorker(
        service,
        service.rule_repository,
        service.rule_application,
        NullNotifier(),
        max_quote_age_seconds=config.daemon.max_quote_age_seconds,
        quote_reader=broker.get_quote,
        rate_limiter=rate_limiter,
        leases=leases,
        provider_budget=provider_budget,
    )
    session_auth = SessionAuth(
        session_factory,
        application_secret=secrets.app_api_token,
        ttl=timedelta(hours=config.security.session_hours),
        reauthentication_window=timedelta(
            minutes=config.security.reauthentication_minutes
        ),
        cookie_secure=config.server.secure_cookies,
    )
    audit = AuditRecorder(session_factory)
    operations = OperationsService(
        service,
        audit,
        rate_limiter=rate_limiter,
        leases=leases,
        policy_store_maintenance=policy_store_maintenance,
        provider_budget=provider_budget,
    )
    return ApplicationContainer(
        config=config,
        secrets=secrets,
        engine=runtime.engine,
        session_factory=session_factory,
        rate_limiter=rate_limiter,
        leases=leases,
        policy_store_maintenance=policy_store_maintenance,
        provider_budget=provider_budget,
        broker=broker,
        service=service,
        snapshot_service=service.snapshot_service,
        order_application=service.order_application,
        order_submission=service.order_submission,
        reconciliation=service.reconciliation,
        breakers=service.breakers,
        rule_worker=rule_worker,
        session_auth=session_auth,
        audit=audit,
        operations=operations,
    )
