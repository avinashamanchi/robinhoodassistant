#!/usr/bin/env python3
"""Run bounded release checks with sanitized process inputs."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import resource
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Protocol
import uuid
import xml.etree.ElementTree as ET


DEFAULT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_RELATIVE = Path(".local/verification")
NETWORK_GUARD_PATH = Path(__file__).resolve().with_name(
    "verifier_network_guard.py"
)
EXPECTED_MIGRATION_HEAD = "20260729_0017"
TRUSTED_ANCESTRY_ANCHOR = "4807cc0dc9dd20f21cf174e81034fea656162e3d"
_MAX_CAPTURE_CHARS = 32_768
_MAX_CAPTURE_BYTES = 32_768
_MAX_OUTPUT_FILE_BYTES = 64 * 1024 * 1024
_MAX_JUNIT_BYTES = 32 * 1024 * 1024
_MAX_PYTEST_EVIDENCE_BYTES = 32 * 1024 * 1024
_TERMINATE_GRACE_SECONDS = 0.5
_KILL_GRACE_SECONDS = 0.5
_COMMAND_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")
_SHA = re.compile(r"[0-9a-f]{40,64}")
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_AUTHORIZATION_HEADER = re.compile(
    r"(?im)\bauthorization\b\s*[:=]\s*[^\r\n]*"
    r"(?:\r?\n[ \t]+[^\r\n]*)*"
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
_SAFE_TEST_PATH = re.compile(
    r"tests/(?:[A-Za-z0-9._+-]+/)*test_[A-Za-z0-9._+-]+\.py"
)
_ALLOWED_SKIPPED_NODEIDS = frozenset(
    {
        (
            "tests/test_alpaca_paper_integration.py"
            "::test_paper_account_and_quote"
        ),
        "tests/test_llm_runner.py::test_budget_aborts_run",
    }
)
_ROOT_CREDENTIAL_NAMES = frozenset(
    {
        ".env",
        ".netrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "secrets.json",
        "service-account.json",
        "service_account.json",
    }
)
_PYTEST_PLUGIN_SOURCE = """\
from __future__ import annotations

import json
import os

_COLLECTED = []
_DESELECTED = []
_OUTCOMES = []


def pytest_collection_finish(session):
    _COLLECTED[:] = [item.nodeid for item in session.items]


def pytest_deselected(items):
    _DESELECTED.extend(item.nodeid for item in items)


def pytest_runtest_logreport(report):
    if report.when == "call":
        _OUTCOMES.append(
            {"nodeid": report.nodeid, "outcome": report.outcome}
        )
    elif report.when in {"setup", "teardown"} and report.outcome != "passed":
        _OUTCOMES.append(
            {"nodeid": report.nodeid, "outcome": report.outcome}
        )


def pytest_sessionfinish(session, exitstatus):
    del session
    path = os.environ["TRADING_ASSISTANT_PYTEST_EVIDENCE_PATH"]
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "schema_version": 1,
                "exitstatus": int(exitstatus),
                "collected": _COLLECTED,
                "deselected": _DESELECTED,
                "outcomes": _OUTCOMES,
            },
            handle,
            sort_keys=True,
            separators=(",", ":"),
        )
        handle.write("\\n")
        handle.flush()
        os.fsync(handle.fileno())
"""


@dataclass(frozen=True, slots=True)
class TestManifest:
    """Pinned exact pytest collection identity for one release command."""

    count: int
    digest: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.count, bool)
            or not isinstance(self.count, int)
            or self.count <= 0
            or _SHA256_DIGEST.fullmatch(self.digest) is None
        ):
            raise ValueError("test manifest is invalid")


_MIGRATION_TEST_MANIFEST = TestManifest(
    count=182,
    digest=(
        "sha256:8b7fc05ee92e1ad2a257e8967cdba00f"
        "5948795a5ea98ac61aeeed3a9b09e8ce"
    ),
)
_SECURITY_TEST_MANIFEST = TestManifest(
    count=660,
    digest=(
        "sha256:7b45fe87f34aa26180c48a7bde37fb90"
        "56d95b6854d988cbf7aea34fedbd91a8"
    ),
)
_SAFETY_TEST_MANIFEST = TestManifest(
    count=142,
    digest=(
        "sha256:76c19857c99746deec00743d33567335f"
        "43b7c5bf11869834a1a9eb8bfd100ab"
    ),
)
_FRONTEND_TEST_MANIFEST = TestManifest(
    count=188,
    digest=(
        "sha256:14111d111fa8f069aeca5e898ffea6d5d"
        "09136d6a281624b7c7796ecf866b034"
    ),
)
_FULL_TEST_MANIFEST = TestManifest(
    count=4204,
    digest=(
        "sha256:7f3e1bb708e3605d5f1dc8efc52fb085"
        "0e8471ae71cf3373d617a7aa750a18c4"
    ),
)


@dataclass(frozen=True, slots=True)
class Command:
    """One immutable, explicitly classified release command."""

    name: str
    argv: tuple[str, ...]
    timeout_seconds: float
    network: bool = False
    expects_tests: bool = False
    test_manifest: TestManifest | None = None

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
        if self.expects_tests != (self.test_manifest is not None):
            raise ValueError("test command manifest classification is invalid")


@dataclass(frozen=True, slots=True)
class VerificationStep:
    """Redacted evidence for one attempted local verification command."""

    name: str
    argv: tuple[str, ...]
    status: str
    returncode: int | None
    duration_seconds: float
    detail_code: str
    stdout: str
    stderr: str
    tests_total: int | None = None
    tests_failures: int | None = None
    tests_errors: int | None = None
    tests_skipped: int | None = None


@dataclass(frozen=True, slots=True)
class JUnitEvidence:
    total: int
    failures: int
    errors: int
    skipped: int
    nodeids: tuple[str, ...]
    skipped_nodeids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PytestEvidence:
    collected: tuple[str, ...]
    skipped: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToolIdentity:
    """Canonical executable identity captured before verification starts."""

    name: str
    path: Path
    fingerprint: str
    version: str
    device: int
    inode: int
    size: int
    mtime_ns: int

    @classmethod
    def capture(
        cls,
        name: str,
        path: Path,
        *,
        version: str,
    ) -> ToolIdentity:
        if _COMMAND_NAME.fullmatch(name) is None:
            raise ValueError("tool name is invalid")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("tool version is invalid")
        try:
            canonical = path.resolve(strict=True)
            before = canonical.stat()
        except OSError:
            raise ValueError("tool path is invalid") from None
        if (
            not canonical.is_absolute()
            or not canonical.is_file()
            or not os.access(canonical, os.X_OK)
            or before.st_mode & 0o002
        ):
            raise ValueError("tool path is unsafe")
        digest = hashlib.sha256()
        try:
            with canonical.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            after = canonical.stat()
        except OSError:
            raise ValueError("tool identity is unreadable") from None
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if identity_before != identity_after:
            raise ValueError("tool changed during identity capture")
        return cls(
            name=name,
            path=canonical,
            fingerprint=f"sha256:{digest.hexdigest()}",
            version=version.strip().splitlines()[0][:_MAX_CAPTURE_CHARS],
            device=before.st_dev,
            inode=before.st_ino,
            size=before.st_size,
            mtime_ns=before.st_mtime_ns,
        )

    def is_current(self) -> bool:
        try:
            current = ToolIdentity.capture(
                self.name,
                self.path,
                version=self.version,
            )
        except ValueError:
            return False
        return current == self


@dataclass(frozen=True, slots=True)
class Toolchain:
    git: ToolIdentity
    node: ToolIdentity
    uv: ToolIdentity
    python: ToolIdentity

    def __post_init__(self) -> None:
        if (
            self.git.name != "git"
            or self.node.name != "node"
            or self.uv.name != "uv"
            or self.python.name != "python"
        ):
            raise ValueError("toolchain names are invalid")
        if (
            len(
                {
                    self.git.path,
                    self.node.path,
                    self.uv.path,
                    self.python.path,
                }
            )
            != 4
        ):
            raise ValueError("toolchain executables must be distinct")

    def identities(self) -> tuple[ToolIdentity, ...]:
        return (self.git, self.node, self.uv, self.python)

    def is_current(self) -> bool:
        return all(identity.is_current() for identity in self.identities())


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Complete software-verification outcome; never operational readiness."""

    state: str
    run_id: str
    passed: bool
    detail_code: str
    commit: str | None
    migration_head: str | None
    started_at: str
    finished_at: str
    steps: tuple[VerificationStep, ...]
    tools: tuple[ToolIdentity, ...]
    python_network_guard_verified: bool


