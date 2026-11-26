"""Canonical evidence and transactional publication, using synthetic reports."""

from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from tests.release_report_fixture import install_contract, report_fixture, STAGES


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(schema_version=1),
        lambda p: p.update(state="in_progress"),
        lambda p: p.update(passed=1),
        lambda p: p.update(commit="b" * 40),
        lambda p: p.update(migration_head="wrong"),
        lambda p: p["steps"].pop(),
        lambda p: p["steps"].append(deepcopy(p["steps"][0])),
        lambda p: p["steps"][0].update(returncode=True),
        lambda p: p["steps"][0].update(status="failed"),
        lambda p: p["steps"][1]["tests"].update(total=1),
        lambda p: p["steps"][1]["tests"].update(failures=1),
        lambda p: p["steps"][1]["tests"].update(skipped=True),
        lambda p: p["network_isolation"].update(status="unproven"),
        lambda p: p["network_isolation"].update(os_enforced=True),
        lambda p: p["tools"].pop("git"),
        lambda p: p.update(finished_at="2026-09-30T00:00:00Z"),
    ],
)
def test_invalid_report_is_rejected(tmp_path, mutation):
    from trading_assistant.ops.release_evidence import validate_report, EvidenceError

    install_contract(tmp_path)
    payload = report_fixture()
    mutation(payload)
    with pytest.raises(EvidenceError):
        validate_report(payload, expected_commit="a" * 40, root=tmp_path)


def test_complete_report_is_accepted_and_nonobjects_rejected(tmp_path):
    from trading_assistant.ops.release_evidence import validate_report, EvidenceError

    install_contract(tmp_path)
    assert (
        validate_report(report_fixture(), expected_commit="a" * 40, root=tmp_path)[
            "passed"
        ]
        is True
    )
    for payload in ([], True, None, "passed", {"passed": True, "commit": "a" * 40}):
        with pytest.raises(EvidenceError):
            validate_report(payload, expected_commit="a" * 40, root=tmp_path)


def test_duplicate_json_keys_and_nonfinite_numbers_are_rejected():
    from trading_assistant.ops.release_evidence import decode_report, EvidenceError

    for raw in (b'{"passed":false,"passed":true}', b'{"x":NaN}', b'{"x":1e999}', b"[]"):
        with pytest.raises(EvidenceError):
            decode_report(raw)


def test_reader_rejects_a_symlinked_parent(tmp_path):
    from trading_assistant.ops.release_evidence import read_report, EvidenceError

    original = tmp_path / "original"
    original.mkdir(mode=0o700)
    path = original / "report.json"
    path.write_text("{}")
    path.chmod(0o600)
    linked = tmp_path / "linked"
    linked.symlink_to(original, target_is_directory=True)
    with pytest.raises(EvidenceError):
        read_report(linked / "report.json")


