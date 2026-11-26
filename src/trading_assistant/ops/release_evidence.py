"""Validate and atomically publish complete verifier evidence; never trade."""

from __future__ import annotations

import ast
from datetime import datetime
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid
import zipfile

from ..installation import require_designated

MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 3 * 1024 * 1024
STAGES = (
    "compile",
    "migration-tests",
    "security-tests",
    "safety-tests",
    "frontend-tests",
    "full-tests",
    "branch-coverage",
    "static-gate",
)
_MANIFESTS = {
    "migration-tests": "_MIGRATION_TEST_MANIFEST",
    "security-tests": "_SECURITY_TEST_MANIFEST",
    "safety-tests": "_SAFETY_TEST_MANIFEST",
    "frontend-tests": "_FRONTEND_TEST_MANIFEST",
    "full-tests": "_FULL_TEST_MANIFEST",
    "branch-coverage": "_FULL_TEST_MANIFEST",
}
_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class EvidenceError(ValueError):
    """A stable, value-free reason evidence cannot authorize a release."""


def _require(condition, code):
    if not condition:
        raise EvidenceError(code)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "release_duplicate_json_key")
        result[key] = value
    return result


def decode_report(raw: bytes) -> dict:
    _require(
        type(raw) is bytes and 0 < len(raw) <= MAX_REPORT_BYTES, "release_report_size"
    )
    try:

        def finite_float(text):
            value = float(text)
            _require(math.isfinite(value), "release_nonfinite_json")
            return value

        result = json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_float=finite_float,
            parse_constant=lambda _: (_ for _ in ()).throw(
                EvidenceError("release_nonfinite_json")
            ),
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, EvidenceError):
            raise
        raise EvidenceError("release_report_json") from None
    _require(type(result) is dict, "release_report_object")
    return result


def _contract(root: Path):
    """Read literal policy from the verifier without importing candidate code."""
    try:
        path = root / "scripts/verify_loopback_release.py"
        _require(
            path.is_file() and not path.is_symlink(), "release_contract_unavailable"
        )
        source = path.read_bytes()
        _require(len(source) <= 256 * 1024, "release_contract_unavailable")
        tree = ast.parse(source)
        counts = {}
        head = None
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                if target.id == "EXPECTED_MIGRATION_HEAD":
                    head = ast.literal_eval(node.value)
                if target.id in _MANIFESTS.values() and isinstance(
                    node.value, ast.Call
                ):
                    _require(
                        isinstance(node.value.func, ast.Name)
                        and node.value.func.id == "TestManifest",
                        "release_contract_invalid",
                    )
                    counts[target.id] = next(
                        ast.literal_eval(k.value)
                        for k in node.value.keywords
                        if k.arg == "count"
                    )
        _require(
            type(head) is str and head and set(counts) == set(_MANIFESTS.values()),
            "release_contract_invalid",
        )
        _require(
            all(type(n) is int and n > 0 for n in counts.values()),
            "release_contract_invalid",
        )
        return head, {stage: counts[name] for stage, name in _MANIFESTS.items()}
    except (OSError, SyntaxError, StopIteration, TypeError, ValueError) as error:
        if isinstance(error, EvidenceError):
            raise
        raise EvidenceError("release_contract_unavailable") from None


