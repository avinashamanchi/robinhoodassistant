"""Restart the trading daemon when its persisted heartbeat becomes stale."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from typing import Any
from urllib.request import urlopen

from ..config import load_config


def needs_restart(health: dict[str, Any], stale_seconds: float) -> bool:
    """Return whether health proves the daemon is missing or stale."""
    if not health.get("db_ok", False):
        return True
    age = health.get("heartbeat_age_seconds")
    if age is None:
        return True
    try:
        return float(age) > stale_seconds
    except (TypeError, ValueError):
        return True


def fetch_health(url: str, timeout_seconds: float = 5.0) -> dict[str, Any]:
    with urlopen(url, timeout=timeout_seconds) as response:  # noqa: S310 - local URL
        return json.loads(response.read())


def restart_launch_agent(label: str) -> None:
    target = f"gui/{os.getuid()}/{label}"
    subprocess.run(["launchctl", "kickstart", "-k", target], check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--health-url", default="http://127.0.0.1:8000/health")
    parser.add_argument("--label", default="com.trading.daemon")
    parser.add_argument("--request-timeout", type=float, default=5.0)
    args = parser.parse_args(argv)

    config = load_config()
    health = fetch_health(args.health_url, args.request_timeout)
    if needs_restart(health, config.daemon.heartbeat_stale_seconds):
        restart_launch_agent(args.label)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
