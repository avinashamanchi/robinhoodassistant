"""Operator commands. None of them trades.

    python -m trading_assistant.autopilot readiness   # read-only report
    python -m trading_assistant.autopilot dry-run     # decide once, no orders
    python -m trading_assistant.autopilot backtest    # record backtest evidence

Trading runs only inside the daemon (``autopilot.runner``), which the
operator starts explicitly. ``dry-run`` and ``backtest`` use the
``paper-drill`` role, whose exclusive maintenance tenure means they refuse to
run while the app, daemon or MCP server is up. ``backtest`` downloads real
market data with the operator's Alpaca credentials. ``readiness`` reads only
the report the daemon writes and needs no secrets.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import logging
import sys
import time
from typing import Callable

from .decisions import (
    AutopilotDisabled,
    autopilot_config_problems,
    require_paper,
    resolve_universe,
)
from .engine import Autopilot
from .readiness import REPORT_RELATIVE, render_report

log = logging.getLogger("trading_assistant.autopilot")

RUNTIME_ROLE = "paper-drill"

# Startup failures meaning "the broker could not be reached", not drift.
_TRANSIENT_STARTUP_FAILURES = frozenset(
    {"broker_reconciliation_dependency_unavailable"}
)


def build_container_with_retry(
    build: Callable[[], object],
    *,
    attempts: int,
    retry_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
):
    """Build the runtime container, retrying only transient broker outages.

    Real drift (any other reconciliation failure) fails closed at once. A
    failed attempt has already released its runtime tenure.
    """
    from ..orders.startup import StartupReconciliationFailed

    for attempt in range(1, attempts + 1):
        try:
            return build()
        except StartupReconciliationFailed as exc:
            if str(exc) not in _TRANSIENT_STARTUP_FAILURES or attempt >= attempts:
                raise
            log.warning(
                "autopilot startup broker reconciliation unavailable "
                "(attempt %d/%d); retrying in %ss",
                attempt,
                attempts,
                retry_seconds,
            )
            sleep(retry_seconds)
    raise AssertionError("unreachable")  # pragma: no cover


def _readiness(root) -> int:
    path = root / REPORT_RELATIVE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(
            "no readiness report yet: the daemon writes it after each "
            "autopilot session (autopilot.mode observe or paper)"
        )
        return 1
    print(render_report(payload))
    return 0 if payload.get("ready") else 1


def _with_container(config, run: Callable) -> int:
    from .. import bootstrap
    from ..logging import runtime_startup
    from ..security.secrets import load_role_secrets

    secrets = load_role_secrets(RUNTIME_ROLE, config=config)
    with runtime_startup(RUNTIME_ROLE, secrets):
        container = build_container_with_retry(
            lambda: bootstrap.build_container(config, secrets, runtime_role=RUNTIME_ROLE),
            attempts=3,
            retry_seconds=60.0,
        )
        primary_failure = False
        try:
            return run(container, secrets)
        except BaseException:
            primary_failure = True
            raise
        finally:
            guard = getattr(container, "runtime_tenure_guard", None)
            if guard is not None:
                try:
                    released = guard.close()
                except BaseException:
                    if not primary_failure:
                        raise RuntimeError("runtime_tenure_cleanup_uncertain") from None
                else:
                    if not released and not primary_failure:
                        raise RuntimeError("runtime_tenure_cleanup_uncertain")


def _dry_run(config) -> int:
    from ..analyst.live_features import build_live_feature_provider

    def run(container, secrets) -> int:
        autopilot = Autopilot(
            container.service,
            build_live_feature_provider(
                config,
                secrets,
                scheduled_service=container.service,
                rate_limiter=container.rate_limiter,
                runtime_role=RUNTIME_ROLE,
                require_market_clock=True,
            ),
            universe=resolve_universe(config),
            notional_per_trade=config.autopilot.notional_per_trade,
            max_orders_per_day=config.autopilot.max_orders_per_day,
            strategy=config.autopilot.strategy,
            dry_run=True,
            max_feature_age=timedelta(hours=config.autopilot.max_feature_age_hours),
            tenure_guard=getattr(container, "runtime_tenure_guard", None),
        )
        intended = autopilot.run_once()
        for decision in autopilot.last_decisions:
            print(json.dumps(decision.evidence(), sort_keys=True))
        print(f"dry run: {len(intended)} intended order(s); nothing placed or recorded")
        return 0

    return _with_container(config, run)


def _backtest(config, years: int) -> int:
    from ..installation import source_root
    from .backtest_evidence import fetch_alpaca_frames, record_backtest_evidence
    from .evidence import EvidenceStore

    def run(container, secrets) -> int:
        symbols = resolve_universe(config) + ["SPY"]
        frames = fetch_alpaca_frames(symbols, secrets, years=years)
        detail = record_backtest_evidence(
            config,
            frames,
            store=EvidenceStore(container.session_factory, actor=f"autopilot:{config.autopilot.strategy}"),
            root=source_root(),
            now=datetime.now(timezone.utc),
            data_source="alpaca",
        )
        print(
            f"recorded backtest evidence {detail['window_start']}..{detail['window_end']} "
            f"({detail['calendar_days']} days, {len(detail['rows'])} rows). "
            f"{detail['disclaimer']}"
        )
        return 0

    return _with_container(config, run)


_RETIRED = {"--once", "--startup-attempts", "--startup-retry-seconds"}


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    if not arguments or arguments[0] in _RETIRED or arguments[0] == "--dry-run":
        if arguments[:1] == ["--dry-run"]:
            arguments = ["dry-run", *arguments[1:]]
        else:
            print(
                "the standalone autopilot loop was retired: the daemon hosts "
                "the autopilot (autopilot.mode observe|paper). Commands: "
                "readiness, dry-run, backtest",
                file=sys.stderr,
            )
            return 2
    parser = argparse.ArgumentParser(
        prog="python -m trading_assistant.autopilot",
        description="Autopilot operator commands. None of them trades.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("readiness", help="print the daemon's readiness report")
    commands.add_parser("dry-run", help="decide once; place and record nothing")
    backtest = commands.add_parser(
        "backtest", help="record real-data backtest evidence (uses Alpaca data)"
    )
    backtest.add_argument("--years", type=int, default=3, choices=range(2, 11))
    args = parser.parse_args(arguments)

    from ..config import BrokerKind, load_config
    from ..installation import source_root

    if args.command == "readiness":
        return _readiness(source_root())

    config = load_config()
    require_paper(config)
    if config.trading.broker is not BrokerKind.ALPACA:
        raise AutopilotDisabled("autopilot commands require trading.broker=alpaca (paper)")
    problems = autopilot_config_problems(config)
    if problems:
        raise AutopilotDisabled("autopilot configuration cannot trade: " + "; ".join(problems))
    if args.command == "dry-run":
        return _dry_run(config)
    return _backtest(config, args.years)
