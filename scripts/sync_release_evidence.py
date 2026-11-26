"""Operator-only GitHub Actions transport using managed Composio authentication.

No trading credentials, application bootstrap, GitHub token environment variable,
or cross-provider fallback is used here. Signed download URLs are never logged.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import resource
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from trading_assistant.ops.release_evidence import (
    EvidenceError,
    MAX_ARCHIVE_BYTES,
    clean_commit,
    decode_report,
    import_actions_report,
    publish_report,
    read_report,
    read_report_bytes,
    validate_report,
)
from trading_assistant.installation import require_designated

_MAX_TOOL_BYTES = 8 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise EvidenceError("release_download_redirect")


class ComposioActionsClient:
    def __init__(
        self, repository, *, executable=None, runner=subprocess.run, open_url=None
    ):
        if (
            not isinstance(repository, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None
        ):
            raise EvidenceError("release_repository_invalid")
        self.owner, self.repo = repository.split("/")
        self.runner = runner
        if executable is None:
            found = shutil.which("composio")
            if not found:
                raise EvidenceError("release_composio_missing")
            path = Path(found).resolve(strict=True)
            info = path.stat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid not in (0, os.getuid())
                or info.st_mode & 0o022
            ):
                raise EvidenceError("release_composio_unsafe")
            executable = str(path)
        self.executable = executable
        self.open_url = open_url or build_opener(ProxyHandler({}), _NoRedirect()).open
        self._invoke(["search", "github workflow runs jobs artifacts download"])

    def _invoke(self, args):
        try:

            def limit_files():
                _soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
                limit = (
                    _MAX_TOOL_BYTES
                    if hard == resource.RLIM_INFINITY
                    else min(_MAX_TOOL_BYTES, hard)
                )
                resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                completed = self.runner(
                    [self.executable, *args],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=errors,
                    preexec_fn=limit_files,
                    timeout=45,
                    env={
                        "HOME": str(Path.home()),
                        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                        "LANG": "en_US.UTF-8",
                    },
                )
                if completed.returncode != 0:
                    raise EvidenceError("release_managed_auth_unavailable")
                if any(
                    os.fstat(h.fileno()).st_size >= _MAX_TOOL_BYTES
                    for h in (output, errors)
                ):
                    raise EvidenceError("release_tool_output_size")
                output.seek(0)
                result = json.loads(output.read(_MAX_TOOL_BYTES + 1))
            if not isinstance(result, dict):
                raise EvidenceError("release_tool_output_invalid")
            if result.get("storedInFile") is True:
                path = Path(result.get("outputFilePath", ""))
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as handle:
                    info = os.fstat(handle.fileno())
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.getuid()
                        or info.st_nlink != 1
                    ):
                        raise EvidenceError("release_tool_file_unsafe")
                    raw = handle.read(_MAX_TOOL_BYTES + 1)
                if len(raw) > _MAX_TOOL_BYTES:
                    raise EvidenceError("release_tool_output_size")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise EvidenceError("release_tool_output_invalid")
            return result
        except (
            OSError,
            subprocess.SubprocessError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
        ):
            raise EvidenceError("release_managed_auth_unavailable") from None

    def _execute(self, tool, **params):
        result = self._invoke(
            [
                "execute",
                tool,
                "-d",
                json.dumps(dict(owner=self.owner, repo=self.repo, **params)),
            ]
        )
        if result.get("successful") is not True or not isinstance(
            result.get("data"), dict
        ):
            raise EvidenceError("release_github_request_failed")
        return result["data"]

    def run(self, run_id):
        return self._execute("GITHUB_GET_A_WORKFLOW_RUN", run_id=run_id)

    def jobs(self, run_id):
        data = self._execute(
            "GITHUB_LIST_JOBS_FOR_A_WORKFLOW_RUN", run_id=run_id, per_page=100
        )
        if data.get("total_count") != len(data.get("jobs", [])):
            raise EvidenceError("release_github_jobs_incomplete")
        return data.get("jobs")

    def artifacts(self, run_id):
        data = self._execute(
            "GITHUB_LIST_WORKFLOW_RUN_ARTIFACTS", run_id=run_id, per_page=100
        )
        if data.get("total_count") != len(data.get("artifacts", [])):
            raise EvidenceError("release_github_artifacts_incomplete")
        return data.get("artifacts")

    def latest_run(self, commit):
        data = self._execute(
            "GITHUB_LIST_WORKFLOW_RUNS_FOR_A_REPOSITORY", head_sha=commit, per_page=10
        )
        runs = data.get("workflow_runs")
        if not isinstance(runs, list):
            raise EvidenceError("release_github_runs_invalid")
        candidates = [
            r
            for r in runs
            if isinstance(r, dict)
            and r.get("path") == ".github/workflows/ci.yml"
            and r.get("head_sha") == commit
        ]
        if not candidates:
            raise EvidenceError("release_github_run_missing")
        newest = max(candidates, key=lambda r: r.get("id", 0))
        if type(newest.get("id")) is not int:
            raise EvidenceError("release_github_runs_invalid")
        return newest["id"]

    def download(self, artifact_id):
        data = self._execute("GITHUB_DOWNLOAD_AN_ARTIFACT", artifact_id=artifact_id)
        file = data.get("artifact_file")
        if not isinstance(file, dict) or file.get("mimetype") != "application/zip":
            raise EvidenceError("release_download_invalid")
        size = data.get("size_bytes")
        if type(size) is not int or not 0 < size <= MAX_ARCHIVE_BYTES:
            raise EvidenceError("release_download_size")
        url = file.get("s3url")
        if not isinstance(url, str):
            raise EvidenceError("release_download_url")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
            or not parsed.hostname.endswith(".r2.cloudflarestorage.com")
        ):
            raise EvidenceError("release_download_url")
        try:
            with self.open_url(url, timeout=30) as response:
                if response.status != 200:
                    raise EvidenceError("release_download_status")
                raw = response.read(MAX_ARCHIVE_BYTES + 1)
            if len(raw) != size:
                raise EvidenceError("release_download_size")
            return raw
        except (OSError, ValueError):
            raise EvidenceError("release_download_unavailable") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser(
        "sync", help="import actual successful CI evidence for the clean runtime HEAD"
    )
    sync.add_argument("--repository", required=True)
    sync.add_argument("--run-id", type=int)
    local = commands.add_parser(
        "publish-local", help="publish a complete report from a clean same-commit clone"
    )
    local.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    root = Path.cwd().resolve(strict=True)
    try:
        require_designated(root)
        commit = clean_commit(root)
        if args.command == "publish-local":
            raw = read_report_bytes(args.report)
            validate_report(decode_report(raw), expected_commit=commit, root=root)
            path = publish_report(
                raw,
                root=root,
                provenance={"source": "local_clean_clone_verifier"},
            )
        else:
            try:
                if args.run_id is not None:
                    raise EvidenceError("release_explicit_run_requested")
                validate_report(
                    read_report(root / ".local/verification/release-results.json"),
                    expected_commit=commit,
                    root=root,
                )
                print(json.dumps({"status": "current", "commit": commit}))
                return 0
            except EvidenceError:
                pass
            client = ComposioActionsClient(args.repository)
            run_id = args.run_id or client.latest_run(commit)
            path = import_actions_report(
                root=root, repository=args.repository, run_id=run_id, client=client
            )
        print(json.dumps({"status": "published", "commit": commit, "path": str(path)}))
        return 0
    except (EvidenceError, OSError) as error:
        print(
            str(error)
            if isinstance(error, EvidenceError)
            else "release_publication_unavailable",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
