"""One fail-closed production composition root and runtime safety helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from trading_assistant.app.auth import SessionAuth
from trading_assistant.app.main import create_app
from trading_assistant.config import BrokerKind, Secrets, TradingMode
from trading_assistant.db.migrate import upgrade
from trading_assistant.db.schema import SchemaOutOfDate
from trading_assistant.db.session import create_db_engine
from trading_assistant.operations import AuditRecorder, OperationsService


def _migrated_secrets(tmp_path: Path) -> Secrets:
    database_url = f"sqlite:///{tmp_path}/runtime.db"
    upgrade(create_db_engine(database_url))
    return Secrets(
        database_url=database_url,
        app_api_token="operator-secret-for-bootstrap-tests",
    )


def _injected_container(service, secrets):
    audit = AuditRecorder(service.session_factory)
    return SimpleNamespace(
        config=service.config,
        secrets=secrets,
        service=service,
        session_factory=service.session_factory,
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
        lambda config, supplied: seen.append(
            ("backend_secrets", supplied)
        )
        or object(),
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
        json={"secret": secrets.app_api_token},
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
    roles: list[str | None] = []
    container = SimpleNamespace(
        service=service,
        rule_worker=SimpleNamespace(notifier=None),
        audit=AuditRecorder(service.session_factory),
    )

    def capture_container(*args, **kwargs):
        roles.append(kwargs.get("runtime_role"))
        return container

    monkeypatch.setattr(bootstrap, "build_container", capture_container)
    monkeypatch.setattr(app_main, "load_config", lambda: config)
    monkeypatch.setattr(app_main, "Secrets", lambda: secrets)
    monkeypatch.setattr(daemon_main, "load_config", lambda: config)
    monkeypatch.setattr(daemon_main, "Secrets", lambda: secrets)
    monkeypatch.setattr(
        daemon_main,
        "build_notifier",
        lambda supplied_config, supplied_secrets: object(),
    )
    monkeypatch.setattr(mcp_server, "load_config", lambda: config)
    monkeypatch.setattr(mcp_server, "Secrets", lambda: secrets)

    assert app_main.build_default_container() is container
    assert daemon_main.build_monitor().service is service
    assert mcp_server.build_default_service() is service
    assert roles == ["app", "daemon", "mcp"]


def test_application_container_reuses_exact_trading_service_components(
    tmp_path,
    app_config,
):
    from trading_assistant.bootstrap import build_container

    container = build_container(
        app_config,
        _migrated_secrets(tmp_path),
    )

    assert container.snapshot_service is container.service.snapshot_service
    assert container.order_application is container.service.order_application
    assert container.order_submission is container.service.order_submission
    assert container.reconciliation is container.service.reconciliation
    assert container.breakers is container.service.breakers
    assert container.rule_worker.service is container.service
    assert container.rule_worker.repository is container.service.rule_repository
    assert container.session_auth.session_factory is container.session_factory


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

    unsafe = app_config.model_copy(update=config_update(app_config))

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
            app_config,
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
