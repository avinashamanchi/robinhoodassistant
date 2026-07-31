"""Launch features: health/heartbeat (D3), preflight helpers (B3), and a
full order lifecycle integration (B2)."""

from __future__ import annotations

import base64
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import textwrap
import time
import tomllib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text

from tests.app_factory import create_app
from trading_assistant.app.errors import ApiError
from trading_assistant.assets import AssetClass
from trading_assistant.broker.models import Account, Position
from trading_assistant.config import BrokerKind, Secrets
from trading_assistant.db.models import AuditEvent, Fill, Heartbeat, Order
from trading_assistant.db.session import create_db_engine
from trading_assistant.dependencies import RequiredDependencyUnavailable
from trading_assistant.operations import MutationContext, OperationsService
from trading_assistant.operations.health import build_operational_health
from trading_assistant.risk.breakers import BreakerScope
from trading_assistant.security.sensitive_fields import (
    persist_sensitive,
    sensitive_store,
)
from tests.safety_helpers import bootstrap_database_to_revision


_ROOT = Path(__file__).resolve().parents[1]
_START_SCRIPT = _ROOT / "scripts/start.sh"
_START_CURL = "/usr/bin/curl"
_LIVENESS_URL = "https://localhost:8020/health/live"
_LIVE_PAYLOAD = '{"alive":true,"database_reachable":true}'
_TRUSTED_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _write_start_harness_executable(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)


@dataclass(frozen=True)
class StartScriptHarness:
    project: Path
    script: Path
    ca: Path
    log: Path
    curl_state: Path
    serve_state: Path
    parent: Path

    def run(
        self,
        *,
        control: str = "running",
        liveness: str = "valid",
        environment_overrides: dict[str, str] | None = None,
        interpreter: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        pid_file = self.project / "logs/app.pid"
        if control == "running":
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text("123\n", encoding="ascii")
        elif pid_file.exists():
            pid_file.unlink()

        environment = {
            "HARNESS_CONTROL": control,
            "HARNESS_CURL_STATE": str(self.curl_state),
            "HARNESS_LIVENESS": liveness,
            "HARNESS_LOG": str(self.log),
            "HARNESS_SERVE_STATE": str(self.serve_state),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
        }
        if environment_overrides is not None:
            environment.update(environment_overrides)
        command = [
            *interpreter,
            str(self.script),
        ]
        return subprocess.run(
            command,
            cwd=self.project,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def events(self) -> list[dict[str, object]]:
        if not self.log.exists():
            return []
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
        ]


@pytest.fixture
def start_script_harness(tmp_path: Path) -> StartScriptHarness:
    project = tmp_path / "project"
    script = project / "scripts/start.sh"
    python = project / ".venv/bin/python"
    ca = project / ".local/tls/rootCA.pem"
    fake_curl = tmp_path / "absolute-curl"
    log = tmp_path / "events.jsonl"
    curl_state = tmp_path / "curl-count"
    serve_state = tmp_path / "serve-seen"

    ca.parent.mkdir(parents=True)
    ca.write_text("test-only CA placeholder\n", encoding="ascii")

    fake_python_source = textwrap.dedent(
        f"""\
        #!{sys.executable} -I
        import json
        import os
        from pathlib import Path
        import sys

        REAL_PYTHON = {sys.executable!r}
        log = Path(os.environ["HARNESS_LOG"])
        serve_state = Path(os.environ["HARNESS_SERVE_STATE"])

        def record(**detail):
            with log.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {{"event": "python", **detail}},
                        sort_keys=True,
                    )
                    + "\\n"
                )

        arguments = sys.argv[1:]
        injected_environment = sorted(
            name
            for name in (
                "PYTHONBREAKPOINT",
                "PYTHONCASEOK",
                "PYTHONEXECUTABLE",
                "PYTHONHOME",
                "PYTHONINSPECT",
                "PYTHONPATH",
                "PYTHONPLATLIBDIR",
                "PYTHONSTARTUP",
                "PYTHONUSERBASE",
                "PYTHONWARNINGS",
            )
            if name in os.environ
        )
        record(
            arguments=arguments,
            bash_env=os.environ.get("BASH_ENV"),
            injected_environment=injected_environment,
            instance_id=os.environ.get("TRADING_APP_INSTANCE_ID"),
            path=os.environ.get("PATH"),
            python_no_user_site=os.environ.get("PYTHONNOUSERSITE"),
            python_safe_path=os.environ.get("PYTHONSAFEPATH"),
        )

        validator = (
            arguments[:2] == ["-I", "-c"]
            or (
                arguments[:1] == ["-c"]
                and "secrets.token_hex" not in arguments[1]
            )
        )
        if validator:
            validator_environment = os.environ.copy()
            for name in (
                "PYTHONBREAKPOINT",
                "PYTHONCASEOK",
                "PYTHONEXECUTABLE",
                "PYTHONHOME",
                "PYTHONINSPECT",
                "PYTHONNOUSERSITE",
                "PYTHONPATH",
                "PYTHONPLATLIBDIR",
                "PYTHONSAFEPATH",
                "PYTHONSTARTUP",
                "PYTHONUSERBASE",
                "PYTHONWARNINGS",
            ):
                validator_environment.pop(name, None)
            os.execve(
                REAL_PYTHON,
                [REAL_PYTHON, *arguments],
                validator_environment,
            )

        if (
            arguments[:2]
            == ["-m", "trading_assistant.ops.control"]
            and len(arguments) >= 3
        ):
            command = arguments[2]
            if command == "expected-argv":
                print(
                    "/test/python "
                    "-m trading_assistant.ops.serve"
                )
                raise SystemExit(0)
            if command == "validate":
                raise SystemExit(
                    0
                    if os.environ["HARNESS_CONTROL"] == "running"
                    else 1
                )
            if command == "ready":
                raise SystemExit(
                    0
                    if (
                        os.environ["HARNESS_CONTROL"] == "running"
                        or serve_state.exists()
                    )
                    else 1
                )
            if command in {{"begin-start", "abandon-start"}}:
                raise SystemExit(0)
            raise SystemExit(97)

        if (
            arguments[:1] == ["-c"]
            and "secrets.token_hex" in arguments[1]
        ):
            print("a" * 64)
            raise SystemExit(0)

        if arguments == [
            "-m",
            "trading_assistant.ops.serve",
        ]:
            serve_state.touch()
            raise SystemExit(0)

        raise SystemExit(98)
        """
    )
    _write_start_harness_executable(python, fake_python_source)

    fake_curl_source = textwrap.dedent(
        f"""\
        #!{sys.executable} -I
        import json
        import os
        from pathlib import Path
        import sys
        import time

        log = Path(os.environ["HARNESS_LOG"])
        state = Path(os.environ["HARNESS_CURL_STATE"])
        call_number = (
            int(state.read_text(encoding="ascii"))
            if state.exists()
            else 0
        ) + 1
        state.write_text(str(call_number), encoding="ascii")
        arguments = sys.argv[1:]
        with log.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {{
                        "event": "curl",
                        "arguments": arguments,
                        "home": os.environ.get("HOME"),
                        "https_proxy": os.environ.get("HTTPS_PROXY"),
                        "path": os.environ.get("PATH"),
                    }},
                    sort_keys=True,
                )
                + "\\n"
            )

        mode = os.environ["HARNESS_LIVENESS"]
        first_call = call_number == 1
        if mode == "stall-once" and first_call:
            bounded = (
                "--connect-timeout" in arguments
                and "--max-time" in arguments
            )
            time.sleep(0.05 if bounded else 1.2)
            raise SystemExit(28)
        if mode == "curl-error-valid-body-once" and first_call:
            print({_LIVE_PAYLOAD!r})
            raise SystemExit(22)
        if first_call and mode == "duplicate-json":
            print(
                '{{"alive":false,"alive":true,'
                '"database_reachable":true}}'
            )
        elif first_call and mode == "integer-json":
            print('{{"alive":1,"database_reachable":1}}')
        elif first_call and mode == "float-json":
            print('{{"alive":1.0,"database_reachable":1.0}}')
        elif first_call and mode == "oversized":
            print({_LIVE_PAYLOAD!r} + (" " * 2048))
        elif first_call and mode == "invalid-utf8":
            sys.stdout.buffer.write(
                b'{{"alive":true,"database_reachable":true}}\\xff'
            )
        elif first_call and mode == "extra-json":
            print(
                '{{"alive":true,"database_reachable":true,'
                '"extra":true}}'
            )
        elif first_call and mode == "false-json":
            print('{{"alive":false,"database_reachable":true}}')
        else:
            print({_LIVE_PAYLOAD!r})
        """
    )
    _write_start_harness_executable(fake_curl, fake_curl_source)

    production_source = _START_SCRIPT.read_text(encoding="utf-8")
    assert _START_CURL in production_source
    injected_source = production_source.replace(
        _START_CURL,
        str(fake_curl),
    )
    _write_start_harness_executable(script, injected_source)

    return StartScriptHarness(
        project=project,
        script=script,
        ca=ca,
        log=log,
        curl_state=curl_state,
        serve_state=serve_state,
        parent=tmp_path,
    )


def _persist_audit_fixture(session, event: AuditEvent) -> AuditEvent:
    return persist_sensitive(
        session,
        event,
        {"reason": "test fixture", "detail_json": "{}"},
    )


def _persist_order_fixture(session, order: Order) -> Order:
    return persist_sensitive(
        session,
        order,
        {"approval_reason": "test fixture"},
    )


def _sensitive_head_database(tmp_path, name: str) -> str:
    path = tmp_path / name
    url = f"sqlite:///{path}"
    bootstrap_database_to_revision(url, "head")
    return url


def test_startup_guard_uses_database_encryption_inspector_and_blocks_required(
    tmp_path,
    app_config,
    monkeypatch,
):
    from trading_assistant.ops import serve
    from trading_assistant.preflight import StructuralCheck

    url = _sensitive_head_database(tmp_path, "startup-required.db")
    config = app_config.model_copy(
        update={
            "trading": app_config.trading.model_copy(
                update={"broker": BrokerKind.ALPACA}
            )
        }
    )
    engine = create_db_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE sensitive_migration_state "
                "SET active_key_id=:active_key_id"
            ),
            {"active_key_id": config.encryption.active_key_id},
        )
    monkeypatch.setattr(
        serve,
        "_tls_check",
        lambda _config: StructuralCheck("tls", "passed", "ok"),
    )

    with pytest.raises(serve.StartupGuardBlocked) as captured:
        serve.run_startup_guard(
            config=config,
            secrets=Secrets(
                database_url=url,
                app_api_token="A7v!9qL2#mN4$pR6&tU8*wX0-zB3_cD5",
                field_encryption_keys={
                    config.encryption.active_key_id: (
                        base64.b64encode(b"l" * 32).decode()
                    )
                },
            ),
            secret_loaded_at=datetime.now(timezone.utc),
        )

    codes = {check.code for check in captured.value.checks}
    assert "sensitive_migration_required" in codes
    assert "encryption_inspector_unavailable" not in codes


