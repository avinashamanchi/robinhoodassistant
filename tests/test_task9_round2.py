"""Independent review round 2: exact composition and production runtimes."""

from __future__ import annotations

import base64
from contextlib import nullcontext
from decimal import Decimal
import logging
from pathlib import Path
import plistlib
import subprocess
from types import SimpleNamespace

import pytest

from tests.app_factory import create_app
from trading_assistant.broker.mock import MockBroker
from trading_assistant.config import BrokerKind, Secrets
from trading_assistant.db.migrate import upgrade
from trading_assistant.db.session import create_db_engine
from trading_assistant.risk.clock import FakeClock


class _Agent:
    def chat(self, message, **context):
        return {"reply": message, "context": context}


@pytest.mark.parametrize(
    ("service_present", "agent_present"),
    [(True, False), (False, True)],
)
def test_create_app_rejects_partial_explicit_injection_without_ambient_reads(
    make_service,
    monkeypatch,
    service_present,
    agent_present,
):
    import trading_assistant.app.main as app_main

    monkeypatch.setattr(
        app_main,
        "load_role_secrets",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ambient providers must not be read")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        app_main,
        "build_default_stack",
        lambda: (_ for _ in ()).throw(
            AssertionError("partial injection must not build a second stack")
        ),
    )

    with pytest.raises(RuntimeError, match="service and agent"):
        create_app(
            service=make_service() if service_present else None,
            agent=_Agent() if agent_present else None,
            api_token="complete-explicit-token",
            planning=None,
        )


def test_create_app_complete_explicit_injection_never_reads_ambient_secrets(
    make_service,
    monkeypatch,
):
    import trading_assistant.app.main as app_main

    service = make_service()
    agent = _Agent()
    monkeypatch.setattr(
        app_main,
        "load_role_secrets",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ambient providers must not be read")
        ),
        raising=False,
    )

    app = create_app(
        service=service,
        agent=agent,
        api_token="complete-explicit-token",
        planning=None,
        bind_host="127.0.0.1",
    )

    from trading_assistant import bootstrap

    assert bootstrap._is_test_application_container(
        app.state.container
    )
    assert app.state.trading_service is service
    assert app.state.agent is agent
    assert (
        app.state.runtime_secrets.app_api_token.get_secret_value()
        == "complete-explicit-token"
    )


def test_create_app_outside_container_requires_shared_container_for_auto_planning(
    make_service,
):
    with pytest.raises(RuntimeError, match="shared ApplicationContainer"):
        create_app(
            service=make_service(),
            agent=_Agent(),
            api_token="complete-explicit-token",
        )


def test_explicit_runtime_secrets_cannot_bypass_shared_planning_budget(
    make_service,
    monkeypatch,
):
    from trading_assistant.llm import factory as llm_factory

    secrets = Secrets(app_api_token="explicit-planning-token")
    constructed = []
    monkeypatch.setattr(
        llm_factory,
        "build_llm_backend",
        lambda *_args, **_kwargs: constructed.append(True) or object(),
    )
    service = make_service()

    with pytest.raises(RuntimeError, match="shared ApplicationContainer"):
        create_app(
            service=service,
            agent=_Agent(),
            api_token=secrets.app_api_token,
            runtime_secrets=secrets,
        )

    assert constructed == []


def test_automatic_app_root_refuses_before_container_construction(
    monkeypatch,
):
    import trading_assistant.app.main as app_main
    from trading_assistant import bootstrap

    built = []
    monkeypatch.setattr(
        bootstrap,
        "_build_guarded_container",
        lambda *_args, **_kwargs: built.append(True),
    )

    with pytest.raises(
        RuntimeError,
        match="^production_startup_guard_required$",
    ):
        app_main.create_app()

    assert built == []


def test_mcp_startup_failure_prevents_transport_run(monkeypatch):
    from trading_assistant.mcp_server import server

    calls = {"build": 0, "run": 0}

    def fail_startup():
        calls["build"] += 1
        raise RuntimeError("schema gate failed")

    monkeypatch.setattr(
        server,
        "build_default_container",
        fail_startup,
        raising=False,
    )
    monkeypatch.setattr(
        server.mcp,
        "run",
        lambda: calls.__setitem__("run", calls["run"] + 1),
    )

    with pytest.raises(RuntimeError, match="schema gate failed"):
        server.main()

    assert calls == {"build": 1, "run": 0}


