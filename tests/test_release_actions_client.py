"""Managed-auth transport tests; no live service calls or credentials."""

import io
import json
from types import SimpleNamespace

import pytest


def tool_result(kwargs, raw):
    kwargs["stdout"].write(raw.encode())
    return SimpleNamespace(returncode=0)


def test_managed_subprocess_uses_bounded_private_spools():
    from tests.release_actions_fixture import ComposioActionsClient
    import os, stat

    def runner(args, **kwargs):
        assert "capture_output" not in kwargs
        assert kwargs["stdin"] is not None
        assert callable(kwargs["preexec_fn"])
        for name in ("stdout", "stderr"):
            info = os.fstat(kwargs[name].fileno())
            assert stat.S_ISREG(info.st_mode)
            assert info.st_mode & 0o077 == 0
        kwargs["stdout"].write(b'{"successful":true,"data":{}}')
        return SimpleNamespace(returncode=0)

    ComposioActionsClient("owner/repo", executable="/synthetic/composio", runner=runner)


def test_managed_client_resolves_tools_then_returns_authenticated_metadata():
    from tests.release_actions_fixture import ComposioActionsClient

    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        data = {} if "search" in args else {"id": 12}
        return tool_result(kwargs, json.dumps({"successful": True, "data": data}))

    client = ComposioActionsClient(
        "owner/repo", executable="/synthetic/composio", runner=runner
    )
    assert client.run(12) == {"id": 12}
    assert calls[0][1] == "search"
    assert calls[1][1:3] == ["execute", "GITHUB_GET_A_WORKFLOW_RUN"]
    assert "12" in calls[1][-1]


@pytest.mark.parametrize(
    "problem",
    [
        "failed",
        "bad_json",
        "redirect",
        "http",
        "user_info",
        "foreign_host",
        "too_big",
        "wrong_size",
    ],
)
def test_download_transport_fails_closed(problem):
    from tests.release_actions_fixture import ComposioActionsClient
    from trading_assistant.ops.release_evidence import EvidenceError

    url = "https://temp.account.r2.cloudflarestorage.com/artifact?opaque=test-only"
    if problem == "http":
        url = url.replace("https:", "http:")
    if problem == "user_info":
        url = url.replace("https://", "https://user:password@")
    if problem == "foreign_host":
        url = "https://attacker.invalid/artifact"
    payload = dict(
        artifact_file=dict(mimetype="application/zip", s3url=url), size_bytes=3
    )
    if problem == "too_big":
        payload["size_bytes"] = 4 * 1024 * 1024
    if problem == "wrong_size":
        payload["size_bytes"] = 2

    def runner(args, **kwargs):
        raw = (
            "invalid"
            if problem == "bad_json"
            else json.dumps(dict(successful=problem != "failed", data=payload))
        )
        return tool_result(kwargs, raw)

    class Download(io.BytesIO):
        status = 302 if problem == "redirect" else 200

    def open_url(_url, timeout):
        return Download(b"zip")

    with pytest.raises(EvidenceError):
        client = ComposioActionsClient(
            "owner/repo",
            executable="/synthetic/composio",
            runner=runner,
            open_url=open_url,
        )
        client.download(9)


def test_download_bytes_are_bounded_and_signed_url_is_not_rewritten():
    from tests.release_actions_fixture import ComposioActionsClient

    url = "https://temp.account.r2.cloudflarestorage.com/artifact?opaque=test-only"

    def runner(args, **kwargs):
        return tool_result(
            kwargs,
            json.dumps(
                dict(
                    successful=True,
                    data=dict(
                        artifact_file=dict(mimetype="application/zip", s3url=url),
                        size_bytes=3,
                    ),
                )
            ),
        )

    class Download(io.BytesIO):
        status = 200

    seen = []

    def open_url(value, timeout):
        seen.append(value)
        return Download(b"zip")

    client = ComposioActionsClient(
        "owner/repo", executable="/synthetic/composio", runner=runner, open_url=open_url
    )
    assert client.download(9) == b"zip"
    assert seen == [url]


def test_local_publication_uses_only_one_bounded_report_read(tmp_path, monkeypatch):
    from pathlib import Path
    import subprocess
    from tests.release_actions_fixture import module
    from tests.release_report_fixture import install_contract, report_fixture
    from trading_assistant.ops import release_evidence

    root = tmp_path / "runtime"
    root.mkdir()
    install_contract(root)
    (root / ".gitignore").write_text(".local/\n")
    subprocess.run(["/usr/bin/git", "init", "-q", str(root)], check=True)
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
    raw = json.dumps(report_fixture(sha)).encode()
    source = tmp_path / "report.json"
    source.write_bytes(raw)
    source.chmod(0o600)
    original = Path.read_bytes

    def reject_unbounded_source_read(path):
        if path == source:
            raise AssertionError("report source reopened with an unbounded read")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", reject_unbounded_source_read)
    monkeypatch.setattr(module, "require_designated", lambda path: path)
    monkeypatch.setattr(release_evidence, "require_designated", lambda path: path)
    monkeypatch.chdir(root)
    assert module.main(["publish-local", "--report", str(source)]) == 0
    assert (root / ".local/verification/release-results.json").read_bytes() == raw
