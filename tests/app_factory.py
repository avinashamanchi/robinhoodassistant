"""Compatibility helper that turns explicit local fakes into a sealed test stack."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from trading_assistant.config import Secrets
from trading_assistant.security.secrets import secret_is_set

_UNSET = object()


def build_test_app_container(
    service,
    agent,
    *,
    secrets,
    source_container=None,
):
    """Issue one marked app container from explicit fake capabilities."""
    from trading_assistant import bootstrap
    from trading_assistant.app.auth import SessionAuth
    from trading_assistant.app.limits import (
        ConcurrencyLeaseService,
        DurableRateLimiter,
        PolicyStoreMaintenance,
    )
    from trading_assistant.operations import (
        AuditRecorder,
        OperationsService,
    )

    if source_container is None:
        rate_limiter = DurableRateLimiter(service.session_factory)
        leases = ConcurrencyLeaseService(service.session_factory)
        policy_store = PolicyStoreMaintenance(
            rate_limiter,
            leases,
        )
        provider_budget = bootstrap.build_provider_budget_service(
            service.config,
            service.session_factory,
        )
        audit = AuditRecorder(service.session_factory)
        source_container = SimpleNamespace(
            config=service.config,
            secrets=secrets,
            engine=service.session_factory.kw.get("bind"),
            session_factory=service.session_factory,
            rate_limiter=rate_limiter,
            leases=leases,
            policy_store_maintenance=policy_store,
            provider_budget=provider_budget,
            broker=service.broker,
            service=service,
            snapshot_service=service.snapshot_service,
            order_application=service.order_application,
            order_submission=service.order_submission,
            reconciliation=service.reconciliation,
            breakers=service.breakers,
            rule_worker=None,
            session_auth=SessionAuth(
                service.session_factory,
                application_secret=secrets.app_api_token,
                ttl=timedelta(
                    hours=service.config.security.session_hours
                ),
                reauthentication_window=timedelta(
                    minutes=(
                        service.config.security
                        .reauthentication_minutes
                    )
                ),
                cookie_secure=service.config.server.secure_cookies,
            ),
            audit=audit,
            operations=OperationsService(
                service,
                audit,
                rate_limiter=rate_limiter,
                leases=leases,
                policy_store_maintenance=policy_store,
                provider_budget=provider_budget,
            ),
            runtime_tenure_guard=None,
            startup_evidence=None,
        )
    return bootstrap.build_test_container(
        service.config,
        secrets,
        broker=service.broker,
        clock=service.clock,
        service=service,
        agent=agent,
        source_container=source_container,
    )


def create_app(
    service=None,
    agent=None,
    *,
    container=None,
    planning=_UNSET,
    runtime_secrets=None,
    api_token=None,
    **kwargs,
):
    """Build route-test apps through the production test-container issuer."""
    from trading_assistant import bootstrap
    from trading_assistant.app.main import create_test_app

    if bootstrap._is_test_application_container(container):
        if agent is None:
            call = {"container": container, **kwargs}
            if planning is not _UNSET:
                call["planning"] = planning
            if api_token is not None:
                call["api_token"] = api_token
            return create_test_app(**call)

    source_container = container
    if source_container is not None:
        if service is not None and service is not source_container.service:
            raise RuntimeError("container and service do not match")
        service = source_container.service
        if runtime_secrets is None:
            runtime_secrets = source_container.secrets

    if (service is None) != (agent is None):
        raise RuntimeError("service and agent must be injected together")
    if service is None:
        raise RuntimeError("explicit_test_stack_required")
    if planning is _UNSET and source_container is None:
        if not secret_is_set(api_token):
            raise RuntimeError("APP_API_TOKEN is required")
        raise RuntimeError(
            "automatic planning requires a shared ApplicationContainer"
        )
    if (
        source_container is not None
        and runtime_secrets is not None
        and runtime_secrets is not source_container.secrets
    ):
        raise RuntimeError(
            "container and runtime Secrets do not match"
        )

    secrets = runtime_secrets
    if secrets is None:
        secrets = Secrets(app_api_token=api_token or "")
    marked = build_test_app_container(
        service,
        agent,
        secrets=secrets,
        source_container=source_container,
    )
    call = {"container": marked, **kwargs}
    if planning is not _UNSET:
        call["planning"] = planning
    if api_token is not None:
        call["api_token"] = api_token
    return create_test_app(**call)