def test_mcp_valid_startup_configures_exact_container_once(
    make_service,
    monkeypatch,
):
    from trading_assistant.mcp_server import server

    service = make_service()
    audit = object()
    guard = SimpleNamespace(
        lost=False,
        set_on_lost=lambda _callback: None,
        close=lambda: True,
    )
    container = SimpleNamespace(
        service=service,
        audit=audit,
        secrets=Secrets(app_api_token="mcp-valid-startup-secret"),
        runtime_tenure_guard=guard,
    )
    calls = {"build": 0, "run": 0}

    def build_once():
        calls["build"] += 1
        return container

    monkeypatch.setattr(
        server,
        "build_default_container",
        build_once,
        raising=False,
    )
    async def run_stdio_async():
        calls["run"] += 1

    monkeypatch.setattr(server.mcp, "run_stdio_async", run_stdio_async)
    server._service = None
    server._audit = None

    server.main()

    assert calls == {"build": 1, "run": 1}
    assert server._service is service
    assert server._audit is audit


def test_mcp_transport_startup_failure_is_in_role_log(
    tmp_path,
    make_service,
    monkeypatch,
):
    from trading_assistant.mcp_server import server

    marker = "mcp-transport-startup-secret"
    service = make_service()
    guard = SimpleNamespace(
        lost=False,
        set_on_lost=lambda _callback: None,
        close=lambda: True,
    )
    container = SimpleNamespace(
        service=service,
        audit=object(),
        secrets=Secrets(app_api_token=marker),
        runtime_tenure_guard=guard,
    )
    monkeypatch.setattr(
        server,
        "build_default_container",
        lambda: container,
    )
    async def run_stdio_async():
        raise RuntimeError(marker)

    monkeypatch.setattr(server.mcp, "run_stdio_async", run_stdio_async)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RuntimeError, match=marker):
        server.main()

    content = (
        tmp_path / "logs" / "mcp.runtime.log"
    ).read_text(encoding="utf-8")
    assert "startup_failed role=mcp" in content
    assert marker not in content


@pytest.mark.parametrize(
    "runtime_role",
    [
        "app",
        "daemon",
        "mcp",
        "preflight",
        "paper-drill",
        "watchdog",
        "backup",
    ],
)
def test_every_production_role_has_private_bounded_startup_log(
    tmp_path,
    monkeypatch,
    runtime_role,
):
    from logging.handlers import RotatingFileHandler
    import trading_assistant.logging as runtime_logging

    marker = f"{runtime_role}-secret-marker"
    secrets = Secrets(app_api_token=marker)
    monkeypatch.chdir(tmp_path)
    startup = getattr(runtime_logging, "runtime_startup", None)
    assert startup is not None

    with pytest.raises(RuntimeError, match="startup probe"):
        with startup(runtime_role, secrets):
            raise RuntimeError(f"startup probe {marker}")

    path = tmp_path / "logs" / f"{runtime_role}.runtime.log"
    handlers = [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_trading_assistant_path", None)
        == str(path)
    ]
    assert len(handlers) == 1
    assert isinstance(handlers[0], RotatingFileHandler)
    assert handlers[0].maxBytes > 0
    assert handlers[0].backupCount > 0
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    content = path.read_text(encoding="utf-8")
    assert f"startup_failed role={runtime_role}" in content
    assert marker not in content


@pytest.mark.parametrize(
    ("builder_name", "runtime_role"),
    [
        ("paper_drill", "paper-drill"),
    ],
)
def test_service_utility_roots_pass_exact_role_and_secrets(
    app_config,
    make_service,
    monkeypatch,
    builder_name,
    runtime_role,
):
    from trading_assistant import bootstrap, preflight
    from trading_assistant.ops import paper_drill

    service = make_service()
    secrets = Secrets(app_api_token="utility-role-secret")
    observed = []

    def build(config, supplied_secrets, **kwargs):
        observed.append(
            (config, supplied_secrets, kwargs.get("runtime_role"))
        )
        return SimpleNamespace(
            service=service,
            runtime_tenure_guard=None,
        )

    monkeypatch.setattr(bootstrap, "build_container", build)
    owner = paper_drill.build_paper_service(app_config, secrets)

    with owner as result:
        assert result is service
    assert observed == [(app_config, secrets, runtime_role)]


