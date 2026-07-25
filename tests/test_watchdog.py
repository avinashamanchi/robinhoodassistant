"""Heartbeat watchdog decisions are deterministic and fail safe."""

from __future__ import annotations

import logging
import subprocess
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from sqlalchemy import update

from trading_assistant.db.migrate import upgrade
from trading_assistant.db.models import Heartbeat, utcnow
from trading_assistant.db.session import (
    create_db_engine,
    make_session_factory,
)
from trading_assistant.ops import watchdog
from trading_assistant.ops.watchdog import (
    labels_to_restart,
    needs_restart,
    read_database_health,
)

_LIVE = {"alive": True, "database_reachable": True}


def test_watchdog_restarts_only_when_health_is_stale_or_broken():
    assert needs_restart(
        {"db_ok": True, "heartbeat_age_seconds": 30}, stale_seconds=180
    ) is False
    assert needs_restart(
        {"db_ok": True, "heartbeat_age_seconds": 181}, stale_seconds=180
    ) is True
    assert needs_restart(
        {"db_ok": True, "heartbeat_age_seconds": None}, stale_seconds=180
    ) is True
    assert needs_restart(
        {"db_ok": False, "heartbeat_age_seconds": 1}, stale_seconds=180
    ) is True


@pytest.mark.parametrize(
    "db_ok",
    [
        "true",
        "false",
        1,
        0,
        1.0,
        False,
        None,
    ],
)
def test_watchdog_requires_literal_true_database_health(db_ok):
    assert needs_restart(
        {"db_ok": db_ok, "heartbeat_age_seconds": 0},
        stale_seconds=180,
    ) is True


@pytest.mark.parametrize(
    "stale_seconds",
    [
        None,
        "180",
        True,
        False,
        float("nan"),
        float("inf"),
        float("-inf"),
        0,
        -1,
    ],
)
def test_watchdog_rejects_invalid_runtime_stale_threshold(stale_seconds):
    assert needs_restart(
        {"db_ok": True, "heartbeat_age_seconds": 0},
        stale_seconds=stale_seconds,
    ) is True


@pytest.mark.parametrize(
    "age",
    [
        -0.001,
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        False,
        "0",
        "10.5",
        [],
        {},
    ],
)
def test_watchdog_rejects_non_finite_negative_bool_and_malformed_ages(age):
    assert needs_restart(
        {"db_ok": True, "heartbeat_age_seconds": age},
        stale_seconds=180,
    ) is True


@pytest.mark.parametrize(
    "age",
    [
        0,
        180,
        Decimal("0"),
        Decimal("180"),
    ],
)
def test_watchdog_freshness_boundary_is_inclusive(age):
    assert needs_restart(
        {"db_ok": True, "heartbeat_age_seconds": age},
        stale_seconds=180,
    ) is False


def test_watchdog_restarts_immediately_above_inclusive_boundary():
    assert needs_restart(
        {"db_ok": True, "heartbeat_age_seconds": 180.000001},
        stale_seconds=180,
    ) is True


@pytest.mark.parametrize(
    "age",
    [
        -1,
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        "30",
        object(),
    ],
)
def test_invalid_age_restarts_only_daemon_when_api_is_live(age):
    assert labels_to_restart(
        api_health=_LIVE,
        database_health={
            "db_ok": True,
            "heartbeat_age_seconds": age,
        },
        stale_seconds=180,
    ) == {"com.trading.daemon"}


def test_healthy_api_and_fresh_daemon_restart_nothing():
    assert labels_to_restart(
        api_health=_LIVE,
        database_health={"db_ok": True, "heartbeat_age_seconds": 10},
        stale_seconds=180,
    ) == set()


@pytest.mark.parametrize(
    "api_health",
    [
        [],
        "ok",
        True,
        False,
        1,
        0,
        {"status": True},
        {"status": "OK"},
        {},
        {"status": "ok", "alive": True},
    ],
)
def test_liveness_requires_exact_mapping_contract(api_health):
    assert labels_to_restart(
        api_health=api_health,
        database_health={"db_ok": True, "heartbeat_age_seconds": 10},
        stale_seconds=180,
    ) == {"com.trading.app"}


def test_healthy_api_and_stale_daemon_restart_daemon_only():
    assert labels_to_restart(
        api_health=_LIVE,
        database_health={"db_ok": True, "heartbeat_age_seconds": 999},
        stale_seconds=180,
    ) == {"com.trading.daemon"}


