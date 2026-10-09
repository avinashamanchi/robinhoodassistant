"""Decision primitives shared by the engine, evidence and readiness gate.

Strategies are the backtester's own classes (``trading_assistant.strategies``)
so a backtest of ``autopilot.strategy`` evaluates exactly the rule that runs.
Only stateless strategies belong here: the daemon may restart between
sessions, and a strategy that remembered earlier bars would silently reset.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

from ..config import AppConfig, TradingMode
from ..db.models import NONTERMINAL_STATES
from ..broker.models import OrderStatus
from ..signals.models import MarketFeatures
from ..strategies.base import SignalAction, Strategy
from ..strategies.sma_crossover import SmaCrossover
from ..strategies.sma_trend import SmaTrend

ACTOR_PREFIX = "autopilot:"

LONG = "long"
FLAT = "flat"
HOLD = "hold"

STRATEGIES: dict[str, Callable[[], Strategy]] = {
    "sma_trend": SmaTrend,
    "sma_crossover": SmaCrossover,
}

# Orders that may still execute at the broker, including uncertain
# submissions (ACCEPTANCE_UNKNOWN) until reconciliation resolves them. A
# PROPOSED order cannot execute without approval and expires on its own.
IN_FLIGHT_STATUSES = tuple(
    status.value
    for status in NONTERMINAL_STATES
    if status is not OrderStatus.PROPOSED
)

# Skip reasons meaning the cycle could not see the market properly.
DEGRADED_REASONS = frozenset(
    {
        "order_sync_unavailable",
        "features_unavailable",
        "features_stale",
        "positions_unavailable",
    }
)


class AutopilotDisabled(RuntimeError):
    """The autopilot must not run in this configuration."""


def require_paper(config: AppConfig) -> None:
    """Fail closed unless the configuration is paper trading."""
    if config.trading.mode is not TradingMode.PAPER:
        raise AutopilotDisabled(
            "autopilot refuses to run unless trading.mode=paper"
        )


def strategy_decision(strategy: Strategy, features: MarketFeatures) -> str:
    """Translate a strategy signal into the desired position state.

    BUY means "be long", SELL means "be flat", and HOLD means "change
    nothing"; in particular, incomplete features never produce an exit.
    """
    action = strategy.on_bar(features).action
    if action is SignalAction.BUY:
        return LONG
    if action is SignalAction.SELL:
        return FLAT
    return HOLD


@dataclass(frozen=True)
class Decision:
    """What the autopilot concluded for one symbol in one cycle."""

    symbol: str
    signal: str
    action: str
    reason: str
    held: Optional[Decimal] = None
    owned: Optional[Decimal] = None
    as_of: Optional[str] = None
    feature_age_hours: Optional[float] = None
    order_status: Optional[str] = None

    def evidence(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "signal": self.signal,
            "action": self.action,
            "reason": self.reason,
            "held": None if self.held is None else str(self.held),
            "owned": None if self.owned is None else str(self.owned),
            "as_of": self.as_of,
            "feature_age_hours": self.feature_age_hours,
            "order_status": self.order_status,
        }


def resolve_universe(config: AppConfig) -> list[str]:
    return [
        s.upper()
        for s in (
            config.autopilot.universe
            or config.screener.universe
            or config.risk.ticker_allowlist
        )
    ]


def autopilot_config_problems(config: AppConfig) -> list[str]:
    """Settings that would make every cycle a silent no-op or a failure.

    Each is something the risk engine or outbound policy would reject on
    every cycle, so it is reported at startup instead.
    """
    from ..assets import AssetClass

    problems: list[str] = []
    notional = Decimal(str(config.autopilot.notional_per_trade))
    allowlist = {s.upper() for s in config.risk.ticker_allowlist}
    for symbol in resolve_universe(config):
        if AssetClass.for_symbol(symbol) is AssetClass.CRYPTO:
            problems.append(
                f"{symbol}: the autopilot does not trade crypto"
            )
            continue
        if symbol not in allowlist:
            problems.append(f"{symbol}: not in risk.ticker_allowlist")
    if notional > Decimal(str(config.risk.max_notional_per_order)):
        problems.append(
            "autopilot.notional_per_trade exceeds risk.max_notional_per_order"
        )
    return problems