def test_preflight_uses_dedicated_non_llm_service_composition(
    app_config,
    make_service,
    monkeypatch,
):
    from trading_assistant import bootstrap, preflight

    service = make_service()
    secrets = Secrets(app_api_token="preflight-role-secret")
    observed: list[tuple[object, object]] = []

    def build_preflight(config, supplied_secrets):
        observed.append((config, supplied_secrets))
        return SimpleNamespace(
            service=service,
            runtime_tenure_guard=None,
        )

    monkeypatch.setattr(
        bootstrap,
        "build_preflight_service",
        build_preflight,
        raising=False,
    )
    monkeypatch.setattr(
        bootstrap,
        "build_container",
        lambda *_args, **_kwargs: pytest.fail(
            "preflight must not use the app/LLM container"
        ),
    )
    monkeypatch.setattr(
        bootstrap,
        "build_quarantine_summarizer",
        lambda *_args, **_kwargs: pytest.fail(
            "preflight must never construct an LLM provider"
        ),
    )

    with preflight._build_service(app_config, secrets) as result:
        assert result is service

    assert observed == [(app_config, secrets)]


def test_dedicated_preflight_builder_constructs_no_llm_capability(
    app_config,
    monkeypatch,
):
    from trading_assistant import bootstrap

    secrets = Secrets(app_api_token="preflight-builder-secret")
    session_factory = object()
    runtime = SimpleNamespace(
        engine=object(),
        session_factory=session_factory,
    )
    broker = object()
    service = object()
    observed: list[tuple[str, object]] = []

    monkeypatch.setattr(
        bootstrap,
        "require_configured_role_origins",
        lambda config, role: observed.append(("origins", role)),
    )
    monkeypatch.setattr(
        bootstrap,
        "_guard_runtime",
        lambda config, supplied: observed.append(("guard", supplied)),
    )
    monkeypatch.setattr(
        bootstrap,
        "prepare_database_runtime",
        lambda supplied, *, runtime_role: (
            observed.append(("database_role", runtime_role)) or runtime
        ),
    )
    monkeypatch.setattr(
        bootstrap,
        "build_sensitive_data_cipher",
        lambda *_args, **_kwargs: pytest.fail(
            "read-only preflight must not construct a field cipher"
        ),
    )
    monkeypatch.setattr(
        bootstrap,
        "bind_sensitive_cipher",
        lambda *_args, **_kwargs: pytest.fail(
            "read-only preflight must not install write/decrypt hooks"
        ),
    )
    monkeypatch.setattr(
        bootstrap,
        "build_broker",
        lambda config, supplied, *, runtime_role: (
            observed.append(("broker_role", runtime_role)) or broker
        ),
    )
    monkeypatch.setattr(
        bootstrap,
        "_arm_production_paper_broker",
        lambda supplied_broker: observed.append(
            ("arm_broker", supplied_broker)
        ),
    )
    monkeypatch.setattr(
        bootstrap,
        "build_clock",
        lambda *_args, **_kwargs: pytest.fail(
            "read-only reconciliation must not construct a clock client"
        ),
    )
    monkeypatch.setattr(
        bootstrap,
        "TradingService",
        lambda *_args, **_kwargs: pytest.fail(
            "preflight must not expose mutable TradingService"
        ),
    )
    monkeypatch.setattr(
        bootstrap,
        "ReadOnlyPreflightService",
        lambda supplied_broker, factory: (
            observed.append(
                ("read_only_service", (supplied_broker, factory))
            )
            or service
        ),
    )
    monkeypatch.setattr(
        bootstrap,
        "build_quarantine_summarizer",
        lambda *_args, **_kwargs: pytest.fail(
            "dedicated preflight composition reached an LLM builder"
        ),
    )

    container = bootstrap.build_preflight_service(app_config, secrets)

    assert container.service is service
    assert container.runtime_tenure_guard is None
    assert ("origins", "preflight") in observed
    assert ("database_role", "preflight") in observed
    assert ("broker_role", "preflight") in observed
    assert (
        "read_only_service",
        (broker, session_factory),
    ) in observed


