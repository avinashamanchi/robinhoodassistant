"""launchd installer: designated installation, venv, and generated jobs.

The installer runs in a disposable project tree (path with a space) with a
temporary HOME and fake launchctl/sleep/curl, so these tests never touch the
operator's real LaunchAgents. A fake venv interpreter runs the real Python
but answers the "which checkout does this venv import" probe, so stale and
broken environments can be simulated. None of this is evidence about the
operator's installed jobs.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import textwrap

import pytest

_REPO = Path(__file__).resolve().parent.parent
_DESIGNATION = Path("Library/Application Support/trading-assistant/installation-root")


@dataclass
class Installer:
    project: Path
    home: Path
    fake_bin: Path
    other: Path

    def designate(self, root: Path | None = None, *, mode: int = 0o600) -> Path:
        path = self.home / _DESIGNATION
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        path.write_text(f"{root or self.project}\n", encoding="utf-8")
        path.chmod(mode)
        return path

    def run(self, *args: str, venv: str = "ok", cwd: Path | None = None,
            script: Path | None = None, env: dict[str, str] | None = None):
        return subprocess.run(
            ["/bin/bash", str(script or self.project / "scripts/launchd/install.sh"), *args],
            cwd=cwd or self.home,
            env={
                "HOME": str(self.home),
                "PATH": f"{self.fake_bin}:/usr/bin:/bin",
                "HARNESS_VENV": venv,
                "HARNESS_PROJECT": str(self.project),
                "HARNESS_OTHER": str(self.other),
                **(env or {}),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )

    def plists(self) -> dict[str, dict]:
        agents = self.home / "Library" / "LaunchAgents"
        return {
            path.stem: plistlib.loads(path.read_bytes())
            for path in agents.glob("com.trading.*.plist")
        }


def _make_checkout(root: Path) -> None:
    for relative in (
        "scripts/launchd/install.sh",
        "scripts/operator.sh",
        "src/trading_assistant/installation.py",
        "pyproject.toml",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_REPO / relative, target)
    (root / "src/trading_assistant/__init__.py").write_text("", encoding="utf-8")


@pytest.fixture
def installer(tmp_path: Path) -> Installer:
    project = tmp_path / "trading assistant"
    other = tmp_path / "other checkout"
    home = tmp_path / "home dir"
    fake_bin = tmp_path / "bin"
    for directory in (home, fake_bin):
        directory.mkdir()
    _make_checkout(project)
    _make_checkout(other)
    for command in ("launchctl", "sleep", "curl"):
        tool = fake_bin / command
        tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        tool.chmod(0o700)
    venv_python = project / ".venv/bin/python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import os
            import sys

            arguments = sys.argv[1:]
            if "-c" in arguments and "trading_assistant" in arguments[arguments.index("-c") + 1]:
                mode = os.environ["HARNESS_VENV"]
                if mode == "broken":
                    sys.stderr.write("ModuleNotFoundError: trading_assistant\\n")
                    raise SystemExit(1)
                root = os.environ["HARNESS_PROJECT" if mode == "ok" else "HARNESS_OTHER"]
                print(os.path.join(root, "src", "trading_assistant"))
                raise SystemExit(0)
            os.execv({sys.executable!r}, [{sys.executable!r}, *arguments])
            """
        ),
        encoding="utf-8",
    )
    venv_python.chmod(0o700)
    return Installer(project=project, home=home, fake_bin=fake_bin, other=other)


def _assert_no_side_effects(installer: Installer) -> None:
    assert installer.plists() == {}
    assert not (installer.project / "logs").exists()
    assert not (installer.project / ".local").exists()


# ── designation and venv are required before any side effect ─────────────────
def test_installer_refuses_without_a_designation(installer):
    completed = installer.run()

    assert completed.returncode == 1
    assert "installation_not_designated" in completed.stderr
    assert "refusing to install launchd jobs" in completed.stderr
    _assert_no_side_effects(installer)


