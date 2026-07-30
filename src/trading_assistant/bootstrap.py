"""Single fail-closed production composition root."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from types import (
    FunctionType,
    GetSetDescriptorType,
    MappingProxyType,
    MemberDescriptorType,
    MethodType,
)
from uuid import uuid4

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .app.auth import SessionAuth
from .analyst.untrusted import QuarantineSummarizer
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
from .operations.security_posture import (
    _ConsumedStartupGuard,
    StartupGuardReceipt,
    StartupPostureEvidence,
    _consume_startup_guard_receipt,
    _validate_consumed_startup_guard,
)
from .ops.tenure import (
    LocalProcessInspector,
    ProcessIdentity,
    RuntimeTenureGuard,
    RuntimeTenureService,
    TenureCloseResult,
    TenureGuardedBroker,
    TenureUncertain,
    install_runtime_mutation_barrier,
)
from .ops.preflight_probe import (
    PreflightReconciliationProbe,
    ReadOnlyPreflightService,
)
from .risk.breakers import BreakerService
from .rules.worker import RuleWorker
from .preflight import SensitiveEncryptionStateInspector
from .security.crypto import (
    SensitiveDataCipher,
    build_sensitive_data_cipher,
)
from .security.candidates import (
    CandidateDraftService,
    CandidateQueueService,
    CandidateSigner,
)
from .security.secrets import RuntimeSecrets, secret_is_set
from .security.outbound import require_configured_role_origins
from .security.sensitive_fields import bind_sensitive_cipher
from .service import TradingService


@dataclass(frozen=True)
class DatabaseRuntime:
    engine: Engine
    session_factory: sessionmaker[Session]


class StartupEncryptionBlocked(RuntimeError):
    """A stable, redacted local encryption prerequisite failed."""

    def __init__(self, stable_code: str) -> None:
        self.stable_code = stable_code
        super().__init__(stable_code)


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
    sensitive_cipher: SensitiveDataCipher | None = None
    runtime_tenure_guard: RuntimeTenureGuard | None = None
    quarantine_summarizer: QuarantineSummarizer | None = None
    candidate_signer: CandidateSigner | None = None
    candidate_drafts: CandidateDraftService | None = None
    candidate_queue: CandidateQueueService | None = None
    startup_evidence: StartupPostureEvidence | None = None


_TEST_CONTAINER_SEAL = object()
_TEST_BROKER_GRAPH_MAX_DEPTH = 24
_TEST_BROKER_GRAPH_MAX_NODES = 512
_TEST_BROKER_METHODS = (
    "get_quote",
    "get_account",
    "get_positions",
    "submit_order",
    "submit_bracket",
    "get_order_by_client_id",
    "get_open_orders",
    "get_order_status",
    "get_fill_activities",
    "cancel_order",
)
_TEST_BROKER_REQUIRED_METHODS = frozenset(
    {
        "get_quote",
        "get_account",
        "get_positions",
        "submit_order",
        "get_order_by_client_id",
        "get_open_orders",
        "get_order_status",
        "cancel_order",
    }
)
_TEST_BROKER_EXACT_MAPPING_TYPES = frozenset({dict, Counter})
_TEST_BROKER_EXACT_SEQUENCE_TYPES = frozenset(
    {list, tuple, set, frozenset, deque}
)
_TEST_BROKER_CONTAINER_BASE_TYPES = (
    dict,
    list,
    tuple,
    set,
    frozenset,
    deque,
)
_STATIC_ATTRIBUTE_MISSING = object()


@dataclass(frozen=True)
class _TestApplicationContainer:
    """Opaque test-only wrapper issued only after fake-capability checks."""

    _source: object = field(repr=False)
    _test_agent: object = field(repr=False)
    _test_composition: object = field(
        default=_TEST_CONTAINER_SEAL,
        repr=False,
    )

    def __getattr__(self, name: str):
        return getattr(self._source, name)


@dataclass(frozen=True)
class PreflightServiceContainer:
    """Minimal preflight owner with no app, agent, notifier, or LLM surface."""

    service: PreflightReconciliationProbe
    runtime_tenure_guard: None = None


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


def acquire_runtime_guard(
    runtime: DatabaseRuntime,
    role: str,
    *,
    process_identity: ProcessIdentity | None = None,
    process_inspector=None,
    tenure_clock=None,
    tenure_owner_factory=None,
) -> RuntimeTenureGuard:
    """Acquire, start renewal, and install SQL fencing for one writer role."""
    if role not in {"app", "daemon", "mcp", "validation"}:
        raise ValueError("runtime_role_invalid")
    inspector = process_inspector or LocalProcessInspector()
    identity = process_identity
    if identity is None:
        try:
            identity = inspector.current()
        except Exception:
            raise TenureUncertain() from None
    service_kwargs = {"process_inspector": inspector}
    if tenure_clock is not None:
        service_kwargs["clock"] = tenure_clock
    if tenure_owner_factory is not None:
        service_kwargs["owner_factory"] = tenure_owner_factory
    handle = RuntimeTenureService(
        runtime.session_factory,
        **service_kwargs,
    ).acquire_runtime(
        role,
        identity,
        ttl_seconds=30,
    )
    guard = RuntimeTenureGuard(
        handle,
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    try:
        guard.start()
        install_runtime_mutation_barrier(runtime.engine, guard)
    except BaseException:
        close_result = getattr(
            guard,
            "close_result",
            TenureCloseResult.NOT_ATTEMPTED,
        )
        if close_result is TenureCloseResult.CONFIRMED:
            raise
        if close_result is TenureCloseResult.UNCERTAIN:
            raise TenureUncertain() from None
        try:
            released = guard.close()
        except BaseException:
            raise TenureUncertain() from None
        if not released:
            raise TenureUncertain() from None
        raise
    return guard


def acquire_maintenance_guard(
    runtime: DatabaseRuntime,
    *,
    process_identity: ProcessIdentity | None = None,
    process_inspector=None,
    tenure_clock=None,
    tenure_owner_factory=None,
) -> RuntimeTenureGuard:
    """Acquire the mutually exclusive maintenance tenure for a paper drill."""
    inspector = process_inspector or LocalProcessInspector()
    identity = process_identity
    if identity is None:
        try:
            identity = inspector.current()
        except Exception:
            raise TenureUncertain() from None
    service_kwargs = {"process_inspector": inspector}
    if tenure_clock is not None:
        service_kwargs["clock"] = tenure_clock
    if tenure_owner_factory is not None:
        service_kwargs["owner_factory"] = tenure_owner_factory
    handle = RuntimeTenureService(
        runtime.session_factory,
        **service_kwargs,
    ).acquire_maintenance(
        identity,
        ttl_seconds=30,
    )
    guard = RuntimeTenureGuard(
        handle,
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    try:
        guard.start()
        install_runtime_mutation_barrier(runtime.engine, guard)
    except BaseException:
        close_result = getattr(
            guard,
            "close_result",
            TenureCloseResult.NOT_ATTEMPTED,
        )
        if close_result is TenureCloseResult.CONFIRMED:
            raise
        if close_result is TenureCloseResult.UNCERTAIN:
            raise TenureUncertain() from None
        try:
            released = guard.close()
        except BaseException:
            raise TenureUncertain() from None
        if not released:
            raise TenureUncertain() from None
        raise
    return guard


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


def build_quarantine_summarizer(
    config: AppConfig,
    secrets: RuntimeSecrets,
    provider_budget: ProviderBudgetService,
    *,
    runtime_role: str = "app",
) -> QuarantineSummarizer | None:
    """Compose the no-tools reader separately from the privileged analyst."""
    if (
        runtime_role not in {"app", "daemon"}
        or not config.analyst.news_enabled
    ):
        return None
    from .llm.factory import build_llm_backend

    return QuarantineSummarizer(
        build_llm_backend(
            config,
            secrets,
            provider_budget=provider_budget,
            category="untrusted",
            runtime_role=runtime_role,
        )
    )


def build_container(
    config: AppConfig,
    secrets: RuntimeSecrets,
    *,
    runtime_role: str,
    startup_guard_receipt: StartupGuardReceipt | None = None,
    process_identity: ProcessIdentity | None = None,
    process_inspector=None,
    tenure_clock=None,
    tenure_owner_factory=None,
) -> ApplicationContainer:
    if runtime_role == "app":
        if startup_guard_receipt is None:
            raise RuntimeError("app_startup_guard_required")
        return _build_guarded_container(
            config,
            secrets,
            runtime_role=runtime_role,
            startup_guard_receipt=startup_guard_receipt,
            process_identity=process_identity,
            process_inspector=process_inspector,
            tenure_clock=tenure_clock,
            tenure_owner_factory=tenure_owner_factory,
        )
    if startup_guard_receipt is not None:
        raise RuntimeError("startup_guard_receipt_role_invalid")
    return _build_container(
        config,
        secrets,
        runtime_role=runtime_role,
        process_identity=process_identity,
        process_inspector=process_inspector,
        tenure_clock=tenure_clock,
        tenure_owner_factory=tenure_owner_factory,
    )


def _build_guarded_container(
    config: AppConfig,
    secrets: RuntimeSecrets,
    *,
    runtime_role: str,
    startup_guard_receipt: StartupGuardReceipt,
    process_identity: ProcessIdentity | None = None,
    process_inspector=None,
    tenure_clock=None,
    tenure_owner_factory=None,
) -> ApplicationContainer:
    """Private production path for the exact startup-guard composition."""

    consumed_startup_guard = _consume_startup_guard_receipt(
        startup_guard_receipt,
        config=config,
        secrets=secrets,
        runtime_role=runtime_role,
    )
    return _build_container(
        config,
        secrets,
        runtime_role=runtime_role,
        process_identity=process_identity,
        process_inspector=process_inspector,
        tenure_clock=tenure_clock,
        tenure_owner_factory=tenure_owner_factory,
        _consumed_startup_guard=consumed_startup_guard,
    )


def build_test_container(
    config: AppConfig,
    secrets: RuntimeSecrets,
    *,
    broker: BrokerClient,
    clock,
    service: TradingService | None = None,
    agent: object | None = None,
    source_container: object | None = None,
    runtime_role: str = "app",
) -> ApplicationContainer | _TestApplicationContainer:
    """Compose with explicit fakes while retaining production-safe config."""
    if agent is not None:
        _require_test_clock_capability(clock)
    if service is None:
        if source_container is not None:
            raise RuntimeError("test_container_source_invalid")
        source = _build_container(
            config,
            secrets,
            broker=broker,
            clock=clock,
            runtime_role=runtime_role,
            enforce_runtime_tenure=False,
        )
    else:
        if service.config is not config:
            raise RuntimeError("test_service_config_mismatch")
        if service.broker is not broker or service.clock is not clock:
            raise RuntimeError("test_service_capability_mismatch")
        clocks = getattr(service, "_clocks", None)
        if type(clocks) is not dict:
            raise RuntimeError("production_test_capability_forbidden")
        if agent is not None:
            for configured_clock in clocks.values():
                _require_test_clock_capability(configured_clock)
            _require_test_broker_identity(
                service=service,
                expected_broker=broker,
            )
        if (
            source_container is None
            or getattr(source_container, "config", None) is not config
            or getattr(source_container, "secrets", None) is not secrets
            or getattr(source_container, "service", None) is not service
        ):
            raise RuntimeError("test_container_source_invalid")
        if (
            getattr(source_container, "runtime_tenure_guard", None)
            is not None
            or getattr(source_container, "startup_evidence", None)
            is not None
        ):
            raise RuntimeError("production_test_capability_forbidden")
        source = source_container
    if agent is None:
        return source
    service = source.service
    clocks = getattr(service, "_clocks", None)
    if type(clocks) is not dict:
        raise RuntimeError("production_test_capability_forbidden")
    for configured_clock in clocks.values():
        _require_test_clock_capability(configured_clock)
    _require_test_broker_identity(
        service=service,
        expected_broker=broker,
        source=source,
    )
    return _TestApplicationContainer(
        _source=source,
        _test_agent=agent,
    )


def _require_test_clock_capability(clock: object) -> None:
    from .risk.clock import CryptoClock, FakeClock

    if not isinstance(clock, (CryptoClock, FakeClock)):
        raise RuntimeError("production_test_capability_forbidden")


def _require_test_broker_identity(
    *,
    service: object,
    expected_broker: object,
    source: object | None = None,
) -> None:
    """Require one bounded fake broker graph throughout the app stack."""
    from .broker.mock import MockBroker

    if type(service) is not TradingService:
        raise RuntimeError("production_test_capability_forbidden")
    broker_key = _require_bounded_test_broker_graph(
        expected_broker,
        mock_broker_type=MockBroker,
    )
    snapshot_service = service.snapshot_service
    order_submission = service.order_submission
    reconciliation = service.reconciliation
    if (
        type(snapshot_service) is not PortfolioSnapshotService
        or type(order_submission) is not OrderSubmissionService
        or type(reconciliation) is not ReconciliationService
        or service.broker is not expected_broker
        or snapshot_service.broker is not expected_broker
        or order_submission.broker is not expected_broker
        or reconciliation.broker is not expected_broker
        or order_submission.snapshot_service is not snapshot_service
        or reconciliation.broker_key
        != broker_key
        or service.startup_reconciliation.broker_key
        != broker_key
        or snapshot_service.startup_reconciliation_key
        not in (None, broker_key)
    ):
        raise RuntimeError("production_test_capability_forbidden")
    if source is not None and (
        getattr(source, "service", None) is not service
        or getattr(source, "broker", None) is not expected_broker
        or getattr(source, "snapshot_service", None)
        is not snapshot_service
        or getattr(source, "order_submission", None)
        is not order_submission
        or getattr(source, "reconciliation", None)
        is not reconciliation
        or getattr(
            getattr(source, "operations", None),
            "service",
            None,
        )
        is not service
    ):
        raise RuntimeError("production_test_capability_forbidden")


def _require_bounded_test_broker_graph(
    root: object,
    *,
    mock_broker_type: type,
) -> str:
    """Reject retained broker authority in a bounded static fake-owned graph."""
    if mock_broker_type not in _static_type_mro(type(root)):
        raise RuntimeError("production_test_capability_forbidden")
    root_state = _static_instance_state(
        root,
        _static_type_mro(type(root)),
    )
    if root_state is None:
        raise RuntimeError("production_test_capability_forbidden")

    broker_key = _static_broker_attribute(
        root,
        root_state,
        "reconciliation_key",
    )
    if type(broker_key) is not str or not broker_key:
        raise RuntimeError("production_test_capability_forbidden")

    stack = [(root, 0)]
    for method_name in _TEST_BROKER_METHODS:
        method = _static_broker_attribute(
            root,
            root_state,
            method_name,
        )
        if method is _STATIC_ATTRIBUTE_MISSING:
            if method_name in _TEST_BROKER_REQUIRED_METHODS:
                raise RuntimeError(
                    "production_test_capability_forbidden"
                )
            continue
        if type(method) not in {FunctionType, MethodType, partial}:
            raise RuntimeError("production_test_capability_forbidden")
        stack.append((method, 1))

    seen = set()
    visited = 0
    while stack:
        value, depth = stack.pop()
        if depth > _TEST_BROKER_GRAPH_MAX_DEPTH:
            raise RuntimeError("production_test_capability_forbidden")
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        visited += 1
        if visited > _TEST_BROKER_GRAPH_MAX_NODES:
            raise RuntimeError("production_test_capability_forbidden")
        if (
            value is not root
            and BrokerClient in _static_type_mro(type(value))
        ):
            raise RuntimeError("production_test_capability_forbidden")
        children = _test_broker_owned_children(value)
        if len(children) > _TEST_BROKER_GRAPH_MAX_NODES:
            raise RuntimeError("production_test_capability_forbidden")
        stack.extend((child, depth + 1) for child in children)
    return broker_key


def _static_broker_attribute(
    root: object,
    root_state: dict,
    name: str,
):
    if name in root_state:
        return root_state[name]
    for owner in _static_type_mro(type(root)):
        namespace = _static_type_namespace(owner)
        if name in namespace:
            return namespace[name]
    return _STATIC_ATTRIBUTE_MISSING


def _test_broker_owned_children(value: object) -> tuple[object, ...]:
    value_type = type(value)
    value_mro = _static_type_mro(value_type)
    if type in value_mro:
        return _static_class_owned_values(
            _static_type_mro(value),
            include_all=True,
        )
    if value_type in _TEST_BROKER_EXACT_MAPPING_TYPES:
        return tuple(dict.keys(value)) + tuple(dict.values(value))
    if value_type in _TEST_BROKER_EXACT_SEQUENCE_TYPES:
        return tuple(value)
    if any(
        container_type in value_mro
        for container_type in _TEST_BROKER_CONTAINER_BASE_TYPES
    ):
        raise RuntimeError("production_test_capability_forbidden")
    if value_type is partial:
        keywords = value.keywords or {}
        return (value.func, *value.args, *keywords.keys(), *keywords.values())
    if value_type is MethodType:
        return (value.__self__, value.__func__)
    if value_type is FunctionType:
        captured = []
        if value.__defaults__:
            captured.extend(value.__defaults__)
        if value.__kwdefaults__:
            captured.extend(value.__kwdefaults__.keys())
            captured.extend(value.__kwdefaults__.values())
        for cell in value.__closure__ or ():
            try:
                captured.append(cell.cell_contents)
            except ValueError:
                continue
        return tuple(captured)
    if value_type is property:
        return tuple(
            child
            for child in (
                object.__getattribute__(value, "fget"),
                object.__getattribute__(value, "fset"),
                object.__getattribute__(value, "fdel"),
            )
            if child is not None
        )
    if value_type in {staticmethod, classmethod}:
        return (object.__getattribute__(value, "__func__"),)
    if any(
        descriptor_type in value_mro
        for descriptor_type in (property, staticmethod, classmethod)
    ):
        raise RuntimeError("production_test_capability_forbidden")
    state = _static_instance_state(value, value_mro)
    class_values = _static_class_owned_values(
        value_mro,
        include_all=state is not None,
    )
    if state is None:
        return class_values
    return (
        *tuple(dict.keys(state)),
        *tuple(dict.values(state)),
        *class_values,
    )


def _static_type_mro(value_type: type) -> tuple[type, ...]:
    try:
        mro = type.__getattribute__(value_type, "__mro__")
    except BaseException:
        raise RuntimeError(
            "production_test_capability_forbidden"
        ) from None
    if type(mro) is not tuple:
        raise RuntimeError("production_test_capability_forbidden")
    return mro


def _static_type_namespace(owner: type) -> MappingProxyType:
    try:
        namespace = type.__getattribute__(owner, "__dict__")
    except BaseException:
        raise RuntimeError(
            "production_test_capability_forbidden"
        ) from None
    if type(namespace) is not MappingProxyType:
        raise RuntimeError("production_test_capability_forbidden")
    return namespace


def _static_instance_state(
    value: object,
    value_mro: tuple[type, ...],
) -> dict | None:
    descriptor = _STATIC_ATTRIBUTE_MISSING
    for owner in value_mro:
        namespace = _static_type_namespace(owner)
        if "__dict__" in namespace:
            descriptor = namespace["__dict__"]
            break
    if descriptor is _STATIC_ATTRIBUTE_MISSING:
        return None
    if type(descriptor) not in {
        GetSetDescriptorType,
        MemberDescriptorType,
    }:
        raise RuntimeError("production_test_capability_forbidden")
    try:
        state = object.__getattribute__(value, "__dict__")
    except BaseException:
        raise RuntimeError(
            "production_test_capability_forbidden"
        ) from None
    if type(state) is not dict:
        raise RuntimeError("production_test_capability_forbidden")
    return state


def _static_class_owned_values(
    value_mro: tuple[type, ...],
    *,
    include_all: bool,
) -> tuple[object, ...]:
    values = []
    for owner in value_mro:
        namespace = _static_type_namespace(owner)
        declared_slots = "__slots__" in namespace
        for name in sorted(namespace):
            if name.startswith("__"):
                continue
            raw_value = namespace[name]
            if type(raw_value) is MemberDescriptorType:
                if declared_slots:
                    raise RuntimeError(
                        "production_test_capability_forbidden"
                    )
                continue
            if include_all or _is_static_class_graph_value(raw_value):
                values.append(raw_value)
    return tuple(values)


def _is_static_class_graph_value(value: object) -> bool:
    value_type = type(value)
    if value_type in (
        *_TEST_BROKER_EXACT_MAPPING_TYPES,
        *_TEST_BROKER_EXACT_SEQUENCE_TYPES,
        FunctionType,
        MethodType,
        partial,
        property,
        staticmethod,
        classmethod,
    ):
        return True
    value_mro = _static_type_mro(value_type)
    return (
        BrokerClient in value_mro
        or property in value_mro
        or staticmethod in value_mro
        or classmethod in value_mro
    )


def _is_test_application_container(container: object) -> bool:
    try:
        if (
            type(container) is not _TestApplicationContainer
            or container._test_composition is not _TEST_CONTAINER_SEAL
            or container._test_agent is None
        ):
            return False
        source = container._source
        service = source.service
        if (
            source.config is not service.config
            or getattr(source, "runtime_tenure_guard", None)
            is not None
            or getattr(source, "startup_evidence", None)
            is not None
        ):
            return False
        _require_test_clock_capability(service.clock)
        _require_test_broker_identity(
            service=service,
            expected_broker=service.broker,
            source=source,
        )
        clocks = getattr(service, "_clocks", None)
        if type(clocks) is not dict:
            return False
        for configured_clock in clocks.values():
            _require_test_clock_capability(configured_clock)
    except Exception:
        return False
    return True


def build_preflight_service(
    config: AppConfig,
    secrets: RuntimeSecrets,
) -> PreflightServiceContainer:
    """Compose only the read-only paper reconciliation preflight capability."""

    require_configured_role_origins(config, "preflight")
    _guard_runtime(config, secrets)
    runtime = prepare_database_runtime(
        secrets,
        runtime_role="preflight",
    )
    broker = build_broker(
        config,
        secrets,
        runtime_role="preflight",
    )
    _arm_production_paper_broker(broker)
    service = ReadOnlyPreflightService(
        broker,
        runtime.session_factory,
    )
    return PreflightServiceContainer(service=service)


def _build_container(
    config: AppConfig,
    secrets: RuntimeSecrets,
    *,
    runtime_role: str | None = None,
    broker: BrokerClient | None = None,
    clock=None,
    process_identity: ProcessIdentity | None = None,
    process_inspector=None,
    tenure_clock=None,
    tenure_owner_factory=None,
    enforce_runtime_tenure: bool = True,
    _consumed_startup_guard: _ConsumedStartupGuard | None = None,
) -> ApplicationContainer:
    effective_role = runtime_role or "app"
    if effective_role not in {
        "app",
        "daemon",
        "mcp",
        "paper-drill",
        "safety-drill",
    }:
        raise ValueError("runtime_role_invalid")
    if enforce_runtime_tenure and effective_role == "safety-drill":
        raise ValueError("runtime_role_invalid")
    require_configured_role_origins(config, effective_role)
    startup_evidence = None
    if _consumed_startup_guard is not None:
        startup_evidence = _validate_consumed_startup_guard(
            _consumed_startup_guard,
            config=config,
            secrets=secrets,
            runtime_role=effective_role,
        )
    _guard_runtime(config, secrets)
    runtime = prepare_database_runtime(
        secrets,
        runtime_role=effective_role if enforce_runtime_tenure else None,
    )
    session_factory = runtime.session_factory
    try:
        sensitive_cipher = build_sensitive_data_cipher(
            config.encryption,
            secrets,
        )
    except Exception:
        raise StartupEncryptionBlocked(
            "sensitive_key_unavailable"
        ) from None
    bind_sensitive_cipher(session_factory, sensitive_cipher)
    runtime_tenure_guard: RuntimeTenureGuard | None = None
    if enforce_runtime_tenure:
        if effective_role == "paper-drill":
            runtime_tenure_guard = acquire_maintenance_guard(
                runtime,
                process_identity=process_identity,
                process_inspector=process_inspector,
                tenure_clock=tenure_clock,
                tenure_owner_factory=tenure_owner_factory,
            )
        else:
            runtime_tenure_guard = acquire_runtime_guard(
                runtime,
                effective_role,
                process_identity=process_identity,
                process_inspector=process_inspector,
                tenure_clock=tenure_clock,
                tenure_owner_factory=tenure_owner_factory,
            )
    try:
        return _finish_container(
            config,
            secrets,
            runtime=runtime,
            runtime_role=effective_role,
            broker=broker,
            clock=clock,
            sensitive_cipher=sensitive_cipher,
            runtime_tenure_guard=runtime_tenure_guard,
            consumed_startup_guard=_consumed_startup_guard,
            startup_evidence=startup_evidence,
        )
    except BaseException:
        if runtime_tenure_guard is not None:
            if not runtime_tenure_guard.close():
                raise TenureUncertain() from None
        raise


def _finish_container(
    config: AppConfig,
    secrets: RuntimeSecrets,
    *,
    runtime: DatabaseRuntime,
    runtime_role: str,
    broker: BrokerClient | None,
    clock,
    sensitive_cipher: SensitiveDataCipher | None,
    runtime_tenure_guard: RuntimeTenureGuard | None,
    consumed_startup_guard: _ConsumedStartupGuard | None,
    startup_evidence: StartupPostureEvidence | None,
) -> ApplicationContainer:
    session_factory = runtime.session_factory
    if runtime_tenure_guard is not None:
        runtime_tenure_guard.ensure_owned()
        check = SensitiveEncryptionStateInspector(
            runtime.engine,
            schema_version=config.encryption.schema_version,
            active_key_id=config.encryption.active_key_id,
            cipher=sensitive_cipher,
        ).inspect()
        if not check.passed:
            raise StartupEncryptionBlocked(check.code)
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
    if runtime_tenure_guard is not None:
        runtime_tenure_guard.ensure_owned()
    provider_budget = build_provider_budget_service(
        config,
        session_factory,
    )
    quarantine_summarizer = build_quarantine_summarizer(
        config,
        secrets,
        provider_budget,
        runtime_role=runtime_role,
    )

    production_broker = broker is None
    if broker is None:
        if runtime_tenure_guard is not None:
            runtime_tenure_guard.ensure_owned()
        broker = build_broker(
            config,
            secrets,
            runtime_role=runtime_role,
        )
        if runtime_tenure_guard is not None:
            runtime_tenure_guard.ensure_owned()
        _arm_production_paper_broker(broker)
        if runtime_tenure_guard is not None:
            broker = TenureGuardedBroker(
                broker,
                runtime_tenure_guard,
            )
    if clock is None:
        clock = build_clock(
            config,
            secrets,
            runtime_role=runtime_role,
        )
    service = TradingService(
        broker,
        session_factory,
        config,
        clock,
        external_source=None,
        require_startup_reconciliation=production_broker,
    )
    candidate_signer = (
        CandidateSigner.from_runtime_secrets(secrets)
        if secret_is_set(secrets.candidate_signing_key)
        else None
    )
    candidate_drafts = (
        CandidateDraftService(service, candidate_signer)
        if candidate_signer is not None
        else None
    )
    candidate_queue = (
        CandidateQueueService(service, candidate_signer)
        if candidate_signer is not None
        else None
    )
    if production_broker:
        if runtime_tenure_guard is not None:
            runtime_tenure_guard.ensure_owned()
        startup_actor = f"runtime:{runtime_role}"
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
            if runtime_tenure_guard is not None:
                runtime_tenure_guard.ensure_owned()
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
        _consumed_startup_guard=consumed_startup_guard,
        _startup_secrets=(
            secrets if consumed_startup_guard is not None else None
        ),
        _startup_runtime_role=(
            runtime_role if consumed_startup_guard is not None else None
        ),
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
        sensitive_cipher=sensitive_cipher,
        runtime_tenure_guard=runtime_tenure_guard,
        quarantine_summarizer=quarantine_summarizer,
        candidate_signer=candidate_signer,
        candidate_drafts=candidate_drafts,
        candidate_queue=candidate_queue,
        startup_evidence=startup_evidence,
    )