def test_watchdog_database_runtime_reuses_explicit_secrets_and_role(
    monkeypatch,
):
    from trading_assistant import bootstrap
    from trading_assistant.ops import watchdog

    secrets = Secrets(
        database_url="sqlite:///watchdog-explicit.db",
        app_api_token="watchdog-role-secret",
    )
    observed = []

    def prepare(supplied_secrets, **kwargs):
        observed.append(
            (supplied_secrets, kwargs.get("runtime_role"))
        )
        raise RuntimeError("stop after composition probe")

    monkeypatch.setattr(
        bootstrap,
        "prepare_database_runtime",
        prepare,
    )

    result = watchdog.read_database_health(
        secrets=secrets,
        runtime_role="watchdog",
    )

    assert result == {
        "db_ok": False,
        "heartbeat_age_seconds": None,
    }
    assert observed == [(secrets, "watchdog")]


def test_watchdog_main_reuses_one_secret_and_passes_runtime_role(
    tmp_path,
    monkeypatch,
):
    from trading_assistant.ops import watchdog

    secrets = Secrets(app_api_token="watchdog-main-secret")
    calls = {"secrets": 0}
    observed = []
    liveness_transport = object()

    def one_secrets(role, *, config):
        calls["secrets"] += 1
        assert role == "watchdog"
        return secrets

    monkeypatch.setattr(
        watchdog,
        "load_role_secrets",
        one_secrets,
        raising=False,
    )
    monkeypatch.setattr(
        watchdog,
        "load_config",
        lambda: SimpleNamespace(
            daemon=SimpleNamespace(heartbeat_stale_seconds=180),
            server=SimpleNamespace(
                tls_ca_path=".local/tls/rootCA.pem"
            ),
        ),
    )
    monkeypatch.setattr(
        watchdog,
        "build_local_liveness_transport",
        lambda ca_certificate_path: (
            liveness_transport
            if str(ca_certificate_path) == ".local/tls/rootCA.pem"
            else (_ for _ in ()).throw(
                AssertionError("noncanonical liveness CA")
            )
        ),
    )

    def fetch_health(_url, _timeout, *, transport):
        assert transport is liveness_transport
        return {
            "alive": True,
            "database_reachable": True,
        }

    monkeypatch.setattr(watchdog, "fetch_health", fetch_health)

    def database_health(**kwargs):
        observed.append(
            (kwargs.get("secrets"), kwargs.get("runtime_role"))
        )
        return {"db_ok": True, "heartbeat_age_seconds": 10}

    monkeypatch.setattr(
        watchdog,
        "read_database_health",
        database_health,
    )
    monkeypatch.chdir(tmp_path)

    assert watchdog.main([]) == 0
    assert calls == {"secrets": 1}
    assert observed == [(secrets, "watchdog")]


def test_daemon_main_logs_startup_reconciliation_failure_with_one_secret(
    tmp_path,
    monkeypatch,
):
    from logging.handlers import RotatingFileHandler
    from trading_assistant.daemon import main as daemon_main

    marker = "daemon-startup-reconciliation-secret"
    secrets = Secrets(app_api_token=marker)
    config = object()
    secret_reads = 0
    observed = []

    class ReconciliationFailureMonitor:
        async def run(self):
            raise RuntimeError(marker)

    def one_secrets(role, *, config):
        nonlocal secret_reads
        secret_reads += 1
        assert role == "daemon"
        assert config is not None
        return secrets

    def build(supplied_config, supplied_secrets):
        observed.append((supplied_config, supplied_secrets))
        return ReconciliationFailureMonitor()

    monkeypatch.setattr(
        daemon_main,
        "load_role_secrets",
        one_secrets,
        raising=False,
    )
    monkeypatch.setattr(daemon_main, "load_config", lambda: config)
    monkeypatch.setattr(daemon_main, "_build_monitor", build)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RuntimeError, match=marker):
        daemon_main.main()

    path = tmp_path / "logs" / "daemon.runtime.log"
    handlers = [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_trading_assistant_path", None)
        == str(path)
    ]
    assert secret_reads == 1
    assert observed == [(config, secrets)]
    assert len(handlers) == 1
    assert isinstance(handlers[0], RotatingFileHandler)
    assert handlers[0].maxBytes > 0
    assert handlers[0].backupCount > 0
    assert path.stat().st_mode & 0o777 == 0o600
    content = path.read_text(encoding="utf-8")
    assert "startup_failed role=daemon" in content
    assert marker not in content