def test_startup_guard_allows_only_internally_consistent_complete_encryption(
    tmp_path,
    app_config,
    monkeypatch,
):
    from trading_assistant.ops import serve
    from trading_assistant.preflight import StructuralCheck

    url = _sensitive_head_database(tmp_path, "startup-complete.db")
    config = app_config.model_copy(
        update={
            "trading": app_config.trading.model_copy(
                update={"broker": BrokerKind.ALPACA}
            )
        }
    )
    now = datetime.now(timezone.utc)
    engine = create_db_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE sensitive_migration_state SET "
                "state='complete',active_key_id=:active_key_id,"
                "rows_total=0,rows_completed=0,backup_path_hash=:backup_hash,"
                "started_at=:started_at,completed_at=:completed_at,"
                "updated_at=:updated_at"
            ),
            {
                "active_key_id": config.encryption.active_key_id,
                "backup_hash": "a" * 64,
                "started_at": now - timedelta(minutes=2),
                "completed_at": now - timedelta(minutes=1),
                "updated_at": now,
            },
        )
    monkeypatch.setattr(
        serve,
        "_tls_check",
        lambda _config: StructuralCheck("tls", "passed", "ok"),
    )

    secrets = Secrets(
        database_url=url,
        app_api_token="A7v!9qL2#mN4$pR6&tU8*wX0-zB3_cD5",
        field_encryption_keys={
            config.encryption.active_key_id: (
                base64.b64encode(b"l" * 32).decode()
            )
        },
    )
    receipt = serve.run_startup_guard(
        config=config,
        secrets=secrets,
        secret_loaded_at=now,
    )
    from trading_assistant.operations import security_posture as posture

    evidence = posture._validate_startup_guard_receipt(
        receipt,
        config=config,
        secrets=secrets,
        runtime_role="app",
    )

    assert all(
        check.status == "pass"
        for check in evidence.structural_checks
    )
    assert any(
        check.name is posture.StartupCheckName.ENCRYPTION
        and check.detail_code is posture.StartupDetailCode.OK
        for check in evidence.structural_checks
    )


def test_stop_uses_only_cooperative_control_and_never_targets_a_pid():
    """A check-then-kill implementation remains unsafe when macOS reuses a PID."""
    stop = Path("scripts/stop.sh").read_text(encoding="utf-8")
    identity_path = Path("scripts/lib/app-process-identity.sh")
    identity = (
        identity_path.read_text(encoding="utf-8")
        if identity_path.exists()
        else ""
    )

    assert "trading_assistant.ops.control stop" in stop
    assert "kill " not in stop
    assert "kill " not in identity


def test_operator_console_script_targets_fixed_terminal_entrypoint():
    project = tomllib.loads(
        Path("pyproject.toml").read_text(encoding="utf-8")
    )["project"]

    assert project["scripts"]["trading-operator"] == (
        "trading_assistant.ops.operator_terminal:main"
    )


def test_controlled_start_rejects_nonprivileged_shell_entry(
    start_script_harness: StartScriptHarness,
):
    completed = start_script_harness.run(
        interpreter=("/bin/bash",),
    )

    assert completed.returncode != 0
    assert start_script_harness.events() == []


