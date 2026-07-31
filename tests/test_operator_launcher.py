from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_LAUNCHER = _ROOT / "scripts/operator.sh"
_CANONICAL_ROOT = "/Users/avi/Desktop/robinhood/trading-assistant"
_CURL = "/usr/bin/curl"
_STAT = "/usr/bin/stat"
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
    python_target: Path
    ca: Path
    start_script: Path
    fake_curl: Path
    fake_stat: Path
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
        environment_overrides: dict[str, str] | None = None,
        direct: bool = False,
        interpreter: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        directory = self.project if cwd is None else cwd
        cd_mode = "-L" if logical_cwd else "-P"
        environment = {
            "HARNESS_CONTROL": control,
            "HARNESS_LIVENESS": liveness,
            "HARNESS_LOG": str(self.log),
            "HARNESS_REAL_PYTHON": sys.executable,
            "HARNESS_START": start,
            "HARNESS_START_SCRIPT": str(self.start_script),
            "HARNESS_STATE": str(self.state),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
        }
        if environment_overrides is not None:
            environment.update(environment_overrides)
        if direct or interpreter:
            command = [
                *interpreter,
                launcher,
                *arguments,
            ]
        else:
            command = [
                "/bin/bash",
                "-c",
                f'cd {cd_mode} -- "$1" && shift && exec "$@"',
                "operator-launcher-harness",
                str(directory),
                launcher,
                *arguments,
            ]
        return subprocess.run(
            command,
            cwd=directory if direct or interpreter else self.parent,
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
    fake_stat = tmp_path / "absolute-stat"
    log = tmp_path / "events.jsonl"
    state = tmp_path / "app-started"
    python_target = tmp_path / "repo-python-target"
    start_script = project / "scripts/start.sh"

    project.mkdir()
    ca.parent.mkdir(parents=True)
    (project / ".local").chmod(0o700)
    ca.parent.chmod(0o700)
    ca.write_text("test-only CA placeholder\n", encoding="ascii")
    ca.chmod(0o644)

    fake_python_source = textwrap.dedent(
        f"""\
        #!{sys.executable} -I
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

        startup_environment = sorted(
            name
            for name in (
                "PYTHONEXECUTABLE",
                "PYTHONBREAKPOINT",
                "PYTHONHOME",
                "PYTHONINSPECT",
                "PYTHONNOUSERSITE",
                "PYTHONPATH",
                "PYTHONSAFEPATH",
                "PYTHONSTARTUP",
                "PYTHONUSERBASE",
                "PYTHONWARNINGS",
            )
            if name in os.environ
        )
        arguments = sys.argv[1:]
        isolated = arguments[:1] == ["-I"]
        owned_arguments = arguments[1:] if isolated else arguments
        common = {{
            "arguments": arguments,
            "isolated": isolated,
            "path": os.environ.get("PATH"),
            "startup_environment": startup_environment,
        }}

        if owned_arguments[:1] == ["-c"]:
            record("python-c", **common)
            os.execv(REAL_PYTHON, [REAL_PYTHON, *arguments])

        if owned_arguments[:2] == [
            "-m",
            "trading_assistant.ops.control",
        ] and len(owned_arguments) >= 3:
            command = owned_arguments[2]
            started = state.exists()
            record(
                "control",
                command=command,
                started=started,
                **common,
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

        if owned_arguments == [
            "-m",
            "trading_assistant.ops.operator_terminal",
        ]:
            record("terminal", **common)
            raise SystemExit(0)

        record("unexpected-python", **common)
        raise SystemExit(98)
        """
    )
    _write_executable(python_target, fake_python_source)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.parent.parent.chmod(0o755)
    python.parent.chmod(0o755)
    python.symlink_to(python_target)

    fake_start_source = textwrap.dedent(
        """\
        #!/bin/bash
        set -euo pipefail
        case "$-" in
          *p*) privileged=true ;;
          *) privileged=false ;;
        esac
        /usr/bin/printf \
          '{"event":"start","privileged":%s}\\n' \
          "$privileged" >> "$HARNESS_LOG"
        if [[ "${HARNESS_WARNING_PROBE:-0}" == "1" ]]; then
          PYTHONPATH="$HARNESS_WARNING_MODULE_PATH" \
            "$HARNESS_REAL_PYTHON" -c '
        import json
        import os
        from pathlib import Path
        import sys

        event = {
            "event": "start-python",
            "no_user_site": bool(sys.flags.no_user_site),
            "pythonbreakpoint": os.environ.get("PYTHONBREAKPOINT"),
            "pythonwarnings": os.environ.get("PYTHONWARNINGS"),
            "safe_path": bool(sys.flags.safe_path),
        }
        with Path(os.environ["HARNESS_LOG"]).open(
            "a",
            encoding="utf-8",
        ) as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\\n")
        '
        fi
        if [[ "$HARNESS_START" == "fail" ]]; then
          exit 23
        fi
        /usr/bin/touch "$HARNESS_STATE"
        """
    )
    _write_executable(start_script, fake_start_source)

    fake_curl_source = textwrap.dedent(
        f"""\
        #!{sys.executable} -I
        import json
        import os
        from pathlib import Path
        import sys
        import time

        log = Path(os.environ["HARNESS_LOG"])
        with log.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {{
                        "event": "curl",
                        "arguments": sys.argv[1:],
                        "path": os.environ.get("PATH"),
                    }},
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
        if mode == "stall":
            time.sleep(0.05 if "--max-time" in sys.argv else 1.5)
            raise SystemExit(28)
        if mode == "oversized":
            print({_LIVE_PAYLOAD!r} + (" " * 2048))
        elif mode == "duplicate-json":
            print(
                '{{"alive":false,"alive":true,'
                '"database_reachable":true}}'
            )
        elif mode == "invalid-utf8":
            sys.stdout.buffer.write(
                b'{{"alive":true,"database_reachable":true}}\\xff'
            )
        elif mode == "wrong-json":
            print('{{"alive":true}}')
        elif mode == "extra-json":
            print(
                '{{"alive":true,"database_reachable":true,'
                '"extra":true}}'
            )
        elif mode == "integer-truthy-json":
            print('{{"alive":1,"database_reachable":1}}')
        elif mode == "float-truthy-json":
            print('{{"alive":1.0,"database_reachable":1.0}}')
        else:
            print({_LIVE_PAYLOAD!r})
        """
    )
    _write_executable(fake_curl, fake_curl_source)

    fake_stat_source = textwrap.dedent(
        f"""\
        #!{sys.executable} -I
        import os
        import subprocess
        import sys

        arguments = sys.argv[1:]
        completed = subprocess.run(
            [{_STAT!r}, *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        output = completed.stdout
        if (
            completed.returncode == 0
            and os.environ.get("HARNESS_FOREIGN_START_OWNER") == "1"
            and arguments[-1:] == [
                os.environ["HARNESS_START_SCRIPT"]
            ]
            and "%u:%p:%l" in arguments
        ):
            owner, mode, links = output.strip().split(":")
            output = f"{{int(owner) + 1}}:{{mode}}:{{links}}\\n"
        sys.stdout.write(output)
        sys.stderr.write(completed.stderr)
        raise SystemExit(completed.returncode)
        """
    )
    _write_executable(fake_stat, fake_stat_source)

    production_source = _LAUNCHER.read_text(encoding="utf-8")
    assert _CANONICAL_ROOT in production_source
    assert _CURL in production_source
    assert _STAT in production_source
    injected_source = production_source.replace(
        _CANONICAL_ROOT,
        str(project),
    ).replace(
        _CURL,
        str(fake_curl),
    ).replace(
        _STAT,
        str(fake_stat),
    )
    _write_executable(launcher, injected_source)

    return LauncherHarness(
        project=project,
        launcher=launcher,
        python=python,
        python_target=python_target,
        ca=ca,
        start_script=start_script,
        fake_curl=fake_curl,
        fake_stat=fake_stat,
        log=log,
        state=state,
        parent=tmp_path,
    )


def _event_names(harness: LauncherHarness) -> list[str]:
    return [str(event["event"]) for event in harness.events()]


def _assert_menu_was_not_launched(harness: LauncherHarness) -> None:
    assert "terminal" not in _event_names(harness)


def _python_events(
    harness: LauncherHarness,
) -> list[dict[str, object]]:
    return [
        event
        for event in harness.events()
        if event["event"] in {"control", "python-c", "terminal"}
    ]


def _replace_with_symlink(path: Path, target: Path) -> None:
    is_directory = path.is_dir()
    path.rename(target)
    path.symlink_to(target, target_is_directory=is_directory)


def test_operator_launcher_is_canonical_and_does_not_start_daemon():
    source = _LAUNCHER.read_text(encoding="utf-8")

    assert _CANONICAL_ROOT in source
    assert "scripts/start.sh" in source
    assert "trading_assistant.ops.operator_terminal" in source
    assert "set -euo pipefail" in source
    assert "umask 077" in source
    assert _CURL in source
    assert source.startswith("#!/bin/bash -p\n")
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


def test_operator_launcher_rejects_non_privileged_bash(
    launcher_harness: LauncherHarness,
):
    completed = launcher_harness.run(
        direct=True,
        interpreter=("/bin/bash",),
    )

    assert completed.returncode != 0
    assert launcher_harness.events() == []


def test_operator_launcher_isolates_every_owned_python_invocation(
    launcher_harness: LauncherHarness,
):
    completed = launcher_harness.run(direct=True)

    assert completed.returncode == 0, completed.stderr
    events = _python_events(launcher_harness)
    assert events
    assert all(event["isolated"] is True for event in events)
    assert all(
        event["path"] == "/usr/bin:/bin:/usr/sbin:/sbin"
        for event in events
    )


def test_operator_launcher_ignores_path_bash_hijack(
    launcher_harness: LauncherHarness,
):
    fake_bin = launcher_harness.parent / "fake-bin"
    marker = launcher_harness.parent / "fake-bash-used"
    _write_executable(
        fake_bin / "bash",
        textwrap.dedent(
            """\
            #!/bin/bash
            /usr/bin/touch "$HARNESS_FAKE_BASH_MARKER"
            exec /bin/bash "$@"
            """
        ),
    )

    completed = launcher_harness.run(
        direct=True,
        environment_overrides={
            "HARNESS_FAKE_BASH_MARKER": str(marker),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert not marker.exists()


def test_operator_launcher_ignores_bash_env_startup_hook(
    launcher_harness: LauncherHarness,
):
    marker = launcher_harness.parent / "bash-env-used"
    bash_env = launcher_harness.parent / "bash-env"
    bash_env.write_text(
        '/usr/bin/touch "$HARNESS_BASH_ENV_MARKER"\n',
        encoding="utf-8",
    )

    completed = launcher_harness.run(
        direct=True,
        environment_overrides={
            "BASH_ENV": str(bash_env),
            "HARNESS_BASH_ENV_MARKER": str(marker),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert not marker.exists()


def test_operator_launcher_ignores_exported_pwd_function(
    launcher_harness: LauncherHarness,
):
    completed = launcher_harness.run(
        direct=True,
        environment_overrides={
            "BASH_FUNC_pwd%%": "() { printf '%s\\n' /attacker; }",
        },
    )

    assert completed.returncode == 0, completed.stderr


def test_operator_launcher_blocks_pythonpath_startup_hijack(
    launcher_harness: LauncherHarness,
):
    marker = launcher_harness.parent / "pythonpath-used"
    hijack = launcher_harness.parent / "python-hijack"
    hijack.mkdir()
    (hijack / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('used', encoding='utf-8')\n",
        encoding="utf-8",
    )

    completed = launcher_harness.run(
        direct=True,
        environment_overrides={
            "PYTHONPATH": str(hijack),
            "PYTHONSTARTUP": str(hijack / "sitecustomize.py"),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert not marker.exists()
    assert all(
        event["startup_environment"] == []
        for event in _python_events(launcher_harness)
    )


def test_operator_launcher_scrubs_start_child_python_injection(
    launcher_harness: LauncherHarness,
):
    marker = launcher_harness.parent / "pythonwarnings-imported"
    warning_modules = launcher_harness.parent / "warning-modules"
    warning_modules.mkdir()
    probe_environment = {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(warning_modules),
    }
    warning_hook = warning_modules / "warning_probe.py"
    warning_hook.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).touch()\n"
        "class InjectedWarning(Warning):\n"
        "    pass\n",
        encoding="utf-8",
    )
    warning_option = "ignore::warning_probe.InjectedWarning"
    primitive = subprocess.run(
        [sys.executable, "-c", "pass"],
        cwd=launcher_harness.project,
        env={
            **probe_environment,
            "PYTHONWARNINGS": warning_option,
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert primitive.returncode == 0, primitive.stderr
    assert marker.exists(), (
        "test warning hook did not execute: "
        f"{primitive.stderr}"
    )
    marker.unlink()

    completed = launcher_harness.run(
        direct=True,
        control="absent",
        liveness="after-start",
        environment_overrides={
            "HARNESS_WARNING_MODULE_PATH": str(warning_modules),
            "HARNESS_WARNING_PROBE": "1",
            "PYTHONBREAKPOINT": "warning_probe.injected_breakpoint",
            "PYTHONWARNINGS": warning_option,
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert not marker.exists()
    child = next(
        event
        for event in launcher_harness.events()
        if event["event"] == "start-python"
    )
    assert child == {
        "event": "start-python",
        "no_user_site": True,
        "pythonbreakpoint": None,
        "pythonwarnings": None,
        "safe_path": True,
    }
    assert all(
        event["startup_environment"] == []
        for event in _python_events(launcher_harness)
    )


@pytest.mark.parametrize(
    "relative_path",
    (
        ".venv",
        ".venv/bin",
        ".local",
        ".local/tls",
        "scripts",
        ".local/tls/rootCA.pem",
        "scripts/start.sh",
    ),
)
def test_operator_launcher_rejects_symlink_descendants(
    launcher_harness: LauncherHarness,
    relative_path: str,
):
    path = launcher_harness.project / relative_path
    target = launcher_harness.parent / (
        "real-" + relative_path.replace("/", "-").lstrip(".")
    )
    _replace_with_symlink(path, target)

    completed = launcher_harness.run(direct=True)

    assert completed.returncode != 0
    assert launcher_harness.events() == []


@pytest.mark.parametrize("mode", (0o600, 0o640, 0o664))
def test_operator_launcher_requires_exact_ca_mode(
    launcher_harness: LauncherHarness,
    mode: int,
):
    launcher_harness.ca.chmod(mode)

    completed = launcher_harness.run(direct=True)

    assert completed.returncode != 0
    assert launcher_harness.events() == []


def test_operator_launcher_rejects_hardlinked_ca(
    launcher_harness: LauncherHarness,
):
    os.link(
        launcher_harness.ca,
        launcher_harness.parent / "root-ca-hardlink",
    )

    completed = launcher_harness.run(direct=True)

    assert completed.returncode != 0
    assert launcher_harness.events() == []


def test_operator_launcher_requires_private_tls_directory(
    launcher_harness: LauncherHarness,
):
    launcher_harness.ca.parent.chmod(0o755)

    completed = launcher_harness.run(direct=True)

    assert completed.returncode != 0
    assert launcher_harness.events() == []


def test_operator_launcher_rejects_writable_python_target(
    launcher_harness: LauncherHarness,
):
    launcher_harness.python_target.chmod(0o722)

    completed = launcher_harness.run(direct=True)

    assert completed.returncode != 0
    assert launcher_harness.events() == []


def test_operator_launcher_rejects_hardlinked_python_target(
    launcher_harness: LauncherHarness,
):
    os.link(
        launcher_harness.python_target,
        launcher_harness.parent / "python-hardlink",
    )

    completed = launcher_harness.run(direct=True)

    assert completed.returncode != 0
    assert launcher_harness.events() == []


def test_operator_launcher_rejects_writable_start_script(
    launcher_harness: LauncherHarness,
):
    launcher_harness.start_script.chmod(0o770)

    completed = launcher_harness.run(
        direct=True,
        control="absent",
        liveness="after-start",
    )

    assert completed.returncode != 0
    assert launcher_harness.events() == []


def test_operator_launcher_rejects_hardlinked_start_script(
    launcher_harness: LauncherHarness,
):
    os.link(
        launcher_harness.start_script,
        launcher_harness.parent / "start-hardlink",
    )

    completed = launcher_harness.run(
        direct=True,
        control="absent",
        liveness="after-start",
    )

    assert completed.returncode != 0
    assert launcher_harness.events() == []


def test_operator_launcher_rejects_foreign_owned_start_script(
    launcher_harness: LauncherHarness,
):
    completed = launcher_harness.run(
        direct=True,
        control="absent",
        liveness="after-start",
        environment_overrides={
            "HARNESS_FOREIGN_START_OWNER": "1",
        },
    )

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


@pytest.mark.parametrize(
    "liveness",
    ["integer-truthy-json", "float-truthy-json"],
)
def test_operator_launcher_requires_boolean_liveness_values(
    launcher_harness: LauncherHarness,
    liveness: str,
):
    completed = launcher_harness.run(
        direct=True,
        liveness=liveness,
    )

    assert completed.returncode != 0
    assert "start" not in _event_names(launcher_harness)
    _assert_menu_was_not_launched(launcher_harness)


@pytest.mark.parametrize(
    "liveness",
    ["oversized", "duplicate-json", "invalid-utf8"],
)
def test_operator_launcher_rejects_noncanonical_liveness_bytes(
    launcher_harness: LauncherHarness,
    liveness: str,
):
    completed = launcher_harness.run(
        direct=True,
        liveness=liveness,
    )

    assert completed.returncode != 0
    assert "start" not in _event_names(launcher_harness)
    _assert_menu_was_not_launched(launcher_harness)


def test_operator_launcher_bounds_stalled_liveness_response(
    launcher_harness: LauncherHarness,
):
    started = time.monotonic()
    completed = launcher_harness.run(
        direct=True,
        liveness="stall",
    )
    elapsed = time.monotonic() - started

    assert completed.returncode != 0
    assert elapsed < 1.0
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
    home = launcher_harness.parent / "home"
    home.mkdir()
    (home / ".curlrc").write_text("--insecure\n", encoding="utf-8")
    completed = launcher_harness.run(
        environment_overrides={
            "HOME": str(home),
            "HTTPS_PROXY": "http://127.0.0.1:9",
        },
    )

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
        str(launcher_harness.ca),
        _LIVENESS_URL,
    ]
    assert curl["path"] == "/usr/bin:/bin:/usr/sbin:/sbin"
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
    start_event = next(
        event for event in events if event["event"] == "start"
    )
    assert start_event["privileged"] is True


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