@pytest.mark.parametrize(
    ("module_name", "runtime_role"),
    [
        ("preflight", "preflight"),
        ("paper_drill", "paper-drill"),
    ],
)
def test_utility_main_reuses_one_secret_and_role_log(
    tmp_path,
    monkeypatch,
    module_name,
    runtime_role,
):
    from trading_assistant import preflight
    from trading_assistant.ops import paper_drill

    module = preflight if module_name == "preflight" else paper_drill
    secrets = Secrets(app_api_token=f"{runtime_role}-main-secret")
    calls = {"secrets": 0}
    observed = []

    selected_provider = None
    if module_name == "preflight":
        from trading_assistant.security.secrets import (
            MacOSKeychainSecretProvider,
        )

        selected_provider = MacOSKeychainSecretProvider(backend=object())

    def one_secrets(role, *, config, provider=None):
        calls["secrets"] += 1
        assert role == runtime_role
        if module_name == "preflight":
            assert provider is selected_provider
        return secrets

    monkeypatch.setattr(
        module,
        "load_role_secrets",
        one_secrets,
        raising=False,
    )
    config = SimpleNamespace()
    monkeypatch.setattr(module, "load_config", lambda *_args: config)
    monkeypatch.chdir(tmp_path)
    if module_name == "preflight":
        monkeypatch.setattr(
            module,
            "_run",
            lambda supplied_config, supplied_secrets, **_kwargs: (
                observed.append((supplied_config, supplied_secrets)) or 0
            ),
        )
        result = module.run(provider=selected_provider)
    else:
        service = object()
        monkeypatch.setattr(
            module,
            "build_paper_service",
            lambda supplied_config, supplied_secrets: (
                observed.append(
                    (supplied_config, supplied_secrets)
                )
                or nullcontext(service)
            ),
        )
        monkeypatch.setattr(
            module,
            "run_paper_drill",
            lambda supplied_config, supplied_service, **_kwargs: {
                "ok": (
                    supplied_config is config
                    and supplied_service is service
                )
            },
        )
        result = module.main([])

    assert result == 0
    assert calls == {"secrets": 1}
    assert observed == [(config, secrets)]
    assert (
        tmp_path / "logs" / f"{runtime_role}.runtime.log"
    ).exists()


def test_backup_main_reuses_one_secret_and_writes_role_log(
    tmp_path,
    monkeypatch,
    app_config,
):
    import trading_assistant.logging as runtime_logging
    from trading_assistant.ops import backup
    from trading_assistant.ops.tenure import (
        ProcessIdentity,
        ProcessProof,
    )

    source = tmp_path / "source.sqlite3"
    engine = create_db_engine(f"sqlite:///{source}")
    assert upgrade(engine) is None
    engine.dispose()
    destination = tmp_path / "backups"
    backup_key = b"task9-encrypted-backup-key-32byt"
    assert len(backup_key) == 32
    secrets = Secrets(
        database_url=f"sqlite:///{source}",
        app_api_token="backup-role-secret",
        backup_encryption_key=base64.b64encode(backup_key).decode(),
    )
    calls = {"secrets": 0}

    config = app_config.model_copy(
        update={
            "encryption": app_config.encryption.model_copy(
                update={"backup_key_id": "task9-backup-2026"}
            )
        }
    )

    def one_secrets(role, *, config):
        calls["secrets"] += 1
        assert role == "backup"
        return secrets

    class OfflineInspector:
        def inspect(self, _identity):
            return ProcessProof.NOT_SAME

    log_path = tmp_path / "logs" / "backup.runtime.log"
    monkeypatch.setattr(
        runtime_logging,
        "runtime_log_path",
        lambda role: log_path,
    )

    assert backup.main(
        ["--destination", str(destination)],
        config_loader=lambda: config,
        secrets_loader=one_secrets,
        process_identity=ProcessIdentity(
            pid=87654,
            start_identity="task9-backup-process",
        ),
        process_inspector=OfflineInspector(),
    ) == 0
    assert calls["secrets"] == 1
    assert log_path.exists()
    artifacts = list(destination.glob("*.aesgcm"))
    assert len(artifacts) == 1
    assert list(destination.glob("*.sqlite3")) == []
    assert b"SQLite format 3" not in artifacts[0].read_bytes()