def test_controlled_start_isolates_shell_and_python_environment(
    start_script_harness: StartScriptHarness,
):
    fake_bin = start_script_harness.parent / "fake-bin"
    fake_bash_marker = start_script_harness.parent / "fake-bash-used"
    bash_env_marker = start_script_harness.parent / "bash-env-used"
    _write_start_harness_executable(
        fake_bin / "bash",
        textwrap.dedent(
            """\
            #!/bin/bash
            /usr/bin/touch "$HARNESS_FAKE_BASH_MARKER"
            exec /bin/bash "$@"
            """
        ),
    )
    bash_env = start_script_harness.parent / "bash-env"
    bash_env.write_text(
        '/usr/bin/touch "$HARNESS_BASH_ENV_MARKER"\n',
        encoding="utf-8",
    )

    completed = start_script_harness.run(
        environment_overrides={
            "BASH_ENV": str(bash_env),
            "CDPATH": "/attacker",
            "ENV": str(bash_env),
            "HARNESS_BASH_ENV_MARKER": str(bash_env_marker),
            "HARNESS_FAKE_BASH_MARKER": str(fake_bash_marker),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "PYTHONBREAKPOINT": "attacker.breakpoint",
            "PYTHONCASEOK": "1",
            "PYTHONEXECUTABLE": "/attacker/python",
            "PYTHONHOME": "/attacker",
            "PYTHONINSPECT": "1",
            "PYTHONNOUSERSITE": "0",
            "PYTHONPATH": "/attacker",
            "PYTHONPLATLIBDIR": "attacker",
            "PYTHONSAFEPATH": "0",
            "PYTHONSTARTUP": "/attacker/startup.py",
            "PYTHONUSERBASE": "/attacker",
            "PYTHONWARNINGS": "error::attacker.Warning",
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert not fake_bash_marker.exists()
    assert not bash_env_marker.exists()
    python_events = [
        event
        for event in start_script_harness.events()
        if event["event"] == "python"
    ]
    assert python_events
    assert all(
        event["path"] == _TRUSTED_PATH
        and event["bash_env"] is None
        and event["injected_environment"] == []
        and event["python_no_user_site"] == "1"
        and event["python_safe_path"] == "1"
        for event in python_events
    )


def test_controlled_start_uses_pinned_bounded_https_liveness(
    start_script_harness: StartScriptHarness,
):
    home = start_script_harness.parent / "home"
    home.mkdir()
    (home / ".curlrc").write_text("--insecure\n", encoding="utf-8")

    completed = start_script_harness.run(
        environment_overrides={
            "ALL_PROXY": "http://127.0.0.1:9",
            "HOME": str(home),
            "HTTPS_PROXY": "http://127.0.0.1:9",
        },
    )

    assert completed.returncode == 0, completed.stderr
    events = start_script_harness.events()
    curl = next(event for event in events if event["event"] == "curl")
    assert curl["arguments"] == [
        "--disable",
        "--fail",
        "--silent",
        "--show-error",
        "--noproxy",
        "*",
        "--resolve",
        "localhost:8020:127.0.0.1",
        "--proto",
        "=https",
        "--connect-timeout",
        "2",
        "--max-time",
        "3",
        "--max-filesize",
        "1024",
        "--cacert",
        str(start_script_harness.ca),
        _LIVENESS_URL,
    ]
    assert curl["path"] == _TRUSTED_PATH
    validators = [
        event
        for event in events
        if event["event"] == "python"
        and event["arguments"][:2] == ["-I", "-c"]
    ]
    assert len(validators) == 1


@pytest.mark.parametrize(
    "liveness",
    (
        "duplicate-json",
        "integer-json",
        "float-json",
        "oversized",
        "invalid-utf8",
        "extra-json",
        "false-json",
    ),
)
def test_controlled_start_retries_noncanonical_liveness(
    start_script_harness: StartScriptHarness,
    liveness: str,
):
    completed = start_script_harness.run(liveness=liveness)

    assert completed.returncode == 0, completed.stderr
    curl_events = [
        event
        for event in start_script_harness.events()
        if event["event"] == "curl"
    ]
    assert len(curl_events) == 2


def test_controlled_start_uses_finite_liveness_timeouts(
    start_script_harness: StartScriptHarness,
):
    started = time.monotonic()
    completed = start_script_harness.run(liveness="stall-once")
    elapsed = time.monotonic() - started

    assert completed.returncode == 0, completed.stderr
    assert elapsed < 1.0
    assert sum(
        event["event"] == "curl"
        for event in start_script_harness.events()
    ) == 2


def test_controlled_start_preserves_pipefail_for_curl_errors(
    start_script_harness: StartScriptHarness,
):
    completed = start_script_harness.run(
        liveness="curl-error-valid-body-once",
    )

    assert completed.returncode == 0, completed.stderr
    assert sum(
        event["event"] == "curl"
        for event in start_script_harness.events()
    ) == 2


def test_controlled_start_preserves_serve_and_expected_argv(
    start_script_harness: StartScriptHarness,
):
    completed = start_script_harness.run(
        control="absent",
        environment_overrides={
            "PYTHONBREAKPOINT": "attacker.breakpoint",
            "PYTHONHOME": "/attacker",
            "PYTHONPATH": "/attacker",
            "PYTHONWARNINGS": "error::attacker.Warning",
        },
    )

    assert completed.returncode == 0, completed.stderr
    python_events = [
        event
        for event in start_script_harness.events()
        if event["event"] == "python"
    ]
    expected_argv = next(
        event
        for event in python_events
        if event["arguments"][-1:] == ["expected-argv"]
    )
    serve = next(
        event
        for event in python_events
        if event["arguments"][-1:] == [
            "trading_assistant.ops.serve"
        ]
    )
    assert expected_argv["arguments"] == [
        "-m",
        "trading_assistant.ops.control",
        "expected-argv",
    ]
    assert serve["arguments"] == [
        "-m",
        "trading_assistant.ops.serve",
    ]
    assert serve["instance_id"] == "a" * 64
    assert serve["injected_environment"] == []
    assert serve["python_no_user_site"] == "1"
    assert serve["python_safe_path"] == "1"


def test_manual_launchers_check_interpreter_before_using_it_and_delegate_recovery():
    start = Path("scripts/start.sh").read_text(encoding="utf-8")
    stop = Path("scripts/stop.sh").read_text(encoding="utf-8")

    for script in (start, stop):
        assert script.index('if [[ ! -x "$PY" ]]') < script.index(
            "expected-argv"
        )
    assert "refusing to replace stale or malformed app control metadata" not in start
    assert "refusing to replace existing app control socket" not in start
    assert "trading_assistant.ops.control validate" in start
    assert start.count(
        "trading_assistant.ops.control ready"
    ) == 2
    assert start.count("--port 8020") == 2
    assert "trading_assistant.ops.serve" in start
    assert "trading_assistant.ops.control begin-start" in start
    assert start.index("begin-start") < start.index(
        "trading_assistant.ops.serve"
    )
    assert "trading_assistant.ops.control app-absent" in stop
    assert "$LSOF" not in stop
    assert "--cacert \"$TLS_CA\"" in start
    assert "https://localhost:8020/health/live" in start
    assert "startup, control artifact, listener" in stop


class _StubAgent:
    def chat(self, message, **context):
        return {"reply": "ok", "tool_calls": []}


def _approve(svc, order_id):
    return svc.approve_order(
        order_id,
        actor="operator:test",
        reason="launch test",
        request_id="launch-test-approval",
    )


def _propose(svc, **kwargs):
    return svc.propose_order(
        **kwargs,
        actor="operator:test",
        reason="launch test proposal",
        request_id="launch-test-proposal",
    )


def _sync(svc):
    return svc.sync_open_orders(
        actor="operator:test",
        reason="launch test broker reconciliation",
        request_id="launch-test-sync",
    )


# ── D3 health + heartbeat ───────────────────────────────────────
def test_health_reflects_heartbeat(make_service):
    svc = make_service()
    assert svc.health()["daemon_alive"] is False        # no heartbeat yet
    svc.write_heartbeat("daemon")
    h = svc.health()
    assert h["db_ok"] is True and h["daemon_alive"] is True
    assert h["heartbeat_age_seconds"] < 5


def test_authenticated_health_ignores_non_daemon_heartbeat(make_service):
    svc = make_service()
    svc.write_heartbeat("app")

    assert svc.health()["daemon_alive"] is False


def test_only_liveness_endpoint_is_anonymous(make_service):
    app = create_app(service=make_service(), agent=_StubAgent(), api_token="tok", planning=None)
    client = TestClient(app)

    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {
        "alive": True,
        "database_reachable": True,
    }
    assert client.get("/health").status_code == 401


def test_reject_http_receipt_captures_request_and_idempotency_identity(
    authenticated_client,
):
    client, csrf = authenticated_client
    service = client.trading_service
    proposal = service.propose_order(
        "AAPL",
        "buy",
        "market",
        qty="1",
        actor="operator:test",
        reason="create rejection target",
        request_id="create-rejection-target",
    )

    response = client.post(
        f"/reject/{proposal['order_id']}",
        headers={
            "X-CSRF-Token": csrf,
            "X-Request-ID": "incoming-request-id",
            "Idempotency-Key": "reject-once",
        },
        json={"reason": "operator review"},
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "incoming-request-id"
    with service.session_factory() as session:
        event = session.query(AuditEvent).filter_by(
            action="http.reject",
            request_id="incoming-request-id",
        ).one()
    assert event.actor == "operator:local"
    assert event.idempotency_key == "reject-once"
    assert event.result_code == "http_200"
    assert event.latency_ms >= 0


def test_approval_success_survives_supplementary_audit_failure_without_resubmit(
    authenticated_client,
):
    client, csrf = authenticated_client
    service = client.trading_service
    proposal = service.propose_order(
        "AAPL",
        "buy",
        "market",
        notional="100",
        actor="operator:test",
        reason="prepare approval audit failure",
        request_id="prepare-approval-audit-failure",
    )

    class FailingBoundaryAudit:
        def record(self, context, action, *args, **kwargs):
            if action == "http.approve":
                raise RuntimeError("supplementary HTTP audit unavailable")

    client.app.state.audit = FailingBoundaryAudit()
    headers = {
        "X-CSRF-Token": csrf,
        "X-Request-ID": "approval-audit-failure",
        "Idempotency-Key": "approval-audit-failure-once",
    }
    first = client.post(
        f"/approve/{proposal['order_id']}",
        headers=headers,
        json={"reason": "human approval remains authoritative"},
    )
    second = client.post(
        f"/approve/{proposal['order_id']}",
        headers=headers,
        json={"reason": "human approval remains authoritative"},
    )

    assert first.status_code == 200
    assert first.headers["X-Request-ID"] == "approval-audit-failure"
    assert second.status_code == 409
    assert service.broker.submit_calls == 1
    with service.session_factory() as session:
        approval_rows = session.query(AuditEvent).filter_by(
            action="order.approve",
            target_id=str(proposal["order_id"]),
        ).all()
    assert len(approval_rows) == 1
    assert approval_rows[0].actor == "operator:local"
    assert approval_rows[0].request_id == "approval-audit-failure"


def test_operational_health_excludes_contact_committed_after_safety_snapshot(
    engine,
    make_service,
):
    service = make_service()
    interleaved = False

    def commit_contact_after_snapshot_starts(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        nonlocal interleaved
        if interleaved or "FROM heartbeats" not in statement:
            return
        interleaved = True
        with service.session_factory() as writer:
            _persist_audit_fixture(
                writer,
                AuditEvent(
                    actor="daemon:test",
                    action="orders.sync",
                    target_type="broker_orders",
                    target_id="all",
                    request_id="interleaved-health-contact",
                    result_code="reconciled",
                ),
            )
            writer.commit()

    event.listen(
        engine,
        "after_cursor_execute",
        commit_contact_after_snapshot_starts,
    )
    try:
        report = build_operational_health(service).as_dict()
    finally:
        event.remove(
            engine,
            "after_cursor_execute",
            commit_contact_after_snapshot_starts,
        )

    assert interleaved is True
    assert report["last_confirmed_broker_contact"] is None
    assert report["reconciliation_age_seconds"] is None


def test_operational_health_never_clamps_future_contact_to_zero(
    make_service,
):
    service = make_service()
    with service.session_factory() as session:
        _persist_audit_fixture(
            session,
            AuditEvent(
                actor="daemon:test",
                action="positions.reconcile",
                target_type="portfolio",
                target_id="alpaca-paper",
                request_id="future-health-contact",
                result_code="in_sync",
                created_at=(
                    service.snapshot_service.now()
                    + timedelta(days=1)
                ),
            ),
        )
        session.commit()

    report = build_operational_health(service).as_dict()

    assert report["last_confirmed_broker_contact"] is not None
    assert report["broker_contact_evidence_valid"] is False
    assert report["reconciliation_age_seconds"] is None
    assert (
        report["broker_contact_observed_at"]
        == report["observed_at"]
    )


@pytest.mark.parametrize(
    ("age_seconds", "expected_valid"),
    ((1, True), (301, False)),
)
def test_operational_health_applies_configured_reconciliation_freshness(
    make_service,
    age_seconds,
    expected_valid,
):
    service = make_service()
    with service.session_factory() as session:
        _persist_audit_fixture(
            session,
            AuditEvent(
                actor="daemon:test",
                action="positions.reconcile",
                target_type="portfolio",
                target_id="alpaca-paper",
                request_id=f"aged-health-contact-{age_seconds}",
                result_code="in_sync",
                created_at=(
                    service.snapshot_service.now()
                    - timedelta(seconds=age_seconds)
                ),
            ),
        )
        session.commit()

    report = build_operational_health(service).as_dict()

    assert report["reconciliation_max_age_seconds"] == 300.0
    assert report["broker_contact_evidence_valid"] is expected_valid
    assert report["reconciliation_age_seconds"] >= age_seconds


@pytest.mark.parametrize("operation", ["panic", "reset"])
def test_operations_domain_success_survives_supplementary_audit_failure(
    make_service,
    caplog,
    operation,
):
    service = make_service()
    marker = "provider-secret-must-not-enter-operations-log"

    class FailingBoundaryAudit:
        def record(self, *args, **kwargs):
            raise RuntimeError(marker)

    operations = OperationsService(
        service,
        FailingBoundaryAudit(),
    )
    context = MutationContext(
        actor="operator:test",
        request_id=f"operations-{operation}-audit-failure",
        reason=f"{operation} after supplementary audit outage",
    )
    if operation == "panic":
        result = operations.panic(context)
        assert result["safe"] is True
    else:
        tripped = service.breakers.trip(
            BreakerScope.loss(AssetClass.EQUITY),
            "prepare operations reset audit failure",
            "operator:test",
            request_id="prepare-operations-reset-audit-failure",
        )
        result = operations.reset_breaker(
            AssetClass.EQUITY,
            expected_generation=tripped.generation,
            context=context,
        )
        assert result["tripped"] is False

    assert (
        f"boundary_audit_unavailable action=operations.{operation}"
        in caplog.text
    )
    assert marker not in caplog.text


@pytest.mark.parametrize(
    "plist_name",
    [
        "com.trading.app.plist",
        "com.trading.daemon.plist",
    ],
)
def test_launchd_discards_unbounded_stream_files(plist_name):
    path = Path("scripts/launchd") / plist_name
    with path.open("rb") as handle:
        config = plistlib.load(handle)

    assert config["StandardOutPath"] == "/dev/null"
    assert config["StandardErrorPath"] == "/dev/null"


@pytest.mark.parametrize(
    ("workflow", "success"),
    [
        ("approve", True),
        ("approve", False),
        ("reject", True),
        ("reject", False),
        ("cancel", True),
        ("cancel", False),
        ("reset", True),
        ("reset", False),
        ("panic", True),
        ("panic", False),
        ("backtest", True),
        ("backtest", False),
    ],
)
def test_http_mutation_provenance_matrix(
    authenticated_client,
    monkeypatch,
    workflow,
    success,
):
    from trading_assistant.backtest import runner as backtest_runner

    client, csrf = authenticated_client
    service = client.trading_service
    request_id = f"provenance-{workflow}-{'ok' if success else 'failure'}"
    idempotency_key = f"{request_id}-once"
    reason = f"{workflow} provenance {'success' if success else 'failure'}"
    body = {"reason": reason}

    if workflow in {"approve", "reject", "cancel"}:
        if success:
            proposal = service.propose_order(
                "AAPL",
                "buy",
                "market",
                notional="100",
                actor="operator:setup",
                reason=f"prepare {workflow} provenance",
                request_id=f"prepare-{request_id}",
            )
            order_id = proposal["order_id"]
            if workflow == "cancel":
                service.approve_order(
                    order_id,
                    actor="operator:setup",
                    reason="prepare submitted cancellation",
                    request_id=f"submit-{request_id}",
                )
        else:
            order_id = 999_999
        path = {
            "approve": f"/approve/{order_id}",
            "reject": f"/reject/{order_id}",
            "cancel": f"/orders/{order_id}/cancel",
        }[workflow]
    elif workflow == "reset":
        tripped = service.breakers.trip(
            BreakerScope.loss(AssetClass.EQUITY),
            "prepare reset provenance",
            "operator:setup",
            request_id=f"prepare-{request_id}",
        )
        body.update(
            {
                "scope": "loss:equity",
                "expected_generation": (
                    tripped.generation
                    if success
                    else tripped.generation + 1
                ),
            }
        )
        path = "/killswitch/reset"
    elif workflow == "panic":
        path = "/panic"
        if not success:
            monkeypatch.setattr(
                client.app.state.operations,
                "panic",
                lambda context: {
                    "safe": False,
                    "unconfirmed_order_ids": [101],
                },
            )
    else:
        path = "/backtests/run"
        body["symbols"] = ["AAPL"]

        class Report:
            def to_dict(self):
                return {"simulated": True}

        if success:
            monkeypatch.setattr(
                backtest_runner,
                "run_synthetic_backtest",
                lambda *args, **kwargs: (7, Report()),
            )
        else:
            def fail_backtest(*args, **kwargs):
                raise ApiError(
                    "backtest_failed",
                    503,
                    "Backtest dependency unavailable",
                )

            monkeypatch.setattr(
                backtest_runner,
                "run_synthetic_backtest",
                fail_backtest,
            )

    response = client.post(
        path,
        headers={
            "X-CSRF-Token": csrf,
            "X-Request-ID": request_id,
            "Idempotency-Key": idempotency_key,
        },
        json=body,
    )

    expected_status = {
        ("approve", False): 404,
        ("reject", False): 404,
        ("cancel", False): 404,
        ("reset", False): 409,
        ("panic", False): 503,
        ("backtest", False): 503,
    }.get((workflow, success), 200)
    assert response.status_code == expected_status
    assert response.headers["X-Request-ID"] == request_id
    action = {
        "approve": "http.approve",
        "reject": "http.reject",
        "cancel": "http.cancel",
        "reset": "http.breaker_reset",
        "panic": "http.panic",
        "backtest": "http.backtest_run",
    }[workflow]
    with service.session_factory() as session:
        receipt = session.query(AuditEvent).filter_by(
            action=action,
            request_id=request_id,
        ).one()
        receipt_reason = sensitive_store(session).read(
            receipt,
            "reason",
        )
    assert receipt.actor == "operator:local"
    assert receipt.idempotency_key == idempotency_key
    assert receipt_reason == reason
    assert receipt.result_code == f"http_{expected_status}"
    assert receipt.latency_ms >= 0


# ── B3 preflight helpers (keyless and read-only) ────────────────
def test_preflight_reports_paper_only_and_dangerous_switches_separately(
    app_config,
):
    from trading_assistant import preflight
    from trading_assistant.config import BrokerKind

    assert preflight._config_parses().status == "PASS"
    config = app_config.model_copy(
        update={
            "trading": app_config.trading.model_copy(
                update={"broker": BrokerKind.ALPACA}
            )
        }
    )
    assert preflight._paper_only(config).status == "PASS"
    assert preflight._dangerous_switches_off(config, Secrets()).status == "PASS"

    unsafe = config.model_copy(
        update={
            "features": config.features.model_copy(
                update={"auto_execute_preapproved_rules": True}
            )
        }
    )
    assert preflight._dangerous_switches_off(unsafe, Secrets()).status == "FAIL"


def test_preflight_app_secret_quality_is_independent_of_provider_credentials():
    from trading_assistant import preflight

    assert preflight._app_secret_quality(Secrets(app_api_token="short")).status == "FAIL"
    assert (
        preflight._app_secret_quality(
            Secrets(app_api_token="x" * 32)
        ).status
        == "FAIL"
    )
    assert (
        preflight._app_secret_quality(
            Secrets(app_api_token="placeholder-token-" * 2)
        ).status
        == "FAIL"
    )
    assert (
        preflight._app_secret_quality(
            Secrets(app_api_token="01234567" * 4)
        ).status
        == "FAIL"
    )
    result = preflight._app_secret_quality(
        Secrets(app_api_token="A7v!9qL2#mN4$pR6&tU8*wX0-zB3_cD5")
    )
    assert result.status == "PASS"
    assert "basic format/placeholder checks" in result.detail


def _structural_preflight_secrets(app_config, *, include_llm: bool = True):
    selected_llm = {
        "anthropic": {"anthropic_api_key": "configured-anthropic"},
        "gemini": {"gemini_api_key": "configured-gemini"},
        "groq": {"groq_api_key": "configured-groq"},
    }[app_config.llm.provider]
    return Secrets(
        app_api_token="A7v!9qL2#mN4$pR6&tU8*wX0-zB3_cD5",
        alpaca_api_key="configured-alpaca",
        alpaca_secret_key="configured-alpaca-secret",
        database_url="sqlite:///fixture-preflight.db",
        candidate_signing_key="configured-candidate",
        backup_encryption_key="configured-backup",
        field_encryption_keys={
            app_config.encryption.active_key_id: "configured-field-key",
        },
        **(selected_llm if include_llm else {}),
    )


def test_structural_preflight_has_exact_checks_and_uses_injected_keychain(
    app_config,
):
    from trading_assistant import preflight
    from trading_assistant.security.secrets import (
        MacOSKeychainSecretProvider,
    )

    calls = []

    class ExplodingKeyring:
        def get_password(self, *_args, **_kwargs):
            raise AssertionError("structural preflight reread Keychain")

    provider = MacOSKeychainSecretProvider(backend=ExplodingKeyring())
    checks = preflight._structural_preflight_checks(
        app_config,
        _structural_preflight_secrets(app_config),
        provider=provider,
        tls_validator=lambda _server: calls.append("tls") or object(),
        encryption_checker=lambda _config, _secrets: (
            calls.append("encryption")
            or preflight.StructuralCheck(
                "encryption",
                "passed",
                "ok",
            )
        ),
    )

    assert [check.name for check in checks] == [
        "KEYCHAIN",
        "LOCAL_TLS",
        "FIELD_ENCRYPTION",
        "OUTBOUND_ORIGINS",
        "INTEGRATIONS_DISABLED",
    ]
    assert [check.status for check in checks] == [preflight.PASS] * 5
    assert [check.detail for check in checks] == ["ok"] * 5
    assert calls == ["tls", "encryption"]


def test_structural_preflight_keychain_failure_is_stable_and_value_free(
    app_config,
):
    from trading_assistant import preflight
    from trading_assistant.security.secrets import (
        MacOSKeychainSecretProvider,
    )

    provider = MacOSKeychainSecretProvider(backend=object())
    checks = preflight._structural_preflight_checks(
        app_config,
        _structural_preflight_secrets(
            app_config,
            include_llm=False,
        ),
        provider=provider,
        tls_validator=lambda _server: object(),
        encryption_checker=lambda _config, _secrets: (
            preflight.StructuralCheck("encryption", "passed", "ok")
        ),
    )

    keychain = checks[0]
    assert keychain.name == "KEYCHAIN"
    assert keychain.status == preflight.FAIL
    assert keychain.detail == "required_fields_missing"
    assert "gemini_api_key" not in keychain.detail
    assert "anthropic_api_key" not in keychain.detail
    assert "groq_api_key" not in keychain.detail


def test_structural_preflight_rejects_origin_drift_without_exposing_url(
    app_config,
):
    from trading_assistant import preflight
    from trading_assistant.security.secrets import (
        MacOSKeychainSecretProvider,
    )

    marker = "https://fixture-origin.invalid"
    unsafe = app_config.model_copy(
        update={
            "provider_origins": app_config.provider_origins.model_copy(
                update={"alpaca_data": marker}
            )
        }
    )
    checks = preflight._structural_preflight_checks(
        unsafe,
        _structural_preflight_secrets(app_config),
        provider=MacOSKeychainSecretProvider(backend=object()),
        tls_validator=lambda _server: object(),
        encryption_checker=lambda _config, _secrets: (
            preflight.StructuralCheck("encryption", "passed", "ok")
        ),
    )

    outbound = checks[3]
    assert outbound.name == "OUTBOUND_ORIGINS"
    assert outbound.status == preflight.FAIL
    assert outbound.detail == "origin_manifest_mismatch"
    assert marker not in outbound.detail


def test_structural_preflight_rejects_noncanonical_loopback_bind(
    app_config,
):
    from trading_assistant import preflight
    from trading_assistant.security.secrets import (
        MacOSKeychainSecretProvider,
    )

    unsafe = app_config.model_copy(
        update={
            "server": app_config.server.model_copy(
                update={"bind_host": "0.0.0.0"}
            )
        }
    )
    checks = preflight._structural_preflight_checks(
        unsafe,
        _structural_preflight_secrets(app_config),
        provider=MacOSKeychainSecretProvider(backend=object()),
        tls_validator=lambda _server: object(),
        encryption_checker=lambda _config, _secrets: (
            preflight.StructuralCheck("encryption", "passed", "ok")
        ),
    )

    local_tls = checks[1]
    assert local_tls.name == "LOCAL_TLS"
    assert local_tls.status == preflight.FAIL
    assert local_tls.detail == "local_tls_invalid"


def test_structural_failure_runs_every_local_check_then_stops_before_outbound(
    app_config,
    monkeypatch,
    capsys,
):
    from trading_assistant import preflight
    from trading_assistant.security.secrets import (
        MacOSKeychainSecretProvider,
    )

    seen = []

    def structural(*_args, **_kwargs):
        checks = []
        for name in (
            "KEYCHAIN",
            "LOCAL_TLS",
            "FIELD_ENCRYPTION",
            "OUTBOUND_ORIGINS",
            "INTEGRATIONS_DISABLED",
        ):
            seen.append(name)
            checks.append(
                preflight.Result(
                    name,
                    preflight.FAIL
                    if name == "FIELD_ENCRYPTION"
                    else preflight.PASS,
                    "migration_incomplete"
                    if name == "FIELD_ENCRYPTION"
                    else "ok",
                )
            )
        return checks

    def outbound_explosion(*_args, **_kwargs):
        raise AssertionError(
            "outbound/dependent construction occurred before structural pass"
        )

    monkeypatch.setattr(
        preflight,
        "_structural_preflight_checks",
        structural,
    )
    monkeypatch.setattr(preflight, "_alpaca", outbound_explosion)
    monkeypatch.setattr(preflight, "_db", outbound_explosion)
    monkeypatch.setattr(preflight, "_build_service", outbound_explosion)
    monkeypatch.setattr(
        preflight,
        "_llm_provider_configured",
        outbound_explosion,
    )
    monkeypatch.setattr(
        preflight,
        "_notification_configuration",
        outbound_explosion,
    )

    result = preflight._run(
        app_config,
        _structural_preflight_secrets(app_config),
        provider=MacOSKeychainSecretProvider(backend=object()),
    )

    output = capsys.readouterr().out
    assert result == 1
    assert seen == [
        "KEYCHAIN",
        "LOCAL_TLS",
        "FIELD_ENCRYPTION",
        "OUTBOUND_ORIGINS",
        "INTEGRATIONS_DISABLED",
    ]
    assert "=> NOT READY" in output
    assert "Alpaca paper auth" not in output


def test_structural_preflight_never_accepts_missing_keychain_provenance(
    app_config,
):
    from trading_assistant import preflight

    checks = preflight._structural_preflight_checks(
        app_config,
        _structural_preflight_secrets(app_config),
        provider=None,
        tls_validator=lambda _server: object(),
        encryption_checker=lambda _config, _secrets: (
            preflight.StructuralCheck("encryption", "passed", "ok")
        ),
    )

    assert checks[0] == preflight.Result(
        "KEYCHAIN",
        preflight.FAIL,
        "provider_unproven",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tls_cert_path", ".local/tls/renamed.pem"),
        ("tls_key_path", ".local/tls/renamed-key.pem"),
        ("tls_ca_path", ".local/tls/renamed-root.pem"),
        ("tls_cert_path", "elsewhere/localhost.pem"),
        ("tls_key_path", "elsewhere/localhost-key.pem"),
        ("tls_ca_path", "elsewhere/rootCA.pem"),
    ],
)
def test_structural_preflight_requires_exact_canonical_tls_paths(
    app_config,
    field,
    value,
):
    from trading_assistant import preflight
    from trading_assistant.security.secrets import (
        MacOSKeychainSecretProvider,
    )

    unsafe = app_config.model_copy(
        update={
            "server": app_config.server.model_copy(
                update={field: value}
            )
        }
    )
    tls_calls: list[object] = []
    checks = preflight._structural_preflight_checks(
        unsafe,
        _structural_preflight_secrets(app_config),
        provider=MacOSKeychainSecretProvider(backend=object()),
        tls_validator=lambda server: tls_calls.append(server),
        encryption_checker=lambda _config, _secrets: (
            preflight.StructuralCheck("encryption", "passed", "ok")
        ),
    )

    assert checks[1] == preflight.Result(
        "LOCAL_TLS",
        preflight.FAIL,
        "local_tls_invalid",
    )
    assert tls_calls == []


def test_preflight_provider_constructor_failure_still_runs_all_five_checks(
    app_config,
    monkeypatch,
    capsys,
):
    from trading_assistant import preflight

    calls: list[str] = []

    def fail_provider():
        calls.append("provider")
        raise RuntimeError("fixture-provider-marker")

    def tls_validator(_server):
        calls.append("tls")

    def encryption_checker(_config, secrets):
        calls.append("encryption")
        assert secrets is None
        return preflight.StructuralCheck(
            "encryption",
            "blocked",
            "sensitive_key_unavailable",
        )

    def explode(*_args, **_kwargs):
        raise AssertionError("non-structural dependency was constructed")

    monkeypatch.setattr(preflight, "load_config", lambda *_args: app_config)
    monkeypatch.setattr(preflight, "_alpaca", explode)
    monkeypatch.setattr(preflight, "_db", explode)
    monkeypatch.setattr(preflight, "_build_service", explode)

    result = preflight.run(
        provider_factory=fail_provider,
        tls_validator=tls_validator,
        encryption_checker=encryption_checker,
    )

    output = capsys.readouterr().out
    assert result == 1
    assert calls == ["provider", "tls", "encryption"]
    for name in (
        "KEYCHAIN",
        "LOCAL_TLS",
        "FIELD_ENCRYPTION",
        "OUTBOUND_ORIGINS",
        "INTEGRATIONS_DISABLED",
    ):
        assert output.count(name) == 1
    assert "sensitive_key_unavailable" in output
    assert "fixture-provider-marker" not in output
    assert "Alpaca paper auth" not in output


def test_preflight_keychain_load_failure_still_runs_all_five_checks(
    app_config,
    monkeypatch,
    capsys,
):
    from trading_assistant import preflight
    from trading_assistant.security.secrets import (
        MacOSKeychainSecretProvider,
    )

    calls: list[str] = []

    class FailingBackend:
        def get_password(self, *_args):
            raise RuntimeError("fixture-keychain-marker")

    provider = MacOSKeychainSecretProvider(backend=FailingBackend())
    monkeypatch.setattr(preflight, "load_config", lambda *_args: app_config)

    result = preflight.run(
        provider=provider,
        tls_validator=lambda _server: calls.append("tls"),
        encryption_checker=lambda _config, secrets: (
            calls.append("encryption")
            or (
                preflight.StructuralCheck(
                    "encryption",
                    "blocked",
                    "sensitive_key_unavailable",
                )
                if secrets is None
                else None
            )
        ),
    )

    output = capsys.readouterr().out
    assert result == 1
    assert calls == ["tls", "encryption"]
    for name in (
        "KEYCHAIN",
        "LOCAL_TLS",
        "FIELD_ENCRYPTION",
        "OUTBOUND_ORIGINS",
        "INTEGRATIONS_DISABLED",
    ):
        assert output.count(name) == 1
    assert "sensitive_key_unavailable" in output
    assert "fixture-keychain-marker" not in output


def test_preflight_constructs_loads_and_passes_one_keychain_provider(
    app_config,
    monkeypatch,
):
    from trading_assistant import logging as runtime_logging
    from trading_assistant import preflight
    from trading_assistant.security.secrets import (
        MacOSKeychainSecretProvider,
    )

    provider = MacOSKeychainSecretProvider(backend=object())
    secrets = _structural_preflight_secrets(app_config)
    observed: list[tuple[str, object]] = []

    def provider_factory():
        observed.append(("construct", provider))
        return provider

    def load(role, *, config, provider):
        assert role == "preflight"
        assert config is app_config
        observed.append(("load", provider))
        return secrets

    def run_loaded(config, loaded, *, provider):
        assert config is app_config
        assert loaded is secrets
        observed.append(("run", provider))
        return 0

    monkeypatch.setattr(preflight, "load_config", lambda *_args: app_config)
    monkeypatch.setattr(preflight, "load_role_secrets", load)
    monkeypatch.setattr(preflight, "_run", run_loaded)
    monkeypatch.setattr(
        runtime_logging,
        "runtime_startup",
        lambda *_args, **_kwargs: nullcontext(),
    )

    assert preflight.run(provider_factory=provider_factory) == 0
    assert observed == [
        ("construct", provider),
        ("load", provider),
        ("run", provider),
    ]


def test_preflight_encryption_check_is_metadata_only_and_never_builds_cipher(
    app_config,
    monkeypatch,
):
    from trading_assistant import preflight
    from trading_assistant.db import session as db_session
    from trading_assistant.security import crypto

    engine = object()
    observed: dict[str, object] = {}

    class MetadataOnlyInspector:
        def __init__(
            self,
            supplied_engine,
            *,
            schema_version,
            active_key_id,
            cipher=None,
        ):
            observed.update(
                engine=supplied_engine,
                schema_version=schema_version,
                active_key_id=active_key_id,
                cipher=cipher,
            )

        def inspect(self, *, metadata_only=False):
            observed["metadata_only"] = metadata_only
            return preflight.StructuralCheck("encryption", "passed", "ok")

    monkeypatch.setattr(db_session, "create_db_engine", lambda _url: engine)
    monkeypatch.setattr(
        crypto,
        "build_sensitive_data_cipher",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("preflight attempted decryption setup")
        ),
    )
    monkeypatch.setattr(
        preflight,
        "SensitiveEncryptionStateInspector",
        MetadataOnlyInspector,
    )

    result = preflight._default_encryption_check(
        app_config,
        _structural_preflight_secrets(app_config),
    )

    assert result.passed
    assert observed == {
        "engine": engine,
        "schema_version": app_config.encryption.schema_version,
        "active_key_id": app_config.encryption.active_key_id,
        "cipher": None,
        "metadata_only": True,
    }


def _install_preflight_alpaca_stubs(
    monkeypatch,
    *,
    account_error: Exception | None = None,
    clock_error: Exception | None = None,
    quote_error: Exception | None = None,
    account: Account | None = None,
    positions: list[Position] | None = None,
):
    from trading_assistant.broker import alpaca

    class BrokerStub:
        def get_account(self):
            if account_error is not None:
                raise account_error
            return account or Account(
                buying_power=Decimal("100000"),
                equity=Decimal("100000"),
                cash=Decimal("100000"),
            )

        def get_positions(self):
            return list(positions or [])

        def get_quote(self, _ticker):
            if quote_error is not None:
                raise quote_error
            return SimpleNamespace(last=Decimal("100"))

    class ClockStub:
        def is_open(self):
            if clock_error is not None:
                raise clock_error
            return False

    monkeypatch.setattr(
        alpaca.AlpacaBroker,
        "from_credentials",
        lambda *_args, **_kwargs: BrokerStub(),
    )
    monkeypatch.setattr(
        alpaca.AlpacaClock,
        "from_credentials",
        lambda *_args, **_kwargs: ClockStub(),
    )


def test_preflight_quote_failure_does_not_fail_auth_or_clock(monkeypatch):
    from trading_assistant import preflight

    _install_preflight_alpaca_stubs(
        monkeypatch,
        quote_error=ValueError("provider-secret-invalid-quote"),
    )

    auth, clock, data = preflight._alpaca(
        Secrets(alpaca_api_key="key", alpaca_secret_key="secret")
    )

    assert (auth.status, clock.status, data.status) == (
        preflight.PASS,
        preflight.PASS,
        preflight.FAIL,
    )
    assert "provider-secret-invalid-quote" not in data.detail


def test_preflight_account_failure_does_not_fail_clock_or_data(monkeypatch):
    from trading_assistant import preflight

    _install_preflight_alpaca_stubs(
        monkeypatch,
        account_error=ConnectionError("provider-secret-account"),
    )

    auth, clock, data = preflight._alpaca(
        Secrets(alpaca_api_key="key", alpaca_secret_key="secret")
    )

    assert (auth.status, clock.status, data.status) == (
        preflight.FAIL,
        preflight.PASS,
        preflight.PASS,
    )


def test_preflight_clock_failure_does_not_fail_auth_or_data(monkeypatch):
    from trading_assistant import preflight

    _install_preflight_alpaca_stubs(
        monkeypatch,
        clock_error=RuntimeError("provider-secret-clock"),
    )

    auth, clock, data = preflight._alpaca(
        Secrets(alpaca_api_key="key", alpaca_secret_key="secret")
    )

    assert (auth.status, clock.status, data.status) == (
        preflight.PASS,
        preflight.FAIL,
        preflight.PASS,
    )


def test_preflight_rejects_invalid_account_and_position_truth(monkeypatch):
    from trading_assistant import preflight

    invalid_position = Position(
        ticker="AAPL",
        qty=Decimal("1"),
        avg_entry_price=Decimal("NaN"),
        current_price=Decimal("100"),
    )
    _install_preflight_alpaca_stubs(
        monkeypatch,
        account=Account(
            buying_power=Decimal("NaN"),
            equity=Decimal("100000"),
            cash=Decimal("100000"),
        ),
        positions=[invalid_position],
    )

    auth, clock, data = preflight._alpaca(
        Secrets(alpaca_api_key="key", alpaca_secret_key="secret")
    )

    assert (auth.status, clock.status, data.status) == (
        preflight.FAIL,
        preflight.PASS,
        preflight.PASS,
    )


def test_preflight_needs_me_is_not_ready_and_nonzero(
    app_config,
    monkeypatch,
    capsys,
):
    from trading_assistant import preflight

    monkeypatch.setattr(preflight, "load_config", lambda *_args: app_config)
    monkeypatch.setattr(
        preflight,
        "_config_parses",
        lambda: preflight.Result("config", preflight.PASS),
    )
    monkeypatch.setattr(
        preflight,
        "_paper_only",
        lambda _config: preflight.Result("paper", preflight.PASS),
    )
    monkeypatch.setattr(
        preflight,
        "_dangerous_switches_off",
        lambda _config, _secrets: preflight.Result(
            "switches",
            preflight.PASS,
        ),
    )
    monkeypatch.setattr(
        preflight,
        "_app_secret_quality",
        lambda _secrets: preflight.Result("secret", preflight.PASS),
    )
    monkeypatch.setattr(
        preflight,
        "_alpaca",
        lambda _secrets: (
            preflight.Result("alpaca", preflight.NEEDS),
            preflight.Result("clock", preflight.NEEDS),
            preflight.Result("data", preflight.NEEDS),
        ),
    )
    monkeypatch.setattr(
        preflight,
        "_db",
        lambda _secrets: (
            preflight.Result("schema", preflight.PASS),
            preflight.Result("wal", preflight.PASS),
            preflight.Result("breakers", preflight.PASS),
        ),
    )
    monkeypatch.setattr(
        preflight,
        "_llm_provider_configured",
        lambda _config, _secrets: preflight.Result(
            "llm",
            preflight.NEEDS,
        ),
    )
    monkeypatch.setattr(
        preflight,
        "_notification_configuration",
        lambda _config, _secrets: preflight.Result(
            "notifications",
            preflight.SKIP,
        ),
    )
    monkeypatch.setattr(
        preflight,
        "_structural_preflight_checks",
        lambda *_args, **_kwargs: [
            preflight.Result(name, preflight.PASS, "ok")
            for name in (
                "KEYCHAIN",
                "LOCAL_TLS",
                "FIELD_ENCRYPTION",
                "OUTBOUND_ORIGINS",
                "INTEGRATIONS_DISABLED",
            )
        ],
    )

    result = preflight._run(
        app_config,
        Secrets(app_api_token="A7v!9qL2#mN4$pR6&tU8*wX0-zB3_cD5"),
        provider=SimpleNamespace(provider_name="macos-keychain"),
    )

    output = capsys.readouterr().out
    assert result == 1
    assert "=> NOT READY" in output
    assert "=> READY\n" not in output


def test_preflight_llm_check_is_configuration_only_and_never_calls_provider(
    app_config,
    monkeypatch,
):
    from trading_assistant import preflight
    from trading_assistant.llm import factory

    monkeypatch.setattr(
        factory,
        "build_llm_backend",
        lambda *_args, **_kwargs: pytest.fail("preflight made an LLM call"),
    )

    configured = preflight._llm_provider_configured(
        app_config,
        Secrets(gemini_api_key="configured"),
    )
    missing = preflight._llm_provider_configured(app_config, Secrets())

    assert configured.status == "PASS"
    assert configured.detail == "provider=gemini"
    assert missing.status == "NEEDS-ME"


def test_preflight_notification_check_never_sends_message(app_config, monkeypatch):
    from trading_assistant import preflight
    from trading_assistant.notifications.telegram import TelegramNotifier

    monkeypatch.setattr(
        TelegramNotifier,
        "send",
        lambda *_args, **_kwargs: pytest.fail("preflight sent a notification"),
    )
    enabled = app_config.model_copy(
        update={
            "features": app_config.features.model_copy(
                update={"telegram_notifications": True}
            )
        }
    )

    result = preflight._notification_configuration(
        enabled,
        Secrets(telegram_bot_token="configured", telegram_chat_id="configured"),
    )

    assert result.status == "PASS"
    assert result.detail == "enabled; no message sent"


def test_preflight_reconciliation_reports_position_drift(make_service):
    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.broker.models import Position
    from trading_assistant import preflight
    from trading_assistant.ops.preflight_probe import (
        ReadOnlyPreflightService,
    )

    broker = MockBroker(
        positions=[
            Position("AAPL", Decimal("2"), Decimal("100"), Decimal("100"))
        ]
    )
    mutable_service = make_service(broker=broker)
    result = preflight._reconciliation(
        ReadOnlyPreflightService(
            broker,
            mutable_service.session_factory,
        )
    )

    assert result.status == "FAIL"
    assert "AAPL" in result.detail


def test_preflight_reconciliation_sanitizes_provider_exception_text():
    from trading_assistant import preflight

    class ExplodingService:
        def inspect_reconciliation(self):
            raise RuntimeError("provider-secret-preflight-detail")

    result = preflight._reconciliation(ExplodingService())

    assert result.status == "FAIL"
    assert result.detail == "dependency_failed"
    assert "provider-secret-preflight-detail" not in result.detail


# ── B2 full order lifecycle ─────────────────────────────────────
def test_order_lifecycle_propose_approve_fill(make_service):
    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.broker.models import (
        BrokerFill,
        OrderResult,
        OrderStatus,
    )
    from trading_assistant.db.models import utcnow

    class LifecycleBroker(MockBroker):
        activities = []

        def get_fill_activities(self, after=None):
            return list(self.activities)

    broker = LifecycleBroker()
    svc = make_service(broker=broker)  # AAPL @ 100
    oid = _propose(
        svc,
        ticker="AAPL",
        side="buy",
        order_type="market",
        notional="400",
    )["order_id"]
    assert svc.get_order_status(oid)["status"] == "proposed"

    approve = _approve(svc, oid)
    assert approve["executed"] is True and approve["status"] == "submitted"

    with svc.session_factory() as session:
        order = session.get(Order, oid)
        broker_order_id = order.broker_order_id
        client_order_id = order.idempotency_key
    remote = OrderResult(
        client_order_id,
        broker_order_id,
        OrderStatus.FILLED,
        filled_qty=Decimal("4"),
        avg_fill_price=Decimal("100"),
    )
    broker._orders_by_id[broker_order_id] = remote
    broker._orders_by_key[client_order_id] = remote
    broker.activities = [
        BrokerFill(
            broker_fill_id="lifecycle-fill-1",
            broker_order_id=broker_order_id,
            ticker="AAPL",
            side="buy",
            qty=Decimal("4"),
            price=Decimal("100"),
            filled_at=utcnow(),
        )
    ]

    sync = _sync(svc)
    assert sync["newly_filled"] == 1

    with svc.session_factory() as s:
        assert s.execute(select(func.count()).select_from(Fill)).scalar_one() == 1
        assert s.get(Order, oid).status == "filled"
    # The execution shows up in the log feed the UI reads.
    assert "risk_events" in svc.get_log()


# ── fill/status sync from broker (Alpaca reconciliation) ────────
def test_sync_ingests_fills_and_advances_status(make_service):
    from decimal import Decimal

    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.broker.models import (
        BrokerFill,
        OrderResult,
        OrderStatus,
    )
    from trading_assistant.db.models import Fill, Order, utcnow

    class FillableBroker(MockBroker):
        fill = None
        activities = []

        def get_order_status(self, oid):
            r = super().get_order_status(oid)
            if self.fill:
                return OrderResult(r.idempotency_key, oid, OrderStatus.FILLED,
                                   filled_qty=self.fill[0], avg_fill_price=self.fill[1])
            return r

        def get_fill_activities(self, after=None):
            return list(self.activities)

    broker = FillableBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    oid = _propose(
        svc,
        ticker="AAPL",
        side="buy",
        order_type="market",
        notional="400",
    )["order_id"]
    _approve(svc, oid)                          # -> SUBMITTED with broker_order_id

    broker.fill = (Decimal("4"), Decimal("100"))
    with svc.session_factory() as session:
        broker_order_id = session.get(Order, oid).broker_order_id
    broker.activities = [
        BrokerFill(
            broker_fill_id="launch-fill-1",
            broker_order_id=broker_order_id,
            ticker="AAPL",
            side="buy",
            qty=Decimal("4"),
            price=Decimal("100"),
            filled_at=utcnow(),
        )
    ]
    r = _sync(svc)
    assert r["newly_filled"] == 1
    with svc.session_factory() as s:
        assert s.get(Order, oid).status == "filled"
        assert s.execute(select(func.count()).select_from(Fill)).scalar_one() == 1
    # Idempotent — nothing left open to sync, no duplicate fill.
    assert _sync(svc)["synced"] == 0


def test_sync_surfaces_broker_status_outage_with_exact_failure_audit(
    make_service,
):
    from trading_assistant.broker.mock import MockBroker

    class StatusFailureBroker(MockBroker):
        fail_status = False

        def get_order_status(self, order_id):
            if self.fail_status:
                raise ConnectionError("broker unavailable")
            return super().get_order_status(order_id)

    broker = StatusFailureBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    oid = _propose(
        svc,
        ticker="AAPL",
        side="buy",
        order_type="market",
        qty="1",
    )["order_id"]
    _approve(svc, oid)
    broker.fail_status = True

    with pytest.raises(RequiredDependencyUnavailable):
        _sync(svc)

    with svc.session_factory() as session:
        audit = session.query(AuditEvent).filter_by(
            action="orders.sync",
            request_id="launch-test-sync",
            result_code="dependency_unavailable",
        ).one()
        audit_reason = sensitive_store(session).read(audit, "reason")
    assert audit.actor == "operator:test"
    assert audit_reason == "launch test broker reconciliation"


def test_sync_reports_submitted_outbox_without_broker_id(make_service):
    from trading_assistant.db.models import Order

    svc = make_service()
    with svc.session_factory() as session:
        _persist_order_fixture(
            session,
            Order(
                idempotency_key="unknown-acceptance",
                ticker="AAPL",
                side="buy",
                order_type="limit",
                qty=Decimal("1"),
                limit_price=Decimal("95"),
                status="submitted",
                broker_order_id=None,
            ),
        )
        session.commit()

    result = _sync(svc)

    assert result["failed"] == 1


def test_sync_replaces_synthetic_fill_with_exact_broker_activity(make_service):
    from datetime import timedelta

    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.broker.models import BrokerFill, OrderResult, OrderStatus
    from trading_assistant.db.models import Fill, Order

    class ActivityBroker(MockBroker):
        def get_fill_activities(self, after=None):
            return [
                BrokerFill(
                    broker_fill_id="activity-1",
                    broker_order_id=self.order_id,
                    ticker="AAPL",
                    side="buy",
                    qty=Decimal("2"),
                    price=Decimal("332.03"),
                    filled_at=self.exact_time,
                )
            ]

    broker = ActivityBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    oid = _propose(
        svc,
        ticker="AAPL",
        side="buy",
        order_type="market",
        qty="2",
    )["order_id"]
    _approve(svc, oid)
    with svc.session_factory() as session:
        order = session.get(Order, oid)
        broker.order_id = order.broker_order_id
        broker.exact_time = (
            order.submission_started_at + timedelta(seconds=1)
        )
        synthetic = Fill(
            order_id=oid,
            ticker="AAPL",
            side="buy",
            qty=Decimal("2"),
            price=Decimal("333"),
            broker_fill_id=f"{order.broker_order_id}:2",
        )
        session.add(synthetic)
        session.commit()
        client_id = order.idempotency_key
    filled = OrderResult(
        client_id,
        broker.order_id,
        OrderStatus.FILLED,
        filled_qty=Decimal("2"),
        avg_fill_price=Decimal("332.03"),
    )
    broker._orders_by_id[broker.order_id] = filled
    broker._orders_by_key[client_id] = filled

    result = _sync(svc)

    assert result["newly_filled"] == 1
    with svc.session_factory() as session:
        fills = session.execute(select(Fill).where(Fill.order_id == oid)).scalars().all()
        assert len(fills) == 1
        assert fills[0].broker_fill_id == "activity-1"
        assert fills[0].price == Decimal("332.030000")
        assert fills[0].filled_at == broker.exact_time


def test_sync_preserves_exact_incremental_activity_prices(make_service):
    """Exact broker activities, not cumulative averages, are the P&L authority."""
    from datetime import timedelta

    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.broker.models import (
        BrokerFill,
        OrderResult,
        OrderStatus,
    )
    from trading_assistant.db.models import Fill, utcnow

    class ExactActivityBroker(MockBroker):
        cumulative = (OrderStatus.SUBMITTED, Decimal("0"), None)
        activities = []

        def get_order_status(self, oid):
            original = super().get_order_status(oid)
            status, qty, avg = self.cumulative
            return OrderResult(
                original.idempotency_key,
                oid,
                status,
                filled_qty=qty,
                avg_fill_price=avg,
            )

        def get_fill_activities(self, after=None):
            return list(self.activities)

    broker = ExactActivityBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    oid = _propose(
        svc,
        ticker="AAPL",
        side="buy",
        order_type="market",
        qty="3",
    )["order_id"]
    _approve(svc, oid)
    with svc.session_factory() as session:
        broker_order_id = session.get(Order, oid).broker_order_id

    first_at = utcnow()
    broker.cumulative = (
        OrderStatus.PARTIALLY_FILLED,
        Decimal("1"),
        Decimal("100"),
    )
    first = BrokerFill(
        broker_fill_id="incremental-fill-1",
        broker_order_id=broker_order_id,
        ticker="AAPL",
        side="buy",
        qty=Decimal("1"),
        price=Decimal("100"),
        filled_at=first_at,
    )
    broker.activities = [first]
    _sync(svc)
    broker.cumulative = (OrderStatus.FILLED, Decimal("3"), Decimal("110"))
    broker.activities = [
        first,
        BrokerFill(
            broker_fill_id="incremental-fill-2",
            broker_order_id=broker_order_id,
            ticker="AAPL",
            side="buy",
            qty=Decimal("2"),
            price=Decimal("115"),
            filled_at=first_at + timedelta(seconds=1),
        ),
    ]
    _sync(svc)

    with svc.session_factory() as s:
        buys = (
            s.execute(select(Fill).where(Fill.order_id == oid).order_by(Fill.id))
            .scalars()
            .all()
        )
        assert [(row.qty, row.price) for row in buys] == [
            (Decimal("1.000000"), Decimal("100.000000")),
            (Decimal("2.000000"), Decimal("115.000000")),
        ]
        # Selling all three at 120 realizes 20 + 10 = 30 using the exact FIFO lots.
        s.add(
                Fill(
                    ticker="AAPL",
                    side="sell",
                    qty=Decimal("3"),
                    price=Decimal("120"),
                    broker_fill_id="incremental-fill-closing-sale",
                    filled_at=utcnow(),
                )
        )
        s.commit()
        assert svc._realized_pnl_today(s) == Decimal("30.000000")


def test_telegram_uses_fixed_origin_and_redacts_token_bearing_failures(caplog):
    """Changing the notifier base or logging transport errors would expose the bot token."""
    from types import SimpleNamespace

    from trading_assistant.notifications.telegram import TelegramNotifier

    token = "test-only-token"

    class HTTP:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return SimpleNamespace(
                status_code=200,
                request=SimpleNamespace(url=url),
            )

    http = HTTP()
    notifier = TelegramNotifier(
        enabled=True,
        bot_token=token,
        chat_id="test-chat",
        http=http,
    )

    assert notifier.send("notification") is True
    assert http.calls[0][0] == f"https://api.telegram.org/bot{token}/sendMessage"

    class FailingHTTP:
        def post(self, url, **_kwargs):
            raise RuntimeError(f"failed for {url}")

    with caplog.at_level("WARNING"):
        assert TelegramNotifier(
            enabled=True,
            bot_token=token,
            chat_id="test-chat",
            http=FailingHTTP(),
        ).send("notification") is False

    assert token not in caplog.text
