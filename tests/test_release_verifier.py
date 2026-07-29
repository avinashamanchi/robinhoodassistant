from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess

import pytest

from scripts.verify_loopback_release import Command, ReleaseVerifier


class ScriptedRunner:
    def __init__(
        self,
        responses: list[subprocess.CompletedProcess[str] | BaseException],
    ) -> None:
        self._responses = iter(responses)
        self.calls: list[dict[str, object]] = []

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
    (root / ".gitignore").write_text(".local/\n", encoding="utf-8")
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
                "pytest",
                "tests/test_frontend_ui.py",
                "tests/test_security.py",
                "tests/test_security_headers.py",
                "-v",
            ),
        ),
        ("full-tests", ("uv", "run", "pytest")),
        (
            "branch-coverage",
            (
                "uv",
                "run",
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
    runner = ScriptedRunner([_completed(stdout="1 passed in 0.01s\n")])

    result, _ = _run_one(clean_repository, tmp_path, runner)

    assert result.passed is True
    assert len(runner.calls) == 1
    environment = runner.calls[0]["env"]
    assert isinstance(environment, dict)
    assert set(environment) == {
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "LANG",
        "LC_ALL",
        "PATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONHASHSEED",
        "TRADING_ASSISTANT_OFFLINE_VERIFY",
        "UV_OFFLINE",
    }
    assert environment["TRADING_ASSISTANT_OFFLINE_VERIFY"] == "1"
    assert environment["UV_OFFLINE"] == "1"
    assert "ANTHROPIC_API_KEY" not in environment
    assert "COMPOSIO_API_KEY" not in environment
    assert "HTTP_PROXY" not in environment
    assert runner.calls[0]["cwd"] == clean_repository.resolve()
    assert runner.calls[0]["timeout_seconds"] == 30.0


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
    "stdout",
    (
        "no tests ran in 0.01s\n",
        "1 skipped in 0.01s\n",
        "collected 0 items\n",
        "12 deselected in 0.01s\n",
    ),
)
def test_expected_test_suite_cannot_be_empty_or_entirely_skipped(
    clean_repository: Path,
    tmp_path: Path,
    stdout: str,
):
    runner = ScriptedRunner([_completed(stdout=stdout)])

    result, _ = _run_one(clean_repository, tmp_path, runner)

    assert result.passed is False
    assert result.steps[-1].status == "failed"
    assert result.steps[-1].detail_code == "EXPECTED_SUITE_SKIPPED"


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