def test_api_outage_restarts_app_and_uses_db_heartbeat_for_daemon():
    assert labels_to_restart(
        api_health=None,
        database_health={"db_ok": True, "heartbeat_age_seconds": 10},
        stale_seconds=180,
    ) == {"com.trading.app"}
    assert labels_to_restart(
        api_health=None,
        database_health={"db_ok": True, "heartbeat_age_seconds": 999},
        stale_seconds=180,
    ) == {"com.trading.app", "com.trading.daemon"}


def test_database_unavailable_restarts_daemon_only_when_api_is_live():
    assert labels_to_restart(
        api_health=_LIVE,
        database_health={"db_ok": False, "heartbeat_age_seconds": None},
        stale_seconds=180,
    ) == {"com.trading.daemon"}


def test_database_health_reads_persisted_daemon_heartbeat(
    tmp_path,
):
    db_url = f"sqlite:///{tmp_path}/watchdog.db"
    engine = create_db_engine(db_url)
    upgrade(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        session.add(Heartbeat(source="daemon"))
        session.commit()

    health = read_database_health(db_url)

    assert health["db_ok"] is True
    assert health["heartbeat_age_seconds"] is not None
    assert health["heartbeat_age_seconds"] < 5

    with factory() as session:
        session.execute(
            update(Heartbeat)
            .where(Heartbeat.source == "daemon")
            .values(at=utcnow() - timedelta(seconds=300))
        )
        session.add(Heartbeat(source="other"))
        session.commit()

    stale = read_database_health(db_url)

    assert stale["db_ok"] is True
    assert stale["heartbeat_age_seconds"] >= 299


def test_future_database_heartbeat_restarts_only_daemon(
    tmp_path,
):
    db_url = f"sqlite:///{tmp_path}/future-watchdog.db"
    engine = create_db_engine(db_url)
    upgrade(engine)
    with make_session_factory(engine)() as session:
        session.add(
            Heartbeat(
                source="daemon",
                at=utcnow() + timedelta(seconds=60),
            )
        )
        session.commit()

    health = read_database_health(db_url)

    assert health["db_ok"] is True
    assert health["heartbeat_age_seconds"] < 0
    assert labels_to_restart(
        api_health=_LIVE,
        database_health=health,
        stale_seconds=180,
    ) == {"com.trading.daemon"}


def test_database_health_failure_is_fixed_and_sanitized(monkeypatch):
    marker = "database-watchdog-secret"

    def fail_engine(url):
        raise RuntimeError(marker)

    monkeypatch.setattr(
        watchdog,
        "create_db_engine",
        fail_engine,
        raising=False,
    )

    health = read_database_health("sqlite:///unused.db")

    assert health == {
        "db_ok": False,
        "heartbeat_age_seconds": None,
    }
    assert marker not in str(health)


def test_database_timestamp_subtraction_failure_is_fixed_and_sanitized(
    make_service,
    db_url,
    monkeypatch,
    capsys,
):
    service = make_service()
    service.write_heartbeat("daemon")
    marker = "heartbeat-subtraction-secret"

    class BrokenNow:
        def __sub__(self, other):
            raise RuntimeError(marker)

    monkeypatch.setattr(watchdog, "utcnow", lambda: BrokenNow())

    health = read_database_health(db_url)

    assert health == {
        "db_ok": False,
        "heartbeat_age_seconds": None,
    }
    assert marker not in str(health)
    captured = capsys.readouterr()
    assert marker not in captured.out
    assert marker not in captured.err


def test_main_polls_only_anonymous_liveness_and_restarts_nothing_when_healthy(
    monkeypatch,
):
    observed_urls: list[str] = []
    restarted: list[str] = []

    def fetch(url, timeout):
        observed_urls.append(url)
        assert url.endswith("/health/live")
        assert not url.endswith("/health")
        return dict(_LIVE)

    monkeypatch.setattr(watchdog, "fetch_health", fetch)
    monkeypatch.setattr(
        watchdog,
        "read_database_health",
        lambda **_kwargs: {"db_ok": True, "heartbeat_age_seconds": 10},
    )
    monkeypatch.setattr(
        watchdog,
        "restart_launch_agent",
        restarted.append,
    )

    assert watchdog.main([]) == 0
    assert observed_urls == ["http://127.0.0.1:8000/health/live"]
    assert restarted == []


def test_main_accepts_only_the_exact_liveness_json_contract(monkeypatch):
    restarted: list[str] = []
    monkeypatch.setattr(
        watchdog,
        "urlopen",
        lambda url, timeout: BytesIO(
            b'{"alive":true,"database_reachable":true}'
        ),
    )
    monkeypatch.setattr(
        watchdog,
        "read_database_health",
        lambda **_kwargs: {"db_ok": True, "heartbeat_age_seconds": 10},
    )
    monkeypatch.setattr(
        watchdog,
        "restart_launch_agent",
        restarted.append,
    )

    assert watchdog.main([]) == 0
    assert restarted == []


_EXACT_LIVENESS_JSON = (
    '{"alive":true,"database_reachable":true}'
)


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b'"ok"',
        b"true",
        b"false",
        b"null",
        b"{}",
        b'{"status":true}',
        b'{"status":"OK"}',
        b'{"status":"ok","alive":true}',
        _EXACT_LIVENESS_JSON.encode("utf-16"),
        _EXACT_LIVENESS_JSON.encode("utf-32"),
        b"\xef\xbb\xbf" + _EXACT_LIVENESS_JSON.encode("utf-8"),
        b'{"status":"not-ok","status":"ok"}',
        b'{"status":"ok","meta":{"key":1,"key":2}}',
        b"{",
        b"\xff",
    ],
)
def test_main_malformed_liveness_body_restarts_app_without_crashing(
    monkeypatch,
    body,
):
    restarted: list[str] = []
    monkeypatch.setattr(
        watchdog,
        "urlopen",
        lambda url, timeout: BytesIO(body),
    )
    monkeypatch.setattr(
        watchdog,
        "read_database_health",
        lambda **_kwargs: {"db_ok": True, "heartbeat_age_seconds": 10},
    )
    monkeypatch.setattr(
        watchdog,
        "restart_launch_agent",
        restarted.append,
    )

    assert watchdog.main([]) == 1
    assert restarted == ["com.trading.app"]