class Runner(Protocol):
    def run(
        self,
        *,
        argv: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]: ...


class OutputLimitExceeded(subprocess.SubprocessError):
    """A child reached the verifier's kernel-enforced file-size ceiling."""

    def __init__(self, *, output: str, stderr: str) -> None:
        super().__init__("command output limit exceeded")
        self.output = output
        self.stderr = stderr


class SubprocessRunner:
    """Run fixed argv with private bounded captures and finite cleanup waits."""

    def __init__(
        self,
        *,
        max_output_file_bytes: int = _MAX_OUTPUT_FILE_BYTES,
    ) -> None:
        if (
            isinstance(max_output_file_bytes, bool)
            or not isinstance(max_output_file_bytes, int)
            or max_output_file_bytes <= 0
        ):
            raise ValueError("output file limit must be a positive integer")
        self.max_output_file_bytes = max_output_file_bytes

    def _limit_child_files(self) -> None:
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (
                self.max_output_file_bytes,
                self.max_output_file_bytes,
            ),
        )
        signal.signal(signal.SIGXFSZ, signal.SIG_DFL)

    @staticmethod
    def _capture(handle) -> str:
        handle.flush()
        handle.seek(0)
        payload = handle.read(_MAX_CAPTURE_BYTES + 1)
        truncated = len(payload) > _MAX_CAPTURE_BYTES
        text = payload[:_MAX_CAPTURE_BYTES].decode(
            "utf-8",
            errors="replace",
        )
        if truncated:
            if text and not text.endswith("\n"):
                text += "\n"
            text += "[TRUNCATED]\n"
        return text

    @staticmethod
    def _signal_process_group(
        process: subprocess.Popen[bytes],
        sig: signal.Signals,
    ) -> None:
        try:
            os.killpg(process.pid, sig)
            return
        except OSError:
            pass
        try:
            if sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except OSError:
            pass

    @classmethod
    def _bounded_stop(cls, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        cls._signal_process_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=_TERMINATE_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        cls._signal_process_group(process, signal.SIGKILL)
        try:
            process.wait(timeout=_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            return

    def run(
        self,
        *,
        argv: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        with (
            tempfile.TemporaryFile(mode="w+b") as stdout_handle,
            tempfile.TemporaryFile(mode="w+b") as stderr_handle,
        ):
            os.fchmod(stdout_handle.fileno(), 0o600)
            os.fchmod(stderr_handle.fileno(), 0o600)
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
                preexec_fn=self._limit_child_files,
            )
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                self._bounded_stop(process)
                stdout = self._capture(stdout_handle)
                stderr = self._capture(stderr_handle)
                raise subprocess.TimeoutExpired(
                    cmd=argv,
                    timeout=timeout_seconds,
                    output=stdout,
                    stderr=stderr,
                ) from None
            except BaseException:
                self._bounded_stop(process)
                raise
            output_limit_reached = any(
                os.fstat(handle.fileno()).st_size
                >= self.max_output_file_bytes
                for handle in (stdout_handle, stderr_handle)
            )
            stdout = self._capture(stdout_handle)
            stderr = self._capture(stderr_handle)
            if output_limit_reached:
                raise OutputLimitExceeded(output=stdout, stderr=stderr)
            return subprocess.CompletedProcess(
                args=argv,
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )


def _resolve_toolchain() -> Toolchain:
    candidates = {
        "git": shutil.which("git"),
        "node": shutil.which("node"),
        "uv": shutil.which("uv"),
        "python": sys.executable,
    }
    if any(not value for value in candidates.values()):
        raise RuntimeError("required verifier tool is unavailable")
    identities: dict[str, ToolIdentity] = {}
    for name in ("git", "node", "uv", "python"):
        raw_path = candidates[name]
        if raw_path is None:
            raise RuntimeError("required verifier tool is unavailable")
        if not _tool_path_is_trusted(name, Path(raw_path)):
            raise RuntimeError("required verifier tool is unproven")
        try:
            identities[name] = ToolIdentity.capture(
                name,
                Path(raw_path),
                version="unproven",
            )
        except ValueError:
            raise RuntimeError("required verifier tool is unproven") from None
    return Toolchain(
        git=identities["git"],
        node=identities["node"],
        uv=identities["uv"],
        python=identities["python"],
    )


def _tool_path_is_trusted(name: str, path: Path) -> bool:
    try:
        canonical = path.resolve(strict=True)
        account_home = Path(
            pwd.getpwuid(os.getuid()).pw_dir
        ).resolve(strict=True)
        interpreter = Path(sys.executable).resolve(strict=True)
    except (KeyError, OSError, RuntimeError):
        return False
    if name == "python":
        return canonical == interpreter
    if name == "git":
        roots = (
            Path("/usr/bin"),
            Path("/usr/local/bin"),
            Path("/opt/homebrew/Cellar/git"),
            Path("/home/linuxbrew/.linuxbrew/Cellar/git"),
        )
    elif name == "node":
        roots = (
            Path("/usr/bin"),
            Path("/usr/local/bin"),
            Path("/opt/homebrew/Cellar/node"),
            Path("/home/linuxbrew/.linuxbrew/Cellar/node"),
            Path("/opt/hostedtoolcache/node"),
        )
    elif name == "uv":
        roots = (
            Path("/usr/bin"),
            Path("/usr/local/bin"),
            Path("/opt/homebrew/Cellar/uv"),
            Path("/home/linuxbrew/.linuxbrew/Cellar/uv"),
            Path("/opt/hostedtoolcache/uv"),
            account_home / ".local" / "bin",
            account_home / ".local" / "share" / "uv",
        )
    else:
        return False
    for root in roots:
        try:
            trusted_root = root.resolve(strict=True)
            canonical.relative_to(trusted_root)
            mode = trusted_root.stat().st_mode
        except (OSError, RuntimeError, ValueError):
            continue
        if mode & 0o022:
            continue
        return True
    return False


def _prove_tool_versions(
    toolchain: Toolchain,
    *,
    root: Path,
    environment: dict[str, str],
) -> Toolchain | None:
    runner = SubprocessRunner()
    proven: dict[str, ToolIdentity] = {}
    for identity in toolchain.identities():
        try:
            completed = runner.run(
                argv=(str(identity.path), "--version"),
                cwd=root,
                env=environment,
                timeout_seconds=10.0,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        version_output = f"{completed.stdout}\n{completed.stderr}".strip()
        if completed.returncode != 0 or not version_output:
            return None
        try:
            updated = ToolIdentity.capture(
                identity.name,
                identity.path,
                version=version_output,
            )
        except ValueError:
            return None
        if (
            updated.path != identity.path
            or updated.fingerprint != identity.fingerprint
            or updated.device != identity.device
            or updated.inode != identity.inode
            or updated.size != identity.size
            or updated.mtime_ns != identity.mtime_ns
        ):
            return None
        proven[identity.name] = updated
    return Toolchain(
        git=proven["git"],
        node=proven["node"],
        uv=proven["uv"],
        python=proven["python"],
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    path.chmod(0o700)
    return path


def _private_file(path: Path, content: str) -> Path:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise
    return path


def _safe_environment(
    toolchain: Toolchain,
    *,
    runtime_dir: Path,
) -> dict[str, str]:
    if (
        not NETWORK_GUARD_PATH.is_file()
        or NETWORK_GUARD_PATH.is_symlink()
    ):
        raise RuntimeError("python network guard is unavailable")
    home = _private_directory(runtime_dir / "home")
    xdg_cache = _private_directory(runtime_dir / "xdg-cache")
    xdg_config = _private_directory(runtime_dir / "xdg-config")
    xdg_data = _private_directory(runtime_dir / "xdg-data")
    xdg_state = _private_directory(runtime_dir / "xdg-state")
    injection = _private_directory(runtime_dir / "python-injection")
    _private_file(
        injection / "sitecustomize.py",
        "from scripts.verifier_network_guard import install_network_guard\n"
        "install_network_guard()\n",
    )
    _private_file(
        injection / "verifier_pytest_plugin.py",
        _PYTEST_PLUGIN_SOURCE,
    )
    git_config = _private_file(runtime_dir / "gitconfig", "")
    pip_config = _private_file(runtime_dir / "pip.conf", "")
    uv_config = _private_file(runtime_dir / "uv.toml", "")
    tool_directories = {
        str(identity.path.parent)
        for identity in toolchain.identities()
    }
    return {
        "GIT_CONFIG_GLOBAL": str(git_config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.pathsep.join((*sorted(tool_directories), os.defpath)),
        "PIP_CONFIG_FILE": str(pip_config),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": os.pathsep.join((str(injection), str(DEFAULT_ROOT))),
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        "TRADING_ASSISTANT_LOCAL_VERIFY": "1",
        "TRADING_ASSISTANT_PYTHON_NETWORK_GUARD": "python_socket_guard_v1",
        "TRADING_ASSISTANT_TRUSTED_ANCESTRY_ANCHOR": (
            TRUSTED_ANCESTRY_ANCHOR
        ),
        "TRADING_ASSISTANT_VERIFIED_GIT": str(toolchain.git.path),
        "TRADING_ASSISTANT_VERIFIED_GIT_FINGERPRINT": (
            toolchain.git.fingerprint
        ),
        "UV_OFFLINE": "1",
        "UV_CONFIG_FILE": str(uv_config),
        "XDG_CACHE_HOME": str(xdg_cache),
        "XDG_CONFIG_HOME": str(xdg_config),
        "XDG_DATA_HOME": str(xdg_data),
        "XDG_STATE_HOME": str(xdg_state),
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
    text = _AUTHORIZATION_HEADER.sub("Authorization: [REDACTED]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _SECRET_ASSIGNMENT.sub("[REDACTED]", text)
    text = _ANTHROPIC_KEY.sub("[REDACTED]", text)
    text = _OPENAI_KEY.sub("[REDACTED]", text)
    text = _COMPOSIO_KEY.sub("[REDACTED]", text)
    text = _PRIVATE_PATH.sub("<private-path>", text)
    if len(text) > _MAX_CAPTURE_CHARS:
        return text[:_MAX_CAPTURE_CHARS] + "\n[TRUNCATED]"
    return text


def _git(
    root: Path,
    git_path: Path,
    environment: dict[str, str],
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    try:
        return SubprocessRunner().run(
            argv=(str(git_path), *arguments),
            cwd=root,
            env=environment,
            timeout_seconds=15.0,
        )
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(
            args=("git",),
            returncode=1,
            stdout="",
            stderr="",
        )


def _repository_state(
    root: Path,
    *,
    git_path: Path,
    environment: dict[str, str],
) -> tuple[str | None, str | None]:
    top = _git(
        root,
        git_path,
        environment,
        "rev-parse",
        "--show-toplevel",
    )
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
        git_path,
        environment,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    if status.returncode != 0:
        return None, "GIT_STATE_UNPROVEN"
    if status.stdout:
        return None, "DIRTY_TREE"
    commit = _git(root, git_path, environment, "rev-parse", "HEAD")
    candidate = commit.stdout.strip()
    if commit.returncode != 0 or _SHA.fullmatch(candidate) is None:
        return None, "GIT_STATE_UNPROVEN"
    return candidate, None


def _git_common_directory(
    root: Path,
    *,
    git_path: Path,
    environment: dict[str, str],
) -> Path | None:
    completed = _git(
        root,
        git_path,
        environment,
        "rev-parse",
        "--git-common-dir",
    )
    if completed.returncode != 0:
        return None
    candidate = Path(completed.stdout.strip())
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        common = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    return common if common.is_dir() else None


def _repository_history_error(
    root: Path,
    *,
    git_path: Path,
    environment: dict[str, str],
    trusted_ancestry_anchor: str,
) -> str | None:
    if _SHA.fullmatch(trusted_ancestry_anchor) is None:
        return "TRUSTED_ANCESTRY_UNPROVEN"
    common = _git_common_directory(
        root,
        git_path=git_path,
        environment=environment,
    )
    if common is None:
        return "GIT_HISTORY_UNPROVEN"
    if os.path.lexists(common / "info" / "grafts"):
        return "GIT_HISTORY_GRAFTS"
    if os.path.lexists(common / "objects" / "info" / "alternates"):
        return "GIT_HISTORY_ALTERNATES"

    replacements = _git(
        root,
        git_path,
        environment,
        "for-each-ref",
        "--format=%(refname)",
        "refs/replace",
    )
    if replacements.returncode != 0:
        return "GIT_HISTORY_UNPROVEN"
    if replacements.stdout.strip():
        return "GIT_HISTORY_REPLACE_REFS"

    partial = _git(
        root,
        git_path,
        environment,
        "config",
        "--local",
        "--get-regexp",
        r"^(extensions\.partialclone|remote\..*\.promisor)$",
    )
    if partial.returncode not in {0, 1}:
        return "GIT_HISTORY_UNPROVEN"
    if partial.returncode == 0 or partial.stdout.strip():
        return "GIT_HISTORY_PARTIAL_CLONE"

    shallow = _git(
        root,
        git_path,
        environment,
        "rev-parse",
        "--is-shallow-repository",
    )
    if shallow.returncode != 0:
        return "GIT_HISTORY_UNPROVEN"
    if shallow.stdout.strip() == "true":
        return "GIT_HISTORY_SHALLOW"
    if shallow.stdout.strip() != "false":
        return "GIT_HISTORY_UNPROVEN"

    anchor = _git(
        root,
        git_path,
        environment,
        "cat-file",
        "-e",
        f"{trusted_ancestry_anchor}^{{commit}}",
    )
    ancestry = _git(
        root,
        git_path,
        environment,
        "merge-base",
        "--is-ancestor",
        trusted_ancestry_anchor,
        "HEAD",
    )
    if anchor.returncode != 0 or ancestry.returncode != 0:
        return "TRUSTED_ANCESTRY_UNPROVEN"
    return None


def _ignored_root_credential_exists(
    root: Path,
    *,
    git_path: Path,
    environment: dict[str, str],
) -> bool | None:
    try:
        names = tuple(
            entry.name
            for entry in root.iterdir()
            if (
                entry.name in _ROOT_CREDENTIAL_NAMES
                or (
                    entry.name.startswith(".env.")
                    and entry.name
                    not in {".env.example", ".env.sample", ".env.template"}
                )
            )
        )
    except OSError:
        return None
    for name in names:
        ignored = _git(
            root,
            git_path,
            environment,
            "check-ignore",
            "-q",
            "--",
            name,
        )
        if ignored.returncode == 0:
            return True
        if ignored.returncode != 1:
            return None
    return False


def _python_guard_is_active(
    *,
    python_path: Path,
    root: Path,
    environment: dict[str, str],
) -> bool:
    probe = (
        "import os, socket, sys\n"
        "if os.environ.get('TRADING_ASSISTANT_PYTHON_NETWORK_GUARD') "
        "!= 'python_socket_guard_v1':\n"
        "    raise SystemExit(90)\n"
        "local = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "local.close()\n"
        "try:\n"
        "    socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "except OSError as exc:\n"
        "    if 'python_network_guard_blocked' in str(exc):\n"
        "        print('python-network-guard: verified')\n"
        "        raise SystemExit(0)\n"
        "raise SystemExit(91)\n"
    )
    try:
        completed = SubprocessRunner().run(
            argv=(str(python_path), "-c", probe),
            cwd=root,
            env=environment,
            timeout_seconds=10.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return (
        completed.returncode == 0
        and completed.stdout.strip() == "python-network-guard: verified"
    )


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


def _required_test_files(command: Command) -> tuple[str, ...]:
    required: list[str] = []
    for part in command.argv:
        if not part.startswith("tests/") or not part.endswith(".py"):
            continue
        if (
            _SAFE_TEST_PATH.fullmatch(part) is None
            or any(
                component in {"", ".", ".."}
                for component in PurePosixPath(part).parts
            )
        ):
            raise ValueError("required test path is unsafe")
        if part not in required:
            required.append(part)
    return tuple(required)


def _safe_nodeid(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or any(char in value for char in ("\n", "\r", "\x00", "\x1b"))
    ):
        return None
    relative, separator, remainder = value.partition("::")
    if (
        separator != "::"
        or not remainder
        or _SAFE_TEST_PATH.fullmatch(relative) is None
    ):
        return None
    return value


def _test_manifest(nodeids: tuple[str, ...]) -> TestManifest:
    canonical = "".join(f"{nodeid}\n" for nodeid in sorted(nodeids)).encode(
        "utf-8"
    )
    return TestManifest(
        count=len(nodeids),
        digest=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
    )


def _pytest_evidence(
    path: Path,
    *,
    command: Command,
) -> tuple[PytestEvidence | None, str | None]:
    try:
        if path.is_symlink() or not path.is_file():
            return None, "PYTEST_EVIDENCE_MISSING"
        path.chmod(0o600)
        if path.stat().st_size > _MAX_PYTEST_EVIDENCE_BYTES:
            return None, "PYTEST_EVIDENCE_TOO_LARGE"
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "PYTEST_EVIDENCE_MALFORMED"
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("exitstatus") != 0
    ):
        return None, "PYTEST_EVIDENCE_MALFORMED"
    raw_collected = payload.get("collected")
    raw_deselected = payload.get("deselected")
    raw_outcomes = payload.get("outcomes")
    if (
        not isinstance(raw_collected, list)
        or not isinstance(raw_deselected, list)
        or not isinstance(raw_outcomes, list)
    ):
        return None, "PYTEST_EVIDENCE_MALFORMED"
    collected = tuple(_safe_nodeid(value) for value in raw_collected)
    deselected = tuple(_safe_nodeid(value) for value in raw_deselected)
    if any(value is None for value in (*collected, *deselected)):
        return None, "PYTEST_EVIDENCE_MALFORMED"
    canonical_collected = tuple(
        value for value in collected if value is not None
    )
    canonical_deselected = tuple(
        value for value in deselected if value is not None
    )
    if len(set(canonical_collected)) != len(canonical_collected):
        return None, "DUPLICATE_COLLECTED_NODEID"
    if len(set(canonical_deselected)) != len(canonical_deselected):
        return None, "DUPLICATE_DESELECTED_NODEID"
    if canonical_deselected:
        return None, "TESTS_DESELECTED"
    if not canonical_collected:
        return None, "EXPECTED_SUITE_EMPTY"
    if command.test_manifest is None:
        return None, "TEST_MANIFEST_UNPINNED"
    if _test_manifest(canonical_collected) != command.test_manifest:
        return None, "TEST_MANIFEST_MISMATCH"

    outcomes: dict[str, str] = {}
    for raw in raw_outcomes:
        if not isinstance(raw, dict):
            return None, "PYTEST_EVIDENCE_MALFORMED"
        nodeid = _safe_nodeid(raw.get("nodeid"))
        outcome = raw.get("outcome")
        if nodeid is None or outcome not in {"passed", "failed", "skipped"}:
            return None, "PYTEST_EVIDENCE_MALFORMED"
        if nodeid in outcomes:
            return None, "DUPLICATE_TEST_OUTCOME"
        outcomes[nodeid] = outcome
    if set(outcomes) != set(canonical_collected):
        return None, "INCOMPLETE_TEST_EXECUTION"
    if any(outcome == "failed" for outcome in outcomes.values()):
        return None, "PYTEST_REPORTED_FAILURE"
    skipped = tuple(
        sorted(
            nodeid
            for nodeid, outcome in outcomes.items()
            if outcome == "skipped"
        )
    )
    if any(nodeid not in _ALLOWED_SKIPPED_NODEIDS for nodeid in skipped):
        return None, "UNAPPROVED_TEST_SKIP"
    return (
        PytestEvidence(
            collected=tuple(sorted(canonical_collected)),
            skipped=skipped,
        ),
        None,
    )


def _junit_nodeid(
    *,
    relative: str,
    classname: str,
    name: str,
) -> str | None:
    if (
        not classname
        or len(classname) > 4096
        or any(char in classname for char in ("\n", "\r", "\x00", "\x1b"))
    ):
        return None
    module_name = relative.removesuffix(".py").replace("/", ".")
    if classname == module_name:
        candidate = f"{relative}::{name}"
    elif classname.startswith(f"{module_name}."):
        scope = classname[len(module_name) + 1 :]
        if not scope or any(not part for part in scope.split(".")):
            return None
        candidate = (
            f"{relative}::{scope.replace('.', '::')}::{name}"
        )
    else:
        return None
    return _safe_nodeid(candidate)


def _junit_evidence(
    path: Path,
    *,
    command: Command,
) -> tuple[JUnitEvidence | None, str | None]:
    try:
        if path.is_symlink() or not path.is_file():
            return None, "JUNIT_EVIDENCE_MISSING"
        path.chmod(0o600)
        if path.stat().st_size > _MAX_JUNIT_BYTES:
            return None, "JUNIT_EVIDENCE_TOO_LARGE"
        payload = path.read_bytes()
    except OSError:
        return None, "JUNIT_EVIDENCE_UNREADABLE"
    if not payload:
        return None, "JUNIT_EVIDENCE_EMPTY"
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None, "JUNIT_EVIDENCE_MALFORMED"
    if root.tag == "testsuite":
        suites = (root,)
    elif root.tag == "testsuites":
        suites = tuple(root.findall("testsuite"))
        if len(suites) != len(tuple(root)):
            return None, "JUNIT_EVIDENCE_MALFORMED"
    else:
        return None, "JUNIT_EVIDENCE_MALFORMED"
    if not suites:
        return None, "EXPECTED_SUITE_EMPTY"
    cases = tuple(case for suite in suites for case in suite.findall("testcase"))
    if not cases:
        return None, "EXPECTED_SUITE_EMPTY"

    contributed: set[str] = set()
    identities: set[tuple[str, str, str]] = set()
    failures = 0
    errors = 0
    skipped = 0
    nodeids: set[str] = set()
    skipped_nodeids: set[str] = set()
    for case in cases:
        relative = case.attrib.get("file", "")
        name = case.attrib.get("name", "")
        if (
            _SAFE_TEST_PATH.fullmatch(relative) is None
            or not name
            or any(char in name for char in ("\n", "\r", "\x00"))
        ):
            return None, "JUNIT_EVIDENCE_MALFORMED"
        classname = case.attrib.get("classname", "")
        identity = (relative, classname, name)
        if identity in identities:
            return None, "DUPLICATE_TEST_IDENTITY"
        identities.add(identity)
        nodeid = _junit_nodeid(
            relative=relative,
            classname=classname,
            name=name,
        )
        if nodeid is None or nodeid in nodeids:
            return None, "JUNIT_EVIDENCE_MALFORMED"
        nodeids.add(nodeid)
        contributed.add(relative)
        if case.find("failure") is not None:
            failures += 1
        if case.find("error") is not None:
            errors += 1
        if case.find("skipped") is not None:
            skipped += 1
            skipped_nodeids.add(nodeid)
            if nodeid not in _ALLOWED_SKIPPED_NODEIDS:
                return None, "UNAPPROVED_TEST_SKIP"

    expected_totals = {
        "tests": len(cases),
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
    }
    for suite in suites:
        suite_cases = tuple(suite.findall("testcase"))
        suite_totals = {
            "tests": len(suite_cases),
            "failures": sum(
                case.find("failure") is not None for case in suite_cases
            ),
            "errors": sum(
                case.find("error") is not None for case in suite_cases
            ),
            "skipped": sum(
                case.find("skipped") is not None for case in suite_cases
            ),
        }
        try:
            declared = {
                name: int(suite.attrib[name])
                for name in ("tests", "failures", "errors", "skipped")
            }
        except (KeyError, TypeError, ValueError):
            return None, "JUNIT_EVIDENCE_MALFORMED"
        if declared != suite_totals:
            return None, "JUNIT_AGGREGATE_MISMATCH"
    outer_names = ("tests", "failures", "errors", "skipped")
    if root.tag == "testsuites" and any(
        name in root.attrib for name in outer_names
    ):
        try:
            outer_totals = {
                name: int(root.attrib[name]) for name in outer_names
            }
        except (KeyError, TypeError, ValueError):
            return None, "JUNIT_EVIDENCE_MALFORMED"
        if outer_totals != expected_totals:
            return None, "JUNIT_AGGREGATE_MISMATCH"
    if failures or errors:
        return None, "JUNIT_REPORTED_FAILURE"
    if len(cases) - skipped <= 0:
        return None, "EXPECTED_SUITE_EMPTY"
    if not set(_required_test_files(command)).issubset(contributed):
        return None, "REQUIRED_TEST_FILE_MISSING"
    return (
        JUnitEvidence(
            total=len(cases),
            failures=failures,
            errors=errors,
            skipped=skipped,
            nodeids=tuple(sorted(nodeids)),
            skipped_nodeids=tuple(sorted(skipped_nodeids)),
        ),
        None,
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
    payload: dict[str, object] = {
        "name": step.name,
        "argv": [_redact(part, root=root) for part in step.argv],
        "status": step.status,
        "returncode": step.returncode,
        "duration_seconds": round(step.duration_seconds, 6),
        "detail_code": step.detail_code,
        "stdout": step.stdout,
        "stderr": step.stderr,
    }
    if step.tests_total is not None:
        payload["tests"] = {
            "total": step.tests_total,
            "failures": step.tests_failures,
            "errors": step.tests_errors,
            "skipped": step.tests_skipped,
        }
    return payload


def _tools_payload(
    tools: tuple[ToolIdentity, ...],
    *,
    root: Path,
) -> dict[str, dict[str, str]]:
    return {
        tool.name: {
            "path": _redact(str(tool.path), root=root),
            "fingerprint": tool.fingerprint,
            "version": _redact(tool.version, root=root),
        }
        for tool in tools
    }


def _write_payload(
    payload: dict[str, object],
    *,
    output_dir: Path,
) -> Path:
    directory = _safe_output_directory(output_dir)
    pass_sentinel = directory / "PASS"
    try:
        pass_sentinel.unlink(missing_ok=True)
    except OSError:
        raise RuntimeError("stale PASS sentinel could not be removed") from None
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


def _write_in_progress(
    *,
    run_id: str,
    commit: str,
    migration_head: str,
    started_at: str,
    output_dir: Path,
    root: Path,
    tools: tuple[ToolIdentity, ...],
) -> Path:
    return _write_payload(
        {
            "schema_version": 2,
            "state": "in_progress",
            "run_id": run_id,
            "passed": False,
            "detail_code": "IN_PROGRESS",
            "commit": commit,
            "migration_head": migration_head,
            "started_at": started_at,
            "finished_at": None,
            "steps": [],
            "tool_isolation": "canonical_fingerprint",
            "tools": _tools_payload(tools, root=root),
            "network_isolation": {
                "boundary": "python_runtime_only",
                "hostile_local_process_boundary": (
                    "external_clean_ci_required"
                ),
                "kind": "python_socket_guard",
                "os_enforced": False,
                "status": "unproven",
            },
        },
        output_dir=output_dir,
    )


def _write_preflight(
    *,
    run_id: str,
    started_at: str,
    output_dir: Path,
    root: Path,
    tools: tuple[ToolIdentity, ...] = (),
    detail_code: str = "PREFLIGHT_STARTED",
) -> Path:
    return _write_payload(
        {
            "schema_version": 2,
            "state": "preflight",
            "run_id": run_id,
            "passed": False,
            "detail_code": detail_code,
            "commit": None,
            "migration_head": None,
            "started_at": started_at,
            "finished_at": None,
            "steps": [],
            "tool_isolation": "canonical_fingerprint",
            "tools": _tools_payload(tools, root=root),
            "network_isolation": {
                "boundary": "python_runtime_only",
                "hostile_local_process_boundary": (
                    "external_clean_ci_required"
                ),
                "kind": "python_socket_guard",
                "os_enforced": False,
                "status": "unproven",
            },
        },
        output_dir=output_dir,
    )


def _write_result(
    result: VerificationResult,
    *,
    output_dir: Path,
    root: Path,
) -> Path:
    return _write_payload(
        {
            "schema_version": 2,
            "state": result.state,
            "run_id": result.run_id,
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
            "tool_isolation": "canonical_fingerprint",
            "tools": _tools_payload(result.tools, root=root),
            "network_isolation": {
                "boundary": "python_runtime_only",
                "hostile_local_process_boundary": (
                    "external_clean_ci_required"
                ),
                "kind": "python_socket_guard",
                "os_enforced": False,
                "status": (
                    "verified"
                    if result.python_network_guard_verified
                    else "unproven"
                ),
            },
        },
        output_dir=output_dir,
    )


class ReleaseVerifier:
    """Orchestrate fixed checks; this is not a hostile-host sandbox."""

    def __init__(
        self,
        *,
        root: Path = DEFAULT_ROOT,
        runner: Runner | None = None,
        output_dir: Path | None = None,
        commands: tuple[Command, ...] | None = None,
        toolchain: Toolchain | None = None,
        trusted_ancestry_anchor: str | None = None,
    ) -> None:
        self.root = root.resolve(strict=True)
        self.runner = runner or SubprocessRunner()
        self._toolchain_injected = toolchain is not None
        self.toolchain = toolchain
        self.trusted_ancestry_anchor = (
            trusted_ancestry_anchor
            if trusted_ancestry_anchor is not None
            else TRUSTED_ANCESTRY_ANCHOR
        )
        if _SHA.fullmatch(self.trusted_ancestry_anchor) is None:
            raise ValueError("trusted ancestry anchor is invalid")
        self._python_network_guard_verified = False
        self.output_dir = (
            output_dir
            if output_dir is not None
            else self.root / DEFAULT_OUTPUT_RELATIVE
        )
        self.commands = commands if commands is not None else self.default_commands()

    def _require_toolchain(self) -> Toolchain:
        if self.toolchain is None:
            raise RuntimeError("verifier toolchain is unresolved")
        return self.toolchain

    def _resolved_argv(self, command: Command) -> tuple[str, ...]:
        toolchain = self._require_toolchain()
        replacements = {
            "git": str(toolchain.git.path),
            "node": str(toolchain.node.path),
            "uv": str(toolchain.uv.path),
            "python": str(toolchain.python.path),
        }
        executable, *arguments = command.argv
        return (
            replacements.get(executable, executable),
            *arguments,
        )

    def _candidate_state_error(
        self,
        *,
        expected_commit: str,
        environment: dict[str, str],
    ) -> str | None:
        toolchain = self._require_toolchain()
        if not toolchain.is_current():
            return "TOOL_IDENTITY_CHANGED"
        history_error = _repository_history_error(
            self.root,
            git_path=toolchain.git.path,
            environment=environment,
            trusted_ancestry_anchor=self.trusted_ancestry_anchor,
        )
        if history_error is not None:
            return history_error
        current_commit, repository_error = _repository_state(
            self.root,
            git_path=toolchain.git.path,
            environment=environment,
        )
        if repository_error is not None:
            return repository_error
        if current_commit != expected_commit:
            return "REPOSITORY_CHANGED_DURING_VERIFY"
        return None

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
                    "python",
                    "-m",
                    "pytest",
                    "tests/test_migrations.py",
                    "tests/test_startup_schema.py",
                    "-v",
                ),
                timeout_seconds=900.0,
                expects_tests=True,
                test_manifest=_MIGRATION_TEST_MANIFEST,
            ),
            Command(
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
                timeout_seconds=1200.0,
                expects_tests=True,
                test_manifest=_SECURITY_TEST_MANIFEST,
            ),
            Command(
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
                timeout_seconds=1200.0,
                expects_tests=True,
                test_manifest=_SAFETY_TEST_MANIFEST,
            ),
            Command(
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
                timeout_seconds=1200.0,
                expects_tests=True,
                test_manifest=_FRONTEND_TEST_MANIFEST,
            ),
            Command(
                "full-tests",
                ("uv", "run", "python", "-m", "pytest"),
                timeout_seconds=1800.0,
                expects_tests=True,
                test_manifest=_FULL_TEST_MANIFEST,
            ),
            Command(
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
                timeout_seconds=1800.0,
                expects_tests=True,
                test_manifest=_FULL_TEST_MANIFEST,
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
        run_id: str,
        passed: bool,
        detail_code: str,
        commit: str | None,
        migration_head: str | None,
        started_at: str,
        steps: list[VerificationStep],
    ) -> VerificationResult:
        tools = (
            self.toolchain.identities()
            if self.toolchain is not None
            else ()
        )
        result = VerificationResult(
            state="completed",
            run_id=run_id,
            passed=passed,
            detail_code=detail_code,
            commit=commit,
            migration_head=migration_head,
            started_at=started_at,
            finished_at=_utc_now(),
            steps=tuple(steps),
            tools=tools,
            python_network_guard_verified=self._python_network_guard_verified,
        )
        _write_result(
            result,
            output_dir=self.output_dir,
            root=self.root,
        )
        return result

    def run(self) -> VerificationResult:
        started_at = _utc_now()
        run_id = uuid.uuid4().hex
        self._python_network_guard_verified = False
        _write_preflight(
            run_id=run_id,
            started_at=started_at,
            output_dir=self.output_dir,
            root=self.root,
            tools=(
                self.toolchain.identities()
                if self.toolchain is not None
                else ()
            ),
        )
        _write_preflight(
            run_id=run_id,
            started_at=started_at,
            output_dir=self.output_dir,
            root=self.root,
            tools=(
                self.toolchain.identities()
                if self.toolchain is not None
                else ()
            ),
            detail_code="PREFLIGHT_TOOLCHAIN",
        )
        if self.toolchain is None:
            try:
                self.toolchain = _resolve_toolchain()
            except (OSError, RuntimeError, ValueError):
                return self._finish(
                    run_id=run_id,
                    passed=False,
                    detail_code="TOOLCHAIN_UNPROVEN",
                    commit=None,
                    migration_head=None,
                    started_at=started_at,
                    steps=[],
                )
        toolchain = self._require_toolchain()
        with tempfile.TemporaryDirectory(
            prefix="trading-assistant-verifier-"
        ) as runtime_name:
            runtime_dir = Path(runtime_name)
            runtime_dir.chmod(0o700)
            _write_preflight(
                run_id=run_id,
                started_at=started_at,
                output_dir=self.output_dir,
                root=self.root,
                tools=toolchain.identities(),
                detail_code="PREFLIGHT_ENVIRONMENT",
            )
            try:
                environment = _safe_environment(
                    toolchain,
                    runtime_dir=runtime_dir,
                )
                environment[
                    "TRADING_ASSISTANT_TRUSTED_ANCESTRY_ANCHOR"
                ] = self.trusted_ancestry_anchor
            except (OSError, RuntimeError, ValueError):
                return self._finish(
                    run_id=run_id,
                    passed=False,
                    detail_code="PYTHON_NETWORK_GUARD_UNPROVEN",
                    commit=None,
                    migration_head=None,
                    started_at=started_at,
                    steps=[],
                )
            return self._run_isolated(
                started_at=started_at,
                run_id=run_id,
                environment=environment,
            )

    def _run_isolated(
        self,
        *,
        started_at: str,
        run_id: str,
        environment: dict[str, str],
    ) -> VerificationResult:
        toolchain = self._require_toolchain()
        _write_preflight(
            run_id=run_id,
            started_at=started_at,
            output_dir=self.output_dir,
            root=self.root,
            tools=toolchain.identities(),
            detail_code="PREFLIGHT_REPOSITORY",
        )
        if not toolchain.is_current():
            return self._finish(
                run_id=run_id,
                passed=False,
                detail_code="TOOL_IDENTITY_CHANGED",
                commit=None,
                migration_head=None,
                started_at=started_at,
                steps=[],
            )
        history_error = _repository_history_error(
            self.root,
            git_path=toolchain.git.path,
            environment=environment,
            trusted_ancestry_anchor=self.trusted_ancestry_anchor,
        )
        if history_error is not None:
            return self._finish(
                run_id=run_id,
                passed=False,
                detail_code=history_error,
                commit=None,
                migration_head=None,
                started_at=started_at,
                steps=[],
            )
        commit, repository_error = _repository_state(
            self.root,
            git_path=toolchain.git.path,
            environment=environment,
        )
        if repository_error is not None:
            return self._finish(
                run_id=run_id,
                passed=False,
                detail_code=repository_error,
                commit=None,
                migration_head=None,
                started_at=started_at,
                steps=[],
            )
        ignored_credential = _ignored_root_credential_exists(
            self.root,
            git_path=toolchain.git.path,
            environment=environment,
        )
        if ignored_credential is None:
            return self._finish(
                run_id=run_id,
                passed=False,
                detail_code="ROOT_CREDENTIAL_STATE_UNPROVEN",
                commit=commit,
                migration_head=None,
                started_at=started_at,
                steps=[],
            )
        if ignored_credential:
            return self._finish(
                run_id=run_id,
                passed=False,
                detail_code="IGNORED_ROOT_CREDENTIAL_FILE",
                commit=commit,
                migration_head=None,
                started_at=started_at,
                steps=[],
            )
        _write_preflight(
            run_id=run_id,
            started_at=started_at,
            output_dir=self.output_dir,
            root=self.root,
            tools=toolchain.identities(),
            detail_code="PREFLIGHT_MIGRATION",
        )
        migration_head, migration_error = _migration_head(self.root)
        if migration_error is not None:
            return self._finish(
                run_id=run_id,
                passed=False,
                detail_code=migration_error,
                commit=commit,
                migration_head=migration_head,
                started_at=started_at,
                steps=[],
            )

        if commit is None or migration_head is None:
            return self._finish(
                run_id=run_id,
                passed=False,
                detail_code="VERIFICATION_STATE_UNPROVEN",
                commit=commit,
                migration_head=migration_head,
                started_at=started_at,
                steps=[],
            )
        _write_in_progress(
            run_id=run_id,
            commit=commit,
            migration_head=migration_head,
            started_at=started_at,
            output_dir=self.output_dir,
            root=self.root,
            tools=toolchain.identities(),
        )
        if not self._toolchain_injected:
            proven_toolchain = _prove_tool_versions(
                toolchain,
                root=self.root,
                environment=environment,
            )
            if proven_toolchain is None:
                return self._finish(
                    run_id=run_id,
                    passed=False,
                    detail_code="TOOL_VERSION_UNPROVEN",
                    commit=commit,
                    migration_head=migration_head,
                    started_at=started_at,
                    steps=[],
                )
            self.toolchain = proven_toolchain
            toolchain = proven_toolchain
            state_error = self._candidate_state_error(
                expected_commit=commit,
                environment=environment,
            )
            if state_error is not None:
                return self._finish(
                    run_id=run_id,
                    passed=False,
                    detail_code=state_error,
                    commit=commit,
                    migration_head=migration_head,
                    started_at=started_at,
                    steps=[],
                )
        if not _python_guard_is_active(
            python_path=toolchain.python.path,
            root=self.root,
            environment=environment,
        ):
            return self._finish(
                run_id=run_id,
                passed=False,
                detail_code="PYTHON_NETWORK_GUARD_UNPROVEN",
                commit=commit,
                migration_head=migration_head,
                started_at=started_at,
                steps=[],
            )
        self._python_network_guard_verified = True

        steps: list[VerificationStep] = []
        evidence_directory = _safe_output_directory(self.output_dir)
        for command in self.commands:
            before_error = self._candidate_state_error(
                expected_commit=commit,
                environment=environment,
            )
            if before_error is not None:
                return self._finish(
                    run_id=run_id,
                    passed=False,
                    detail_code=before_error,
                    commit=commit,
                    migration_head=migration_head,
                    started_at=started_at,
                    steps=steps,
                )
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
                    run_id=run_id,
                    passed=False,
                    detail_code="NETWORK_COMMAND_REJECTED",
                    commit=commit,
                    migration_head=migration_head,
                    started_at=started_at,
                    steps=steps,
                )

            started = time.monotonic()
            resolved_argv = self._resolved_argv(command)
            junit_path: Path | None = None
            pytest_evidence_path: Path | None = None
            command_environment = dict(environment)
            if command.expects_tests:
                junit_path = (
                    evidence_directory
                    / f".junit-{run_id}-{command.name}.xml"
                )
                pytest_evidence_path = (
                    evidence_directory
                    / f".pytest-evidence-{run_id}-{command.name}.json"
                )
                junit_path.unlink(missing_ok=True)
                pytest_evidence_path.unlink(missing_ok=True)
                command_environment[
                    "TRADING_ASSISTANT_PYTEST_EVIDENCE_PATH"
                ] = str(pytest_evidence_path)
                resolved_argv = (
                    *resolved_argv,
                    f"--junitxml={junit_path}",
                    "-o",
                    "junit_family=legacy",
                    "-o",
                    "addopts=",
                    "-p",
                    "verifier_pytest_plugin",
                    "-p",
                    "no:cacheprovider",
                )
                if any(part.startswith("--cov") for part in command.argv):
                    resolved_argv = (
                        *resolved_argv,
                        "-p",
                        "pytest_cov.plugin",
                    )
            try:
                completed = self.runner.run(
                    argv=resolved_argv,
                    cwd=self.root,
                    env=command_environment,
                    timeout_seconds=float(command.timeout_seconds),
                )
            except subprocess.TimeoutExpired as exc:
                if junit_path is not None:
                    junit_path.unlink(missing_ok=True)
                if pytest_evidence_path is not None:
                    pytest_evidence_path.unlink(missing_ok=True)
                steps.append(
                    VerificationStep(
                        name=command.name,
                        argv=resolved_argv,
                        status="timed_out",
                        returncode=None,
                        duration_seconds=time.monotonic() - started,
                        detail_code="COMMAND_TIMEOUT",
                        stdout=_redact(exc.output, root=self.root),
                        stderr=_redact(exc.stderr, root=self.root),
                    )
                )
                after_error = self._candidate_state_error(
                    expected_commit=commit,
                    environment=environment,
                )
                return self._finish(
                    run_id=run_id,
                    passed=False,
                    detail_code=after_error or "COMMAND_TIMEOUT",
                    commit=commit,
                    migration_head=migration_head,
                    started_at=started_at,
                    steps=steps,
                )
            except OutputLimitExceeded as exc:
                if junit_path is not None:
                    junit_path.unlink(missing_ok=True)
                if pytest_evidence_path is not None:
                    pytest_evidence_path.unlink(missing_ok=True)
                steps.append(
                    VerificationStep(
                        name=command.name,
                        argv=resolved_argv,
                        status="failed",
                        returncode=None,
                        duration_seconds=time.monotonic() - started,
                        detail_code="COMMAND_OUTPUT_LIMIT",
                        stdout=_redact(exc.output, root=self.root),
                        stderr=_redact(exc.stderr, root=self.root),
                    )
                )
                after_error = self._candidate_state_error(
                    expected_commit=commit,
                    environment=environment,
                )
                return self._finish(
                    run_id=run_id,
                    passed=False,
                    detail_code=after_error or "COMMAND_OUTPUT_LIMIT",
                    commit=commit,
                    migration_head=migration_head,
                    started_at=started_at,
                    steps=steps,
                )
            except BaseException:
                if junit_path is not None:
                    junit_path.unlink(missing_ok=True)
                if pytest_evidence_path is not None:
                    pytest_evidence_path.unlink(missing_ok=True)
                steps.append(
                    VerificationStep(
                        name=command.name,
                        argv=resolved_argv,
                        status="failed",
                        returncode=None,
                        duration_seconds=time.monotonic() - started,
                        detail_code="COMMAND_RUNNER_ERROR",
                        stdout="",
                        stderr="",
                    )
                )
                after_error = self._candidate_state_error(
                    expected_commit=commit,
                    environment=environment,
                )
                return self._finish(
                    run_id=run_id,
                    passed=False,
                    detail_code=after_error or "COMMAND_RUNNER_ERROR",
                    commit=commit,
                    migration_head=migration_head,
                    started_at=started_at,
                    steps=steps,
                )

            duration = time.monotonic() - started
            stdout = _redact(completed.stdout, root=self.root)
            stderr = _redact(completed.stderr, root=self.root)
            junit: JUnitEvidence | None = None
            junit_error: str | None = None
            pytest_evidence: PytestEvidence | None = None
            pytest_error: str | None = None
            if completed.returncode == 0 and command.expects_tests:
                if junit_path is None:
                    junit_error = "JUNIT_EVIDENCE_MISSING"
                else:
                    junit, junit_error = _junit_evidence(
                        junit_path,
                        command=command,
                    )
                if junit_error is None:
                    if pytest_evidence_path is None:
                        pytest_error = "PYTEST_EVIDENCE_MISSING"
                    else:
                        pytest_evidence, pytest_error = _pytest_evidence(
                            pytest_evidence_path,
                            command=command,
                        )
                if (
                    junit_error is None
                    and pytest_error is None
                    and junit is not None
                    and pytest_evidence is not None
                    and (
                        junit.total != len(pytest_evidence.collected)
                        or junit.skipped != len(pytest_evidence.skipped)
                    )
                ):
                    pytest_error = "PYTEST_JUNIT_MISMATCH"
                if (
                    junit_error is None
                    and pytest_error is None
                    and junit is not None
                    and pytest_evidence is not None
                    and (
                        junit.nodeids != pytest_evidence.collected
                        or junit.skipped_nodeids != pytest_evidence.skipped
                    )
                ):
                    pytest_error = "PYTEST_JUNIT_IDENTITY_MISMATCH"
            if junit_path is not None:
                junit_path.unlink(missing_ok=True)
            if pytest_evidence_path is not None:
                pytest_evidence_path.unlink(missing_ok=True)
            if completed.returncode < 0:
                status = "signaled"
                detail_code = "COMMAND_SIGNAL"
            elif completed.returncode != 0:
                status = "failed"
                detail_code = "COMMAND_FAILED"
            elif junit_error is not None:
                status = "failed"
                detail_code = junit_error
            elif pytest_error is not None:
                status = "failed"
                detail_code = pytest_error
            else:
                status = "passed"
                detail_code = "PASS"
            steps.append(
                VerificationStep(
                    name=command.name,
                    argv=resolved_argv,
                    status=status,
                    returncode=completed.returncode,
                    duration_seconds=duration,
                    detail_code=detail_code,
                    stdout=stdout,
                    stderr=stderr,
                    tests_total=junit.total if junit is not None else None,
                    tests_failures=(
                        junit.failures if junit is not None else None
                    ),
                    tests_errors=junit.errors if junit is not None else None,
                    tests_skipped=(
                        junit.skipped if junit is not None else None
                    ),
                )
            )
            after_error = self._candidate_state_error(
                expected_commit=commit,
                environment=environment,
            )
            if after_error is not None:
                return self._finish(
                    run_id=run_id,
                    passed=False,
                    detail_code=after_error,
                    commit=commit,
                    migration_head=migration_head,
                    started_at=started_at,
                    steps=steps,
                )
            if status != "passed":
                return self._finish(
                    run_id=run_id,
                    passed=False,
                    detail_code=detail_code,
                    commit=commit,
                    migration_head=migration_head,
                    started_at=started_at,
                    steps=steps,
                )

        final_commit, final_repository_error = _repository_state(
            self.root,
            git_path=toolchain.git.path,
            environment=environment,
        )
        if final_repository_error is not None:
            return self._finish(
                run_id=run_id,
                passed=False,
                detail_code=final_repository_error,
                commit=commit,
                migration_head=migration_head,
                started_at=started_at,
                steps=steps,
            )
        if final_commit != commit:
            return self._finish(
                run_id=run_id,
                passed=False,
                detail_code="REPOSITORY_CHANGED_DURING_VERIFY",
                commit=commit,
                migration_head=migration_head,
                started_at=started_at,
                steps=steps,
            )
        return self._finish(
            run_id=run_id,
            passed=True,
            detail_code="PASS",
            commit=commit,
            migration_head=migration_head,
            started_at=started_at,
            steps=steps,
        )


def verify_release(
    *,
    root: Path = DEFAULT_ROOT,
    runner: Runner | None = None,
    output_dir: Path | None = None,
    commands: tuple[Command, ...] | None = None,
    toolchain: Toolchain | None = None,
    trusted_ancestry_anchor: str | None = None,
) -> VerificationResult:
    """Public orchestration boundary that invalidates PASS before construction."""

    candidate_root = Path(os.path.abspath(root))
    destination = (
        output_dir
        if output_dir is not None
        else candidate_root / DEFAULT_OUTPUT_RELATIVE
    )
    _write_preflight(
        run_id=uuid.uuid4().hex,
        started_at=_utc_now(),
        output_dir=destination,
        root=candidate_root,
        tools=toolchain.identities() if toolchain is not None else (),
        detail_code="PREFLIGHT_CONSTRUCTION",
    )
    verifier = ReleaseVerifier(
        root=candidate_root,
        runner=runner,
        output_dir=destination,
        commands=commands,
        toolchain=toolchain,
        trusted_ancestry_anchor=trusted_ancestry_anchor,
    )
    return verifier.run()


def main() -> int:
    try:
        result = verify_release()
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
