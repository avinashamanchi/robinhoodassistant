from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_LAUNCHER = _ROOT / "scripts/operator.sh"
_CANONICAL_ROOT = "/Users/avi/Desktop/robinhood/trading-assistant"
_CURL = "/usr/bin/curl"
_LIVENESS_URL = "https://localhost:8020/health/live"
_LIVE_PAYLOAD = '{"alive":true,"database_reachable":true}'


def _write_executable(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)


@dataclass(frozen=True)
class LauncherHarness:
    project: Path
    launcher: Path
    python: Path
    ca: Path
    log: Path
    state: Path
    parent: Path

    def run(
        self,
        *,
        cwd: Path | None = None,
        launcher: str = "./scripts/operator.sh",
        logical_cwd: bool = False,
        arguments: tuple[str, ...] = (),
        control: str = "running",
        liveness: str = "valid",
        start: str = "ok",
    ) -> subprocess.CompletedProcess[str]:
        directory = self.project if cwd is None else cwd
        cd_mode = "-L" if logical_cwd else "-P"
        environment = {
            "HARNESS_CONTROL": control,
            "HARNESS_LIVENESS": liveness,
            "HARNESS_LOG": str(self.log),
            "HARNESS_START": start,
            "HARNESS_STATE": str(self.state),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
        }
        return subprocess.run(
            [
                "/bin/bash",
                "-c",
                f'cd {cd_mode} -- "$1" && shift && exec "$@"',
                "operator-launcher-harness",
                str(directory),
                launcher,
                *arguments,
            ],
            cwd=self.parent,
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
def launcher_harness(tmp_path: Path) -> LauncherHarness:
    project = tmp_path / "canonical-project"
    launcher = project / "scripts/operator.sh"
    python = project / ".venv/bin/python"
    ca = project / ".local/tls/rootCA.pem"
    fake_curl = tmp_path / "absolute-curl"
    log = tmp_path / "events.jsonl"
    state = tmp_path / "app-started"

    project.mkdir()
    ca.parent.mkdir(parents=True)
    ca.write_text("test-only CA placeholder\n", encoding="ascii")

    fake_python_source = textwrap.dedent(
        f"""\
        #!{sys.executable}
        import json
        import os
        from pathlib import Path
        import sys

        REAL_PYTHON = {sys.executable!r}
        log = Path(os.environ["HARNESS_LOG"])
        state = Path(os.environ["HARNESS_STATE"])

        def record(event, **detail):
            with log.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({{"event": event, **detail}}, sort_keys=True)
                    + "\\n"
                )

        arguments = sys.argv[1:]
        if arguments[:1] == ["-c"]:
            record("python-c")
            os.execv(REAL_PYTHON, [REAL_PYTHON, *arguments])

        if arguments[:2] == [
            "-m",
            "trading_assistant.ops.control",
        ] and len(arguments) >= 3:
            command = arguments[2]
            started = state.exists()
            record(
                "control",
                command=command,
                arguments=arguments,
                started=started,
            )
            scenario = os.environ["HARNESS_CONTROL"]
            if command == "expected-argv":
                print(
                    "/test/python "
                    "-m trading_assistant.ops.serve"
                )
                raise SystemExit(0)
            if command == "ready":
                if scenario == "running":
                    raise SystemExit(0)
                if scenario == "absent":
                    raise SystemExit(0 if started else 1)
                raise SystemExit(1)
            if command == "app-absent":
                raise SystemExit(
                    0
                    if scenario == "absent" and not started
                    else 1
                )
            raise SystemExit(97)

        if arguments == [
            "-m",
            "trading_assistant.ops.operator_terminal",
        ]:
            record("terminal")
            raise SystemExit(0)

        record("unexpected-python", arguments=arguments)
        raise SystemExit(98)
        """
    )
    _write_executable(python, fake_python_source)

    fake_start_source = textwrap.dedent(
        f"""\
        #!{sys.executable}
        import json
        import os
        from pathlib import Path

        log = Path(os.environ["HARNESS_LOG"])
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({{"event": "start"}}) + "\\n")
        if os.environ["HARNESS_START"] == "fail":
            raise SystemExit(23)
        Path(os.environ["HARNESS_STATE"]).touch(mode=0o600)
        """
    )
    _write_executable(project / "scripts/start.sh", fake_start_source)

    fake_curl_source = textwrap.dedent(
        f"""\
        #!{sys.executable}
        import json
        import os
        from pathlib import Path
        import sys

        log = Path(os.environ["HARNESS_LOG"])
        with log.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {{"event": "curl", "arguments": sys.argv[1:]}},
                    sort_keys=True,
                )
                + "\\n"
            )

        mode = os.environ["HARNESS_LIVENESS"]
        if mode == "fail":
            raise SystemExit(22)
        if mode == "after-start" and not Path(
            os.environ["HARNESS_STATE"]
        ).exists():
            raise SystemExit(22)
        if mode == "wrong-json":
            print('{{"alive":true}}')
        elif mode == "extra-json":
            print(
                '{{"alive":true,"database_reachable":true,'
                '"extra":true}}'
            )
        else:
            print({_LIVE_PAYLOAD!r})
        """
    )
    _write_executable(fake_curl, fake_curl_source)

    production_source = _LAUNCHER.read_text(encoding="utf-8")
    assert _CANONICAL_ROOT in production_source
    assert _CURL in production_source
    injected_source = production_source.replace(
        _CANONICAL_ROOT,
        str(project),
    ).replace(
        _CURL,
        str(fake_curl),
    )
    _write_executable(launcher, injected_source)

    return LauncherHarness(
        project=project,
        launcher=launcher,
        python=python,
        ca=ca,
        log=log,
        state=state,
        parent=tmp_path,
    )


def _event_names(harness: LauncherHarness) -> list[str]:
    return [str(event["event"]) for event in harness.events()]


def _assert_menu_was_not_launched(harness: LauncherHarness) -> None:
    assert "terminal" not in _event_names(harness)


def test_operator_launcher_is_canonical_and_does_not_start_daemon():
    source = _LAUNCHER.read_text(encoding="utf-8")

    assert _CANONICAL_ROOT in source
    assert "scripts/start.sh" in source
    assert "trading_assistant.ops.operator_terminal" in source
    assert "set -euo pipefail" in source
    assert "umask 077" in source
    assert _CURL in source
    for forbidden in (
        "trading_assistant.daemon.main",
        "curl -k",
        "--insecure",
        "pkill",
        "killall",
        "sync_open_orders",
        "reconcile",
        "breaker reset",
        "generate",
        "approve",
        "place_order",
        "eval ",
        "source ",
    ):
        assert forbidden not in source


def test_operator_launcher_has_valid_bash_syntax():
    completed = subprocess.run(
        ["/bin/bash", "-n", str(_LAUNCHER)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_operator_launcher_rejects_arguments_without_running_dependencies(
    launcher_harness: LauncherHarness,
):
    completed = launcher_harness.run(
        arguments=("--root", "/tmp/not-authorized"),
    )

    assert completed.returncode != 0
    assert launcher_harness.events() == []


def test_operator_launcher_rejects_wrong_cwd_before_menu(
    launcher_harness: LauncherHarness,
):
    outside = launcher_harness.parent / "outside"
    outside.mkdir()

    completed = launcher_harness.run(
        cwd=outside,
        launcher=str(launcher_harness.launcher),
    )

    assert completed.returncode != 0
    assert launcher_harness.events() == []


def test_operator_launcher_rejects_symlink_root_before_menu(
    launcher_harness: LauncherHarness,
):
    symlink_root = launcher_harness.parent / "symlink-project"
    symlink_root.symlink_to(
        launcher_harness.project,
        target_is_directory=True,
    )

    completed = launcher_harness.run(
        cwd=symlink_root,
        logical_cwd=True,
    )

    assert completed.returncode != 0
    assert launcher_harness.events() == []


def test_operator_launcher_rejects_missing_venv_before_menu(
    launcher_harness: LauncherHarness,
):
    launcher_harness.python.unlink()

    completed = launcher_harness.run()

    assert completed.returncode != 0
    assert launcher_harness.events() == []


def test_operator_launcher_rejects_missing_ca_before_menu(
    launcher_harness: LauncherHarness,
):
    launcher_harness.ca.unlink()

    completed = launcher_harness.run()

    assert completed.returncode != 0
    assert launcher_harness.events() == []


@pytest.mark.parametrize(
    "liveness",
    ["fail", "wrong-json", "extra-json"],
)
def test_operator_launcher_requires_exact_verified_liveness_before_menu(
    launcher_harness: LauncherHarness,
    liveness: str,
):
    completed = launcher_harness.run(liveness=liveness)

    assert completed.returncode != 0
    assert "start" not in _event_names(launcher_harness)
    _assert_menu_was_not_launched(launcher_harness)


def test_operator_launcher_stops_when_controlled_start_fails(
    launcher_harness: LauncherHarness,
):
    completed = launcher_harness.run(
        control="absent",
        liveness="after-start",
        start="fail",
    )

    assert completed.returncode != 0
    assert _event_names(launcher_harness).count("start") == 1
    _assert_menu_was_not_launched(launcher_harness)


def test_operator_launcher_stops_on_wrong_process_control_without_starting(
    launcher_harness: LauncherHarness,
):
    completed = launcher_harness.run(
        control="wrong",
        liveness="valid",
    )

    assert completed.returncode != 0
    assert "start" not in _event_names(launcher_harness)
    _assert_menu_was_not_launched(launcher_harness)


def test_operator_launcher_reuses_verified_running_app_and_execs_menu(
    launcher_harness: LauncherHarness,
):
    completed = launcher_harness.run()

    assert completed.returncode == 0, completed.stderr
    events = launcher_harness.events()
    names = [event["event"] for event in events]
    assert "start" not in names
    assert names[-1] == "terminal"
    ready = [
        event
        for event in events
        if event["event"] == "control"
        and event["command"] == "ready"
    ]
    assert len(ready) == 2
    curl = next(event for event in events if event["event"] == "curl")
    assert curl["arguments"] == [
        "--fail",
        "--silent",
        "--show-error",
        "--cacert",
        str(launcher_harness.ca),
        _LIVENESS_URL,
    ]
    assert "python-c" in names


def test_operator_launcher_starts_only_proven_absent_app_once_then_revalidates(
    launcher_harness: LauncherHarness,
):
    completed = launcher_harness.run(
        control="absent",
        liveness="after-start",
    )

    assert completed.returncode == 0, completed.stderr
    events = launcher_harness.events()
    names = [event["event"] for event in events]
    assert names.count("start") == 1
    assert names[-1] == "terminal"
    assert any(
        event["event"] == "control"
        and event["command"] == "app-absent"
        and event["started"] is False
        for event in events
    )
    post_start_ready = [
        event
        for event in events
        if event["event"] == "control"
        and event["command"] == "ready"
        and event["started"] is True
    ]
    assert len(post_start_ready) == 2
    assert any(event["event"] == "curl" for event in events)


def test_operator_launcher_stops_when_post_start_liveness_is_not_verified(
    launcher_harness: LauncherHarness,
):
    completed = launcher_harness.run(
        control="absent",
        liveness="fail",
    )

    assert completed.returncode != 0
    assert _event_names(launcher_harness).count("start") == 1
    _assert_menu_was_not_launched(launcher_harness)
