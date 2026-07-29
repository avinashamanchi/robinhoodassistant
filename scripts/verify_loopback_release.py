#!/usr/bin/env python3
"""Run deterministic release checks without credentials or network authority."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from typing import Protocol


DEFAULT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_RELATIVE = Path(".local/verification")
EXPECTED_MIGRATION_HEAD = "20260729_0017"
_MAX_CAPTURE_CHARS = 32_768
_COMMAND_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")
_SHA = re.compile(r"[0-9a-f]{40,64}")
_TESTS_PASSED = re.compile(r"\b\d+\s+passed\b", re.IGNORECASE)
_TESTS_FAILED = re.compile(r"\b\d+\s+(?:failed|error|errors)\b", re.IGNORECASE)
_TESTS_SKIPPED = re.compile(r"\b\d+\s+skipped\b", re.IGNORECASE)
_TESTS_DESELECTED = re.compile(r"\b\d+\s+deselected\b", re.IGNORECASE)
_NO_TESTS = (
    re.compile(r"\bno tests ran\b", re.IGNORECASE),
    re.compile(r"\bcollected\s+0\s+items\b", re.IGNORECASE),
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[-_]?key|authorization|credential|password|secret|token)"
    r"\b\s*[:=]\s*[^\s,;]+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}")
_ANTHROPIC_KEY = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")
_OPENAI_KEY = re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")
_COMPOSIO_KEY = re.compile(r"\bck_[A-Za-z0-9_-]{20,80}\b")
_PRIVATE_PATH = re.compile(
    r"(?:(?:/Users|/home)/[^/\s\"']+|/private/(?:tmp|var)/[^/\s\"']+)"
    r"(?:/[^\s\"']*)?"
)


@dataclass(frozen=True, slots=True)
class Command:
    """One immutable, explicitly classified release command."""

    name: str
    argv: tuple[str, ...]
    timeout_seconds: float
    network: bool = False
    expects_tests: bool = False

    def __post_init__(self) -> None:
        if _COMMAND_NAME.fullmatch(self.name) is None:
            raise ValueError("invalid command name")
        if (
            not isinstance(self.argv, tuple)
            or not self.argv
            or any(
                not isinstance(part, str)
                or not part
                or "\x00" in part
                or "\n" in part
                or "\r" in part
                for part in self.argv
            )
        ):
            raise ValueError("invalid command argv")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or float(self.timeout_seconds) <= 0
        ):
            raise ValueError("command timeout must be finite and positive")
        if type(self.network) is not bool or type(self.expects_tests) is not bool:
            raise ValueError("command classification must be boolean")


@dataclass(frozen=True, slots=True)
class VerificationStep:
    """Redacted evidence for one attempted offline command."""

    name: str
    argv: tuple[str, ...]
    status: str
    returncode: int | None
    duration_seconds: float
    detail_code: str
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Complete software-verification outcome; never operational readiness."""

    passed: bool
    detail_code: str
    commit: str | None
    migration_head: str | None
    started_at: str
    finished_at: str
    steps: tuple[VerificationStep, ...]


class Runner(Protocol):
    def run(
        self,
        *,
        argv: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]: ...