def validate_report(payload, *, expected_commit: str, root: Path) -> dict:
    _require(type(payload) is dict, "release_report_object")
    _require(
        type(expected_commit) is str and _SHA.fullmatch(expected_commit),
        "release_commit_invalid",
    )
    _require(
        type(payload.get("schema_version")) is int and payload["schema_version"] == 2,
        "release_schema_invalid",
    )
    _require(
        payload.get("passed") is True
        and payload.get("state") == "completed"
        and payload.get("detail_code") == "PASS",
        "release_not_passed",
    )
    _require(payload.get("commit") == expected_commit, "release_commit_mismatch")
    head, counts = _contract(Path(root))
    _require(payload.get("migration_head") == head, "release_migration_mismatch")
    _require(
        type(payload.get("run_id")) is str
        and re.fullmatch(r"[0-9a-f]{32}", payload["run_id"]),
        "release_run_invalid",
    )
    try:
        times = [
            datetime.fromisoformat(payload[k].replace("Z", "+00:00"))
            for k in ("started_at", "finished_at")
        ]
        _require(
            all(t.tzinfo is not None for t in times) and times[1] >= times[0],
            "release_times_invalid",
        )
    except (KeyError, AttributeError, TypeError, ValueError):
        raise EvidenceError("release_times_invalid") from None
    _require(
        payload.get("tool_isolation") == "canonical_fingerprint",
        "release_tools_unproven",
    )
    tools = payload.get("tools")
    _require(
        type(tools) is dict and set(tools) == {"git", "node", "uv", "python"},
        "release_tools_unproven",
    )
    for tool in tools.values():
        _require(
            type(tool) is dict
            and type(tool.get("fingerprint")) is str
            and _DIGEST.fullmatch(tool["fingerprint"])
            and all(type(tool.get(k)) is str and tool[k] for k in ("path", "version")),
            "release_tools_unproven",
        )
    network = payload.get("network_isolation")
    _require(
        type(network) is dict
        and network
        == dict(
            boundary="python_runtime_only",
            hostile_local_process_boundary="external_clean_ci_required",
            kind="python_socket_guard",
            os_enforced=False,
            status="verified",
        ),
        "release_network_unproven",
    )
    _require(network["os_enforced"] is False, "release_network_unproven")
    steps = payload.get("steps")
    _require(
        type(steps) is list
        and all(type(s) is dict for s in steps)
        and tuple(s.get("name") for s in steps) == STAGES,
        "release_stages_invalid",
    )
    for step in steps:
        _require(
            step.get("status") == "passed"
            and step.get("detail_code") == "PASS"
            and type(step.get("returncode")) is int
            and step["returncode"] == 0,
            "release_stage_failed",
        )
        duration = step.get("duration_seconds")
        _require(
            type(duration) in (int, float)
            and 0 <= duration <= 86400
            and math.isfinite(duration),
            "release_duration_invalid",
        )
        _require(
            type(step.get("argv")) is list
            and step["argv"]
            and all(type(a) is str and a for a in step["argv"]),
            "release_command_invalid",
        )
        _require(
            all(type(step.get(k)) is str for k in ("stdout", "stderr")),
            "release_output_invalid",
        )
        if step["name"] in counts:
            tests = step.get("tests")
            _require(
                type(tests) is dict
                and all(
                    type(tests.get(k)) is int and tests[k] >= 0
                    for k in ("total", "errors", "failures", "skipped")
                ),
                "release_tests_invalid",
            )
            _require(
                tests["total"] == counts[step["name"]]
                and tests["failures"] == tests["errors"] == 0
                and tests["skipped"] <= tests["total"],
                "release_tests_incomplete",
            )
            _require(
                tests["skipped"] == 0
                or (
                    step["name"] in {"full-tests", "branch-coverage"}
                    and tests["skipped"] <= 2
                ),
                "release_tests_incomplete",
            )
    return payload


def read_report_bytes(path: Path) -> bytes:
    """Read one bounded private regular file without following its symlink."""
    try:
        path = Path(os.path.abspath(path))
        _require(
            path.parent.resolve(strict=True) == path.parent, "release_parent_unsafe"
        )
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            _require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and info.st_nlink == 1
                and not info.st_mode & 0o077,
                "release_file_unsafe",
            )
            raw = handle.read(MAX_REPORT_BYTES + 1)
            _require(0 < len(raw) <= MAX_REPORT_BYTES, "release_report_size")
            return raw
    except OSError:
        raise EvidenceError("release_file_unavailable") from None


def read_report(path: Path) -> dict:
    return decode_report(read_report_bytes(path))


def clean_commit(root: Path) -> str:
    try:

        def git(*args):
            return subprocess.check_output(
                [
                    "/usr/bin/git",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "core.untrackedCache=false",
                    "-C",
                    str(root),
                    *args,
                ],
                timeout=15,
                stderr=subprocess.DEVNULL,
                text=True,
                env={
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C",
                    "LC_ALL": "C",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_SYSTEM": "/dev/null",
                    "GIT_OPTIONAL_LOCKS": "0",
                    "GIT_TERMINAL_PROMPT": "0",
                },
            ).strip()

        _require(
            Path(git("rev-parse", "--show-toplevel")) == root,
            "release_checkout_invalid",
        )
        commit = git("rev-parse", "HEAD")
        _require(_SHA.fullmatch(commit), "release_commit_invalid")
        _require(
            not git("status", "--porcelain", "--untracked-files=normal"),
            "release_checkout_dirty",
        )
        return commit
    except (OSError, subprocess.SubprocessError):
        raise EvidenceError("release_checkout_unavailable") from None


