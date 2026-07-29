from __future__ import annotations

import ast
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from scripts import verify_loopback_release as verifier_module
from scripts.verify_loopback_release import (
    Command,
    ReleaseVerifier,
    SubprocessRunner,
)


class ScriptedRunner:
    def __init__(
        self,
        responses: list[subprocess.CompletedProcess[str] | BaseException],
        *,
        junit_xml: str | None = (
            "<testsuite tests=\"1\" failures=\"0\" errors=\"0\" skipped=\"0\">"
            "<testcase classname=\"tests.test_example\" "
            "name=\"test_example\" file=\"tests/test_example.py\"/>"
            "</testsuite>"
        ),
    ) -> None:
        self._responses = iter(responses)
        self.calls: list[dict[str, object]] = []
        self.junit_xml = junit_xml

    def run(
        self,
        *,
        argv: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(
            {
                "argv": argv,
                "cwd": cwd,
                "env": env,
                "timeout_seconds": timeout_seconds,
            }
        )
        junit_argument = next(
            (
                part
                for part in argv
                if part.startswith("--junitxml=")
            ),
            None,
        )
        if junit_argument is not None and self.junit_xml is not None:
            junit_path = Path(junit_argument.split("=", 1)[1])
            junit_path.write_text(self.junit_xml, encoding="utf-8")
            junit_path.chmod(0o600)
        response = next(self._responses)
        if isinstance(response, BaseException):
            raise response
        return response


class MutatingRunner(ScriptedRunner):
    def run(
        self,
        *,
        argv: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        completed = super().run(
            argv=argv,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )
        (cwd / "unexpected-release-output.txt").write_text(
            "unexpected\n",
            encoding="utf-8",
        )
        return completed


class EvidenceObservingRunner(ScriptedRunner):
    def __init__(
        self,
        responses: list[subprocess.CompletedProcess[str] | BaseException],
        evidence_path: Path,
    ) -> None:
        super().__init__(responses)
        self.evidence_path = evidence_path
        self.observed_payload: dict[str, object] | None = None
        self.observed_mode: int | None = None

    def run(
        self,
        *,
        argv: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        if self.evidence_path.exists():
            self.observed_payload = json.loads(
                self.evidence_path.read_text(encoding="utf-8")
            )
            self.observed_mode = os.stat(self.evidence_path).st_mode & 0o777
        return super().run(
            argv=argv,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )


class MutatingToolRunner(ScriptedRunner):
    def __init__(
        self,
        responses: list[subprocess.CompletedProcess[str] | BaseException],
        tool_path: Path,
    ) -> None:
        super().__init__(responses)
        self.tool_path = tool_path

    def run(
        self,
        *,
        argv: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        completed = super().run(
            argv=argv,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )
        self.tool_path.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
        self.tool_path.chmod(0o700)
        return completed


class EnvironmentObservingRunner(ScriptedRunner):
    def __init__(
        self,
        responses: list[subprocess.CompletedProcess[str] | BaseException],
    ) -> None:
        super().__init__(responses)
        self.private_modes: dict[str, int] = {}

    def run(
        self,
        *,
        argv: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        for name in (
            "HOME",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
        ):
            path = Path(env[name])
            self.private_modes[name] = os.stat(path).st_mode & 0o777
        return super().run(
            argv=argv,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )


def _completed(
    returncode: int = 0,
    *,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=("offline",),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *args),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def clean_repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    versions = root / "migrations" / "versions"
    versions.mkdir(parents=True)
    (versions / "20260729_0017_release.py").write_text(
        'revision = "20260729_0017"\n'
        "down_revision = None\n",
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(".local/\n.env\n.netrc\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "--all")
    _git(
        root,
        "-c",
        "user.name=Release Test",
        "-c",
        "user.email=release-test@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    return root


def _one_test_command(
    *,
    network: bool = False,
    timeout_seconds: float = 30.0,
) -> Command:
    return Command(
        "focused-tests",
        ("uv", "run", "pytest", "tests/test_example.py", "-v"),
        timeout_seconds=timeout_seconds,
        network=network,
        expects_tests=True,
    )


def _run_one(
    clean_repository: Path,
    tmp_path: Path,
    runner: ScriptedRunner,
    *,
    command: Command | None = None,
):
    output_dir = tmp_path / "evidence"
    result = ReleaseVerifier(
        root=clean_repository,
        runner=runner,
        output_dir=output_dir,
        commands=(command or _one_test_command(),),
    ).run()
    return result, output_dir


def test_release_verifier_has_only_the_exact_offline_commands():
    commands = ReleaseVerifier.default_commands()

    assert tuple((command.name, command.argv) for command in commands) == (
        (
            "compile",
            ("uv", "run", "python", "-m", "compileall", "-q", "src"),
        ),
        (
            "migration-tests",
                (
                    "uv",
                    "run",
                    "python",
                    "-m",
                    "pytest",
                "tests/test_migrations.py",
                "tests/test_startup_schema.py",
                "-v",
            ),
        ),
        (
            "security-tests",
                (
                    "uv",
                    "run",
                    "python",
                    "-m",
                    "pytest",
                "tests/test_secret_provider.py",
                "tests/test_transport_boundary.py",
                "tests/test_outbound_policy.py",
                "tests/test_sensitive_crypto.py",
                "tests/test_sensitive_migration.py",
                "tests/test_untrusted_content.py",
                "tests/test_candidate_boundary.py",
                "tests/test_security_posture.py",
                "-v",
            ),
        ),
        (
            "safety-tests",
                (
                    "uv",
                    "run",
                    "python",
                    "-m",
                    "pytest",
                "tests/test_risk_engine.py",
                "tests/test_killswitch.py",
                "tests/test_breakers.py",
                "tests/test_submission_barrier.py",
                "tests/test_order_submission.py",
                "tests/stress/test_stress_scenarios.py",
                "-v",
            ),
        ),
        (
            "frontend-tests",
                (
                    "uv",
                    "run",
                    "python",
                    "-m",
                    "pytest",
                "tests/test_frontend_ui.py",
                "tests/test_security.py",
                "tests/test_security_headers.py",
                "-v",
            ),
        ),
        ("full-tests", ("uv", "run", "python", "-m", "pytest")),
        (
            "branch-coverage",
                (
                    "uv",
                    "run",
                    "python",
                    "-m",
                    "pytest",
                "--cov=trading_assistant.risk",
                "--cov=trading_assistant.orders",
                "--cov=trading_assistant.rules",
                "--cov=trading_assistant.app.auth",
                "--cov=trading_assistant.security",
                "--cov-branch",
                "--cov-fail-under=90",
            ),
        ),
        (
            "static-gate",
            ("uv", "run", "python", "scripts/check_release_safety.py"),
        ),
    )
    flat = "\n".join(" ".join(command.argv) for command in commands)
    assert "alpaca_paper_integration" not in flat
    assert "safety_drill --armed" not in flat
    assert "daemon.main" not in flat
    assert "ops.secrets" not in flat
    assert all(command.network is False for command in commands)
    assert all(
        math.isfinite(command.timeout_seconds)
        and command.timeout_seconds > 0
        for command in commands
    )


def test_runner_receives_only_sanitized_offline_environment(
    clean_repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-cross-boundary")
    monkeypatch.setenv("COMPOSIO_API_KEY", "must-not-cross-boundary")
    monkeypatch.setenv("HTTP_PROXY", "http://must-not-cross-boundary.invalid")
    runner = EnvironmentObservingRunner(
        [_completed(stdout="1 passed in 0.01s\n")]
    )

    result, _ = _run_one(clean_repository, tmp_path, runner)

    assert result.passed is True
    assert len(runner.calls) == 1
    environment = runner.calls[0]["env"]
    assert isinstance(environment, dict)
    assert set(environment) == {
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PIP_CONFIG_FILE",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONHASHSEED",
        "PYTHONNOUSERSITE",
        "PYTHONPATH",
        "PYTHON_KEYRING_BACKEND",
        "TRADING_ASSISTANT_OFFLINE_VERIFY",
        "TRADING_ASSISTANT_PYTHON_NETWORK_GUARD",
        "TRADING_ASSISTANT_VERIFIED_GIT",
        "TRADING_ASSISTANT_VERIFIED_GIT_FINGERPRINT",
        "UV_OFFLINE",
        "UV_CONFIG_FILE",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    }
    assert environment["TRADING_ASSISTANT_OFFLINE_VERIFY"] == "1"
    assert environment["UV_OFFLINE"] == "1"
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert (
        environment["PYTHON_KEYRING_BACKEND"]
        == "keyring.backends.null.Keyring"
    )
    assert (
        environment["TRADING_ASSISTANT_PYTHON_NETWORK_GUARD"]
        == "python_socket_guard_v1"
    )
    assert "ANTHROPIC_API_KEY" not in environment
    assert "COMPOSIO_API_KEY" not in environment
    assert "HTTP_PROXY" not in environment
    assert runner.calls[0]["cwd"] == clean_repository.resolve()
    assert runner.calls[0]["timeout_seconds"] == 30.0
    assert runner.private_modes == {
        "HOME": 0o700,
        "XDG_CACHE_HOME": 0o700,
        "XDG_CONFIG_HOME": 0o700,
        "XDG_DATA_HOME": 0o700,
        "XDG_STATE_HOME": 0o700,
    }


def test_tools_are_resolved_once_and_recorded_before_ambient_path_changes(
    clean_repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    expected_commit = _git(clean_repository, "rev-parse", "HEAD").stdout.strip()
    runner = ScriptedRunner([_completed()])
    command = Command(
        "compile",
        ("uv", "run", "python", "-m", "compileall", "-q", "src"),
        timeout_seconds=30.0,
    )
    output_dir = tmp_path / "evidence"
    verifier = ReleaseVerifier(
        root=clean_repository,
        runner=runner,
        output_dir=output_dir,
        commands=(command,),
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "rev-parse --show-toplevel" ]; then\n'
        f"  printf '%s\\n' {str(clean_repository)!r}\n"
        'elif [ "$1" = "status" ]; then\n'
        "  exit 0\n"
        "else\n"
        "  printf '%040d\\n' 0\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o700)
    monkeypatch.setenv("PATH", str(fake_bin))

    result = verifier.run()

    assert result.passed is True
    assert result.commit == expected_commit
    assert Path(runner.calls[0]["argv"][0]).is_absolute()
    assert Path(runner.calls[0]["argv"][2]).is_absolute()
    payload = json.loads(
        (output_dir / "release-results.json").read_text(encoding="utf-8")
    )
    assert payload["state"] == "completed"
    assert payload["tool_isolation"] == "canonical_fingerprint"
    assert set(payload["tools"]) == {"git", "python", "uv"}
    for evidence in payload["tools"].values():
        assert evidence["fingerprint"].startswith("sha256:")
        assert len(evidence["fingerprint"]) == len("sha256:") + 64
        assert evidence["version"]


def test_constructor_resolves_tool_files_without_executing_pre_evidence_commands(
    clean_repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    def reject_pre_evidence_execution(*_args, **_kwargs):
        raise AssertionError("tool executed before IN_PROGRESS evidence")

    monkeypatch.setattr(
        verifier_module.SubprocessRunner,
        "run",
        reject_pre_evidence_execution,
    )

    verifier = ReleaseVerifier(
        root=clean_repository,
        runner=ScriptedRunner([_completed()]),
        output_dir=tmp_path / "evidence",
        commands=(
            Command(
                "compile",
                ("python", "-m", "compileall", "-q", "src"),
                timeout_seconds=30.0,
            ),
        ),
    )

    assert verifier.toolchain.git.path.is_absolute()
    assert verifier.toolchain.uv.path.is_absolute()
    assert verifier.toolchain.python.path.is_absolute()


def test_tool_mutation_between_command_checks_fails_closed(
    clean_repository: Path,
    tmp_path: Path,
):
    fake_uv = tmp_path / "uv-test-tool"
    fake_uv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_uv.chmod(0o700)
    git_path = Path(shutil.which("git") or "")
    assert git_path.is_absolute()
    toolchain = verifier_module.Toolchain(
        git=verifier_module.ToolIdentity.capture(
            "git",
            git_path,
            version="git test version",
        ),
        uv=verifier_module.ToolIdentity.capture(
            "uv",
            fake_uv,
            version="uv test version",
        ),
        python=verifier_module.ToolIdentity.capture(
            "python",
            Path(sys.executable),
            version="python test version",
        ),
    )
    runner = MutatingToolRunner([_completed(returncode=1)], fake_uv)
    command = Command(
        "compile",
        ("uv", "run", "python", "-m", "compileall", "-q", "src"),
        timeout_seconds=30.0,
    )

    result = ReleaseVerifier(
        root=clean_repository,
        runner=runner,
        output_dir=tmp_path / "evidence",
        commands=(command,),
        toolchain=toolchain,
    ).run()

    assert result.passed is False
    assert result.detail_code == "TOOL_IDENTITY_CHANGED"


def test_failed_step_stops_success_claim_and_remaining_commands(
    clean_repository: Path,
    tmp_path: Path,
):
    commands = (
        Command(
            "compile",
            ("uv", "run", "python", "-m", "compileall", "-q", "src"),
            timeout_seconds=30.0,
        ),
        _one_test_command(),
        Command(
            "static-gate",
            ("uv", "run", "python", "scripts/check_release_safety.py"),
            timeout_seconds=30.0,
        ),
    )
    runner = ScriptedRunner(
        [
            _completed(),
            _completed(returncode=1, stderr="test failed\n"),
            _completed(),
        ]
    )
    output_dir = tmp_path / "evidence"
    (output_dir).mkdir()
    (output_dir / "PASS").write_text("stale\n", encoding="utf-8")

    result = ReleaseVerifier(
        root=clean_repository,
        runner=runner,
        output_dir=output_dir,
        commands=commands,
    ).run()

    assert result.passed is False
    assert [step.status for step in result.steps] == [
        "passed",
        "failed",
    ]
    assert len(runner.calls) == 2
    assert not (output_dir / "PASS").exists()
    payload = json.loads(
        (output_dir / "release-results.json").read_text(encoding="utf-8")
    )
    assert payload["passed"] is False
    assert payload["steps"][-1]["status"] == "failed"


def test_in_progress_evidence_replaces_stale_pass_before_first_command(
    clean_repository: Path,
    tmp_path: Path,
):
    output_dir = tmp_path / "evidence"
    output_dir.mkdir()
    evidence_path = output_dir / "release-results.json"
    evidence_path.write_text(
        json.dumps(
            {
                "state": "completed",
                "passed": True,
                "run_id": "stale-run",
            }
        ),
        encoding="utf-8",
    )
    evidence_path.chmod(0o600)
    runner = EvidenceObservingRunner([_completed()], evidence_path)
    command = Command(
        "compile",
        ("uv", "run", "python", "-m", "compileall", "-q", "src"),
        timeout_seconds=30.0,
    )

    result = ReleaseVerifier(
        root=clean_repository,
        runner=runner,
        output_dir=output_dir,
        commands=(command,),
    ).run()

    assert runner.observed_payload is not None
    assert runner.observed_payload["state"] == "in_progress"
    assert runner.observed_payload["passed"] is False
    assert runner.observed_payload["detail_code"] == "IN_PROGRESS"
    assert runner.observed_payload["commit"] == result.commit
    assert runner.observed_payload["run_id"] != "stale-run"
    assert runner.observed_mode == 0o600
    completed = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert completed["state"] == "completed"
    assert completed["run_id"] == runner.observed_payload["run_id"]
    assert result.run_id == completed["run_id"]


def test_result_json_is_private_and_redacts_command_output(
    clean_repository: Path,
    tmp_path: Path,
):
    composio_like = "ck_" + "MixedCase9ValueWithEnoughEntropy"
    anthropic_like = "sk-ant-" + "MixedCase9ValueWithEnoughEntropy"
    runner = ScriptedRunner(
        [
            _completed(
                returncode=1,
                stdout=(
                    f"COMPOSIO_API_KEY={composio_like}\n"
                    f"path={clean_repository}/private.txt\n"
                ),
                stderr=f"Authorization: Bearer {anthropic_like}\n",
            )
        ]
    )

    result, output_dir = _run_one(clean_repository, tmp_path, runner)

    assert result.passed is False
    result_path = output_dir / "release-results.json"
    serialized = result_path.read_text(encoding="utf-8")
    assert composio_like not in serialized
    assert anthropic_like not in serialized
    assert str(clean_repository) not in serialized
    assert "[REDACTED]" in serialized
    assert "<repo>" in serialized
    assert os.stat(result_path).st_mode & 0o777 == 0o600


def test_generic_bearer_token_is_redacted_before_assignment_patterns(
    clean_repository: Path,
    tmp_path: Path,
):
    generic_bearer = (
        "eyJhbGciOiJIUzI1NiJ9."
        "verifierSyntheticPayload."
        "verifierSyntheticSignature"
    )
    runner = ScriptedRunner(
        [
            _completed(
                returncode=1,
                stderr=f"Authorization: Bearer {generic_bearer}\n",
            )
        ]
    )

    result, output_dir = _run_one(clean_repository, tmp_path, runner)

    assert result.passed is False
    serialized = (output_dir / "release-results.json").read_text(
        encoding="utf-8"
    )
    assert generic_bearer not in serialized
    assert "[REDACTED]" in serialized


@pytest.mark.parametrize(
    ("response", "expected_status", "expected_detail"),
    [
        (
            subprocess.TimeoutExpired(
                cmd=("uv", "run", "pytest"),
                timeout=30.0,
                output="partial",
                stderr="timed out",
            ),
            "timed_out",
            "COMMAND_TIMEOUT",
        ),
        (
            _completed(returncode=-15, stderr="terminated"),
            "signaled",
            "COMMAND_SIGNAL",
        ),
    ],
)
def test_timeout_and_signal_termination_fail_closed(
    clean_repository: Path,
    tmp_path: Path,
    response,
    expected_status: str,
    expected_detail: str,
):
    runner = ScriptedRunner([response])

    result, output_dir = _run_one(clean_repository, tmp_path, runner)

    assert result.passed is False
    assert result.steps[-1].status == expected_status
    assert result.steps[-1].detail_code == expected_detail
    assert not (output_dir / "PASS").exists()


def test_subprocess_runner_uses_private_bounded_regular_spools(tmp_path: Path):
    runner = SubprocessRunner()
    source = (
        "import os, stat\n"
        "print('regular=' + str(stat.S_ISREG(os.fstat(1).st_mode)))\n"
        "print('mode=' + oct(stat.S_IMODE(os.fstat(1).st_mode)))\n"
        "print('x' * 100000)\n"
    )

    completed = runner.run(
        argv=(sys.executable, "-c", source),
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", os.defpath)},
        timeout_seconds=5.0,
    )

    assert completed.returncode == 0
    assert completed.stdout.startswith("regular=True\nmode=0o600\n")
    assert len(completed.stdout) < 40_000
    assert completed.stdout.endswith("[TRUNCATED]\n")


def test_subprocess_runner_timeout_has_a_finite_cleanup_deadline(
    tmp_path: Path,
):
    runner = SubprocessRunner()
    escaped_child = (
        "import subprocess, sys, time\n"
        "subprocess.Popen("
        "[sys.executable, '-c', 'import time; time.sleep(2)'], "
        "start_new_session=True)\n"
        "time.sleep(30)\n"
    )
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        runner.run(
            argv=(sys.executable, "-c", escaped_child),
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", os.defpath)},
            timeout_seconds=0.1,
        )

    assert time.monotonic() - started < 1.5


@pytest.mark.parametrize(
    "relative_path",
    (
        "scripts/verify_loopback_release.py",
        "scripts/check_release_safety.py",
    ),
)
def test_verifier_scripts_do_not_use_unbounded_pipe_capture(
    relative_path: str,
):
    tree = ast.parse(Path(relative_path).read_text(encoding="utf-8"))
    violations: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "communicate"
            ):
                violations.append((node.lineno, "communicate"))
            for keyword in node.keywords:
                if (
                    keyword.arg == "capture_output"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    violations.append((node.lineno, "capture_output"))
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "subprocess"
            and node.attr == "PIPE"
        ):
            violations.append((node.lineno, "subprocess.PIPE"))

    assert violations == []


def test_dirty_tree_blocks_before_any_verification_command(
    clean_repository: Path,
    tmp_path: Path,
):
    (clean_repository / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    runner = ScriptedRunner([_completed(stdout="1 passed\n")])

    result, _ = _run_one(clean_repository, tmp_path, runner)

    assert result.passed is False
    assert result.steps == ()
    assert result.detail_code == "DIRTY_TREE"
    assert runner.calls == []


def test_migration_head_mismatch_blocks_before_any_verification_command(
    clean_repository: Path,
    tmp_path: Path,
):
    migration = next(
        (clean_repository / "migrations" / "versions").glob("*.py")
    )
    migration.write_text(
        'revision = "unexpected_head"\n'
        "down_revision = None\n",
        encoding="utf-8",
    )
    _git(clean_repository, "add", "--all")
    _git(
        clean_repository,
        "-c",
        "user.name=Release Test",
        "-c",
        "user.email=release-test@example.invalid",
        "commit",
        "-qm",
        "unexpected head",
    )
    runner = ScriptedRunner([_completed(stdout="1 passed\n")])

    result, _ = _run_one(clean_repository, tmp_path, runner)

    assert result.passed is False
    assert result.steps == ()
    assert result.detail_code == "MIGRATION_HEAD_MISMATCH"
    assert runner.calls == []


@pytest.mark.parametrize(
    "junit",
    (
        "<testsuite tests=\"0\" failures=\"0\" errors=\"0\" skipped=\"0\"/>",
        (
            "<testsuite tests=\"1\" failures=\"0\" errors=\"0\" skipped=\"1\">"
            "<testcase classname=\"tests.test_alpaca_paper_integration\" "
            "name=\"test_paper_account_and_quote\" "
            "file=\"tests/test_alpaca_paper_integration.py\">"
            "<skipped message=\"credentials absent\"/></testcase>"
            "</testsuite>"
        ),
    ),
)
def test_expected_test_suite_requires_at_least_one_executed_case(
    clean_repository: Path,
    tmp_path: Path,
    junit: str,
):
    runner = ScriptedRunner(
        [_completed(stdout="synthetic pytest output\n")],
        junit_xml=junit,
    )

    result, _ = _run_one(clean_repository, tmp_path, runner)

    assert result.passed is False
    assert result.steps[-1].status == "failed"
    assert result.steps[-1].detail_code == "EXPECTED_SUITE_EMPTY"


def test_expected_suite_requires_parseable_junit_evidence(
    clean_repository: Path,
    tmp_path: Path,
):
    runner = ScriptedRunner(
        [_completed(stdout="1 passed in 0.01s\n")],
        junit_xml=None,
    )

    result, _ = _run_one(clean_repository, tmp_path, runner)

    assert result.passed is False
    assert result.detail_code == "JUNIT_EVIDENCE_MISSING"


def test_junit_rejects_partial_unapproved_skips_despite_passing_cases(
    clean_repository: Path,
    tmp_path: Path,
):
    skipped_cases = "".join(
        (
            "<testcase classname=\"tests.test_example\" "
            f"name=\"test_skipped_{index}\" file=\"tests/test_example.py\">"
            "<skipped message=\"conditional\"/>"
            "</testcase>"
        )
        for index in range(200)
    )
    junit = (
        "<testsuite tests=\"201\" failures=\"0\" errors=\"0\" skipped=\"200\">"
        "<testcase classname=\"tests.test_example\" "
        "name=\"test_passed\" file=\"tests/test_example.py\"/>"
        f"{skipped_cases}</testsuite>"
    )
    runner = ScriptedRunner(
        [_completed(stdout="1 passed, 200 skipped in 0.01s\n")],
        junit_xml=junit,
    )

    result, _ = _run_one(clean_repository, tmp_path, runner)

    assert result.passed is False
    assert result.detail_code == "UNAPPROVED_TEST_SKIP"


def test_junit_proves_each_explicit_focused_test_file_contributed_cases(
    clean_repository: Path,
    tmp_path: Path,
):
    command = Command(
        "focused-tests",
        (
            "uv",
            "run",
            "pytest",
            "tests/test_example.py",
            "tests/test_required.py",
            "-v",
        ),
        timeout_seconds=30.0,
        expects_tests=True,
    )
    runner = ScriptedRunner([_completed(stdout="1 passed\n")])

    result, _ = _run_one(
        clean_repository,
        tmp_path,
        runner,
        command=command,
    )

    assert result.passed is False
    assert result.detail_code == "REQUIRED_TEST_FILE_MISSING"


def test_junit_skip_allowlist_is_exact_and_value_free(
    clean_repository: Path,
    tmp_path: Path,
):
    junit = (
        "<testsuite tests=\"3\" failures=\"0\" errors=\"0\" skipped=\"2\">"
        "<testcase classname=\"tests.test_example\" "
        "name=\"test_passed\" file=\"tests/test_example.py\"/>"
        "<testcase classname=\"tests.test_alpaca_paper_integration\" "
        "name=\"test_paper_account_and_quote\" "
        "file=\"tests/test_alpaca_paper_integration.py\">"
        "<skipped message=\"credentials absent\"/></testcase>"
        "<testcase classname=\"tests.test_llm_runner\" "
        "name=\"test_budget_aborts_run\" file=\"tests/test_llm_runner.py\">"
        "<skipped message=\"not enough triggers\"/></testcase>"
        "</testsuite>"
    )
    command = Command(
        "full-tests",
        ("uv", "run", "pytest"),
        timeout_seconds=30.0,
        expects_tests=True,
    )
    runner = ScriptedRunner(
        [_completed(stdout="1 passed, 2 skipped\n")],
        junit_xml=junit,
    )

    result, output_dir = _run_one(
        clean_repository,
        tmp_path,
        runner,
        command=command,
    )

    assert result.passed is True
    payload = json.loads(
        (output_dir / "release-results.json").read_text(encoding="utf-8")
    )
    assert payload["steps"][0]["tests"] == {
        "errors": 0,
        "failures": 0,
        "skipped": 2,
        "total": 3,
    }


def test_network_marked_command_is_rejected_without_invoking_runner(
    clean_repository: Path,
    tmp_path: Path,
):
    runner = ScriptedRunner([_completed(stdout="1 passed\n")])

    result, _ = _run_one(
        clean_repository,
        tmp_path,
        runner,
        command=_one_test_command(network=True),
    )

    assert result.passed is False
    assert result.steps[-1].status == "failed"
    assert result.steps[-1].detail_code == "NETWORK_COMMAND_REJECTED"
    assert runner.calls == []


@pytest.mark.parametrize("credential_name", (".env", ".netrc"))
def test_ignored_root_credentials_block_before_commands_without_reading_values(
    clean_repository: Path,
    tmp_path: Path,
    credential_name: str,
):
    marker = "must-not-appear-in-verifier-evidence"
    (clean_repository / credential_name).write_text(marker, encoding="utf-8")
    runner = ScriptedRunner([_completed()])
    command = Command(
        "compile",
        ("python", "-m", "compileall", "-q", "src"),
        timeout_seconds=30.0,
    )

    result = ReleaseVerifier(
        root=clean_repository,
        runner=runner,
        output_dir=tmp_path / "evidence",
        commands=(command,),
    ).run()

    assert result.passed is False
    assert result.detail_code == "IGNORED_ROOT_CREDENTIAL_FILE"
    assert runner.calls == []
    serialized = (
        tmp_path / "evidence" / "release-results.json"
    ).read_text(encoding="utf-8")
    assert marker not in serialized


@pytest.mark.parametrize("blocked_family", ("AF_INET", "AF_INET6"))
def test_python_network_guard_blocks_inet_but_allows_unix_socket_creation(
    clean_repository: Path,
    tmp_path: Path,
    blocked_family: str,
):
    source = (
        "import socket;"
        "local=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);"
        "local.close();"
        "print('unix-ok');"
        f"socket.socket(socket.{blocked_family},socket.SOCK_STREAM)"
    )
    command = Command(
        "python-network-attempt",
        ("python", "-c", source),
        timeout_seconds=5.0,
    )

    result = ReleaseVerifier(
        root=clean_repository,
        output_dir=tmp_path / "evidence",
        commands=(command,),
    ).run()

    assert result.passed is False
    assert result.detail_code == "COMMAND_FAILED"
    assert result.steps[0].stdout == "unix-ok\n"
    assert "python_network_guard_blocked" in result.steps[0].stderr
    payload = json.loads(
        (tmp_path / "evidence" / "release-results.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["network_isolation"] == {
        "kind": "python_socket_guard",
        "os_enforced": False,
        "status": "verified",
    }


def test_python_network_guard_blocks_default_inet_socket_constructor(
    clean_repository: Path,
    tmp_path: Path,
):
    command = Command(
        "python-default-network-attempt",
        ("python", "-c", "import socket;socket.socket()"),
        timeout_seconds=5.0,
    )

    result = ReleaseVerifier(
        root=clean_repository,
        output_dir=tmp_path / "evidence",
        commands=(command,),
    ).run()

    assert result.passed is False
    assert result.detail_code == "COMMAND_FAILED"
    assert "python_network_guard_blocked" in result.steps[0].stderr


@pytest.mark.parametrize(
    "constructor",
    (
        "socket.SocketType(socket.AF_INET,socket.SOCK_STREAM)",
        "__import__('_socket').socket(socket.AF_INET,socket.SOCK_STREAM)",
        "socket.getnameinfo(('127.0.0.1',80),0)",
        "__import__('_socket').getnameinfo(('127.0.0.1',80),0)",
    ),
)
def test_python_network_guard_blocks_standard_socket_alias_bypasses(
    clean_repository: Path,
    tmp_path: Path,
    constructor: str,
):
    command = Command(
        "python-socket-alias-attempt",
        ("python", "-c", f"import socket;{constructor}"),
        timeout_seconds=5.0,
    )

    result = ReleaseVerifier(
        root=clean_repository,
        output_dir=tmp_path / "evidence",
        commands=(command,),
    ).run()

    assert result.passed is False
    assert result.detail_code == "COMMAND_FAILED"
    assert "python_network_guard_blocked" in result.steps[0].stderr


def test_missing_python_network_guard_fails_before_configured_commands(
    clean_repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        verifier_module,
        "NETWORK_GUARD_PATH",
        tmp_path / "missing-network-guard.py",
        raising=False,
    )
    runner = ScriptedRunner([_completed()])

    result = ReleaseVerifier(
        root=clean_repository,
        runner=runner,
        output_dir=tmp_path / "evidence",
        commands=(
            Command(
                "compile",
                ("python", "-m", "compileall", "-q", "src"),
                timeout_seconds=30.0,
            ),
        ),
    ).run()

    assert result.passed is False
    assert result.detail_code == "PYTHON_NETWORK_GUARD_UNPROVEN"
    assert runner.calls == []


def test_output_directory_rejects_symlinked_parent(
    clean_repository: Path,
    tmp_path: Path,
):
    real_parent = tmp_path / "real-output-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-output-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    runner = ScriptedRunner([_completed(stdout="1 passed in 0.01s\n")])

    with pytest.raises(RuntimeError, match="output directory is unsafe"):
        ReleaseVerifier(
            root=clean_repository,
            runner=runner,
            output_dir=linked_parent / "evidence",
            commands=(_one_test_command(),),
        ).run()

    assert not (real_parent / "evidence" / "release-results.json").exists()


def test_command_that_changes_candidate_tree_cannot_produce_pass(
    clean_repository: Path,
    tmp_path: Path,
):
    runner = MutatingRunner([_completed(stdout="1 passed in 0.01s\n")])

    result, output_dir = _run_one(clean_repository, tmp_path, runner)

    assert [step.status for step in result.steps] == ["passed"]
    assert result.passed is False
    assert result.detail_code == "DIRTY_TREE"
    payload = json.loads(
        (output_dir / "release-results.json").read_text(encoding="utf-8")
    )
    assert payload["passed"] is False
    assert payload["detail_code"] == "DIRTY_TREE"
    assert not (output_dir / "PASS").exists()


def test_failed_command_still_rechecks_candidate_tree_before_reporting_failure(
    clean_repository: Path,
    tmp_path: Path,
):
    runner = MutatingRunner(
        [_completed(returncode=1, stderr="synthetic command failure\n")]
    )

    result, _ = _run_one(clean_repository, tmp_path, runner)

    assert result.passed is False
    assert result.detail_code == "DIRTY_TREE"