def test_main_oversized_liveness_body_restarts_app_without_crashing(
    monkeypatch,
):
    restarted: list[str] = []
    oversized = (
        _EXACT_LIVENESS_JSON.encode("utf-8") + (b" " * 2048)
    )
    monkeypatch.setattr(
        watchdog,
        "urlopen",
        lambda url, timeout: BytesIO(oversized),
    )
    monkeypatch.setattr(
        watchdog,
        "read_database_health",
        lambda **_kwargs: {"db_ok": True, "heartbeat_age_seconds": 10},
    )
    monkeypatch.setattr(
        watchdog,
        "restart_launch_agent",
        restarted.append,
    )

    assert watchdog.main([]) == 1
    assert restarted == ["com.trading.app"]


def test_fetch_health_decodes_text_and_rejects_nested_duplicate_members(
    monkeypatch,
):
    observed: dict[str, object] = {}
    original_loads = watchdog.json.loads
    body = b'{"status":"ok","meta":{"key":1,"key":2}}'

    def loads(value, **kwargs):
        observed["value_type"] = type(value)
        observed["object_pairs_hook"] = kwargs.get("object_pairs_hook")
        return original_loads(value, **kwargs)

    monkeypatch.setattr(watchdog.json, "loads", loads)
    monkeypatch.setattr(
        watchdog,
        "urlopen",
        lambda url, timeout: BytesIO(body),
    )

    assert watchdog.fetch_health("http://127.0.0.1/health/live") is None
    assert observed["value_type"] is str
    hook = observed["object_pairs_hook"]
    assert callable(hook)
    with pytest.raises(ValueError):
        hook([("key", 1), ("key", 2)])


@pytest.mark.parametrize(
    "age",
    [
        -1,
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        "20",
        {"corrupt": "value"},
    ],
)
def test_main_invalid_database_age_restarts_daemon_only(
    monkeypatch,
    age,
):
    restarted: list[str] = []
    monkeypatch.setattr(
        watchdog,
        "fetch_health",
        lambda url, timeout: dict(_LIVE),
    )
    monkeypatch.setattr(
        watchdog,
        "read_database_health",
        lambda **_kwargs: {
            "db_ok": True,
            "heartbeat_age_seconds": age,
        },
    )
    monkeypatch.setattr(
        watchdog,
        "restart_launch_agent",
        restarted.append,
    )

    assert watchdog.main([]) == 1
    assert restarted == ["com.trading.daemon"]


def test_main_healthy_api_and_stale_daemon_restarts_daemon_only(
    monkeypatch,
):
    restarted: list[str] = []
    monkeypatch.setattr(
        watchdog,
        "fetch_health",
        lambda url, timeout: dict(_LIVE),
    )
    monkeypatch.setattr(
        watchdog,
        "read_database_health",
        lambda **_kwargs: {"db_ok": True, "heartbeat_age_seconds": 999},
    )
    monkeypatch.setattr(
        watchdog,
        "restart_launch_agent",
        restarted.append,
    )

    assert watchdog.main([]) == 1
    assert restarted == ["com.trading.daemon"]