def _private_directory(root: Path) -> tuple[Path, int]:
    _require(root.is_dir() and root.resolve(strict=True) == root, "release_root_unsafe")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    path = root
    try:
        for component in (".local", "verification"):
            try:
                os.mkdir(component, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            successor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = successor
            path /= component
            info = os.fstat(descriptor)
            _require(
                info.st_uid == os.getuid() and not info.st_mode & 0o077,
                "release_directory_unsafe",
            )
        return path, descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _safe_existing(directory: int, name: str):
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    _require(
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and info.st_nlink == 1
        and not info.st_mode & 0o077,
        "release_destination_unsafe",
    )


def publish_report(raw: bytes, *, root: Path, provenance: dict | None = None) -> Path:
    root = Path(os.path.abspath(root))
    require_designated(root)
    commit = clean_commit(root)
    validate_report(decode_report(raw), expected_commit=commit, root=root)
    parent, directory = _private_directory(root)
    lock = None
    temporary = ".publish-" + uuid.uuid4().hex + ".tmp"
    try:
        lock = os.open(
            ".publish.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
        info = os.fstat(lock)
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and info.st_nlink == 1
            and not info.st_mode & 0o077,
            "release_lock_unsafe",
        )
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _safe_existing(directory, "release-results.json")
        if provenance is not None:
            digest = hashlib.sha256(raw).hexdigest()
            receipt = json.dumps(
                dict(provenance, report_sha256=digest), sort_keys=True, allow_nan=False
            ).encode()
            name = f"provenance-{digest}.json"
            _safe_existing(directory, name)
            try:
                existing = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
            except FileNotFoundError:
                stage = ".provenance-" + uuid.uuid4().hex + ".tmp"
                try:
                    fd = os.open(
                        stage,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory,
                    )
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(receipt)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.link(
                        stage,
                        name,
                        src_dir_fd=directory,
                        dst_dir_fd=directory,
                        follow_symlinks=False,
                    )
                finally:
                    try:
                        os.unlink(stage, dir_fd=directory)
                    except FileNotFoundError:
                        pass
            else:
                with os.fdopen(existing, "rb") as handle:
                    prior = decode_report(handle.read(MAX_REPORT_BYTES + 1))
                _require(
                    prior.get("report_sha256") == digest, "release_provenance_invalid"
                )
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _require(clean_commit(root) == commit, "release_checkout_changed")
        current = parent.lstat()
        opened = os.fstat(directory)
        _require(
            stat.S_ISDIR(current.st_mode)
            and (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino),
            "release_directory_changed",
        )
        _safe_existing(directory, "release-results.json")
        os.fsync(directory)
        os.replace(
            temporary,
            "release-results.json",
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
        return parent / "release-results.json"
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        if lock is not None:
            os.close(lock)
        os.close(directory)


def unpack_report(raw: bytes, *, digest: str) -> bytes:
    _require(
        type(raw) is bytes and len(raw) <= MAX_ARCHIVE_BYTES, "release_archive_size"
    )
    _require(
        type(digest) is str
        and _DIGEST.fullmatch(digest)
        and digest == "sha256:" + hashlib.sha256(raw).hexdigest(),
        "release_archive_digest",
    )
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            _require(len(entries) == 1, "release_archive_members")
            entry = entries[0]
            mode = entry.external_attr >> 16
            _require(
                entry.filename == "release-results.json"
                and not entry.is_dir()
                and not entry.flag_bits & 1
                and not stat.S_ISLNK(mode)
                and (stat.S_IFMT(mode) in (0, stat.S_IFREG))
                and 0 < entry.file_size <= MAX_REPORT_BYTES,
                "release_archive_member",
            )
            with archive.open(entry) as handle:
                result = handle.read(MAX_REPORT_BYTES + 1)
            _require(len(result) == entry.file_size, "release_archive_size")
            return result
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        raise EvidenceError("release_archive_invalid") from None


def import_actions_report(*, root: Path, repository: str, run_id: int, client) -> Path:
    """Import an actual artifact only after authenticated run/job verification."""
    _require(
        type(repository) is str
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository),
        "release_repository_invalid",
    )
    _require(type(run_id) is int and run_id > 0, "release_github_run_invalid")
    root = Path(os.path.abspath(root))
    require_designated(root)
    commit = clean_commit(root)
    metadata = client.run(run_id)

    def trusted_run(run):
        _require(
            type(run) is dict
            and type(run.get("id")) is int
            and run.get("id") == run_id
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
            and run.get("head_sha") == commit
            and run.get("path") == ".github/workflows/ci.yml"
            and type(run.get("repository")) is dict
            and run["repository"].get("full_name") == repository
            and type(run.get("head_repository")) is dict
            and run["head_repository"].get("full_name") == repository
            and type(run.get("run_attempt")) is int
            and run["run_attempt"] > 0,
            "release_github_run_untrusted",
        )

    trusted_run(metadata)
    attempt = metadata["run_attempt"]
    jobs = client.jobs(run_id)
    _require(
        type(jobs) is list
        and len(jobs) == 2
        and {j.get("name") for j in jobs if type(j) is dict}
        == {"verification", "secret-scan"},
        "release_github_jobs_invalid",
    )
    _require(
        all(
            type(j.get("run_id")) is int
            and j["run_id"] == run_id
            and type(j.get("run_attempt")) is int
            and j["run_attempt"] == attempt
            and j.get("head_sha") == commit
            and j.get("status") == "completed"
            and j.get("conclusion") == "success"
            for j in jobs
        ),
        "release_github_jobs_failed",
    )
    verification = next(j for j in jobs if j["name"] == "verification")
    steps = verification.get("steps")
    _require(
        type(steps) is list and all(type(s) is dict for s in steps),
        "release_github_steps_invalid",
    )
    for name in (
        "Run the deterministic release verifier",
        "Audit installed dependencies",
        "Apply and verify migrations on an isolated database",
        "Run the isolated mock safety drill",
    ):
        found = [s for s in steps if s.get("name") == name]
        _require(
            len(found) == 1
            and found[0].get("status") == "completed"
            and found[0].get("conclusion") == "success",
            "release_github_steps_failed",
        )
    artifacts = client.artifacts(run_id)
    _require(
        type(artifacts) is list and all(type(a) is dict for a in artifacts),
        "release_github_artifacts_invalid",
    )
    selected = [
        a
        for a in artifacts
        if a.get("name") == f"release-evidence-{commit}-{run_id}-{attempt}"
    ]
    _require(len(selected) == 1, "release_github_artifact_missing")
    artifact = selected[0]
    origin = artifact.get("workflow_run")
    _require(
        artifact.get("expired") is False
        and type(artifact.get("id")) is int
        and artifact["id"] > 0
        and type(artifact.get("size_in_bytes")) is int
        and 0 < artifact["size_in_bytes"] <= MAX_ARCHIVE_BYTES
        and type(origin) is dict
        and type(origin.get("id")) is int
        and origin.get("id") == run_id
        and origin.get("head_sha") == commit,
        "release_github_artifact_untrusted",
    )
    raw = client.download(artifact["id"])
    _require(
        type(raw) is bytes and len(raw) == artifact["size_in_bytes"],
        "release_github_artifact_size",
    )
    report = unpack_report(raw, digest=artifact.get("digest"))
    latest = client.run(run_id)
    trusted_run(latest)
    _require(latest["run_attempt"] == attempt, "release_github_attempt_changed")
    return publish_report(
        report,
        root=root,
        provenance=dict(
            source="github_actions_artifact",
            repository=repository,
            github_run_id=run_id,
            github_run_attempt=attempt,
            artifact_id=artifact["id"],
            artifact_digest=artifact["digest"],
            workflow_path=".github/workflows/ci.yml",
            run_url=f"https://github.com/{repository}/actions/runs/{run_id}",
        ),
    )
