"""Restart the trading daemon when its persisted heartbeat becomes stale."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from typing import Any
from urllib.request import urlopen

from sqlalchemy import select

from ..config import Secrets, load_config
from ..db.models import Heartbeat, utcnow
from ..db.session import create_db_engine, make_session_factory


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


def labels_to_restart(
    *,
    api_health: dict[str, Any] | None,
    database_health: dict[str, Any],
    stale_seconds: float,
) -> set[str]:
    labels: set[str] = set()
    if api_health is None or api_health.get("status") != "ok":
        labels.add("com.trading.app")
    if needs_restart(database_health, stale_seconds):
        labels.add("com.trading.daemon")
    return labels


def fetch_health(url: str, timeout_seconds: float = 5.0) -> dict[str, Any]:
    with urlopen(url, timeout=timeout_seconds) as response:  # noqa: S310 - local URL
        return json.loads(response.read())


def read_database_health(
    database_url: str | None = None,
) -> dict[str, Any]:
    try:
        factory = make_session_factory(
            create_db_engine(database_url or Secrets().database_url)
        )
        with factory() as session:
            last = session.execute(
                select(Heartbeat)
                .where(Heartbeat.source == "daemon")
                .order_by(Heartbeat.id.desc())
                .limit(1)
            ).scalar_one_or_none()
        age = (utcnow() - last.at).total_seconds() if last is not None else None
        return {"db_ok": True, "heartbeat_age_seconds": age}
    except Exception:
        return {"db_ok": False, "heartbeat_age_seconds": None}


def restart_launch_agent(label: str) -> None:
    target = f"gui/{os.getuid()}/{label}"
    subprocess.run(["launchctl", "kickstart", "-k", target], check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--health-url",
        default="http://127.0.0.1:8000/health/live",
    )
    parser.add_argument("--label", default="com.trading.daemon")
    parser.add_argument("--request-timeout", type=float, default=5.0)
    args = parser.parse_args(argv)

    config = load_config()
    try:
        api_health = fetch_health(args.health_url, args.request_timeout)
    except Exception:
        api_health = None
    labels = labels_to_restart(
        api_health=api_health,
        database_health=read_database_health(),
        stale_seconds=config.daemon.heartbeat_stale_seconds,
    )
    for label in sorted(labels):
        restart_launch_agent(args.label if label == "com.trading.daemon" else label)
    if labels:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
