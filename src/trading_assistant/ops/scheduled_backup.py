"""Pause installed launchd runtimes for the exclusive encrypted backup."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import time

from . import backup
from .tenure import TenureUnavailable


_JOBS = (
    ("com.trading.watchdog", "trading_assistant.ops.watchdog"),
    ("com.trading.daemon", "trading_assistant.daemon.main"),
    ("com.trading.app", "trading_assistant.ops.serve"),
)


def _launchctl(
    runner, command: str, target: str, *, domain: str | None = None,
) -> bool:
    arguments = ["/bin/launchctl", command]
    if domain is not None:
        arguments.append(domain)
    arguments.append(target)
    result = runner(
        arguments,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
    )
    if result.returncode == 0:
        return True
    if command == "print" and result.returncode == 113:
        return False
    raise RuntimeError("scheduled_backup_launchctl_failed")


@contextmanager
def paused_runtime(
    project: Path,
    *,
    agents: Path | None = None,
    runner=subprocess.run,
    sleep=time.sleep,
):
    """Restore exactly the jobs originally loaded, including on backup failure."""
    root = project.resolve(strict=True)
    local = root / ".local"
    info = local.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise RuntimeError("scheduled_backup_directory_invalid")
    agents = agents or Path.home() / "Library/LaunchAgents"
    domain = f"gui/{os.getuid()}"
    descriptor = os.open(
        local / "scheduled-backup.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    stopped = []
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
        ):
            raise RuntimeError("scheduled_backup_lock_invalid")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("scheduled_backup_already_running") from None
        selected = []
        for label, module in _JOBS:
            target = f"{domain}/{label}"
            if not _launchctl(runner, "print", target):
                continue
            path = agents / f"{label}.plist"
            info = path.lstat()
            contents = path.read_bytes()
            job = plistlib.loads(contents)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o022
                or job.get("Label") != label
                or job.get("WorkingDirectory") != str(root)
                or job.get("ProgramArguments") != [
                    str(root / ".venv/bin/python"), "-m", module,
                ]
            ):
                raise RuntimeError("scheduled_backup_job_invalid")
            selected.append((target, path, contents))
        for entry in selected:
            # Track before bootout so a response lost after stopping is recovered.
            stopped.append(entry)
            _launchctl(runner, "bootout", entry[0])
        yield
    finally:
        failures = []
        for target, path, contents in reversed(stopped):
            try:
                if not _launchctl(runner, "print", target):
                    if path.is_symlink() or path.read_bytes() != contents:
                        raise RuntimeError("scheduled_backup_job_changed")
                    if target.endswith("/com.trading.watchdog"):
                        # Permit the restored runtimes to acquire tenure before
                        # the first watchdog sample reads old heartbeat data.
                        sleep(10)
                    _launchctl(runner, "bootstrap", str(path), domain=domain)
            except Exception:
                failures.append(target.rsplit("/", 1)[-1])
        os.close(descriptor)
        if failures:
            raise RuntimeError("scheduled_backup_restart_failed " + ",".join(failures))


def main(argv: list[str] | None = None) -> int:
    from ..config import load_config
    from ..installation import require_designated
    from ..security.secrets import load_role_secrets

    root = Path.cwd()
    require_designated(root)
    config = load_config()
    secrets = load_role_secrets("backup", config=config)
    # Validate arguments before stopping any runtime.
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--retention-days", type=int, default=14)
    args = parser.parse_args(argv)
    if args.retention_days <= 0:
        parser.error("retention-days must be positive")
    with paused_runtime(root):
        deadline = time.monotonic() + 40
        while True:
            try:
                return backup.main(
                    [
                        "--destination", args.destination,
                        "--retention-days", str(args.retention_days),
                    ],
                    config_loader=lambda: config,
                    secrets_loader=lambda role, **kwargs: secrets,
                )
            except TenureUnavailable as error:
                if (
                    error.stable_code != "runtime_tenure_active"
                    or time.monotonic() >= deadline
                ):
                    raise
                time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
