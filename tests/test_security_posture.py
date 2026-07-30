"""Task 10: immutable, redacted, read-only security posture."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from trading_assistant.broker.models import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)
from trading_assistant.db.models import (
    AuditEvent,
    Base,
    CircuitBreakerState,
    ConcurrencyLease,
    FILL_RECONCILIATION_REQUIRED,
    FILL_RECONCILIATION_SUPERSEDED,
    FILL_RECONCILIATION_TRUSTED,
    Fill,
    Heartbeat,
    MutationInterlock,
    Order,
    PLAN_CANCEL_NONE,
    ProviderBudgetDay,
    ProviderReservation,
    RateWindow,
    Rule,
    RuleGroup,
    RuntimeTenure,
    SensitiveMigrationState,
    StartupReconciliationState,
    UntrustedIngestEvent,
)
from trading_assistant.app.limits import DurableRateLimiter
from trading_assistant.llm.budget import BudgetLimits, ProviderBudgetService
from trading_assistant.notifications.base import NullNotifier
from trading_assistant.orders.startup import StartupReconciliationGate
from trading_assistant.rules.models import RuleState
from trading_assistant.security.crypto import (
    SensitiveDataCipher,
    SensitiveFieldRef,
)
from trading_assistant.security.secrets import RuntimeSecrets
from trading_assistant.security.sensitive_fields import persist_sensitive


NOW = datetime(2026, 7, 28, 19, 30, tzinfo=timezone.utc)
NARRATIVE_MARKER = "decrypted-narrative-must-never-appear"


def _posture_module():
    return importlib.import_module(
        "trading_assistant.operations.security_posture"
    )


def _reader(
    *,
    app_config,
    session_factory,
    consumed_startup_guard=None,
    startup_secrets=None,
    reconciliation_enabled=True,
):
    posture = _posture_module()
    configured = app_config.security.provider_budget
    startup_kwargs = (
        {
            "_consumed_startup_guard": consumed_startup_guard,
            "_startup_secrets": startup_secrets,
            "_startup_runtime_role": "app",
        }
        if consumed_startup_guard is not None
        else {}
    )
    return posture.SecurityPostureService(
        config=app_config,
        session_factory=session_factory,
        reconciliation_key="mock",
        reconciliation_enabled=reconciliation_enabled,
        rate_limiter=DurableRateLimiter(session_factory),
        provider_budget=ProviderBudgetService(
            session_factory,
            BudgetLimits(
                calls=configured.daily_calls,
                input_tokens=configured.daily_input_tokens,
                output_tokens=configured.daily_output_tokens,
                reservation_ttl_seconds=(
                    configured.reservation_ttl_seconds
                ),
            ),
            clock=lambda: NOW,
        ),
        clock=lambda: NOW,
        **startup_kwargs,
    )


def _issued_startup_receipt(
    app_config,
    *,
    secrets=None,
    checks=None,
):
    posture = _posture_module()
    secrets = secrets or RuntimeSecrets(
        app_api_token="posture-receipt-test-secret-0123456789",
        database_url="sqlite:///posture-receipt-never-opened.db",
    )
    checks = checks or (
        SimpleNamespace(
            name="runtime_configuration",
            passed=True,
            code="ok",
        ),
        SimpleNamespace(name="loopback_https", passed=True, code="ok"),
        SimpleNamespace(name="tls", passed=True, code="ok"),
        SimpleNamespace(name="database", passed=True, code="ok"),
        SimpleNamespace(name="encryption", passed=True, code="ok"),
    )
    receipt = posture._issue_startup_guard_receipt(
        config=app_config,
        secrets=secrets,
        checks=checks,
        observed_at=NOW,
        secret_loaded_at=NOW - timedelta(seconds=2),
        runtime_role="app",
    )
    return receipt, secrets


def _consumed_startup_context(app_config):
    posture = _posture_module()
    receipt, secrets = _issued_startup_receipt(app_config)
    return (
        posture._consume_startup_guard_receipt(
            receipt,
            config=app_config,
            secrets=secrets,
            runtime_role="app",
        ),
        secrets,
    )


def _checks_by_name(report, name):
    return [check for check in report.checks if check.name.value == name]


def _snapshot_all_tables(session_factory):
    with session_factory() as session:
        return {
            table.name: tuple(
                session.execute(
                    select(table).order_by(
                        *[
                            table.c[column]
                            for column in table.primary_key.columns.keys()
                        ]
                    )
                ).all()
            )
            for table in sorted(
                Base.metadata.tables.values(),
                key=lambda item: item.name,
            )
        }


def test_security_posture_reports_evidence_not_permission(
    authenticated_client,
):
    client, _csrf = authenticated_client

    response = client.get("/security/posture")

    assert response.status_code == 200
    body = response.json()
    checks = {
        (item["name"], item.get("scope")): item
        for item in body["checks"]
    }
    assert checks[("broker_mode", None)]["status"] == "paper"
    assert checks[("webhook_receiver", None)]["status"] == "disabled"
    assert checks[("composio_integration", None)]["status"] == "disabled"
    assert checks[("secret_provider", None)]["status"] == "unknown"
    assert (
        checks[("quote_freshness", None)]["detail_code"]
        == "quote_evidence_unavailable"
    )
    assert body["can_trade"] is False
    encoded = json.dumps(body, sort_keys=True).lower()
    assert "value" not in encoded
    assert "trading_assistant.db" not in encoded
    assert "paper-api.alpaca.markets" not in encoded


def test_posture_models_are_frozen_extra_forbid_and_cannot_authorize():
    posture = _posture_module()
    check = posture.PostureCheck(
        name=posture.PostureName.QUOTE_FRESHNESS,
        status=posture.PostureStatus.UNKNOWN,
        observed_at=NOW,
        detail_code=posture.PostureDetailCode.QUOTE_EVIDENCE_UNAVAILABLE,
    )
    report = posture.SecurityPostureReport(
        observed_at=NOW,
        checks=(check,),
    )

    assert report.can_trade is False
    with pytest.raises(ValidationError):
        check.status = "pass"
    with pytest.raises(ValidationError):
        posture.PostureCheck(
            name="quote_freshness",
            status="unknown",
            observed_at=NOW,
            detail_code="quote_evidence_unavailable",
            unsafe={"secret": "must-not-fit"},
        )
    with pytest.raises(ValidationError):
        posture.SecurityPostureReport(
            observed_at=NOW,
            checks=(check,),
            can_trade=True,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("count", True),
        ("generation", "1"),
        ("budget_used", 1.0),
        ("age_seconds", True),
        ("max_age_seconds", "1.0"),
    ],
)
def test_posture_models_reject_coercive_or_boolean_numeric_scalars(
    field,
    value,
):
    posture = _posture_module()

    with pytest.raises(ValidationError):
        posture.PostureCheck(
            name=posture.PostureName.QUOTE_FRESHNESS,
            status=posture.PostureStatus.UNKNOWN,
            observed_at=NOW,
            detail_code=(
                posture.PostureDetailCode.QUOTE_EVIDENCE_UNAVAILABLE
            ),
            **{field: value},
        )


def test_posture_models_are_strict_and_can_trade_is_exact_false_bool():
    posture = _posture_module()
    check = posture.PostureCheck(
        name=posture.PostureName.QUOTE_FRESHNESS,
        status=posture.PostureStatus.UNKNOWN,
        observed_at=NOW,
        detail_code=posture.PostureDetailCode.QUOTE_EVIDENCE_UNAVAILABLE,
    )
    report = posture.SecurityPostureReport(
        observed_at=NOW,
        checks=(check,),
    )

    assert report.can_trade is False
    assert type(report.can_trade) is bool
    for value in (0, 0.0, "false", None):
        with pytest.raises(ValidationError):
            posture.SecurityPostureReport(
                observed_at=NOW,
                checks=(check,),
                can_trade=value,
            )
    with pytest.raises(ValidationError):
        posture.PostureCheck(
            name="quote_freshness",
            status=posture.PostureStatus.UNKNOWN,
            observed_at=NOW,
            detail_code=(
                posture.PostureDetailCode.QUOTE_EVIDENCE_UNAVAILABLE
            ),
        )
    with pytest.raises(ValidationError):
        posture.PostureCheck(
            name=posture.PostureName.QUOTE_FRESHNESS,
            status=posture.PostureStatus.UNKNOWN,
            observed_at=NOW.isoformat(),
            detail_code=(
                posture.PostureDetailCode.QUOTE_EVIDENCE_UNAVAILABLE
            ),
        )


def test_startup_posture_evidence_is_typed_redacted_and_immutable():
    posture = _posture_module()
    evidence = posture.StartupPostureEvidence(
        observed_at=NOW,
        structural_checks=(
            posture.StartupStructuralCheck(
                name=posture.StartupCheckName.LOOPBACK_HTTPS,
                status="pass",
                detail_code=posture.StartupDetailCode.OK,
            ),
            posture.StartupStructuralCheck(
                name=posture.StartupCheckName.TLS,
                status="blocked",
                detail_code=(
                    posture.StartupDetailCode.TLS_CERTIFICATE_SAN_INVALID
                ),
            ),
        ),
        secret_provider="macos_keychain",
        secret_load_status="pass",
        secret_loaded_at=NOW - timedelta(seconds=2),
    )

    payload = evidence.model_dump(mode="json")
    assert payload["secret_provider"] == "macos_keychain"
    assert payload["secret_loaded_at"] == "2026-07-28T19:29:58Z"
    assert "path" not in json.dumps(payload).lower()
    assert "presence" not in json.dumps(payload).lower()
    assert "key_id" not in json.dumps(payload).lower()
    with pytest.raises(ValidationError):
        evidence.secret_load_status = "blocked"
    with pytest.raises(ValidationError):
        posture.StartupPostureEvidence(
            observed_at=NOW,
            structural_checks=(),
            secret_provider="macos_keychain",
            secret_load_status="pass",
            secret_loaded_at=NOW,
            secret_presence=True,
        )
    with pytest.raises(ValidationError):
        posture.StartupStructuralCheck(
            name="tls",
            status="pass",
            detail_code=posture.StartupDetailCode.OK,
        )


@pytest.mark.parametrize(
    "checks",
    [
        (
            SimpleNamespace(
                name="loopback_https",
                passed=True,
                code="ok",
            ),
            SimpleNamespace(name="tls", passed=True, code="ok"),
            SimpleNamespace(name="encryption", passed=True, code="ok"),
        ),
        (
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
            SimpleNamespace(
                name="tls",
                passed=True,
                code="tls_material_parse_failed",
            ),
            SimpleNamespace(name="database", passed=True, code="ok"),
            SimpleNamespace(name="encryption", passed=True, code="ok"),
        ),
    ],
)
def test_startup_receipt_issuer_rejects_partial_or_inconsistent_guard_checks(
    app_config,
    checks,
):
    posture = _posture_module()
    secrets = RuntimeSecrets(
        app_api_token="receipt-validation-test-secret-0123456789",
        database_url="sqlite:///receipt-validation-never-opened.db",
    )

    with pytest.raises(
        RuntimeError,
        match="startup_guard_receipt_invalid",
    ):
        posture._issue_startup_guard_receipt(
            config=app_config,
            secrets=secrets,
            checks=checks,
            observed_at=NOW,
            secret_loaded_at=NOW - timedelta(seconds=1),
            runtime_role="app",
        )


def test_provider_budget_inspection_reports_expired_ambiguous_without_mutation(
    session_factory,
):
    budget = ProviderBudgetService(
        session_factory,
        BudgetLimits(
            calls=10,
            input_tokens=10_000,
            output_tokens=10_000,
            reservation_ttl_seconds=60,
        ),
        clock=lambda: NOW,
    )
    created_at = NOW - timedelta(minutes=5)
    started = budget.reserve(
        provider="gemini",
        category="chat",
        request_id="1" * 32,
        input_tokens=100,
        output_tokens=50,
        now=created_at,
    )
    budget.mark_started(
        started.reservation_id,
        now=created_at + timedelta(seconds=1),
    )
    unknown = budget.reserve(
        provider="gemini",
        category="analysis",
        request_id="2" * 32,
        input_tokens=200,
        output_tokens=75,
        now=created_at,
    )
    budget.mark_started(
        unknown.reservation_id,
        now=created_at + timedelta(seconds=1),
    )
    budget.mark_unknown(
        unknown.reservation_id,
        now=created_at + timedelta(seconds=2),
    )

    with session_factory() as session:
        before_days = tuple(
            session.execute(
                select(
                    ProviderBudgetDay.provider,
                    ProviderBudgetDay.budget_day,
                    ProviderBudgetDay.calls_used,
                    ProviderBudgetDay.input_tokens_used,
                    ProviderBudgetDay.output_tokens_used,
                    ProviderBudgetDay.reconciliation_required,
                    ProviderBudgetDay.reconciliation_code,
                    ProviderBudgetDay.updated_at,
                ).order_by(
                    ProviderBudgetDay.provider,
                    ProviderBudgetDay.budget_day,
                )
            ).all()
        )
        before_reservations = tuple(
            session.execute(
                select(
                    ProviderReservation.reservation_id,
                    ProviderReservation.state,
                    ProviderReservation.started_at,
                    ProviderReservation.expires_at,
                ).order_by(ProviderReservation.reservation_id)
            ).all()
        )

    inspected = budget.inspect("gemini", now=NOW)

    assert inspected.calls_used == 2
    assert inspected.calls_remaining == 8
    assert inspected.expired_started_count == 1
    assert inspected.expired_unknown_count == 1
    assert inspected.reconciliation_required is True
    assert inspected.reset_at == datetime(
        2026,
        7,
        29,
        tzinfo=timezone.utc,
    )
    with session_factory() as session:
        after_days = tuple(
            session.execute(
                select(
                    ProviderBudgetDay.provider,
                    ProviderBudgetDay.budget_day,
                    ProviderBudgetDay.calls_used,
                    ProviderBudgetDay.input_tokens_used,
                    ProviderBudgetDay.output_tokens_used,
                    ProviderBudgetDay.reconciliation_required,
                    ProviderBudgetDay.reconciliation_code,
                    ProviderBudgetDay.updated_at,
                ).order_by(
                    ProviderBudgetDay.provider,
                    ProviderBudgetDay.budget_day,
                )
            ).all()
        )
        after_reservations = tuple(
            session.execute(
                select(
                    ProviderReservation.reservation_id,
                    ProviderReservation.state,
                    ProviderReservation.started_at,
                    ProviderReservation.expires_at,
                ).order_by(ProviderReservation.reservation_id)
            ).all()
        )
    assert after_days == before_days
    assert after_reservations == before_reservations


def test_launcher_preserves_one_keychain_startup_evidence_chain(
    app_config,
    monkeypatch,
):
    from trading_assistant.ops import serve
    from trading_assistant.preflight import StructuralCheck

    loaded = RuntimeSecrets(
        app_api_token="startup-chain-operator-secret-0123456789",
        database_url="sqlite:///never-opened-in-this-test.db",
    )
    provider = SimpleNamespace(
        provider_name="macos-keychain",
        last_successful_role_load_at=NOW - timedelta(seconds=1),
    )
    calls = {
        "load": 0,
        "guard": 0,
        "build": 0,
        "app": 0,
    }
    checks = (
        StructuralCheck("runtime_configuration", "passed", "ok"),
        StructuralCheck("loopback_https", "passed", "ok"),
        StructuralCheck("tls", "passed", "ok"),
        StructuralCheck("database", "passed", "ok"),
        StructuralCheck("encryption", "passed", "ok"),
    )

    def load_once(role, *, config, provider: object):
        calls["load"] += 1
        assert role == "app"
        assert config is app_config
        assert provider is provider_instance
        return loaded

    def guard_once(*, config, secrets, secret_loaded_at, **_kwargs):
        calls["guard"] += 1
        assert config is app_config
        assert secrets is loaded
        assert secret_loaded_at is provider.last_successful_role_load_at
        posture = _posture_module()
        return posture._issue_startup_guard_receipt(
            config=config,
            secrets=secrets,
            checks=checks,
            observed_at=NOW,
            secret_loaded_at=secret_loaded_at,
            runtime_role="app",
        )

    def build_once(
        config,
        secrets,
        *,
        runtime_role,
        startup_guard_receipt,
    ):
        calls["build"] += 1
        assert config is app_config
        assert secrets is loaded
        assert runtime_role == "app"
        posture = _posture_module()
        context = posture._consume_startup_guard_receipt(
            startup_guard_receipt,
            config=config,
            secrets=secrets,
            runtime_role=runtime_role,
        )
        evidence = posture._validate_consumed_startup_guard(
            context,
            config=config,
            secrets=secrets,
            runtime_role=runtime_role,
        )
        assert evidence.secret_loaded_at is provider.last_successful_role_load_at
        return SimpleNamespace(
            secrets=secrets,
            startup_evidence=evidence,
        )

    def create_once(*, container):
        calls["app"] += 1
        assert container.secrets is loaded
        assert container.startup_evidence.secret_load_status == "pass"
        return SimpleNamespace(
            state=SimpleNamespace(
                install_controlled_shutdown=lambda _callback: None,
                runtime_tenure_guard=None,
            )
        )

    class FakeServer:
        should_exit = False

        def __init__(self, _config):
            pass

        def run(self):
            return None

    provider_instance = provider
    server = app_config.server.model_copy(
        update={
            "tls_cert_path": Path(".local/tls/localhost.pem"),
            "tls_key_path": Path(".local/tls/localhost-key.pem"),
        }
    )
    config = app_config.model_copy(update={"server": server})
    app_config = config
    monkeypatch.setattr(serve, "load_config", lambda: app_config)
    monkeypatch.setattr(
        serve,
        "MacOSKeychainSecretProvider",
        lambda: provider_instance,
    )
    monkeypatch.setattr(serve, "load_role_secrets", load_once)
    monkeypatch.setattr(serve, "run_startup_guard", guard_once)
    monkeypatch.setattr(serve, "_build_guarded_container", build_once)
    monkeypatch.setattr(serve, "_create_guarded_app", create_once)
    monkeypatch.setattr(
        serve,
        "runtime_startup",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        serve,
        "start_app_control",
        lambda _path: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(
        serve.uvicorn,
        "Config",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(serve.uvicorn, "Server", FakeServer)

    assert serve.main() == 0
    assert calls == {"load": 1, "guard": 1, "build": 1, "app": 1}


def test_launcher_keychain_failure_is_stable_and_stops_composition(
    app_config,
    monkeypatch,
):
    from trading_assistant.ops import serve

    composed: list[str] = []

    def unavailable():
        raise RuntimeError(NARRATIVE_MARKER)

    monkeypatch.setattr(serve, "load_config", lambda: app_config)
    monkeypatch.setattr(
        serve,
        "MacOSKeychainSecretProvider",
        unavailable,
    )
    monkeypatch.setattr(
        serve,
        "_build_guarded_container",
        lambda *_args, **_kwargs: composed.append("container"),
    )
    monkeypatch.setattr(
        serve,
        "_create_guarded_app",
        lambda *_args, **_kwargs: composed.append("app"),
    )

    with pytest.raises(serve.StartupGuardBlocked) as captured:
        serve.main()

    assert [check.code for check in captured.value.checks] == [
        "keychain_unavailable"
    ]
    assert NARRATIVE_MARKER not in str(captured.value)
    assert composed == []


def test_startup_failures_remain_independent_typed_evidence(
    app_config,
    session_factory,
):
    posture = _posture_module()
    evidence = posture.StartupPostureEvidence(
        observed_at=NOW - timedelta(seconds=3),
        structural_checks=(
            posture.StartupStructuralCheck(
                name=posture.StartupCheckName.LOOPBACK_HTTPS,
                status="pass",
                detail_code=posture.StartupDetailCode.OK,
            ),
            posture.StartupStructuralCheck(
                name=posture.StartupCheckName.TLS,
                status="blocked",
                detail_code=(
                    posture.StartupDetailCode.TLS_CERTIFICATE_SAN_INVALID
                ),
            ),
        ),
        secret_provider="macos_keychain",
        secret_load_status="blocked",
    )

    report = _reader(
        app_config=app_config,
        session_factory=session_factory,
    ).report(limit_principal="session:1:operator")
    by_name = {
        check.name.value: check
        for check in report.checks
        if check.name.value in {"loopback_https", "tls", "secret_provider"}
    }

    assert by_name["loopback_https"].status.value == "unknown"
    assert by_name["tls"].status.value == "unknown"
    assert (
        by_name["tls"].detail_code.value
        == "startup_evidence_unavailable"
    )
    assert by_name["secret_provider"].status.value == "unknown"
    assert report.can_trade is False
    with pytest.raises(TypeError):
        posture.SecurityPostureService(
            config=app_config,
            session_factory=session_factory,
            reconciliation_key="mock",
            reconciliation_enabled=True,
            startup_evidence=evidence,
            clock=lambda: NOW,
        )


def test_posture_reports_stale_and_unsafe_local_state_without_narratives(
    app_config,
    session_factory,
):
    with session_factory() as session:
        persist_sensitive(
            session,
            CircuitBreakerState(
                scope_key="liquidity:AAPL",
                kind="liquidity",
                target="AAPL",
                tripped=True,
                actor=f"actor-{NARRATIVE_MARKER}",
                generation=4,
                updated_at=NOW - timedelta(minutes=3),
            ),
            {"reason": NARRATIVE_MARKER},
        )
        persist_sensitive(
            session,
            StartupReconciliationState(
                broker="mock",
                generation=7,
                completed_generation=7,
                status="current",
                actor=f"actor-{NARRATIVE_MARKER}",
                request_id=f"request-{NARRATIVE_MARKER}",
                started_at=NOW - timedelta(hours=2),
                completed_at=NOW - timedelta(hours=1),
                updated_at=NOW - timedelta(hours=1),
            ),
            {
                "reason": NARRATIVE_MARKER,
                "evidence_json": json.dumps(
                    {"external": NARRATIVE_MARKER}
                ),
            },
        )
        persist_sensitive(
            session,
            Order(
                idempotency_key="unsafe-order-posture",
                ticker="AAPL",
                side="buy",
                order_type="market",
                notional=Decimal("10"),
                status=OrderStatus.ACCEPTANCE_UNKNOWN.value,
                acceptance_state="not_started",
            ),
            {"approval_reason": NARRATIVE_MARKER},
        )
        session.add_all(
            [
                Heartbeat(
                    source="daemon",
                    at=NOW - timedelta(hours=1),
                ),
                RuntimeTenure(
                    resource_key="runtime:daemon",
                    role="daemon",
                    state="held",
                    owner_id="11111111-1111-1111-1111-111111111111",
                    generation=8,
                    pid=12345,
                    process_start_identity=NARRATIVE_MARKER,
                    acquired_at=NOW - timedelta(hours=2),
                    renewed_at=NOW - timedelta(hours=1, seconds=1),
                    expires_at=NOW - timedelta(hours=1),
                ),
                MutationInterlock(
                    resource_key=f"unsafe:{NARRATIVE_MARKER}",
                    owner=NARRATIVE_MARKER,
                    generation=2,
                    operation="order_cancel",
                    state="uncertain",
                    outcome_code="handler_failed",
                    created_at=NOW - timedelta(minutes=5),
                    updated_at=NOW - timedelta(minutes=4),
                ),
                UntrustedIngestEvent(
                    source_hash="a" * 64,
                    content_hash="b" * 64,
                    byte_length=123,
                    flags_json=json.dumps([NARRATIVE_MARKER]),
                    state="failed",
                    received_at=NOW - timedelta(minutes=10),
                ),
                Fill(
                    order_id=None,
                    ticker="AAPL",
                    side="buy",
                    qty=Decimal("1"),
                    price=Decimal("100"),
                    broker_fill_id=None,
                    reconciliation_state="quarantined",
                    filled_at=NOW - timedelta(minutes=8),
                ),
            ]
        )
        group = RuleGroup(
            group_key="unsafe-rule-group-posture",
            state="active",
            reconciliation_required=True,
        )
        session.add(group)
        session.flush()
        session.add(
            Rule(
                group_id=group.id,
                ticker="AAPL",
                condition_json=json.dumps(
                    {"prompt": NARRATIVE_MARKER}
                ),
                action_json=json.dumps(
                    {"tool_call": NARRATIVE_MARKER}
                ),
                state="active",
            )
        )
        session.commit()

    report = _reader(
        app_config=app_config,
        session_factory=session_factory,
    ).report(limit_principal="session:1:operator")

    breaker = next(
        check
        for check in _checks_by_name(report, "circuit_breaker")
        if check.scope == "liquidity"
    )
    heartbeat = _checks_by_name(report, "daemon_heartbeat")[0]
    reconciliation = _checks_by_name(
        report,
        "startup_reconciliation",
    )[0]
    tenure = _checks_by_name(report, "runtime_tenure")[0]
    unsafe_orders = _checks_by_name(report, "unsafe_orders")[0]
    unsafe_fills = _checks_by_name(report, "unsafe_fills")[0]
    unsafe_rules = _checks_by_name(report, "unsafe_rules")
    uncertain = _checks_by_name(report, "uncertain_interlocks")[0]
    failed_quarantine = next(
        check
        for check in _checks_by_name(report, "quarantine")
        if check.scope == "failed"
    )

    assert (breaker.scope, breaker.status.value, breaker.generation) == (
        "liquidity",
        "tripped",
        4,
    )
    assert breaker.count == 1
    assert heartbeat.status.value == "stale"
    assert reconciliation.status.value == "stale"
    assert reconciliation.generation == 7
    assert reconciliation.completed_generation == 7
    assert tenure.status.value == "stale"
    assert unsafe_orders.count == 1
    assert unsafe_fills.count == 1
    assert {check.scope: check.count for check in unsafe_rules} == {
        "rules": 1,
        "rule_groups": 1,
    }
    assert uncertain.count == 1
    assert failed_quarantine.count == 1
    encoded = report.model_dump_json().lower()
    for forbidden in (
        NARRATIVE_MARKER,
        "reason",
        "actor",
        "request_id",
        "evidence_json",
        "source_hash",
        "content_hash",
        "prompt",
        "tool_call",
    ):
        assert forbidden not in encoded


@pytest.mark.parametrize(
    ("table", "column", "check_name", "scope"),
    [
        ("orders", "status", "unsafe_orders", None),
        ("orders", "acceptance_state", "unsafe_orders", None),
        ("orders", "plan_cancel_state", "unsafe_orders", None),
        ("fills", "reconciliation_state", "unsafe_fills", None),
        ("rules", "state", "unsafe_rules", "rules"),
        ("rule_groups", "state", "unsafe_rules", "rule_groups"),
    ],
)
def test_unknown_persisted_state_domains_are_unknown_never_clear(
    app_config,
    session_factory,
    engine,
    table,
    column,
    check_name,
    scope,
):
    marker = "APP_API_TOKEN_CORRUPT_STATE"
    with session_factory() as session:
        order = Order(
            idempotency_key=f"unknown-domain-{table}-{column}",
            ticker="AAPL",
            side="buy",
            order_type="market",
            notional=Decimal("10"),
            status=OrderStatus.FILLED.value,
            acceptance_state="accepted",
            plan_cancel_state=PLAN_CANCEL_NONE,
        )
        group = RuleGroup(
            group_key=f"unknown-domain-{table}-{column}",
            state=RuleState.TRIGGERED.value,
            reconciliation_required=False,
        )
        persist_sensitive(
            session,
            order,
            {"approval_reason": "unknown domain fixture"},
        )
        session.add(group)
        session.flush()
        fill = Fill(
            order_id=order.id,
            ticker="AAPL",
            side="buy",
            qty=Decimal("1"),
            price=Decimal("100"),
            broker_fill_id=f"fill-{table}-{column}",
            reconciliation_state=FILL_RECONCILIATION_TRUSTED,
            filled_at=NOW - timedelta(minutes=1),
        )
        rule = Rule(
            group_id=group.id,
            ticker="AAPL",
            condition_json="{}",
            action_json="{}",
            state=RuleState.TRIGGERED.value,
        )
        session.add_all((fill, rule))
        session.commit()
        row_id = {
            "orders": order.id,
            "fills": fill.id,
            "rules": rule.id,
            "rule_groups": group.id,
        }[table]

    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"UPDATE {table} SET {column} = ? WHERE id = ?",
            (marker, row_id),
        )

    report = _reader(
        app_config=app_config,
        session_factory=session_factory,
    ).report(limit_principal="session:1:operator")
    check = next(
        item
        for item in _checks_by_name(report, check_name)
        if item.scope == scope
    )

    assert check.status.value == "unknown"
    assert check.detail_code.value == "state_domain_invalid"
    assert check.count is None
    assert marker not in report.model_dump_json()


def test_rule_group_rejects_rule_only_processing_state(
    app_config,
    session_factory,
):
    with session_factory() as session:
        session.add(
            RuleGroup(
                group_key="invalid-processing-group-state",
                state=RuleState.PROCESSING.value,
                reconciliation_required=False,
            )
        )
        session.commit()

    report = _reader(
        app_config=app_config,
        session_factory=session_factory,
    ).report(limit_principal="session:1:operator")
    check = next(
        item
        for item in _checks_by_name(report, "unsafe_rules")
        if item.scope == "rule_groups"
    )

    assert check.status.value == "unknown"
    assert check.detail_code.value == "state_domain_invalid"
    assert check.count is None


def test_breakers_are_aggregated_into_fixed_categories_without_targets(
    app_config,
    session_factory,
):
    rows = (
        ("operator_global", "operator_global", "", False, 2),
        ("data:equity", "data", "equity", False, 3),
        ("loss:crypto", "loss", "crypto", True, 4),
        (
            "liquidity:APP_API_TOKEN",
            "liquidity",
            "APP_API_TOKEN",
            True,
            5,
        ),
    )
    with session_factory() as session:
        for scope_key, kind, target, tripped, generation in rows:
            persist_sensitive(
                session,
                CircuitBreakerState(
                    scope_key=scope_key,
                    kind=kind,
                    target=target,
                    tripped=tripped,
                    actor=NARRATIVE_MARKER,
                    generation=generation,
                    updated_at=NOW - timedelta(minutes=1),
                ),
                {"reason": NARRATIVE_MARKER},
            )
        session.commit()

    report = _reader(
        app_config=app_config,
        session_factory=session_factory,
    ).report(limit_principal="session:1:operator")
    breakers = {
        check.scope: check
        for check in _checks_by_name(report, "circuit_breaker")
    }

    assert set(breakers) == {
        "account",
        "equity",
        "crypto",
        "liquidity",
    }
    assert (breakers["account"].status.value, breakers["account"].count) == (
        "clear",
        0,
    )
    assert (breakers["equity"].status.value, breakers["equity"].count) == (
        "clear",
        0,
    )
    assert (
        breakers["crypto"].status.value,
        breakers["crypto"].count,
        breakers["crypto"].generation,
    ) == ("tripped", 1, 4)
    assert (
        breakers["liquidity"].status.value,
        breakers["liquidity"].count,
        breakers["liquidity"].generation,
    ) == ("tripped", 1, 5)
    encoded = report.model_dump_json()
    assert "liquidity:APP_API_TOKEN" not in encoded
    assert "APP_API_TOKEN" not in encoded


def test_malformed_breaker_scope_makes_breaker_evidence_unknown_without_echo(
    app_config,
    session_factory,
):
    marker = "APP_API_TOKEN"
    with session_factory() as session:
        persist_sensitive(
            session,
            CircuitBreakerState(
                scope_key=f"liquidity:bad:{marker}",
                kind="liquidity",
                target=f"bad:{marker}",
                tripped=True,
                actor=NARRATIVE_MARKER,
                generation=1,
                updated_at=NOW - timedelta(minutes=1),
            ),
            {"reason": NARRATIVE_MARKER},
        )
        session.commit()

    report = _reader(
        app_config=app_config,
        session_factory=session_factory,
    ).report(limit_principal="session:1:operator")
    breakers = _checks_by_name(report, "circuit_breaker")

    assert breakers
    assert all(check.status.value == "unknown" for check in breakers)
    assert all(
        check.detail_code.value == "breaker_scope_invalid"
        for check in breakers
    )
    assert marker not in report.model_dump_json()


def test_malformed_breaker_generation_is_unknown_never_clear(
    app_config,
    session_factory,
):
    with session_factory() as session:
        persist_sensitive(
            session,
            CircuitBreakerState(
                scope_key="operator_global",
                kind="operator_global",
                target="",
                tripped=False,
                actor=NARRATIVE_MARKER,
                generation=0,
                updated_at=NOW - timedelta(minutes=1),
            ),
            {"reason": NARRATIVE_MARKER},
        )
        session.commit()

    report = _reader(
        app_config=app_config,
        session_factory=session_factory,
    ).report(limit_principal="session:1:operator")
    breakers = _checks_by_name(report, "circuit_breaker")

    assert breakers
    assert all(check.status.value == "unknown" for check in breakers)
    assert all(
        check.detail_code.value == "breaker_scope_invalid"
        for check in breakers
    )
    assert NARRATIVE_MARKER not in report.model_dump_json()


@pytest.mark.parametrize(
    (
        "generation",
        "completed_generation",
        "status",
        "started_at",
        "completed_at",
        "updated_at",
        "gate_current",
        "posture_status",
        "detail_code",
    ),
    [
        (
            3,
            3,
            "current",
            NOW - timedelta(minutes=2),
            NOW - timedelta(minutes=1),
            NOW - timedelta(seconds=30),
            True,
            "pass",
            "reconciliation_current",
        ),
        (
            3,
            2,
            "required",
            NOW - timedelta(minutes=2),
            None,
            NOW - timedelta(minutes=1),
            False,
            "blocked",
            "reconciliation_required",
        ),
        (
            0,
            0,
            "current",
            NOW - timedelta(minutes=2),
            NOW - timedelta(minutes=1),
            NOW - timedelta(seconds=30),
            False,
            "unknown",
            "reconciliation_evidence_invalid",
        ),
        (
            3,
            2,
            "current",
            NOW - timedelta(minutes=2),
            NOW - timedelta(minutes=1),
            NOW - timedelta(seconds=30),
            False,
            "unknown",
            "reconciliation_evidence_invalid",
        ),
        (
            3,
            3,
            "current",
            NOW + timedelta(seconds=1),
            NOW - timedelta(minutes=1),
            NOW - timedelta(seconds=30),
            False,
            "unknown",
            "reconciliation_evidence_invalid",
        ),
        (
            3,
            3,
            "current",
            NOW - timedelta(minutes=2),
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=2),
            False,
            "unknown",
            "reconciliation_evidence_invalid",
        ),
        (
            3,
            3,
            "current",
            NOW - timedelta(seconds=30),
            NOW - timedelta(minutes=1),
            NOW - timedelta(seconds=10),
            False,
            "unknown",
            "reconciliation_evidence_invalid",
        ),
        (
            3,
            3,
            "required",
            NOW - timedelta(minutes=2),
            NOW - timedelta(minutes=1),
            NOW - timedelta(seconds=30),
            False,
            "unknown",
            "reconciliation_evidence_invalid",
        ),
    ],
    ids=[
        "fresh-current",
        "required",
        "zero-generation",
        "incomplete-current",
        "future-start",
        "future-completion",
        "timestamps-out-of-order",
        "required-with-completion",
    ],
)
def test_reconciliation_posture_matches_authoritative_safe_column_gate(
    app_config,
    session_factory,
    make_service,
    monkeypatch,
    generation,
    completed_generation,
    status,
    started_at,
    completed_at,
    updated_at,
    gate_current,
    posture_status,
    detail_code,
):
    with session_factory() as session:
        persist_sensitive(
            session,
            StartupReconciliationState(
                broker="mock",
                generation=generation,
                completed_generation=completed_generation,
                status=status,
                actor=NARRATIVE_MARKER,
                request_id=NARRATIVE_MARKER,
                started_at=started_at,
                completed_at=completed_at,
                updated_at=updated_at,
            ),
            {
                "reason": NARRATIVE_MARKER,
                "evidence_json": json.dumps(
                    {"external": NARRATIVE_MARKER}
                ),
            },
        )
        session.commit()

    gate = StartupReconciliationGate(
        session_factory,
        "mock",
        enabled=True,
        clock=lambda: NOW,
    )
    decrypt_calls = 0

    def forbidden_decrypt(*_args, **_kwargs):
        nonlocal decrypt_calls
        decrypt_calls += 1
        raise AssertionError("reconciliation authority decrypted narrative")

    monkeypatch.setattr(
        SensitiveDataCipher,
        "decrypt",
        forbidden_decrypt,
    )
    report = _reader(
        app_config=app_config,
        session_factory=session_factory,
    ).report(limit_principal="session:1:operator")
    check = _checks_by_name(
        report,
        "startup_reconciliation",
    )[0]
    service = make_service(quote_now=lambda: NOW)
    service.snapshot_service.now = lambda: NOW
    service.snapshot_service.startup_reconciliation_key = "mock"
    snapshot = service.snapshot_service.assemble_for_confirmation("AAPL")
    risk = service.risk.check(
        OrderRequest(
            ticker="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            idempotency_key="reconciliation-authority-parity",
            notional=Decimal("100"),
        ),
        snapshot,
    )

    assert gate.is_current() is gate_current
    assert check.status.value == posture_status
    assert check.detail_code.value == detail_code
    assert snapshot.broker_reconciled is gate_current
    assert risk.approved is gate_current
    assert (
        "broker reconciliation is not current" in risk.reasons
    ) is (not gate_current)
    assert decrypt_calls == 0
    assert NARRATIVE_MARKER not in report.model_dump_json()


def test_posture_is_repeatable_concurrent_and_preserves_every_table(
    app_config,
    session_factory,
):
    reader = _reader(
        app_config=app_config,
        session_factory=session_factory,
    )
    before = _snapshot_all_tables(session_factory)

    first = reader.report(limit_principal="session:9:operator")
    second = reader.report(limit_principal="session:9:operator")
    with ThreadPoolExecutor(max_workers=4) as pool:
        concurrent = list(
            pool.map(
                lambda _index: reader.report(
                    limit_principal="session:9:operator"
                ),
                range(8),
            )
        )

    assert second == first
    assert all(report == first for report in concurrent)
    assert _snapshot_all_tables(session_factory) == before


def test_database_failure_keeps_config_and_startup_checks_reportable(
    app_config,
):
    posture = _posture_module()
    context, secrets = _consumed_startup_context(app_config)

    class BrokenFactory:
        def __call__(self):
            raise OSError(NARRATIVE_MARKER)

    reader = posture.SecurityPostureService(
        config=app_config,
        session_factory=BrokenFactory(),
        reconciliation_key="mock",
        reconciliation_enabled=True,
        _consumed_startup_guard=context,
        _startup_secrets=secrets,
        _startup_runtime_role="app",
        clock=lambda: NOW,
    )

    report = reader.report(limit_principal="session:1:operator")
    checks = {
        check.name.value: check
        for check in report.checks
        if check.name.value
        in {
            "broker_mode",
            "loopback_https",
            "tls",
            "unsafe_orders",
            "daemon_heartbeat",
        }
    }
    assert checks["broker_mode"].status.value == "paper"
    assert checks["loopback_https"].status.value == "pass"
    assert checks["tls"].status.value == "pass"
    assert checks["unsafe_orders"].status.value == "unknown"
    assert checks["daemon_heartbeat"].status.value == "unknown"
    breaker_checks = _checks_by_name(report, "circuit_breaker")
    assert {
        check.scope for check in breaker_checks
    } == {"account", "equity", "crypto", "liquidity"}
    assert all(
        check.status.value == "unknown" for check in breaker_checks
    )
    assert NARRATIVE_MARKER not in report.model_dump_json()


def test_request_and_provider_exhaustion_are_read_only_blocked_evidence(
    app_config,
    session_factory,
):
    session_limit = (
        app_config.security.rate_limits.session_read.model_copy(
            update={"requests": 1, "global_requests": 1}
        )
    )
    limits = app_config.security.rate_limits.model_copy(
        update={"session_read": session_limit}
    )
    provider_config = (
        app_config.security.provider_budget.model_copy(
            update={"daily_calls": 1}
        )
    )
    config = app_config.model_copy(
        update={
            "security": app_config.security.model_copy(
                update={
                    "rate_limits": limits,
                    "provider_budget": provider_config,
                }
            )
        }
    )
    principal = "session:11:operator"
    limiter = DurableRateLimiter(session_factory)
    from trading_assistant.app.limits import LimitSpec

    limiter.consume_pair(
        LimitSpec(
            name="session_read",
            principal_requests=1,
            global_requests=1,
            window_seconds=session_limit.window_seconds,
        ),
        principal=principal,
        now=NOW,
    )
    budget = ProviderBudgetService(
        session_factory,
        BudgetLimits(
            calls=1,
            input_tokens=provider_config.daily_input_tokens,
            output_tokens=provider_config.daily_output_tokens,
            reservation_ttl_seconds=60,
        ),
        clock=lambda: NOW,
    )
    created_at = NOW - timedelta(minutes=3)
    reservation = budget.reserve(
        provider="gemini",
        category="chat",
        request_id="3" * 32,
        input_tokens=10,
        output_tokens=10,
        now=created_at,
    )
    budget.mark_started(
        reservation.reservation_id,
        now=created_at + timedelta(seconds=1),
    )
    before = _snapshot_all_tables(session_factory)
    posture = _posture_module()
    reader = posture.SecurityPostureService(
        config=config,
        session_factory=session_factory,
        reconciliation_key="mock",
        reconciliation_enabled=False,
        rate_limiter=limiter,
        provider_budget=budget,
        clock=lambda: NOW,
    )

    report = reader.report(limit_principal=principal)

    request = next(
        check
        for check in _checks_by_name(report, "request_budget")
        if check.scope == "session_read"
    )
    provider = next(
        check
        for check in _checks_by_name(report, "provider_budget")
        if check.scope == "gemini"
    )
    assert request.status.value == "blocked"
    assert request.detail_code.value == "request_budget_exhausted"
    assert request.budget_remaining == 0
    assert provider.status.value == "blocked"
    assert (
        provider.detail_code.value
        == "provider_reconciliation_required"
    )
    assert provider.count == 1
    assert _snapshot_all_tables(session_factory) == before


def test_posture_never_calls_decrypting_encryption_inspection_or_cipher(
    app_config,
    session_factory,
    engine,
    monkeypatch,
):
    from trading_assistant import preflight
    from trading_assistant.ops import encrypt_sensitive

    active_key_id = app_config.encryption.active_key_id
    writer_cipher = SensitiveDataCipher(
        {active_key_id: b"a" * 32},
        active_key_id=active_key_id,
    )
    started_at = NOW - timedelta(minutes=3)
    completed_at = NOW - timedelta(minutes=2)
    encrypted_reason = writer_cipher.encrypt(
        NARRATIVE_MARKER,
        SensitiveFieldRef("audit_events", "1", "reason", 1),
    )
    encrypted_detail = writer_cipher.encrypt(
        json.dumps({"external": NARRATIVE_MARKER}),
        SensitiveFieldRef(
            "audit_events",
            "1",
            "detail_json",
            1,
        ),
    )
    with sqlite3.connect(engine.url.database) as connection:
        connection.execute(
            "INSERT INTO sensitive_migration_state "
            "(singleton_id,schema_version,state,active_key_id,"
            "rows_total,rows_completed,backup_path_hash,started_at,"
            "completed_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                1,
                1,
                "complete",
                active_key_id,
                1,
                1,
                "c" * 64,
                started_at.isoformat(),
                completed_at.isoformat(),
                (NOW - timedelta(minutes=1)).isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO audit_events "
            "(id,actor,action,target_type,target_id,request_id,"
            "idempotency_key,reason,result_code,latency_ms,"
            "detail_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                1,
                "operator:test",
                "posture.mixed",
                "test",
                "1",
                "mixed-posture",
                "",
                encrypted_reason,
                "created",
                0,
                encrypted_detail,
                (NOW - timedelta(minutes=1)).isoformat(),
            ),
        )
        connection.commit()

    calls = {"inspector": 0, "scan": 0, "decrypt": 0}
    real_inspect = preflight.SensitiveEncryptionStateInspector.inspect
    real_scan = encrypt_sensitive.inspect_sensitive_envelopes

    def counted_inspect(inspector):
        calls["inspector"] += 1
        return real_inspect(inspector)

    def counted_scan(*args, **kwargs):
        calls["scan"] += 1
        return real_scan(*args, **kwargs)

    class RaisingCipher(SensitiveDataCipher):
        def decrypt(self, *args, **kwargs):
            calls["decrypt"] += 1
            raise AssertionError("posture attempted ciphertext decryption")

    raising_cipher = RaisingCipher(
        {active_key_id: b"a" * 32},
        active_key_id=active_key_id,
    )
    monkeypatch.setattr(
        preflight.SensitiveEncryptionStateInspector,
        "inspect",
        counted_inspect,
    )
    monkeypatch.setattr(
        encrypt_sensitive,
        "inspect_sensitive_envelopes",
        counted_scan,
    )
    reader = _reader(
        app_config=app_config,
        session_factory=session_factory,
    )
    reader._engine = engine
    reader._sensitive_cipher = raising_cipher

    report = reader.report(limit_principal="session:1:operator")
    encryption = _checks_by_name(
        report,
        "sensitive_encryption",
    )[0]

    assert calls == {"inspector": 0, "scan": 0, "decrypt": 0}
    assert encryption.status.value == "unknown"
    assert (
        encryption.detail_code.value
        == "startup_evidence_unavailable"
    )
    assert encryption.migration_state == "complete"
    assert encryption.schema_version == 1
    assert encryption.rows_total == 1
    assert encryption.rows_completed == 1
    encoded = report.model_dump_json()
    assert active_key_id not in encoded
    assert NARRATIVE_MARKER not in encoded


def test_complete_encryption_uses_canonical_startup_receipt_and_safe_columns(
    app_config,
    session_factory,
):
    active_key_id = app_config.encryption.active_key_id
    with session_factory() as session:
        session.add(
            SensitiveMigrationState(
                singleton_id=1,
                schema_version=1,
                state="complete",
                active_key_id=active_key_id,
                rows_total=0,
                rows_completed=0,
                backup_path_hash="d" * 64,
                started_at=NOW - timedelta(minutes=3),
                completed_at=NOW - timedelta(minutes=2),
                updated_at=NOW - timedelta(minutes=1),
            )
        )
        session.commit()

    context, secrets = _consumed_startup_context(app_config)
    report = _reader(
        app_config=app_config,
        session_factory=session_factory,
        consumed_startup_guard=context,
        startup_secrets=secrets,
    ).report(limit_principal="session:1:operator")
    encryption = _checks_by_name(
        report,
        "sensitive_encryption",
    )[0]

    assert encryption.status.value == "pass"
    assert encryption.detail_code.value == "ok"
    assert encryption.migration_state == "complete"
    assert encryption.rows_total == 0
    assert active_key_id not in report.model_dump_json()


def test_route_access_never_loads_keychain_or_calls_broker(
    authenticated_client,
    monkeypatch,
):
    from trading_assistant import preflight
    from trading_assistant.ops import encrypt_sensitive

    client, _csrf = authenticated_client
    service = client.trading_service
    session_factory = service.session_factory

    def forbidden(*_args, **_kwargs):
        raise AssertionError("posture attempted forbidden I/O")

    for name in (
        "get_quote",
        "get_positions",
        "get_open_orders",
        "get_account",
        "submit_order",
        "cancel_order",
    ):
        if hasattr(service.broker, name):
            monkeypatch.setattr(service.broker, name, forbidden)
    from trading_assistant.security.secrets import (
        MacOSKeychainSecretProvider,
    )

    monkeypatch.setattr(
        MacOSKeychainSecretProvider,
        "__init__",
        forbidden,
    )
    monkeypatch.setattr(
        ProviderBudgetService,
        "status",
        forbidden,
    )
    monkeypatch.setattr(
        ProviderBudgetService,
        "reserve",
        forbidden,
    )
    monkeypatch.setattr(NullNotifier, "send", forbidden)
    monkeypatch.setattr(
        preflight.SensitiveEncryptionStateInspector,
        "inspect",
        forbidden,
    )
    monkeypatch.setattr(
        encrypt_sensitive,
        "inspect_sensitive_envelopes",
        forbidden,
    )
    monkeypatch.setattr(SensitiveDataCipher, "decrypt", forbidden)
    before = _snapshot_all_tables(session_factory)

    first = client.get("/security/posture")
    second = client.get("/security/posture")
    with ThreadPoolExecutor(max_workers=4) as pool:
        concurrent = list(
            pool.map(
                lambda _index: client.get("/security/posture"),
                range(4),
            )
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["can_trade"] is False
    assert second.json()["can_trade"] is False
    assert all(response.status_code == 200 for response in concurrent)
    assert all(
        response.json()["can_trade"] is False
        for response in concurrent
    )
    after = _snapshot_all_tables(session_factory)
    assert (
        after[ConcurrencyLease.__tablename__]
        == before[ConcurrencyLease.__tablename__]
    )
    for table_name in before:
        if table_name == RateWindow.__tablename__:
            continue
        assert after[table_name] == before[table_name], table_name


def test_no_production_consumer_treats_posture_as_authority():
    source_root = Path("src/trading_assistant")
    consumers = [
        path
        for path in source_root.rglob("*.py")
        if path.name != "security_posture.py"
        and "can_trade" in path.read_text(encoding="utf-8")
    ]

    assert consumers == []
