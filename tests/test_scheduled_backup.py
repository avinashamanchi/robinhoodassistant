"""Scheduled backup pauses only installed managed jobs and always restores them."""

from pathlib import Path
import os
import plistlib
import subprocess

import pytest

from trading_assistant.ops.scheduled_backup import paused_runtime


@pytest.fixture
def jobs(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".local").mkdir(mode=0o700)
    agents = tmp_path / "agents"
    agents.mkdir()
    modules = {"watchdog": "watchdog", "daemon": "daemon.main", "app": "ops.serve"}
    for name, module in modules.items():
        label = f"com.trading.{name}"
        if name == "watchdog":
            module = "ops.watchdog"
        path = agents / f"{label}.plist"
        path.write_bytes(plistlib.dumps({
            "Label": label, "WorkingDirectory": str(root),
            "ProgramArguments": [str(root / ".venv/bin/python"), "-m", f"trading_assistant.{module}"],
        }))
        path.chmod(0o600)
    loaded = {"com.trading.app", "com.trading.daemon", "com.trading.watchdog"}
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        command, target = argv[1], argv[-1]
        if command == "bootstrap":
            assert argv[2] == f"gui/{os.getuid()}"
        label = Path(target).stem if command == "bootstrap" else target.rsplit("/", 1)[-1]
        if command == "print":
            return subprocess.CompletedProcess(argv, 0 if label in loaded else 113)
        if command == "bootout":
            loaded.remove(label)
        elif command == "bootstrap":
            loaded.add(label)
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0)

    return root, agents, loaded, calls, run


def test_backup_restores_original_jobs_after_failure(jobs):
    root, agents, loaded, calls, run = jobs
    with pytest.raises(RuntimeError, match="backup failed"):
        with paused_runtime(root, agents=agents, runner=run, sleep=lambda _: None):
            assert loaded == set()
            raise RuntimeError("backup failed")
    assert loaded == {"com.trading.app", "com.trading.daemon", "com.trading.watchdog"}
    assert [Path(a[-1]).stem for a in calls if a[1] == "bootstrap"] == [
        "com.trading.app", "com.trading.daemon", "com.trading.watchdog",
    ]


def test_backup_does_not_start_a_previously_stopped_daemon(jobs):
    root, agents, loaded, calls, run = jobs
    loaded.remove("com.trading.daemon")
    with paused_runtime(root, agents=agents, runner=run, sleep=lambda _: None):
        assert loaded == set()
    assert loaded == {"com.trading.app", "com.trading.watchdog"}


def test_foreign_installation_is_rejected_before_stopping_jobs(jobs):
    root, agents, loaded, calls, run = jobs
    path = agents / "com.trading.app.plist"
    data = plistlib.loads(path.read_bytes())
    data["WorkingDirectory"] = "/another/checkout"
    path.write_bytes(plistlib.dumps(data))
    with pytest.raises(RuntimeError, match="scheduled_backup_job_invalid"):
        with paused_runtime(root, agents=agents, runner=run, sleep=lambda _: None):
            pytest.fail("backup should not run")
    assert not any(a[1] == "bootout" for a in calls)


def test_partial_shutdown_failure_restores_jobs_already_stopped(jobs):
    root, agents, loaded, calls, run = jobs

    def failing_run(argv, **kwargs):
        if argv[1] == "bootout" and argv[2].endswith("/com.trading.daemon"):
            raise subprocess.TimeoutExpired(argv, 20)
        return run(argv, **kwargs)

    with pytest.raises(subprocess.TimeoutExpired):
        with paused_runtime(root, agents=agents, runner=failing_run, sleep=lambda _: None):
            pytest.fail("backup should not run")
    assert loaded == {"com.trading.app", "com.trading.daemon", "com.trading.watchdog"}


def test_overlapping_scheduled_backups_cannot_stop_each_others_jobs(jobs):
    root, agents, loaded, calls, run = jobs
    with paused_runtime(root, agents=agents, runner=run, sleep=lambda _: None):
        with pytest.raises(RuntimeError, match="scheduled_backup_already_running"):
            with paused_runtime(root, agents=agents, runner=run, sleep=lambda _: None):
                pytest.fail("second backup should not run")
