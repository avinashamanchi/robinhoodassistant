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
from trading_assistant.operations.security_posture import (
    PostureCheck,
    PostureDetailCode,
    PostureName,
    PostureStatus,
    SecurityPostureReport,
    StartupDetailCode,
)


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


REQUEST_BUDGET_SCOPES = (
    "login",
    "session_read",
    "broker_read",
    "mutation",
    "approval",
    "privileged",
    "chat",
    "analysis",
    "backtest",
    "provider_read",
    "panic",
)
PROVIDER_BUDGET_SCOPES = ("anthropic", "gemini", "groq")


def safe_posture(
    *,
    observed_at: datetime | None = None,
    heartbeat_status: str = "unknown",
    heartbeat_detail: str = "daemon_heartbeat_missing",
    heartbeat_evidence_at: str | None = None,
) -> dict[str, object]:
    observed = observed_at or datetime.now(timezone.utc)
    observed_text = observed.isoformat()
    prior_text = (observed - timedelta(seconds=1)).isoformat()
    reset_text = (observed + timedelta(minutes=1)).isoformat()

    def check(
        name: str,
        status: str,
        detail_code: str,
        **evidence: object,
    ) -> dict[str, object]:
        return {
            "name": name,
            "status": status,
            "observed_at": observed_text,
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
        if heartbeat_status in {"fresh", "stale"}:
            evidence = datetime.fromisoformat(heartbeat_evidence_at)
            heartbeat["age_seconds"] = (
                observed - evidence
            ).total_seconds()
            heartbeat["max_age_seconds"] = 30.0
    checks = [
        check("broker_mode", "paper", "broker_paper_mode"),
        check(
            "loopback_https",
            "pass",
            "ok",
            evidence_at=prior_text,
        ),
        check("tls", "pass", "ok", evidence_at=prior_text),
        check(
            "secret_provider",
            "pass",
            "macos_keychain",
            evidence_at=prior_text,
        ),
        check(
            "sensitive_encryption",
            "pass",
            "ok",
            migration_state="complete",
            schema_version=1,
            rows_total=0,
            rows_completed=0,
            started_at=prior_text,
            completed_at=prior_text,
            updated_at=prior_text,
        ),
        *[
            check(
                "request_budget",
                "pass",
                "request_budget_available",
                scope=scope,
                budget_used=0,
                budget_remaining=10,
                budget_limit=10,
                reset_at=reset_text,
            )
            for scope in REQUEST_BUDGET_SCOPES
        ],
        *[
            check(
                "provider_budget",
                "pass",
                "provider_budget_available",
                scope=scope,
                count=0,
                budget_used=0,
                budget_remaining=10,
                budget_limit=10,
                input_tokens_used=0,
                input_tokens_remaining=100,
                input_tokens_limit=100,
                output_tokens_used=0,
                output_tokens_remaining=100,
                output_tokens_limit=100,
                reset_at=reset_text,
            )
            for scope in PROVIDER_BUDGET_SCOPES
        ],
        check(
            "webhook_receiver",
            "disabled",
            "integration_disabled",
        ),
        check(
            "composio_integration",
            "disabled",
            "integration_disabled",
        ),
        check(
            "quote_freshness",
            "unknown",
            "quote_evidence_unavailable",
        ),
        heartbeat,
        check(
            "startup_reconciliation",
            "pass",
            "reconciliation_current",
            generation=1,
            completed_generation=1,
            age_seconds=1.0,
            max_age_seconds=300.0,
            started_at=prior_text,
            completed_at=prior_text,
            updated_at=prior_text,
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
            generation=1,
            started_at=prior_text,
            updated_at=prior_text,
            expires_at=reset_text,
        ),
        check(
            "runtime_tenure",
            "released",
            "runtime_tenure_released",
            scope="daemon",
            generation=1,
            started_at=prior_text,
            updated_at=prior_text,
            expires_at=prior_text,
            completed_at=prior_text,
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
        "observed_at": observed_text,
        "checks": checks,
    }


def fresh_heartbeat_posture(
    evidence_at: str | None = None,
) -> dict[str, object]:
    observed = (
        datetime.fromisoformat(evidence_at)
        if evidence_at is not None
        else datetime.now(timezone.utc)
    )
    observed_text = observed.isoformat()
    report = safe_posture(
        observed_at=observed,
        heartbeat_status="fresh",
        heartbeat_detail="daemon_heartbeat_fresh",
        heartbeat_evidence_at=observed_text,
    )
    heartbeat = posture_check(report, "daemon_heartbeat")
    heartbeat.update(
        {
            "age_seconds": 0.0,
            "max_age_seconds": 30.0,
        }
    )
    daemon_tenure = posture_check(
        report,
        "runtime_tenure",
        scope="daemon",
    )
    daemon_tenure.update(
        {
            "status": "held",
            "detail_code": "runtime_tenure_held",
            "completed_at": None,
            "expires_at": (
                observed + timedelta(minutes=1)
            ).isoformat(),
        }
    )
    return report


def real_model_posture() -> dict[str, object]:
    raw = safe_posture()
    raw_checks = raw["checks"]
    assert isinstance(raw_checks, list)
    timestamp_fields = {
        "observed_at",
        "evidence_at",
        "started_at",
        "completed_at",
        "updated_at",
        "expires_at",
        "reset_at",
    }
    checks: list[PostureCheck] = []
    for raw_check in raw_checks:
        assert isinstance(raw_check, dict)
        values = dict(raw_check)
        values["name"] = PostureName(values["name"])
        values["status"] = PostureStatus(values["status"])
        detail = values["detail_code"]
        try:
            values["detail_code"] = PostureDetailCode(detail)
        except ValueError:
            values["detail_code"] = StartupDetailCode(detail)
        for field in timestamp_fields:
            value = values.get(field)
            if isinstance(value, str):
                values[field] = datetime.fromisoformat(value)
        checks.append(PostureCheck(**values))
    report = SecurityPostureReport(
        observed_at=datetime.fromisoformat(str(raw["observed_at"])),
        checks=tuple(checks),
    )
    serialized = report.model_dump(mode="json")
    assert isinstance(serialized, dict)
    return serialized


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


def test_start_accepts_real_security_posture_model_serialization(tmp_path):
    child = FakeChild(pid=4242)
    supervisor, factory, _clock = build_supervisor(tmp_path, child)

    result = supervisor.start(
        posture=real_model_posture(),
        heartbeat_loader=fresh_heartbeat_posture,
    )

    assert result.state == "running"
    assert len(factory.calls) == 1


def test_start_rejects_unknown_posture_check_name(tmp_path):
    report = safe_posture()
    checks = report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "name": "future_safety_gate",
            "status": "pass",
            "observed_at": report["observed_at"],
            "detail_code": "future_safety_gate_clear",
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

    assert result.state == "start_blocked"
    assert result.detail_code == "posture_schema_invalid"
    assert factory.calls == []


@pytest.mark.parametrize(
    "name",
    [
        "broker_mode",
        "loopback_https",
        "tls",
        "secret_provider",
        "sensitive_encryption",
        "webhook_receiver",
        "composio_integration",
        "daemon_heartbeat",
        "startup_reconciliation",
        "quote_freshness",
        "unsafe_orders",
        "unsafe_fills",
        "uncertain_interlocks",
    ],
)
def test_start_rejects_duplicate_singleton_checks(tmp_path, name):
    report = safe_posture()
    checks = report["checks"]
    assert isinstance(checks, list)
    duplicate = dict(posture_check(report, name))
    checks.append(duplicate)
    supervisor, factory, _clock = build_supervisor(
        tmp_path,
        FakeChild(pid=4242),
    )

    result = supervisor.start(
        posture=report,
        heartbeat_loader=fresh_heartbeat_posture,
    )

    assert result.state == "start_blocked"
    assert result.detail_code == "posture_schema_invalid"
    assert factory.calls == []


def test_start_requires_exactly_one_safe_secret_provider(tmp_path):
    report = safe_posture()
    secret = posture_check(report, "secret_provider")
    secret.update(
        {
            "status": "unknown",
            "detail_code": "startup_evidence_unavailable",
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

    assert result.state == "start_blocked"
    assert result.detail_code == "posture_secret_provider_unsafe"
    assert factory.calls == []


def test_start_rejects_duplicate_runtime_tenure_scope(tmp_path):
    report = safe_posture()
    checks = report["checks"]
    assert isinstance(checks, list)
    checks.append(
        dict(
            posture_check(
                report,
                "runtime_tenure",
                scope="app",
            )
        )
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
    assert result.detail_code == "posture_schema_invalid"
    assert factory.calls == []


@pytest.mark.parametrize(
    ("name", "scope"),
    [
        ("request_budget", "session_read"),
        ("provider_budget", "groq"),
        ("circuit_breaker", "liquidity"),
        ("quarantine", "failed"),
        ("unsafe_rules", "rule_groups"),
    ],
)
def test_start_rejects_incomplete_scoped_check_sets(
    tmp_path,
    name,
    scope,
):
    report = safe_posture()
    checks = report["checks"]
    assert isinstance(checks, list)
    report["checks"] = [
        item
        for item in checks
        if not (
            isinstance(item, dict)
            and item.get("name") == name
            and item.get("scope") == scope
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
    assert result.detail_code == "posture_schema_invalid"
    assert factory.calls == []


@pytest.mark.parametrize(
    ("name", "scope"),
    [
        ("request_budget", "analysis"),
        ("provider_budget", "anthropic"),
    ],
)
def test_start_rejects_unknown_budget_safety_evidence(
    tmp_path,
    name,
    scope,
):
    report = safe_posture()
    budget = posture_check(report, name, scope=scope)
    budget.update(
        {
            "status": "unknown",
            "detail_code": "budget_evidence_unavailable",
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

    assert result.state == "start_blocked"
    assert result.detail_code == "posture_budget_evidence_unsafe"
    assert factory.calls == []


def test_quote_freshness_unavailable_matches_current_producer_shape(
    tmp_path,
):
    report = safe_posture()
    quote = posture_check(report, "quote_freshness")
    assert (
        quote["status"],
        quote["detail_code"],
    ) == ("unknown", "quote_evidence_unavailable")
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


def test_start_rejects_future_secret_provider_evidence(tmp_path):
    report = safe_posture()
    observed_at = datetime.fromisoformat(str(report["observed_at"]))
    secret = posture_check(report, "secret_provider")
    secret["evidence_at"] = (
        observed_at + timedelta(microseconds=1)
    ).isoformat()
    supervisor, factory, _clock = build_supervisor(
        tmp_path,
        FakeChild(pid=4242),
    )

    result = supervisor.start(
        posture=report,
        heartbeat_loader=fresh_heartbeat_posture,
    )

    assert result.state == "start_blocked"
    assert result.detail_code == "posture_evidence_time_invalid"
    assert factory.calls == []


@pytest.mark.parametrize(
    ("case", "detail_code"),
    [
        ("encryption_order", "posture_structural_evidence_unsafe"),
        ("reconciliation_order", "posture_reconciliation_unsafe"),
        ("budget_reset", "posture_budget_evidence_unsafe"),
    ],
)
def test_start_rejects_incoherent_safety_evidence_timeline(
    tmp_path,
    case,
    detail_code,
):
    report = safe_posture()
    observed_at = datetime.fromisoformat(str(report["observed_at"]))
    if case == "encryption_order":
        check = posture_check(report, "sensitive_encryption")
        check["started_at"] = (
            observed_at - timedelta(seconds=1)
        ).isoformat()
        check["completed_at"] = (
            observed_at - timedelta(seconds=2)
        ).isoformat()
    elif case == "reconciliation_order":
        check = posture_check(report, "startup_reconciliation")
        check["started_at"] = (
            observed_at - timedelta(seconds=1)
        ).isoformat()
        check["completed_at"] = (
            observed_at - timedelta(seconds=2)
        ).isoformat()
    else:
        check = posture_check(
            report,
            "request_budget",
            scope="session_read",
        )
        check["reset_at"] = observed_at.isoformat()
    supervisor, factory, _clock = build_supervisor(
        tmp_path,
        FakeChild(pid=4242),
    )

    result = supervisor.start(
        posture=report,
        heartbeat_loader=fresh_heartbeat_posture,
    )

    assert result.state == "start_blocked"
    assert result.detail_code == detail_code
    assert factory.calls == []


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
        "secret_provider",
        "sensitive_encryption",
        "request_budget",
        "provider_budget",
        "webhook_receiver",
        "composio_integration",
        "daemon_heartbeat",
        "startup_reconciliation",
        "quote_freshness",
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
    "case",
    ["empty", "checks_not_list", "invalid_check"],
)
def test_start_blocks_malformed_report_objects(tmp_path, case):
    if case == "empty":
        posture: dict[str, object] = {}
    elif case == "checks_not_list":
        posture = {"checks": "not-a-list"}
    else:
        posture = {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "checks": [None],
        }
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
    "offset_seconds",
    [-31, 6],
)
def test_start_blocks_stale_or_future_posture_observation(
    tmp_path,
    offset_seconds,
):
    report = safe_posture(
        observed_at=(
            datetime.now(timezone.utc)
            + timedelta(seconds=offset_seconds)
        )
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
    assert result.detail_code == "posture_not_fresh"
    assert factory.calls == []


def test_start_blocks_one_check_observation_mismatched_from_report(
    tmp_path,
):
    report = safe_posture()
    observed_at = datetime.fromisoformat(str(report["observed_at"]))
    secret = posture_check(report, "secret_provider")
    secret["observed_at"] = (
        observed_at + timedelta(microseconds=1)
    ).isoformat()
    supervisor, factory, _clock = build_supervisor(
        tmp_path,
        FakeChild(pid=4242),
    )

    result = supervisor.start(
        posture=report,
        heartbeat_loader=fresh_heartbeat_posture,
    )

    assert result.state == "start_blocked"
    assert result.detail_code == "posture_invalid"
    assert factory.calls == []


def test_each_fixture_report_has_one_fresh_construction_timestamp():
    first = safe_posture()
    second = safe_posture()

    for report in (first, second):
        observed_at = report["observed_at"]
        checks = report["checks"]
        assert isinstance(observed_at, str)
        assert isinstance(checks, list)
        assert {
            item["observed_at"]
            for item in checks
            if isinstance(item, dict)
        } == {observed_at}
        age = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(observed_at)
        ).total_seconds()
        assert 0 <= age < 1
    assert first["observed_at"] != second["observed_at"]


def test_start_blocks_current_daemon_heartbeat(tmp_path):
    observed_at = datetime.now(timezone.utc)
    report = safe_posture(
        observed_at=observed_at,
        heartbeat_status="fresh",
        heartbeat_detail="daemon_heartbeat_fresh",
        heartbeat_evidence_at=observed_at.isoformat(),
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
    observed_at = datetime.now(timezone.utc)
    report = safe_posture(
        observed_at=observed_at,
        heartbeat_status="stale",
        heartbeat_detail="daemon_heartbeat_stale",
        heartbeat_evidence_at=(
            observed_at - timedelta(minutes=1)
        ).isoformat(),
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


@pytest.mark.parametrize(
    ("status", "detail_code"),
    [
        ("held", "runtime_tenure_held"),
        ("stale", "runtime_tenure_stale"),
    ],
)
def test_start_blocks_maintenance_tenure_conflicting_with_runtime(
    tmp_path,
    status,
    detail_code,
):
    report = safe_posture()
    observed_at = datetime.fromisoformat(str(report["observed_at"]))
    checks = report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "name": "runtime_tenure",
            "status": status,
            "observed_at": report["observed_at"],
            "detail_code": detail_code,
            "scope": "maintenance",
            "generation": 1,
            "started_at": (
                observed_at - timedelta(seconds=2)
            ).isoformat(),
            "updated_at": (
                observed_at - timedelta(seconds=1)
            ).isoformat(),
            "expires_at": (
                observed_at
                + timedelta(seconds=30 if status == "held" else -1)
            ).isoformat(),
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

    assert result.state == "start_blocked"
    assert result.detail_code == "maintenance_tenure_conflict"
    assert factory.calls == []


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
    posture = safe_posture()
    observed_at = posture["observed_at"]
    assert isinstance(observed_at, str)

    result = supervisor.start(
        posture=posture,
        heartbeat_loader=lambda: fresh_heartbeat_posture(observed_at),
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


def test_child_exit_after_validated_heartbeat_is_never_reported_running(
    tmp_path,
):
    child = FakeChild(pid=4242, poll_results=[None, 7])
    supervisor, factory, _clock = build_supervisor(tmp_path, child)

    result = supervisor.start(
        posture=safe_posture(),
        heartbeat_loader=fresh_heartbeat_posture,
    )

    assert result.state == "exited"
    assert result.pid == 4242
    assert result.detail_code == "daemon_exited_after_heartbeat"
    assert child.poll_calls == 2
    assert child.signals == []
    assert child.wait_calls == []
    assert supervisor.owns_child is False
    _argv, options = factory.calls[0]
    assert options["stdout"].closed is True


def test_poststart_truncated_posture_stops_owned_child(tmp_path):
    child = FakeChild(pid=4242)
    supervisor, _factory, _clock = build_supervisor(tmp_path, child)
    prestart = safe_posture()
    poststart = fresh_heartbeat_posture()
    heartbeat = dict(posture_check(poststart, "daemon_heartbeat"))
    poststart["checks"] = [heartbeat]

    result = supervisor.start(
        posture=prestart,
        heartbeat_loader=lambda: poststart,
    )

    assert result.state == "start_blocked"
    assert result.detail_code == "poststart_posture_unsafe"
    assert child.signals == [signal.SIGINT]
    assert child.wait_calls == [15.0]
    assert supervisor.owns_child is False


def test_poststart_new_breaker_stops_owned_child(tmp_path):
    child = FakeChild(pid=4242)
    supervisor, _factory, _clock = build_supervisor(tmp_path, child)
    prestart = safe_posture()
    poststart = fresh_heartbeat_posture()
    breaker = posture_check(
        poststart,
        "circuit_breaker",
        scope="equity",
    )
    breaker.update(
        {
            "status": "tripped",
            "detail_code": "breaker_tripped",
            "count": 1,
        }
    )

    result = supervisor.start(
        posture=prestart,
        heartbeat_loader=lambda: poststart,
    )

    assert result.state == "start_blocked"
    assert result.detail_code == "poststart_posture_unsafe"
    assert child.signals == [signal.SIGINT]
    assert child.wait_calls == [15.0]
    assert supervisor.owns_child is False


@pytest.mark.parametrize(
    ("status", "detail_code"),
    [
        ("held", "runtime_tenure_held"),
        ("stale", "runtime_tenure_stale"),
    ],
)
def test_poststart_maintenance_conflict_stops_owned_child(
    tmp_path,
    status,
    detail_code,
):
    child = FakeChild(pid=4242)
    supervisor, _factory, _clock = build_supervisor(tmp_path, child)
    prestart = safe_posture()
    poststart = fresh_heartbeat_posture()
    observed_at = datetime.fromisoformat(
        str(poststart["observed_at"])
    )
    checks = poststart["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "name": "runtime_tenure",
            "status": status,
            "observed_at": poststart["observed_at"],
            "detail_code": detail_code,
            "scope": "maintenance",
            "generation": 1,
            "started_at": (
                observed_at - timedelta(seconds=2)
            ).isoformat(),
            "updated_at": (
                observed_at - timedelta(seconds=1)
            ).isoformat(),
            "expires_at": (
                observed_at
                + timedelta(seconds=30 if status == "held" else -1)
            ).isoformat(),
        }
    )

    result = supervisor.start(
        posture=prestart,
        heartbeat_loader=lambda: poststart,
    )

    assert result.state == "start_blocked"
    assert result.detail_code == "poststart_posture_unsafe"
    assert child.signals == [signal.SIGINT]
    assert child.wait_calls == [15.0]
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
