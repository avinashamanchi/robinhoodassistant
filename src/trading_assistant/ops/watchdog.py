"""Restart the trading daemon when its persisted heartbeat becomes stale."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
from numbers import Number
import subprocess
from typing import Any
from urllib.request import urlopen

from sqlalchemy import select

from ..config import Secrets, load_config
from ..db.models import Heartbeat, utcnow
from ..db.session import create_db_engine, make_session_factory

_MAX_LIVENESS_RESPONSE_BYTES = 1024
_RESTART_ORDER = (
    ("app", "com.trading.app"),
    ("daemon", "com.trading.daemon"),
)
log = logging.getLogger(__name__)


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Number):
        return None
    try:
        numeric_value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric_value if math.isfinite(numeric_value) else None


def needs_restart(health: object, stale_seconds: object) -> bool:
    """Return false only for a finite age in the inclusive fresh interval."""
    if not isinstance(health, dict) or health.get("db_ok") is not True:
        return True
    numeric_threshold = _finite_number(stale_seconds)
    if numeric_threshold is None or numeric_threshold <= 0:
        return True
    age = health.get("heartbeat_age_seconds")
    numeric_age = _finite_number(age)
    if numeric_age is None:
        return True
    return not (0 <= numeric_age <= numeric_threshold)


def _api_is_live(payload: object) -> bool:
    return type(payload) is dict and payload == {"status": "ok"}


def labels_to_restart(
    *,
    api_health: object,
    database_health: dict[str, Any],
    stale_seconds: float,
) -> set[str]:
    labels: set[str] = set()
    if not _api_is_live(api_health):
        labels.add("com.trading.app")
    if needs_restart(database_health, stale_seconds):
        labels.add("com.trading.daemon")
    return labels


def fetch_health(
    url: str,
    timeout_seconds: float = 5.0,
) -> dict[str, str] | None:
    with urlopen(url, timeout=timeout_seconds) as response:  # noqa: S310 - local URL
        body = response.read(_MAX_LIVENESS_RESPONSE_BYTES + 1)
    if not isinstance(body, (bytes, bytearray)):
        return None
    if len(body) > _MAX_LIVENESS_RESPONSE_BYTES:
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        return None
    return {"status": "ok"} if _api_is_live(payload) else None


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


def restart_selected_components(
    labels: set[str],
    *,
    daemon_label: str,
) -> tuple[str, ...]:
    """Attempt each selected component once and return fixed failure codes."""
    failures: list[str] = []
    attempted_targets: set[str] = set()
    for component, label in _RESTART_ORDER:
        if label not in labels:
            continue
        target = daemon_label if component == "daemon" else label
        if target in attempted_targets:
            failures.append(component)
            log.error(
                "watchdog_restart component=%s result=duplicate_target",
                component,
            )
            continue
        attempted_targets.add(target)
        try:
            restart_launch_agent(target)
        except Exception:
            failures.append(component)
            log.error(
                "watchdog_restart component=%s result=failed",
                component,
            )
        else:
            log.warning(
                "watchdog_restart component=%s result=success",
                component,
            )
    return tuple(failures)


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
    restart_failures = restart_selected_components(
        labels,
        daemon_label=args.label,
    )
    if labels or restart_failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
