"""Independent review round 2: exact composition and production runtimes."""

from __future__ import annotations

from decimal import Decimal
import logging
from pathlib import Path
import plistlib
import subprocess
from types import SimpleNamespace

import pytest

from trading_assistant.app.main import create_app
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

    assert app.state.container is None
    assert app.state.trading_service is service
    assert app.state.agent is agent
    assert app.state.runtime_secrets is None


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


def test_automatic_app_root_logs_post_container_startup_failure(
    tmp_path,
    make_service,
    monkeypatch,
):
    import trading_assistant.app.main as app_main

    marker = "post-container-agent-secret"
    service = make_service()
    secrets = Secrets(app_api_token=marker)
    container = SimpleNamespace(
        service=service,
        secrets=secrets,
    )
    monkeypatch.setattr(
        app_main,
        "build_default_container",
        lambda: container,
    )
    monkeypatch.setattr(
        app_main,
        "_build_agent",
        lambda _container: (_ for _ in ()).throw(
            RuntimeError(marker)
        ),
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RuntimeError, match=marker):
        create_app()

    content = (
        tmp_path / "logs" / "app.runtime.log"
    ).read_text(encoding="utf-8")
    assert "startup_failed role=app" in content
    assert marker not in content


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
    container = SimpleNamespace(
        service=service,
        audit=audit,
        secrets=Secrets(app_api_token="mcp-valid-startup-secret"),
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
    monkeypatch.setattr(
        server.mcp,
        "run",
        lambda: calls.__setitem__("run", calls["run"] + 1),
    )
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
    container = SimpleNamespace(
        service=service,
        audit=object(),
        secrets=Secrets(app_api_token=marker),
    )
    monkeypatch.setattr(
        server,
        "build_default_container",
        lambda: container,
    )
    monkeypatch.setattr(
        server.mcp,
        "run",
        lambda: (_ for _ in ()).throw(RuntimeError(marker)),
    )
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
        ("preflight", "preflight"),
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
        return SimpleNamespace(service=service)

    monkeypatch.setattr(bootstrap, "build_container", build)
    if builder_name == "preflight":
        result = preflight._build_service(app_config, secrets)
    else:
        result = paper_drill.build_paper_service(app_config, secrets)

    assert result is service
    assert observed == [(app_config, secrets, runtime_role)]


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
            daemon=SimpleNamespace(heartbeat_stale_seconds=180)
        ),
    )
    monkeypatch.setattr(
        watchdog,
        "fetch_health",
        lambda _url, _timeout: {
            "alive": True,
            "database_reachable": True,
        },
    )

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

    def one_secrets(role, *, config):
        calls["secrets"] += 1
        assert role == runtime_role
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
            lambda supplied_config, supplied_secrets: (
                observed.append((supplied_config, supplied_secrets)) or 0
            ),
        )
        result = module.run()
    else:
        service = object()
        monkeypatch.setattr(
            module,
            "build_paper_service",
            lambda supplied_config, supplied_secrets: (
                observed.append(
                    (supplied_config, supplied_secrets)
                )
                or service
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
):
    from trading_assistant.ops import backup

    source = tmp_path / "source.sqlite3"
    source.write_bytes(b"placeholder")
    destination = tmp_path / "backups"
    secrets = Secrets(
        database_url=f"sqlite:///{source}",
        app_api_token="backup-role-secret",
    )
    calls = {"secrets": 0}

    config = SimpleNamespace()

    def one_secrets(role, *, config):
        calls["secrets"] += 1
        assert role == "backup"
        return secrets

    monkeypatch.setattr(
        backup,
        "load_config",
        lambda: config,
        raising=False,
    )
    monkeypatch.setattr(
        backup,
        "load_role_secrets",
        one_secrets,
        raising=False,
    )
    monkeypatch.setattr(
        backup,
        "backup_database",
        lambda source_path, destination_dir, retention_days: (
            destination / "created.sqlite3"
        ),
    )
    monkeypatch.chdir(tmp_path)

    assert backup.main(
        ["--destination", str(destination)]
    ) == 0
    assert calls["secrets"] == 1
    assert (tmp_path / "logs" / "backup.runtime.log").exists()


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
        "com.trading.daemon",
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