def test_main_liveness_failure_restarts_app_only_and_sanitizes_error(
    monkeypatch,
    capsys,
):
    marker = "protected-health-401-secret"
    restarted: list[str] = []
    observed_urls: list[str] = []

    def fail_liveness(url, timeout):
        observed_urls.append(url)
        raise RuntimeError(marker)

    monkeypatch.setattr(watchdog, "fetch_health", fail_liveness)
    monkeypatch.setattr(
        watchdog,
        "read_database_health",
        lambda **_kwargs: {"db_ok": True, "heartbeat_age_seconds": 10},
    )
    monkeypatch.setattr(
        watchdog,
        "restart_launch_agent",
        restarted.append,
    )

    assert watchdog.main([]) == 1
    assert observed_urls == ["http://127.0.0.1:8000/health/live"]
    assert restarted == ["com.trading.app"]
    captured = capsys.readouterr()
    assert marker not in captured.out
    assert marker not in captured.err


def test_main_attempts_daemon_after_app_restart_failure(
    monkeypatch,
    caplog,
):
    marker = "launchctl-first-secret"
    restarted: list[str] = []

    monkeypatch.setattr(watchdog, "fetch_health", lambda url, timeout: None)
    monkeypatch.setattr(
        watchdog,
        "read_database_health",
        lambda **_kwargs: {"db_ok": False, "heartbeat_age_seconds": None},
    )

    def restart(label):
        restarted.append(label)
        if label == "com.trading.app":
            raise RuntimeError(marker)

    monkeypatch.setattr(watchdog, "restart_launch_agent", restart)
    monkeypatch.setattr(watchdog.log, "disabled", False)

    with caplog.at_level(logging.WARNING, logger=watchdog.__name__):
        assert watchdog.main([]) == 1

    assert restarted == ["com.trading.app", "com.trading.daemon"]
    records = [
        record
        for record in caplog.records
        if record.name == watchdog.__name__
    ]
    assert [record.getMessage() for record in records] == [
        "watchdog_restart component=app result=failed",
        "watchdog_restart component=daemon result=success",
    ]
    assert [record.levelname for record in records] == ["ERROR", "WARNING"]
    assert marker not in caplog.text


def test_main_collects_both_restart_failures_without_duplicate_attempts(
    monkeypatch,
    caplog,
):
    marker = "launchctl-both-secret"
    restarted: list[str] = []

    monkeypatch.setattr(watchdog, "fetch_health", lambda url, timeout: None)
    monkeypatch.setattr(
        watchdog,
        "read_database_health",
        lambda **_kwargs: {"db_ok": False, "heartbeat_age_seconds": None},
    )

    def restart(label):
        restarted.append(label)
        raise RuntimeError(marker)

    monkeypatch.setattr(watchdog, "restart_launch_agent", restart)
    monkeypatch.setattr(watchdog.log, "disabled", False)

    with caplog.at_level(logging.WARNING, logger=watchdog.__name__):
        assert watchdog.main([]) == 1

    assert restarted == ["com.trading.app", "com.trading.daemon"]
    assert len(restarted) == len(set(restarted))
    records = [
        record
        for record in caplog.records
        if record.name == watchdog.__name__
    ]
    assert [record.getMessage() for record in records] == [
        "watchdog_restart component=app result=failed",
        "watchdog_restart component=daemon result=failed",
    ]
    assert [record.levelname for record in records] == ["ERROR", "ERROR"]
    assert marker not in caplog.text


def test_launchctl_timeout_on_app_still_attempts_daemon_once(
    monkeypatch,
    caplog,
    capsys,
):
    marker = "launchctl-timeout-provider-secret"
    calls: list[tuple[list[str], bool, float]] = []

    monkeypatch.setattr(watchdog, "fetch_health", lambda url, timeout: None)
    monkeypatch.setattr(
        watchdog,
        "read_database_health",
        lambda **_kwargs: {"db_ok": False, "heartbeat_age_seconds": None},
    )
    monkeypatch.setattr(watchdog.os, "getuid", lambda: 501)
    monkeypatch.setattr(watchdog.log, "disabled", False)

    def run(command, *, check, timeout):
        calls.append((command, check, timeout))
        if command[-1].endswith("com.trading.app"):
            raise subprocess.TimeoutExpired(
                cmd=["provider-command-secret", marker],
                timeout=timeout,
                output=marker,
                stderr=marker,
            )

    monkeypatch.setattr(watchdog.subprocess, "run", run)

    with caplog.at_level(logging.WARNING, logger=watchdog.__name__):
        assert watchdog.main([]) == 1

    assert [call[0][-1] for call in calls] == [
        "gui/501/com.trading.app",
        "gui/501/com.trading.daemon",
    ]
    assert all(
        check is True and timeout == 10.0
        for _command, check, timeout in calls
    )
    records = [
        record
        for record in caplog.records
        if record.name == watchdog.__name__
    ]
    assert [record.getMessage() for record in records] == [
        "watchdog_restart component=app result=failed",
        "watchdog_restart component=daemon result=success",
    ]
    captured = capsys.readouterr()
    exposed = f"{caplog.text} {captured.out} {captured.err}"
    assert marker not in exposed
    assert "provider-command-secret" not in exposed