@pytest.mark.parametrize("reader", ["report", "managed_output"])
def test_nonregular_evidence_is_rejected_without_waiting_for_a_writer(tmp_path, reader):
    fifo = tmp_path / "untrusted.fifo"
    os.mkfifo(fifo, 0o600)
    program = """
import json,sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path.cwd()/'src'))
sys.path.insert(0,str(Path.cwd()))
from trading_assistant.ops.release_evidence import read_report,EvidenceError
try:
    if sys.argv[2]=='report':
        read_report(Path(sys.argv[1]))
    else:
        from tests.release_actions_fixture import ComposioActionsClient
        def runner(args,**kwargs):
            kwargs['stdout'].write(json.dumps({'storedInFile':True,'outputFilePath':sys.argv[1]}).encode())
            return SimpleNamespace(returncode=0)
        ComposioActionsClient('owner/repo',executable='/synthetic/client',runner=runner)
except EvidenceError:
    sys.exit(0)
sys.exit(2)
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(fifo), reader],
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    from trading_assistant.ops import release_evidence

    root = tmp_path / "runtime"
    root.mkdir()
    subprocess.run(["/usr/bin/git", "init", "-q", str(root)], check=True)
    install_contract(root)
    (root / ".gitignore").write_text(".local/\n.env\n")
    subprocess.run(["/usr/bin/git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    sha = subprocess.check_output(
        ["/usr/bin/git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    monkeypatch.setattr(release_evidence, "require_designated", lambda p: p)
    return root, sha


def test_publication_is_private_and_preserves_canonical_bytes(checkout):
    from trading_assistant.ops.release_evidence import publish_report

    root, sha = checkout
    raw = json.dumps(report_fixture(sha)).encode()
    (root / ".env").write_text("ignored fixture, no credentials")
    path = publish_report(raw, root=root, provenance={"source": "test-only"})
    assert path.read_bytes() == raw
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    receipt = json.loads(
        (path.parent / f"provenance-{hashlib.sha256(raw).hexdigest()}.json").read_text()
    )
    assert receipt["report_sha256"] == hashlib.sha256(raw).hexdigest()


def test_checkout_identity_ignores_inherited_git_controls(checkout, monkeypatch):
    from trading_assistant.ops.release_evidence import clean_commit

    root, sha = checkout
    monkeypatch.setenv("GIT_DIR", str(root / "does-not-exist"))
    monkeypatch.setenv("GIT_WORK_TREE", str(root.parent))
    assert clean_commit(root) == sha


def test_checkout_inspection_does_not_execute_fsmonitor(checkout):
    from trading_assistant.ops.release_evidence import clean_commit

    root, sha = checkout
    marker = root.parent / "executed-marker"
    script = root.parent / "fsmonitor.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    script.chmod(0o700)
    subprocess.run(
        ["/usr/bin/git", "-C", str(root), "config", "core.fsmonitor", str(script)],
        check=True,
    )
    assert clean_commit(root) == sha
    assert not marker.exists()


@pytest.mark.parametrize(
    "failure",
    [
        "dirty",
        "untracked",
        "head_changed",
        "replace_failed",
        "symlink",
        "parent_symlink",
    ],
)
def test_failed_publication_preserves_previous_evidence(checkout, monkeypatch, failure):
    from trading_assistant.ops import release_evidence as m

    root, sha = checkout
    path = root / ".local/verification/release-results.json"
    path.parent.mkdir(mode=0o700, parents=True)
    (root / ".local").chmod(0o700)
    path.write_bytes(b"previous evidence")
    path.chmod(0o600)
    raw = json.dumps(report_fixture(sha)).encode()
    if failure == "dirty":
        (root / ".gitignore").write_text("changed")
    if failure == "untracked":
        (root / "unexpected.py").write_text("pass")
    if failure == "head_changed":
        original = m.clean_commit
        calls = 0

        def changed(p):
            nonlocal calls
            calls += 1
            return original(p) if calls == 1 else "f" * 40

        monkeypatch.setattr(m, "clean_commit", changed)
    if failure == "replace_failed":
        monkeypatch.setattr(
            m.os,
            "replace",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("test interruption")),
        )
    if failure == "symlink":
        old = path.with_name("original.json")
        path.rename(old)
        path.symlink_to(old)
    if failure == "parent_symlink":
        parent = path.parent
        other = parent.with_name("other")
        parent.rename(other)
        parent.symlink_to(other)
    with pytest.raises((m.EvidenceError, OSError)):
        m.publish_report(raw, root=root)
    assert path.read_bytes() == b"previous evidence"


def archive(raw, name="release-results.json"):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(name, raw)
    return out.getvalue()


@pytest.mark.parametrize(
    "kind", ["traversal", "extra", "duplicate", "symlink", "bad_digest", "oversize"]
)
def test_untrusted_archives_are_rejected(kind):
    from trading_assistant.ops.release_evidence import unpack_report, EvidenceError

    raw = archive(
        b"{}",
        "../release-results.json" if kind == "traversal" else "release-results.json",
    )
    if kind in {"extra", "duplicate", "symlink"}:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("release-results.json", b"{}")
            if kind == "symlink":
                info = zipfile.ZipInfo("link")
                info.create_system = 3
                info.external_attr = 0o120777 << 16
                z.writestr(info, b"target")
            else:
                z.writestr(
                    "extra" if kind == "extra" else "release-results.json", b"{}"
                )
        raw = out.getvalue()
    if kind == "oversize":
        raw = archive(b"x" * (2 * 1024 * 1024 + 1))
    digest = "sha256:" + (
        "0" * 64 if kind == "bad_digest" else hashlib.sha256(raw).hexdigest()
    )
    with pytest.raises(EvidenceError):
        unpack_report(raw, digest=digest)


def test_archive_validates_hash_without_extracting_files():
    from trading_assistant.ops.release_evidence import unpack_report

    raw = archive(b'{"synthetic":true}')
    assert (
        unpack_report(raw, digest="sha256:" + hashlib.sha256(raw).hexdigest())
        == b'{"synthetic":true}'
    )


def test_corrupt_provenance_cannot_be_committed_with_a_report(checkout):
    from trading_assistant.ops import release_evidence as m

    root, sha = checkout
    raw = json.dumps(report_fixture(sha)).encode()
    directory = root / ".local/verification"
    directory.mkdir(parents=True, mode=0o700)
    (root / ".local").chmod(0o700)
    sidecar = directory / f"provenance-{hashlib.sha256(raw).hexdigest()}.json"
    sidecar.write_bytes(b"interrupted sidecar")
    sidecar.chmod(0o600)
    with pytest.raises(m.EvidenceError):
        m.publish_report(raw, root=root, provenance={"source": "test-only"})
    assert not (directory / "release-results.json").exists()


class FakeActions:
    def __init__(self, commit):
        self.report = json.dumps(report_fixture(commit)).encode()
        self.zip = archive(self.report)
        self.metadata = dict(
            id=12,
            status="completed",
            conclusion="success",
            head_sha=commit,
            run_attempt=2,
            path=".github/workflows/ci.yml",
            repository={"full_name": "owner/repo"},
            head_repository={"full_name": "owner/repo"},
        )
        self.job_data = [
            dict(
                id=i,
                name=n,
                status="completed",
                conclusion="success",
                run_id=12,
                run_attempt=2,
                head_sha=commit,
                steps=[
                    dict(name=s, status="completed", conclusion="success")
                    for s in (
                        "Run the deterministic release verifier",
                        "Audit installed dependencies",
                        "Apply and verify migrations on an isolated database",
                        "Run the isolated mock safety drill",
                    )
                ],
            )
            for i, n in [(1, "verification"), (2, "secret-scan")]
        ]
        self.artifact_data = [
            dict(
                id=9,
                name=f"release-evidence-{commit}-12-2",
                expired=False,
                digest="sha256:" + hashlib.sha256(self.zip).hexdigest(),
                size_in_bytes=len(self.zip),
                workflow_run={"id": 12, "head_sha": commit},
            )
        ]

    def run(self, run_id):
        return deepcopy(self.metadata)

    def jobs(self, run_id):
        return deepcopy(self.job_data)

    def artifacts(self, run_id):
        return deepcopy(self.artifact_data)

    def download(self, artifact_id):
        return self.zip


def test_authenticated_artifact_import_publishes_the_original_report(checkout):
    from trading_assistant.ops.release_evidence import import_actions_report

    root, sha = checkout
    client = FakeActions(sha)
    path = import_actions_report(
        root=root, repository="owner/repo", run_id=12, client=client
    )
    assert path.read_bytes() == client.report
    sidecars = list(path.parent.glob("provenance-*.json"))
    assert json.loads(sidecars[0].read_text())["github_run_attempt"] == 2


@pytest.mark.parametrize(
    "problem",
    [
        "repository",
        "head",
        "workflow",
        "failure",
        "fork",
        "job",
        "job_head",
        "job_attempt",
        "step",
        "attempt",
        "expired",
        "digest",
        "run_changed",
        "download_failed",
    ],
)
def test_import_rejects_untrusted_metadata_and_preserves_prior_report(
    checkout, problem
):
    from trading_assistant.ops.release_evidence import (
        import_actions_report,
        EvidenceError,
    )

    root, sha = checkout
    client = FakeActions(sha)
    if problem == "repository":
        client.metadata["repository"]["full_name"] = "attacker/repo"
    if problem == "head":
        client.metadata["head_sha"] = "c" * 40
    if problem == "workflow":
        client.metadata["path"] = ".github/workflows/other.yml"
    if problem == "failure":
        client.metadata["conclusion"] = "failure"
    if problem == "fork":
        client.metadata["head_repository"]["full_name"] = "attacker/repo"
    if problem == "job":
        client.job_data[1]["conclusion"] = "failure"
    if problem == "job_head":
        client.job_data[0]["head_sha"] = "f" * 40
    if problem == "job_attempt":
        client.job_data[0]["run_attempt"] = 1
    if problem == "step":
        client.job_data[0]["steps"].pop()
    if problem == "attempt":
        client.metadata["run_attempt"] = 3
    if problem == "expired":
        client.artifact_data[0]["expired"] = True
    if problem == "digest":
        client.artifact_data[0]["digest"] = "sha256:" + "0" * 64
    if problem == "run_changed":
        original = client.download

        def changing(i):
            client.metadata["run_attempt"] = 3
            return original(i)

        client.download = changing
    if problem == "download_failed":
        client.download = lambda _i: (_ for _ in ()).throw(OSError("test outage"))
    path = root / ".local/verification/release-results.json"
    path.parent.mkdir(mode=0o700, parents=True)
    (root / ".local").chmod(0o700)
    path.write_bytes(b"previous evidence")
    path.chmod(0o600)
    with pytest.raises((EvidenceError, OSError)):
        import_actions_report(
            root=root, repository="owner/repo", run_id=12, client=client
        )
    assert path.read_bytes() == b"previous evidence"
