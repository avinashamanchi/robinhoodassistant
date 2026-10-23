"""Daemon-hosted autopilot (paper-only, readiness-gated).

* ``decisions``: strategies (the backtester's classes), decision vocabulary
  and startup configuration checks.
* ``engine``: one decision cycle; orders only through the risk engine, with
  intent-derived idempotency keys.
* ``identity``: intent keys, configuration and decision-code fingerprints,
  and the running commit.
* ``evidence``: durable, encrypted cycle and backtest evidence.
* ``readiness``: the fail-closed gate for ``autopilot.mode: paper``.
* ``runner``: the daemon's single owner of scheduling and execution.
* ``backtest_evidence``: real-data backtest evidence.
* ``cli``: operator commands; none of them trades.
"""

from .decisions import (
    ACTOR_PREFIX,
    DEGRADED_REASONS,
    FLAT,
    HOLD,
    IN_FLIGHT_STATUSES,
    LONG,
    STRATEGIES,
    AutopilotDisabled,
    Decision,
    autopilot_config_problems,
    require_paper,
    resolve_universe,
    strategy_decision,
)
from .engine import Autopilot

__all__ = [
    "ACTOR_PREFIX",
    "DEGRADED_REASONS",
    "FLAT",
    "HOLD",
    "IN_FLIGHT_STATUSES",
    "LONG",
    "STRATEGIES",
    "Autopilot",
    "AutopilotDisabled",
    "Decision",
    "autopilot_config_problems",
    "require_paper",
    "resolve_universe",
    "strategy_decision",
]