def test_both_launchctl_timeouts_are_collected_before_nonzero_return(
    monkeypatch,
    caplog,
    capsys,
):
    marker = "launchctl-all-timeout-secret"
    calls: list[tuple[list[str], bool, float]] = []

    monkeypatch.setattr(watchdog, "fetch_health", lambda url, timeout: None)
    monkeypatch.setattr(
        watchdog,
        "read_database_health",
        lambda **_kwargs: {"db_ok": False, "heartbeat_age_seconds": None},
    )
    monkeypatch.setattr(watchdog.os, "getuid", lambda: 501)
    monkeypatch.setattr(watchdog.log, "disabled", False)

    def run(command, *, check, timeout):
        calls.append((command, check, timeout))
        raise subprocess.TimeoutExpired(
            cmd=["provider-command-secret", marker],
            timeout=timeout,
            output=marker,
            stderr=marker,
        )

    monkeypatch.setattr(watchdog.subprocess, "run", run)

    with caplog.at_level(logging.WARNING, logger=watchdog.__name__):
        assert watchdog.main([]) == 1

    assert [call[0][-1] for call in calls] == [
        "gui/501/com.trading.app",
        "gui/501/com.trading.daemon",
    ]
    assert len(calls) == len({call[0][-1] for call in calls}) == 2
    assert all(
        check is True and timeout == 10.0
        for _command, check, timeout in calls
    )
    records = [
        record
        for record in caplog.records
        if record.name == watchdog.__name__
    ]
    assert [record.getMessage() for record in records] == [
        "watchdog_restart component=app result=failed",
        "watchdog_restart component=daemon result=failed",
    ]
    captured = capsys.readouterr()
    exposed = f"{caplog.text} {captured.out} {captured.err}"
    assert marker not in exposed
    assert "provider-command-secret" not in exposed


def test_launchctl_kickstart_command_targets_exact_agent(monkeypatch):
    calls = []
    monkeypatch.setattr(watchdog.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        watchdog.subprocess,
        "run",
        lambda command, check, timeout: calls.append(
            (command, check, timeout)
        ),
    )

    watchdog.restart_launch_agent("com.trading.daemon")

    assert calls == [
        (
            [
                "launchctl",
                "kickstart",
                "-k",
                "gui/501/com.trading.daemon",
            ],
            True,
            10.0,
        )
    ]


def test_launchd_and_start_scripts_wire_anonymous_liveness_only():
    install = Path("scripts/launchd/install.sh").read_text(
        encoding="utf-8"
    )
    start = Path("scripts/start.sh").read_text(encoding="utf-8")

    assert (
        "emit_periodic com.trading.watchdog 60 "
        '"$PY" -m trading_assistant.ops.watchdog '
        "--health-url http://127.0.0.1:8000/health/live"
    ) in install
    assert "curl -s http://127.0.0.1:8000/health/live" in install
    assert "curl -s http://127.0.0.1:8000/health/live" in start
    assert "umask 077" in install
    assert "umask 077" in start
    assert "<key>Umask</key><integer>63</integer>" in install
    assert "chmod 700 \"$PROJ/logs\"" in install
    assert (
        "emit ()" in install
        and (
            "StandardOutPath</key><string>/dev/null"
            in install
        )
        and (
            "StandardErrorPath</key><string>/dev/null"
            in install
        )
    )
    assert "touch \"$PROJ/logs/app.launchd.log\"" not in install
    assert "logs/app.runtime.log" in install
    assert "> /dev/null 2>&1" in start
    assert "logs/app.runtime.log" in start
    assert "logs/daemon.runtime.log" in start
    assert "logs/app.log" not in start
    assert "logs/daemon.log" not in start
    assert "http://127.0.0.1:8000/health " not in install
    assert "http://127.0.0.1:8000/health " not in start