def test_installer_refuses_a_checkout_that_is_not_designated(installer):
    installer.designate(installer.other)

    completed = installer.run()

    assert completed.returncode == 1
    assert "installation_root_mismatch" in completed.stderr
    _assert_no_side_effects(installer)


def test_installer_refuses_a_designation_for_a_moved_checkout(installer, tmp_path):
    installer.designate(tmp_path / "old location that moved")

    completed = installer.run()

    assert completed.returncode == 1
    assert "designated_root_missing" in completed.stderr
    _assert_no_side_effects(installer)


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o620, 0o644])
def test_installer_refuses_a_shared_designation(installer, mode):
    installer.designate(mode=mode)

    completed = installer.run()

    assert completed.returncode == 1
    assert "installation_designation_untrusted" in completed.stderr
    _assert_no_side_effects(installer)


@pytest.mark.parametrize(
    ("venv", "code"),
    [("stale", "venv_imports_other_checkout"), ("broken", "venv_cannot_import_package")],
)
def test_installer_refuses_a_venv_that_does_not_run_this_checkout(installer, venv, code):
    installer.designate()

    completed = installer.run(venv=venv)

    assert completed.returncode == 1
    assert code in completed.stderr
    assert "uv sync --all-extras --dev" in completed.stderr
    _assert_no_side_effects(installer)


# ── generated jobs come from the designated installation ─────────────────────
def test_installer_generates_bounded_jobs_for_the_designated_root(installer):
    installer.designate()

    completed = installer.run()

    assert completed.returncode == 0, completed.stderr
    plists = installer.plists()
    assert set(plists) == {"com.trading.app", "com.trading.watchdog", "com.trading.backup"}
    for payload in plists.values():
        assert payload["WorkingDirectory"] == str(installer.project)
        assert payload["ProgramArguments"][0] == str(installer.project / ".venv/bin/python")
        assert payload["StandardOutPath"] == "/dev/null"
        assert payload["StandardErrorPath"] == "/dev/null"
        assert payload["Umask"] == 0o77


def test_installer_result_does_not_depend_on_the_working_directory(installer, tmp_path):
    installer.designate()
    elsewhere = tmp_path / "some other cwd"
    elsewhere.mkdir()

    completed = installer.run(cwd=elsewhere)

    assert completed.returncode == 0, completed.stderr
    assert installer.plists()["com.trading.app"]["WorkingDirectory"] == str(installer.project)


def test_installer_reached_through_a_symlink_uses_the_physical_root(installer, tmp_path):
    installer.designate()
    alias = tmp_path / "alias to project"
    alias.symlink_to(installer.project, target_is_directory=True)

    completed = installer.run(script=alias / "scripts/launchd/install.sh")

    assert completed.returncode == 0, completed.stderr
    assert installer.plists()["com.trading.app"]["WorkingDirectory"] == str(installer.project)


def test_installer_unknown_argument_has_no_side_effects(installer):
    installer.designate()

    completed = installer.run("--root=/elsewhere")

    assert completed.returncode == 2
    _assert_no_side_effects(installer)


# ── no scheduled trading job exists ──────────────────────────────────────────
def test_installer_refuses_the_retired_autopilot_schedule(installer):
    installer.designate()

    completed = installer.run("--with-autopilot")

    assert completed.returncode == 2
    assert "daemon hosts the autopilot" in completed.stderr
    _assert_no_side_effects(installer)


def test_installer_warns_about_a_leftover_autopilot_job(installer):
    installer.designate()
    agents = installer.home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.trading.autopilot.plist").write_bytes(
        plistlib.dumps({"Label": "com.trading.autopilot"})
    )

    completed = installer.run()

    assert completed.returncode == 0, completed.stderr
    assert "retired com.trading.autopilot job is still installed" in completed.stdout
    assert "com.trading.autopilot" in installer.plists()  # left for uninstall.sh
