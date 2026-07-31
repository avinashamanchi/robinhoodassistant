"""Exact-child supervision tests with inert process fakes only."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
import signal
import stat
import subprocess

import pytest

from trading_assistant.ops.operator_daemon import DaemonSupervisor


DAEMON_MODULE = (
    Path(__file__).resolve().parent.parent
    / "src/trading_assistant/ops/operator_daemon.py"
)


class RecordingProcessFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls.append((args, kwargs))
        raise AssertionError("construction must not spawn")


class FakeChild:
    def __init__(
        self,
        *,
        pid: int,
        poll_results: list[int | None] | None = None,
        signal_error: BaseException | None = None,
        wait_error: BaseException | None = None,
    ) -> None:
        self.pid = pid
        self.poll_results = deque(poll_results or [])
        self.poll_calls = 0
        self.signals: list[int] = []
        self.wait_calls: list[float] = []
        self.signal_error = signal_error
        self.wait_error = wait_error

    def poll(self) -> int | None:
        self.poll_calls += 1
        if self.poll_results:
            return self.poll_results.popleft()
        return None

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)
        if self.signal_error is not None:
            raise self.signal_error

    def wait(self, timeout: float) -> int:
        self.wait_calls.append(timeout)
        if self.wait_error is not None:
            raise self.wait_error
        return 0


class ProcessFactory:
    def __init__(self, child: FakeChild) -> None:
        self.child = child
        self.calls: list[tuple[object, dict[str, object]]] = []

    def __call__(self, argv: object, **kwargs: object) -> FakeChild:
        self.calls.append((argv, kwargs))
        return self.child


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


REFERENCE_AT = datetime.now(timezone.utc)
PRESTART_AT = (REFERENCE_AT - timedelta(seconds=1)).isoformat()
FRESH_HEARTBEAT_AT = REFERENCE_AT.isoformat()


def safe_posture(
    *,
    heartbeat_status: str = "unknown",
    heartbeat_detail: str = "daemon_heartbeat_missing",
    heartbeat_evidence_at: str | None = None,
) -> dict[str, object]:
    def check(
        name: str,
        status: str,
        detail_code: str,
        **evidence: object,
    ) -> dict[str, object]:
        return {
            "name": name,
            "status": status,
            "observed_at": PRESTART_AT,
            "detail_code": detail_code,
            **evidence,
        }

    heartbeat = check(
        "daemon_heartbeat",
        heartbeat_status,
        heartbeat_detail,
    )
    if heartbeat_evidence_at is not None:
        heartbeat["evidence_at"] = heartbeat_evidence_at
    checks = [
        check("broker_mode", "paper", "broker_paper_mode"),
        check("loopback_https", "pass", "ok"),
        check("tls", "pass", "ok"),
        check(
            "sensitive_encryption",
            "pass",
            "ok",
            migration_state="complete",
        ),
        heartbeat,
        check(
            "startup_reconciliation",
            "pass",
            "reconciliation_current",
        ),
        *[
            check(
                "quarantine",
                "clear",
                "quarantine_empty",
                scope=scope,
                count=0,
            )
            for scope in ("received", "summarized", "rejected", "failed")
        ],
        *[
            check(
                "circuit_breaker",
                "clear",
                "breaker_clear",
                scope=scope,
                count=0,
                generation=0,
            )
            for scope in ("account", "equity", "crypto", "liquidity")
        ],
        check(
            "runtime_tenure",
            "held",
            "runtime_tenure_held",
            scope="app",
        ),
        check(
            "runtime_tenure",
            "released",
            "runtime_tenure_released",
            scope="daemon",
        ),
        check(
            "unsafe_orders",
            "clear",
            "unsafe_state_clear",
            count=0,
        ),
        check(
            "unsafe_fills",
            "clear",
            "unsafe_state_clear",
            count=0,
        ),
        check(
            "unsafe_rules",
            "clear",
            "unsafe_state_clear",
            scope="rules",
            count=0,
        ),
        check(
            "unsafe_rules",
            "clear",
            "unsafe_state_clear",
            scope="rule_groups",
            count=0,
        ),
        check(
            "uncertain_interlocks",
            "clear",
            "uncertain_interlocks_clear",
            count=0,
        ),
    ]
    return {
        "observed_at": PRESTART_AT,
        "checks": checks,
        "can_trade": False,
    }


def fresh_heartbeat_posture(
    evidence_at: str = FRESH_HEARTBEAT_AT,
) -> dict[str, object]:
    report = safe_posture()
    report["observed_at"] = evidence_at
    checks = report["checks"]
    assert isinstance(checks, list)
    for item in checks:
        assert isinstance(item, dict)
        item["observed_at"] = evidence_at
        if item["name"] == "daemon_heartbeat":
            item.update(
                {
                    "status": "fresh",
                    "detail_code": "daemon_heartbeat_fresh",
                    "evidence_at": evidence_at,
                    "age_seconds": 0.0,
                    "max_age_seconds": 30.0,
                }
            )
    return report


def posture_check(
    report: dict[str, object],
    name: str,
    *,
    scope: str | None = None,
) -> dict[str, object]:
    checks = report["checks"]
    assert isinstance(checks, list)
    matches = [
        item
        for item in checks
        if isinstance(item, dict)
        and item.get("name") == name
        and (scope is None or item.get("scope") == scope)
    ]
    assert len(matches) == 1
    return matches[0]


def build_supervisor(
    tmp_path: Path,
    child: FakeChild,
) -> tuple[DaemonSupervisor, ProcessFactory, FakeClock]:
    factory = ProcessFactory(child)
    clock = FakeClock()
    supervisor = DaemonSupervisor(
        tmp_path,
        process_factory=factory,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    return supervisor, factory, clock


def test_supervisor_is_off_and_spawns_nothing_at_construction(tmp_path):
    factory = RecordingProcessFactory()

    supervisor = DaemonSupervisor(tmp_path, process_factory=factory)

    assert supervisor.status().state == "off"
    assert factory.calls == []


def test_stop_signals_only_owned_child(tmp_path):
    child = FakeChild(pid=4242)
    supervisor = DaemonSupervisor(tmp_path)
    supervisor._child = child

    result = supervisor.stop(timeout_seconds=1)

    assert child.signals == [signal.SIGINT]
    assert child.wait_calls == [1]
    assert result.state == "off"


def test_start_retains_exact_child_and_uses_private_0600_log(tmp_path):
    child = FakeChild(pid=4242)
    supervisor, factory, _clock = build_supervisor(tmp_path, child)

    result = supervisor.start(
        posture=safe_posture(),
        heartbeat_loader=fresh_heartbeat_posture,
    )

    assert result.state == "running"
    assert result.pid == 4242
    assert supervisor.owns_child is True
    assert len(factory.calls) == 1
    argv, options = factory.calls[0]
    assert argv == [
        str(tmp_path / ".venv/bin/python"),
        "-m",
        "trading_assistant.daemon.main",
    ]
    assert options["cwd"] == tmp_path
    assert options["stdin"] is subprocess.DEVNULL
    assert options["stdout"] is options["stderr"]
    assert options["stdout"] is not subprocess.PIPE
    assert options["shell"] is False
    assert options["start_new_session"] is False
    log_path = tmp_path / "logs/daemon.operator.log"
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("name", "scope", "status", "detail_code"),
    [
        ("broker_mode", None, "blocked", "broker_mode_not_paper"),
        ("loopback_https", None, "unknown", "startup_check_unknown"),
        ("tls", None, "blocked", "tls_material_parse_failed"),
        (
            "sensitive_encryption",
            None,
            "unknown",
            "encryption_evidence_unavailable",
        ),
        (
            "startup_reconciliation",
            None,
            "stale",
            "reconciliation_stale",
        ),
        (
            "startup_reconciliation",
            None,
            "blocked",
            "reconciliation_failed",
        ),
        ("circuit_breaker", "equity", "tripped", "breaker_tripped"),
        (
            "circuit_breaker",
            "account",
            "unknown",
            "breaker_scope_invalid",
        ),
        ("quarantine", "failed", "present", "quarantine_items_present"),
        ("unsafe_orders", None, "present", "unsafe_state_present"),
        ("unsafe_fills", None, "present", "unsafe_state_present"),
        ("unsafe_rules", "rules", "present", "unsafe_state_present"),
        (
            "uncertain_interlocks",
            None,
            "present",
            "uncertain_interlocks_present",
        ),
        ("runtime_tenure", "daemon", "held", "runtime_tenure_held"),
    ],
)
def test_start_blocks_every_unsafe_or_unknown_posture_field_without_spawn(
    tmp_path,
    name,
    scope,
    status,
    detail_code,
):
    report = safe_posture()
    item = posture_check(report, name, scope=scope)
    item["status"] = status
    item["detail_code"] = detail_code
    child = FakeChild(pid=4242)
    supervisor, factory, _clock = build_supervisor(tmp_path, child)

    result = supervisor.start(
        posture=report,
        heartbeat_loader=fresh_heartbeat_posture,
    )

    assert result.state == "start_blocked"
    assert result.detail_code
    assert factory.calls == []
    assert supervisor.owns_child is False


@pytest.mark.parametrize(
    "missing_name",
    [
        "broker_mode",
        "loopback_https",
        "tls",
        "sensitive_encryption",
        "startup_reconciliation",
        "circuit_breaker",
        "quarantine",
        "runtime_tenure",
        "unsafe_orders",
        "unsafe_fills",
        "unsafe_rules",
        "uncertain_interlocks",
    ],
)
def test_start_blocks_absent_required_posture_evidence(
    tmp_path,
    missing_name,
):
    report = safe_posture()
    checks = report["checks"]
    assert isinstance(checks, list)
    report["checks"] = [
        item
        for item in checks
        if not (
            isinstance(item, dict)
            and item.get("name") == missing_name
        )
    ]
    supervisor, factory, _clock = build_supervisor(
        tmp_path,
        FakeChild(pid=4242),
    )

    result = supervisor.start(
        posture=report,
        heartbeat_loader=fresh_heartbeat_posture,
    )

    assert result.state == "start_blocked"
    assert factory.calls == []


@pytest.mark.parametrize(
    "posture",
    [
        {},
        {"checks": "not-a-list"},
        {"observed_at": PRESTART_AT, "checks": [None]},
    ],
)
def test_start_blocks_malformed_report_objects(tmp_path, posture):
    supervisor, factory, _clock = build_supervisor(
        tmp_path,
        FakeChild(pid=4242),
    )

    result = supervisor.start(
        posture=posture,
        heartbeat_loader=fresh_heartbeat_posture,
    )

    assert result.state == "start_blocked"
    assert result.detail_code == "posture_invalid"
    assert factory.calls == []


@pytest.mark.parametrize(
    "observed_at",
    [
        (REFERENCE_AT - timedelta(seconds=31)).isoformat(),
        (REFERENCE_AT + timedelta(seconds=6)).isoformat(),
    ],
)
def test_start_blocks_stale_or_future_posture_observation(
    tmp_path,
    observed_at,
):
    report = safe_posture()
    report["observed_at"] = observed_at
    checks = report["checks"]
    assert isinstance(checks, list)
    for item in checks:
        assert isinstance(item, dict)
        item["observed_at"] = observed_at
    supervisor, factory, _clock = build_supervisor(
        tmp_path,
        FakeChild(pid=4242),
    )

    result = supervisor.start(
        posture=report,
        heartbeat_loader=fresh_heartbeat_posture,
    )

    assert result.state == "start_blocked"
    assert result.detail_code == "posture_not_fresh"
    assert factory.calls == []


def test_start_blocks_current_daemon_heartbeat(tmp_path):
    report = safe_posture(
        heartbeat_status="fresh",
        heartbeat_detail="daemon_heartbeat_fresh",
        heartbeat_evidence_at=PRESTART_AT,
    )
    supervisor, factory, _clock = build_supervisor(
        tmp_path,
        FakeChild(pid=4242),
    )

    result = supervisor.start(
        posture=report,
        heartbeat_loader=fresh_heartbeat_posture,
    )

    assert result.state == "start_blocked"
    assert result.detail_code == "daemon_heartbeat_current"
    assert factory.calls == []


def test_start_allows_stale_heartbeat_without_current_daemon_tenure(
    tmp_path,
):
    report = safe_posture(
        heartbeat_status="stale",
        heartbeat_detail="daemon_heartbeat_stale",
        heartbeat_evidence_at="2026-07-30T11:59:00+00:00",
    )
    supervisor, factory, _clock = build_supervisor(
        tmp_path,
        FakeChild(pid=4242),
    )

    result = supervisor.start(
        posture=report,
        heartbeat_loader=fresh_heartbeat_posture,
    )

    assert result.state == "running"
    assert len(factory.calls) == 1


@pytest.mark.parametrize("scope", ["received", "summarized", "rejected"])
def test_start_allows_known_nonfailure_quarantine_items(tmp_path, scope):
    report = safe_posture()
    item = posture_check(report, "quarantine", scope=scope)
    item.update(
        {
            "status": "present",
            "detail_code": "quarantine_items_present",
            "count": 1,
        }
    )
    supervisor, factory, _clock = build_supervisor(
        tmp_path,
        FakeChild(pid=4242),
    )

    result = supervisor.start(
        posture=report,
        heartbeat_loader=fresh_heartbeat_posture,
    )

    assert result.state == "running"
    assert len(factory.calls) == 1


def test_start_requires_heartbeat_evidence_newer_than_prestart_observation(
    tmp_path,
):
    child = FakeChild(pid=4242)
    supervisor, _factory, clock = build_supervisor(tmp_path, child)

    result = supervisor.start(
        posture=safe_posture(),
        heartbeat_loader=lambda: fresh_heartbeat_posture(PRESTART_AT),
    )

    assert clock.sleeps
    assert result.state == "start_blocked"
    assert result.detail_code == "daemon_heartbeat_timeout"
    assert child.signals == [signal.SIGINT]
    assert child.wait_calls == [15.0]
    assert supervisor.owns_child is False


def test_child_exit_before_fresh_heartbeat_is_reported_without_signal(
    tmp_path,
):
    child = FakeChild(pid=4242, poll_results=[7])
    supervisor, _factory, _clock = build_supervisor(tmp_path, child)
    loader_calls = 0

    def loader() -> dict[str, object]:
        nonlocal loader_calls
        loader_calls += 1
        return fresh_heartbeat_posture()

    result = supervisor.start(
        posture=safe_posture(),
        heartbeat_loader=loader,
    )

    assert result.state == "exited"
    assert result.pid == 4242
    assert result.detail_code == "daemon_exited_before_heartbeat"
    assert loader_calls == 0
    assert child.signals == []
    assert child.wait_calls == []
    assert supervisor.owns_child is False


def test_heartbeat_timeout_retains_child_when_sigint_wait_is_unconfirmed(
    tmp_path,
):
    child = FakeChild(
        pid=4242,
        wait_error=subprocess.TimeoutExpired("fake-child", 15.0),
    )
    supervisor, _factory, _clock = build_supervisor(tmp_path, child)

    result = supervisor.start(
        posture=safe_posture(),
        heartbeat_loader=safe_posture,
    )

    assert result.state == "stop_unconfirmed"
    assert result.pid == 4242
    assert child.signals == [signal.SIGINT]
    assert child.wait_calls == [15.0]
    assert supervisor.owns_child is True


def test_heartbeat_timeout_is_unconfirmed_when_sigint_cannot_be_sent(
    tmp_path,
):
    child = FakeChild(
        pid=4242,
        signal_error=ProcessLookupError("signal unavailable"),
    )
    supervisor, _factory, _clock = build_supervisor(tmp_path, child)

    result = supervisor.start(
        posture=safe_posture(),
        heartbeat_loader=safe_posture,
    )

    assert result.state == "stop_unconfirmed"
    assert result.pid == 4242
    assert child.signals == [signal.SIGINT]
    assert child.wait_calls == []
    assert supervisor.owns_child is True


def test_double_start_never_replaces_or_resignals_owned_child(tmp_path):
    child = FakeChild(pid=4242)
    supervisor, factory, _clock = build_supervisor(tmp_path, child)
    first = supervisor.start(
        posture=safe_posture(),
        heartbeat_loader=fresh_heartbeat_posture,
    )

    second = supervisor.start(
        posture=safe_posture(),
        heartbeat_loader=lambda: pytest.fail("must not poll"),
    )

    assert first.state == "running"
    assert second.state == "start_blocked"
    assert second.detail_code == "daemon_child_already_owned"
    assert len(factory.calls) == 1
    assert child.signals == []
    assert supervisor.owns_child is True


def test_stop_observes_already_exited_owned_child_without_signal(tmp_path):
    child = FakeChild(pid=4242)
    supervisor, _factory, _clock = build_supervisor(tmp_path, child)
    assert supervisor.start(
        posture=safe_posture(),
        heartbeat_loader=fresh_heartbeat_posture,
    ).state == "running"
    child.poll_results.append(0)

    result = supervisor.stop(timeout_seconds=2.5)

    assert result.state == "exited"
    assert result.pid == 4242
    assert child.signals == []
    assert child.wait_calls == []
    assert supervisor.owns_child is False


def test_stop_timeout_retains_exact_child_for_later_retry(tmp_path):
    child = FakeChild(
        pid=4242,
        wait_error=subprocess.TimeoutExpired("fake-child", 2.5),
    )
    supervisor, _factory, _clock = build_supervisor(tmp_path, child)
    assert supervisor.start(
        posture=safe_posture(),
        heartbeat_loader=fresh_heartbeat_posture,
    ).state == "running"

    result = supervisor.stop(timeout_seconds=2.5)

    assert result.state == "stop_unconfirmed"
    assert result.pid == 4242
    assert child.signals == [signal.SIGINT]
    assert child.wait_calls == [2.5]
    assert supervisor.owns_child is True


def test_stop_signal_failure_retains_exact_child_as_unconfirmed(tmp_path):
    child = FakeChild(pid=4242)
    supervisor, _factory, _clock = build_supervisor(tmp_path, child)
    assert supervisor.start(
        posture=safe_posture(),
        heartbeat_loader=fresh_heartbeat_posture,
    ).state == "running"
    child.signal_error = ProcessLookupError("signal unavailable")

    result = supervisor.stop(timeout_seconds=2.5)

    assert result.state == "stop_unconfirmed"
    assert result.pid == 4242
    assert child.signals == [signal.SIGINT]
    assert child.wait_calls == []
    assert supervisor.owns_child is True


def test_double_stop_never_signals_an_unowned_process(tmp_path):
    child = FakeChild(pid=4242)
    supervisor, _factory, _clock = build_supervisor(tmp_path, child)
    assert supervisor.start(
        posture=safe_posture(),
        heartbeat_loader=fresh_heartbeat_posture,
    ).state == "running"

    first = supervisor.stop(timeout_seconds=1)
    second = supervisor.stop(timeout_seconds=1)

    assert first.state == "off"
    assert second.state == "off"
    assert child.signals == [signal.SIGINT]
    assert child.wait_calls == [1]


def test_supervisor_source_has_no_broad_process_control():
    source = DAEMON_MODULE.read_text(encoding="utf-8")
    compact = source.replace(" ", "")
    forbidden = (
        "pkill",
        "killall",
        "os." + "kill",
        ".terminate(",
        ".kill(",
        "pgrep",
        "psutil",
    )

    assert not any(value in compact for value in forbidden)
    assert "shell=True" not in compact
    assert "start_new_session=True" not in compact