def test_migration_main_loads_one_role_secret_before_engine_construction(
    app_config,
    monkeypatch,
):
    from trading_assistant.db import migrate

    secrets = Secrets(database_url="sqlite:///migration-role.db")
    observed = []

    def load(role, *, config, **_kwargs):
        observed.append(("load", role, config))
        return secrets

    monkeypatch.setattr(migrate, "load_config", lambda: app_config, raising=False)
    monkeypatch.setattr(
        migrate,
        "load_role_secrets",
        load,
        raising=False,
    )
    monkeypatch.setattr(
        migrate,
        "create_db_engine",
        lambda database_url: observed.append(
            ("engine", database_url)
        )
        or object(),
    )
    monkeypatch.setattr(migrate, "_print_result", lambda *_args: None)
    monkeypatch.setattr(migrate, "require_current_schema", lambda _engine: None)

    assert migrate.main(["status"]) == 0
    assert observed == [
        ("load", "migration", app_config),
        ("engine", secrets.database_url),
    ]


def test_safety_drill_main_passes_one_role_secret_into_offline_drill(
    app_config,
    monkeypatch,
    tmp_path,
):
    from trading_assistant.ops import safety_drill

    secrets = Secrets(database_url="sqlite:///safety-role.db")
    observed = []

    def load(role, *, config, **_kwargs):
        observed.append(("load", role, config))
        return secrets

    monkeypatch.setattr(safety_drill, "load_config", lambda: app_config)
    monkeypatch.setattr(
        safety_drill,
        "load_role_secrets",
        load,
        raising=False,
    )
    monkeypatch.setattr(
        safety_drill,
        "run_safety_drill",
        lambda **kwargs: (
            observed.append(("run", kwargs["secrets"]))
            or SimpleNamespace(safe=True, to_json=lambda: "{}")
        ),
    )
    monkeypatch.chdir(tmp_path)

    assert safety_drill.main(
        ["--database-copy", str(tmp_path / "copy.db"), "--mock"]
    ) == 0
    assert observed == [
        ("load", "safety-drill", app_config),
        ("run", secrets),
    ]


def test_launchd_installer_generates_only_bounded_stream_jobs(
    tmp_path,
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for command in ("launchctl", "sleep", "curl"):
        executable = fake_bin / command
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "HOME": str(home),
        "PATH": f"{fake_bin}:{Path('/usr/bin')}:{Path('/bin')}",
    }

    subprocess.run(
        ["bash", "scripts/launchd/install.sh"],
        cwd=Path(__file__).resolve().parent.parent,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )

    launch_agents = home / "Library" / "LaunchAgents"
    plists = {
        path.stem: plistlib.loads(path.read_bytes())
        for path in launch_agents.glob("com.trading.*.plist")
    }
    assert set(plists) == {
        "com.trading.app",
        "com.trading.watchdog",
        "com.trading.backup",
    }
    for payload in plists.values():
        assert payload["StandardOutPath"] == "/dev/null"
        assert payload["StandardErrorPath"] == "/dev/null"
        assert payload["Umask"] == 0o77


def _alpaca_config(app_config):
    return app_config.model_copy(
        update={
            "trading": app_config.trading.model_copy(
                update={"broker": BrokerKind.ALPACA}
            )
        }
    )


def _migrated_secrets(tmp_path):
    database_url = f"sqlite:///{tmp_path}/round2.db"
    upgrade(create_db_engine(database_url))
    return Secrets(
        database_url=database_url,
        app_api_token="round2-bootstrap-secret",
        field_encryption_keys={
            "local-primary-2026-07": base64.b64encode(
                b"r" * 32
            ).decode("ascii")
        },
    )


def test_public_production_container_rejects_non_alpaca_broker(
    app_config,
    tmp_path,
):
    from trading_assistant.bootstrap import build_container

    with pytest.raises(RuntimeError, match="Alpaca"):
        build_container(
            app_config,
            _migrated_secrets(tmp_path),
            runtime_role="daemon",
        )


def test_test_only_container_injection_keeps_alpaca_config(
    app_config,
    tmp_path,
):
    from trading_assistant import bootstrap

    builder = getattr(bootstrap, "build_test_container", None)
    assert builder is not None
    broker = MockBroker()
    broker.set_price("AAPL", Decimal("100"))
    clock = FakeClock(is_open=True)
    config = _alpaca_config(app_config)

    container = builder(
        config,
        _migrated_secrets(tmp_path),
        broker=broker,
        clock=clock,
    )

    assert container.config.trading.broker is BrokerKind.ALPACA
    assert container.broker is broker
    assert container.service.clock is clock