class SubprocessRunner:
    """Run a fixed argv without shell expansion and terminate its process group."""

    def run(
        self,
        *,
        argv: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    process.kill()
                stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(
                cmd=argv,
                timeout=timeout_seconds,
                output=stdout if stdout is not None else exc.output,
                stderr=stderr if stderr is not None else exc.stderr,
            ) from None
        return subprocess.CompletedProcess(
            args=argv,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_environment() -> dict[str, str]:
    return {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "TRADING_ASSISTANT_OFFLINE_VERIFY": "1",
        "UV_OFFLINE": "1",
    }


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _redact(value: str | bytes | None, *, root: Path) -> str:
    text = _as_text(value)
    root_text = str(root)
    if root_text:
        text = text.replace(root_text, "<repo>")
    text = _SECRET_ASSIGNMENT.sub("[REDACTED]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _ANTHROPIC_KEY.sub("[REDACTED]", text)
    text = _OPENAI_KEY.sub("[REDACTED]", text)
    text = _COMPOSIO_KEY.sub("[REDACTED]", text)
    text = _PRIVATE_PATH.sub("<private-path>", text)
    if len(text) > _MAX_CAPTURE_CHARS:
        return text[:_MAX_CAPTURE_CHARS] + "\n[TRUNCATED]"
    return text


def _git(
    root: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ("git", *arguments),
            cwd=root,
            env=_safe_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(
            args=("git",),
            returncode=1,
            stdout="",
            stderr="",
        )


def _repository_state(root: Path) -> tuple[str | None, str | None]:
    top = _git(root, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return None, "GIT_STATE_UNPROVEN"
    try:
        git_root = Path(top.stdout.strip()).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None, "GIT_STATE_UNPROVEN"
    if git_root != root:
        return None, "GIT_STATE_UNPROVEN"
    status = _git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    if status.returncode != 0:
        return None, "GIT_STATE_UNPROVEN"
    if status.stdout:
        return None, "DIRTY_TREE"
    commit = _git(root, "rev-parse", "HEAD")
    candidate = commit.stdout.strip()
    if commit.returncode != 0 or _SHA.fullmatch(candidate) is None:
        return None, "GIT_STATE_UNPROVEN"
    return candidate, None


def _literal(node: ast.AST) -> object:
    try:
        return ast.literal_eval(node)
    except (TypeError, ValueError):
        raise ValueError("migration metadata is not literal") from None


def _migration_assignment(tree: ast.Module, name: str) -> object:
    values: list[object] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(
            isinstance(target, ast.Name) and target.id == name
            for target in targets
        ):
            values.append(_literal(node.value))
    if len(values) != 1:
        raise ValueError("migration metadata is ambiguous")
    return values[0]


def _migration_head(root: Path) -> tuple[str | None, str | None]:
    versions = root / "migrations" / "versions"
    if (
        not versions.is_dir()
        or versions.is_symlink()
    ):
        return None, "MIGRATION_HEAD_MISMATCH"
    revisions: set[str] = set()
    parents: set[str] = set()
    try:
        paths = sorted(versions.glob("*.py"))
        if not paths:
            raise ValueError("migration set is empty")
        for path in paths:
            if path.is_symlink() or not path.is_file():
                raise ValueError("migration path is unsafe")
            tree = ast.parse(
                path.read_text(encoding="utf-8"),
                filename=path.name,
            )
            revision = _migration_assignment(tree, "revision")
            down_revision = _migration_assignment(tree, "down_revision")
            if (
                not isinstance(revision, str)
                or not revision
                or revision in revisions
            ):
                raise ValueError("migration revision is invalid")
            revisions.add(revision)
            if down_revision is None:
                continue
            down_values = (
                (down_revision,)
                if isinstance(down_revision, str)
                else tuple(down_revision)
                if isinstance(down_revision, (tuple, list))
                else ()
            )
            if not down_values or any(
                not isinstance(parent, str) or not parent
                for parent in down_values
            ):
                raise ValueError("migration parent is invalid")
            parents.update(down_values)
        if not parents.issubset(revisions):
            raise ValueError("migration parent is missing")
        heads = revisions - parents
        if len(heads) != 1:
            raise ValueError("migration graph has multiple heads")
        head = next(iter(heads))
    except (OSError, SyntaxError, TypeError, ValueError):
        return None, "MIGRATION_HEAD_MISMATCH"
    if head != EXPECTED_MIGRATION_HEAD:
        return head, "MIGRATION_HEAD_MISMATCH"
    return head, None


def _suite_was_skipped(output: str) -> bool:
    if any(pattern.search(output) for pattern in _NO_TESTS):
        return True
    return (
        (
            _TESTS_SKIPPED.search(output) is not None
            or _TESTS_DESELECTED.search(output) is not None
        )
        and _TESTS_PASSED.search(output) is None
        and _TESTS_FAILED.search(output) is None
    )


def _safe_output_directory(path: Path) -> Path:
    path = Path(os.path.abspath(path.expanduser()))
    if path.resolve(strict=False) != path:
        raise RuntimeError("verification output directory is unsafe")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (
        path.is_symlink()
        or not path.is_dir()
        or path.resolve(strict=True) != path
    ):
        raise RuntimeError("verification output directory is unsafe")
    path.chmod(0o700)
    return path


def _step_payload(step: VerificationStep, *, root: Path) -> dict[str, object]:
    return {
        "name": step.name,
        "argv": [_redact(part, root=root) for part in step.argv],
        "status": step.status,
        "returncode": step.returncode,
        "duration_seconds": round(step.duration_seconds, 6),
        "detail_code": step.detail_code,
        "stdout": step.stdout,
        "stderr": step.stderr,
    }


def _write_result(
    result: VerificationResult,
    *,
    output_dir: Path,
    root: Path,
) -> Path:
    directory = _safe_output_directory(output_dir)
    pass_sentinel = directory / "PASS"
    try:
        pass_sentinel.unlink(missing_ok=True)
    except OSError:
        raise RuntimeError("stale PASS sentinel could not be removed") from None
    payload = {
        "schema_version": 1,
        "passed": result.passed,
        "detail_code": result.detail_code,
        "commit": result.commit,
        "migration_head": result.migration_head,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "steps": [
            _step_payload(step, root=root)
            for step in result.steps
        ],
    }
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".release-results.",
        suffix=".tmp",
        dir=directory,
        text=True,
    )
    temporary = Path(temporary_name)
    destination = directory / "release-results.json"
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        destination.chmod(0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    return destination


class ReleaseVerifier:
    """Orchestrate a fixed, offline-only software verification program."""

    def __init__(
        self,
        *,
        root: Path = DEFAULT_ROOT,
        runner: Runner | None = None,
        output_dir: Path | None = None,
        commands: tuple[Command, ...] | None = None,
    ) -> None:
        self.root = root.resolve(strict=True)
        self.runner = runner or SubprocessRunner()
        self.output_dir = (
            output_dir
            if output_dir is not None
            else self.root / DEFAULT_OUTPUT_RELATIVE
        )
        self.commands = commands if commands is not None else self.default_commands()

    @staticmethod
    def default_commands() -> tuple[Command, ...]:
        return (
            Command(
                "compile",
                ("uv", "run", "python", "-m", "compileall", "-q", "src"),
                timeout_seconds=120.0,
            ),
            Command(
                "migration-tests",
                (
                    "uv",
                    "run",
                    "pytest",
                    "tests/test_migrations.py",
                    "tests/test_startup_schema.py",
                    "-v",
                ),
                timeout_seconds=900.0,
                expects_tests=True,
            ),
            Command(
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
                timeout_seconds=1200.0,
                expects_tests=True,
            ),
            Command(
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
                timeout_seconds=1200.0,
                expects_tests=True,
            ),
            Command(
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
                timeout_seconds=1200.0,
                expects_tests=True,
            ),
            Command(
                "full-tests",
                ("uv", "run", "pytest"),
                timeout_seconds=1800.0,
                expects_tests=True,
            ),
            Command(
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
                timeout_seconds=1800.0,
                expects_tests=True,
            ),
            Command(
                "static-gate",
                ("uv", "run", "python", "scripts/check_release_safety.py"),
                timeout_seconds=300.0,
            ),
        )

    def _finish(
        self,
        *,
        passed: bool,
        detail_code: str,
        commit: str | None,
        migration_head: str | None,
        started_at: str,
        steps: list[VerificationStep],
    ) -> VerificationResult:
        result = VerificationResult(
            passed=passed,
            detail_code=detail_code,
            commit=commit,
            migration_head=migration_head,
            started_at=started_at,
            finished_at=_utc_now(),
            steps=tuple(steps),
        )
        _write_result(
            result,
            output_dir=self.output_dir,
            root=self.root,
        )
        return result

    def run(self) -> VerificationResult:
        started_at = _utc_now()
        commit, repository_error = _repository_state(self.root)
        if repository_error is not None:
            return self._finish(
                passed=False,
                detail_code=repository_error,
                commit=None,
                migration_head=None,
                started_at=started_at,
                steps=[],
            )
        migration_head, migration_error = _migration_head(self.root)
        if migration_error is not None:
            return self._finish(
                passed=False,
                detail_code=migration_error,
                commit=commit,
                migration_head=migration_head,
                started_at=started_at,
                steps=[],
            )

        environment = _safe_environment()
        steps: list[VerificationStep] = []
        for command in self.commands:
            if command.network:
                steps.append(
                    VerificationStep(
                        name=command.name,
                        argv=command.argv,
                        status="failed",
                        returncode=None,
                        duration_seconds=0.0,
                        detail_code="NETWORK_COMMAND_REJECTED",
                        stdout="",
                        stderr="",
                    )
                )
                return self._finish(
                    passed=False,
                    detail_code="NETWORK_COMMAND_REJECTED",
                    commit=commit,
                    migration_head=migration_head,
                    started_at=started_at,
                    steps=steps,
                )

            started = time.monotonic()
            try:
                completed = self.runner.run(
                    argv=command.argv,
                    cwd=self.root,
                    env=dict(environment),
                    timeout_seconds=float(command.timeout_seconds),
                )
            except subprocess.TimeoutExpired as exc:
                steps.append(
                    VerificationStep(
                        name=command.name,
                        argv=command.argv,
                        status="timed_out",
                        returncode=None,
                        duration_seconds=time.monotonic() - started,
                        detail_code="COMMAND_TIMEOUT",
                        stdout=_redact(exc.output, root=self.root),
                        stderr=_redact(exc.stderr, root=self.root),
                    )
                )
                return self._finish(
                    passed=False,
                    detail_code="COMMAND_TIMEOUT",
                    commit=commit,
                    migration_head=migration_head,
                    started_at=started_at,
                    steps=steps,
                )
            except BaseException:
                steps.append(
                    VerificationStep(
                        name=command.name,
                        argv=command.argv,
                        status="failed",
                        returncode=None,
                        duration_seconds=time.monotonic() - started,
                        detail_code="COMMAND_RUNNER_ERROR",
                        stdout="",
                        stderr="",
                    )
                )
                return self._finish(
                    passed=False,
                    detail_code="COMMAND_RUNNER_ERROR",
                    commit=commit,
                    migration_head=migration_head,
                    started_at=started_at,
                    steps=steps,
                )

            duration = time.monotonic() - started
            stdout = _redact(completed.stdout, root=self.root)
            stderr = _redact(completed.stderr, root=self.root)
            combined = f"{stdout}\n{stderr}"
            if completed.returncode < 0:
                status = "signaled"
                detail_code = "COMMAND_SIGNAL"
            elif completed.returncode != 0:
                status = "failed"
                detail_code = "COMMAND_FAILED"
            elif command.expects_tests and _suite_was_skipped(combined):
                status = "failed"
                detail_code = "EXPECTED_SUITE_SKIPPED"
            else:
                status = "passed"
                detail_code = "PASS"
            steps.append(
                VerificationStep(
                    name=command.name,
                    argv=command.argv,
                    status=status,
                    returncode=completed.returncode,
                    duration_seconds=duration,
                    detail_code=detail_code,
                    stdout=stdout,
                    stderr=stderr,
                )
            )
            if status != "passed":
                return self._finish(
                    passed=False,
                    detail_code=detail_code,
                    commit=commit,
                    migration_head=migration_head,
                    started_at=started_at,
                    steps=steps,
                )

        final_commit, final_repository_error = _repository_state(self.root)
        if final_repository_error is not None:
            return self._finish(
                passed=False,
                detail_code=final_repository_error,
                commit=commit,
                migration_head=migration_head,
                started_at=started_at,
                steps=steps,
            )
        if final_commit != commit:
            return self._finish(
                passed=False,
                detail_code="REPOSITORY_CHANGED_DURING_VERIFY",
                commit=commit,
                migration_head=migration_head,
                started_at=started_at,
                steps=steps,
            )
        return self._finish(
            passed=True,
            detail_code="PASS",
            commit=commit,
            migration_head=migration_head,
            started_at=started_at,
            steps=steps,
        )


def main() -> int:
    try:
        result = ReleaseVerifier().run()
    except BaseException:
        print("release verification: FAIL (INTERNAL_VERIFIER_ERROR)", file=sys.stderr)
        return 1
    for step in result.steps:
        print(f"{step.name}: {step.status} ({step.detail_code})")
    if result.passed:
        print("release verification: PASS")
        return 0
    print(
        f"release verification: FAIL ({result.detail_code})",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
