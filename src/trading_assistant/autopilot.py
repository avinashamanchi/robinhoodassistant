"""Deterministic autonomous paper-trading loop (opt-in, paper-only).

This is the ONLY component that both *decides* and *executes* without a human in
the loop. It is disabled unless ``autopilot.enabled`` is true in ``config.yaml``,
and it hard-refuses to run on anything other than ``trading.mode: paper``.

It does not bypass a single safety guardrail. Every order it places still goes
through :meth:`TradingService.propose_order` followed by
:meth:`TradingService.approve_order`, so the deterministic risk engine — ticker
allowlist, per-order/position/portfolio notional caps, price-sanity, market-hours,
spread/quote-freshness, and the daily-loss kill switch — runs on every order and
remains the final authority. A rejected proposal is simply skipped; the autopilot
never re-enables live trading and never touches ``approve_order`` on a rejection.

Decisions come from a deterministic strategy class shared with the backtest
harness (``trading_assistant.strategies``), evaluated over the same
``MarketFeatures`` the analyst reads (no LLM in the execution path), so a
backtest of ``autopilot.strategy`` evaluates exactly the rule that trades.

Before acting on a symbol the autopilot also requires:

* broker order truth synced first (``sync_open_orders``), so fills from
  earlier cycles are on the local ledger before anything is decided;
* fresh features (newest bar within ``autopilot.max_feature_age_hours``);
* no order for that symbol already in flight at the broker;
* ownership: it exits only the quantity its own fills bought, and never buys
  into or sells out of a position another workflow (a plan, a human approval)
  holds.

    uv run python -m trading_assistant.autopilot --once   # one cycle, then exit
    uv run python -m trading_assistant.autopilot          # run the loop
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional
from uuid import uuid4

from sqlalchemy import func, select

from .broker.models import OrderSide, OrderStatus
from .config import AppConfig, BrokerKind, TradingMode
from .db.models import NONTERMINAL_STATES, Fill, Order, fill_has_trusted_identity
from .dependencies import RequiredDependencyUnavailable
from .signals.models import MarketFeatures
from .strategies.base import SignalAction, Strategy
from .strategies.sma_crossover import SmaCrossover
from .strategies.sma_trend import SmaTrend

log = logging.getLogger(__name__)

ACTOR_PREFIX = "autopilot:"

LONG = "long"
FLAT = "flat"
HOLD = "hold"


class AutopilotDisabled(RuntimeError):
    """Refuse to run the autopilot unless it is explicitly enabled on paper."""


# ── deterministic strategies (MarketFeatures -> long | flat | hold) ───────────
# Only stateless strategies belong here: ``--once`` runs start a fresh process
# every day, so a strategy that remembers earlier bars would silently reset.
STRATEGIES: dict[str, Callable[[], Strategy]] = {
    "sma_trend": SmaTrend,
    "sma_crossover": SmaCrossover,
}


def strategy_decision(strategy: Strategy, features: MarketFeatures) -> str:
    """Translate a strategy signal into the desired position state.

    BUY means "be long", SELL means "be flat", and HOLD means "change nothing" —
    in particular, incomplete features never produce an exit.
    """
    action = strategy.on_bar(features).action
    if action is SignalAction.BUY:
        return LONG
    if action is SignalAction.SELL:
        return FLAT
    return HOLD


# Orders that may still execute at the broker. A PROPOSED order cannot execute
# without approval and expires on its own, so it does not block a symbol.
_IN_FLIGHT_STATUSES = tuple(
    status.value
    for status in NONTERMINAL_STATES
    if status is not OrderStatus.PROPOSED
)

# Startup failures that mean "the broker could not be reached", not "local and
# broker state disagree". Only these are retried; drift always fails closed.
_TRANSIENT_STARTUP_FAILURES = frozenset(
    {"broker_reconciliation_dependency_unavailable"}
)

# Skip reasons that mean the cycle could not see the market properly. A
# ``--once`` run that hits any of them exits nonzero so launchd shows it.
DEGRADED_REASONS = frozenset(
    {
        "order_sync_unavailable",
        "features_unavailable",
        "features_stale",
        "positions_unavailable",
    }
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def require_paper(config: AppConfig) -> None:
    """Fail closed unless the configuration is paper trading."""
    if config.trading.mode is not TradingMode.PAPER:
        raise AutopilotDisabled(
            "autopilot refuses to run unless trading.mode=paper"
        )


@dataclass(frozen=True)
class Decision:
    """What the autopilot concluded for one symbol in one cycle."""

    symbol: str
    signal: str
    action: str
    reason: str
    held: Optional[Decimal] = None
    owned: Optional[Decimal] = None


class Autopilot:
    def __init__(
        self,
        service,
        feature_provider: Callable[[str], MarketFeatures],
        *,
        universe: list[str],
        notional_per_trade: Decimal,
        max_orders_per_day: int,
        strategy: str = "sma_trend",
        decide: Optional[Callable[[MarketFeatures], str]] = None,
        now: Callable[[], datetime] = _utcnow,
        dry_run: bool = False,
        max_feature_age: Optional[timedelta] = timedelta(hours=120),
        tenure_guard=None,
    ) -> None:
        if not universe:
            raise ValueError("autopilot requires a non-empty universe")
        if decide is None and strategy not in STRATEGIES:
            raise ValueError(f"unknown autopilot strategy: {strategy}")
        self.service = service
        self.feature_provider = feature_provider
        self.universe = [s.upper() for s in universe]
        self.notional_per_trade = Decimal(str(notional_per_trade))
        self.max_orders_per_day = int(max_orders_per_day)
        self.strategy = strategy
        if decide is None:
            rule = STRATEGIES[strategy]()
            decide = lambda features: strategy_decision(rule, features)  # noqa: E731
        self.decide = decide
        self.now = now
        self.dry_run = dry_run
        self.max_feature_age = max_feature_age
        self.tenure_guard = tenure_guard
        self.actor = f"{ACTOR_PREFIX}{strategy}"
        self.last_decisions: list[Decision] = []

    # ── helpers ───────────────────────────────────────────────────────────────
    def _require_tenure(self) -> None:
        """Fail closed once this runtime no longer owns its tenure.

        The guard renews in a background thread; after a lapse (for example
        a laptop sleep longer than the lease) another maintenance owner may
        hold the database, so no further order or ledger write may happen.
        Raises ``TenureLost``.
        """
        if self.tenure_guard is not None:
            self.tenure_guard.ensure_owned()

    def _orders_today(self) -> int:
        """Count orders this autopilot has already submitted since UTC midnight."""
        start = (
            self.now()
            .astimezone(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
        )
        with self.service.session_factory() as s:
            return int(
                s.execute(
                    select(func.count())
                    .select_from(Order)
                    .where(
                        Order.approval_actor.like(f"{ACTOR_PREFIX}%"),
                        Order.approved_at >= start,
                    )
                ).scalar_one()
            )

    def _positions(self) -> Optional[dict[str, Decimal]]:
        """Signed broker quantity per symbol, or None if positions can't be read."""
        try:
            positions = self.service.get_positions()
        except RequiredDependencyUnavailable:
            return None
        return {
            str(p["ticker"]).upper(): Decimal(str(p["qty"])) for p in positions
        }

    def _has_in_flight_order(self, session, symbol: str) -> bool:
        """Whether any workflow has an order for ``symbol`` that may still fill."""
        return (
            session.execute(
                select(Order.id)
                .where(
                    Order.ticker == symbol,
                    Order.status.in_(_IN_FLIGHT_STATUSES),
                )
                .limit(1)
            ).first()
            is not None
        )

    def _owned_qty(self, session, symbol: str) -> Decimal:
        """Net quantity bought by autopilot orders, from trusted fills only."""
        fills = session.execute(
            select(Fill)
            .join(Order, Fill.order_id == Order.id)
            .where(
                Order.ticker == symbol,
                Order.approval_actor.like(f"{ACTOR_PREFIX}%"),
            )
        ).scalars().all()
        net = Decimal(0)
        for fill in fills:
            if not fill_has_trusted_identity(fill):
                continue
            if fill.side == OrderSide.BUY.value:
                net += fill.qty
            elif fill.side == OrderSide.SELL.value:
                net -= fill.qty
        return max(net, Decimal(0))

    def _features_stale(self, features: MarketFeatures) -> bool:
        if self.max_feature_age is None:
            return False
        as_of = features.as_of
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        return self.now() - as_of > self.max_feature_age

    def _submit(
        self,
        symbol: str,
        side: str,
        *,
        qty: Optional[Decimal] = None,
        notional: Optional[Decimal] = None,
    ) -> Optional[dict]:
        request_id = uuid4().hex
        reason = f"autopilot {side} via {self.strategy}"
        if self.dry_run:
            intended = {
                "symbol": symbol,
                "side": side,
                "qty": str(qty) if qty is not None else None,
                "notional": str(notional) if notional is not None else None,
                "dry_run": True,
            }
            log.info("autopilot DRY-RUN would place %s", intended)
            return intended
        proposal = self.service.propose_order(
            symbol,
            side,
            "market",
            qty=str(qty) if qty is not None else None,
            notional=str(notional) if notional is not None else None,
            actor=self.actor,
            reason=reason,
            request_id=request_id,
        )
        if proposal.get("status") != OrderStatus.PROPOSED.value:
            log.info(
                "autopilot skip %s %s: %s",
                side,
                symbol,
                proposal.get("risk_reasons") or proposal.get("status"),
            )
            return None
        approved = self.service.approve_order(
            proposal["order_id"],
            actor=self.actor,
            reason=reason,
            request_id=request_id,
        )
        result = {
            "symbol": symbol,
            "side": side,
            "order_id": proposal["order_id"],
            "status": approved.get("status"),
            "executed": bool(approved.get("executed")),
            "broker_order_id": approved.get("broker_order_id"),
        }
        log.info("autopilot placed %s", result)
        return result

    def _transient_breaker_scopes(self):
        """(symbol, scope) pairs: data:<class> once per asset class, plus
        liquidity:<symbol> per universe symbol.

        Deliberately EXCLUDES broker_drift / loss / drawdown / operator_global —
        those are real safety latches a human must clear, never the autopilot.
        """
        from .assets import AssetClass
        from .risk.breakers import BreakerScope

        scopes = []
        seen_classes = set()
        for symbol in self.universe:
            ac = AssetClass.for_symbol(symbol)
            if ac not in seen_classes:
                seen_classes.add(ac)
                scopes.append((symbol, BreakerScope.data(ac)))
            scopes.append((symbol, BreakerScope.liquidity(symbol)))
        return scopes

    def _heal_transient_breakers(self, open_cache: Optional[dict] = None) -> None:
        """Auto-clear latched TRANSIENT market-condition breakers (stale-data,
        per-symbol liquidity/spread) for markets that are currently open.

        Safe because the real-time staleness and spread checks still run on
        every order — the latch is just what a long-running/after-hours session
        left behind. Closed markets are left alone (a reset there cannot prove
        fresh conditions). Never touches drift/loss/drawdown/global breakers.
        """
        cache = {} if open_cache is None else open_cache
        for symbol, scope in self._transient_breaker_scopes():
            if not self._market_open(symbol, cache):
                continue
            try:
                state = self.service.breakers.get(scope)
            except Exception:
                continue
            if state is None or not state.tripped:
                continue
            try:
                self.service.reset_killswitch(
                    scope,
                    actor=self.actor,
                    reason="autopilot transient-breaker self-heal",
                    expected_generation=state.generation,
                    request_id=uuid4().hex,
                )
                log.warning(
                    "autopilot cleared transient breaker %s", scope.key
                )
            except Exception as exc:
                log.info(
                    "autopilot left breaker %s tripped (%s)", scope.key, exc
                )

    def _market_open(self, symbol: str, cache: dict) -> bool:
        """Asset-class-aware market-open check, cached once per class per cycle."""
        from .assets import AssetClass

        ac = AssetClass.for_symbol(symbol)
        if ac not in cache:
            try:
                cache[ac] = bool(self.service.market_is_open(symbol))
            except Exception:
                cache[ac] = False
        return cache[ac]

    def _sync_broker_orders(self) -> bool:
        """Pull broker order/fill truth onto the local ledger before deciding.

        Without this a long-running loop would keep seeing its own filled
        orders as in flight and never credit their fills to ownership.
        """
        try:
            self.service.sync_open_orders(
                actor=self.actor,
                reason="autopilot pre-decision broker order sync",
                request_id=uuid4().hex,
            )
        except Exception:
            log.warning("autopilot broker order sync unavailable; skipping cycle")
            return False
        return True

    def _plan(
        self, signal: str, held: Decimal, owned: Decimal
    ) -> tuple[str, Optional[Decimal], str]:
        """Choose (action, sell quantity, reason) for one symbol."""
        if signal == LONG:
            if held == 0:
                return "buy", None, "enter_long"
            if held > 0 and owned > 0:
                return "none", None, "already_long"
            return "none", None, "position_managed_elsewhere"
        if signal == FLAT:
            if held == 0:
                return "none", None, "already_flat"
            sell_qty = min(owned, held) if held > 0 else Decimal(0)
            if sell_qty > 0:
                return "sell", sell_qty, "exit_long"
            return "none", None, "position_managed_elsewhere"
        return "none", None, "signal_hold"

    # ── one evaluation pass ───────────────────────────────────────────────────
    def run_once(self) -> list[dict]:
        """Evaluate the whole universe once and place any resulting paper orders.

        Symbols whose market is closed are skipped entirely — attempting orders
        after hours only trips data/liquidity breakers on stale quotes, so the
        loop can run continuously and simply resume trading when the market opens.
        Every symbol's outcome is logged and kept in ``last_decisions``.
        """
        require_paper(self.service.config)
        self._require_tenure()
        open_cache: dict = {}
        if not self.dry_run:
            if not self._sync_broker_orders():
                self.last_decisions = [
                    Decision(symbol, "n/a", "none", "order_sync_unavailable")
                    for symbol in self.universe
                ]
                return []
            self._heal_transient_breakers(open_cache)
        executed: list[dict] = []
        decisions: list[Decision] = []
        placed = self._orders_today()
        positions: Optional[dict[str, Decimal]] = None
        for symbol in self.universe:
            if placed >= self.max_orders_per_day:
                log.info(
                    "autopilot daily order cap reached (%d)",
                    self.max_orders_per_day,
                )
                decisions.append(
                    Decision(symbol, "n/a", "none", "daily_cap_reached")
                )
                break
            if not self._market_open(symbol, open_cache):
                decisions.append(Decision(symbol, "n/a", "none", "market_closed"))
                continue
            try:
                features = self.feature_provider(symbol)
            except Exception:
                log.warning(
                    "autopilot features unavailable for %s; skipping", symbol
                )
                decisions.append(
                    Decision(symbol, "n/a", "none", "features_unavailable")
                )
                continue
            if self._features_stale(features):
                log.warning(
                    "autopilot features for %s are stale (as_of=%s); skipping",
                    symbol,
                    features.as_of.isoformat(),
                )
                decisions.append(Decision(symbol, "n/a", "none", "features_stale"))
                continue
            signal = self.decide(features)
            if signal not in (LONG, FLAT):
                decisions.append(Decision(symbol, signal, "none", "signal_hold"))
                continue
            if positions is None:
                positions = self._positions()
                if positions is None:
                    log.warning(
                        "autopilot positions unavailable; ending cycle"
                    )
                    decisions.append(
                        Decision(symbol, signal, "none", "positions_unavailable")
                    )
                    break
            held = positions.get(symbol, Decimal(0))
            with self.service.session_factory() as s:
                in_flight = self._has_in_flight_order(s, symbol)
                owned = self._owned_qty(s, symbol)
            if in_flight:
                decisions.append(
                    Decision(symbol, signal, "none", "order_in_flight", held, owned)
                )
                continue
            action, sell_qty, reason = self._plan(signal, held, owned)
            result: Optional[dict] = None
            if action != "none":
                self._require_tenure()
            if action == "buy":
                result = self._submit(
                    symbol, "buy", notional=self.notional_per_trade
                )
            elif action == "sell":
                result = self._submit(symbol, "sell", qty=sell_qty)
            if action != "none" and result is None:
                reason = "risk_rejected"
            decisions.append(Decision(symbol, signal, action, reason, held, owned))
            if result is not None:
                executed.append(result)
                placed += 1
        for decision in decisions:
            log.info(
                "autopilot decision symbol=%s signal=%s action=%s reason=%s "
                "held=%s owned=%s",
                decision.symbol,
                decision.signal,
                decision.action,
                decision.reason,
                decision.held,
                decision.owned,
            )
        self.last_decisions = decisions
        return executed


# ── production wiring ─────────────────────────────────────────────────────────
def resolve_universe(config: AppConfig) -> list[str]:
    return [
        s.upper()
        for s in (
            config.autopilot.universe
            or config.screener.universe
            or config.risk.ticker_allowlist
        )
    ]


# The autopilot runs under the existing least-privilege ``paper-drill`` runtime
# role: it grants Alpaca paper + database + field-encryption access but NO LLM
# keys and NO ``live_trading_confirm`` visibility — exactly right for a paper-only,
# no-LLM executor. Reusing it avoids widening the audited production-role surface,
# and the runtime tenure lock still guarantees only one such runtime at a time.
_RUNTIME_ROLE = "paper-drill"


def build_autopilot(
    config: AppConfig, secrets, container, *, dry_run: bool = False
) -> Autopilot:
    from .analyst.live_features import build_live_feature_provider

    provider = build_live_feature_provider(
        config,
        secrets,
        scheduled_service=container.service,
        rate_limiter=container.rate_limiter,
        runtime_role=_RUNTIME_ROLE,
    )
    return Autopilot(
        container.service,
        provider,
        universe=resolve_universe(config),
        notional_per_trade=config.autopilot.notional_per_trade,
        max_orders_per_day=config.autopilot.max_orders_per_day,
        strategy=config.autopilot.strategy,
        dry_run=dry_run,
        max_feature_age=timedelta(hours=config.autopilot.max_feature_age_hours),
        tenure_guard=getattr(container, "runtime_tenure_guard", None),
    )


def autopilot_config_problems(config: AppConfig) -> list[str]:
    """Settings that would make every cycle a silent no-op or a daily failure.

    Each problem is something the risk engine or outbound policy would reject
    on every single cycle, so it is reported at startup instead.
    """
    from .assets import AssetClass

    problems: list[str] = []
    notional = Decimal(str(config.autopilot.notional_per_trade))
    for symbol in resolve_universe(config):
        if AssetClass.for_symbol(symbol) is AssetClass.CRYPTO:
            problems.append(
                f"{symbol}: the autopilot runtime role cannot read crypto "
                "market data"
            )
            continue
        if symbol not in {s.upper() for s in config.risk.ticker_allowlist}:
            problems.append(f"{symbol}: not in risk.ticker_allowlist")
    if notional > Decimal(str(config.risk.max_notional_per_order)):
        problems.append(
            "autopilot.notional_per_trade exceeds risk.max_notional_per_order"
        )
    return problems


def run_loop(
    autopilot: Autopilot,
    *,
    interval: float,
    sleep: Callable[[float], None] = time.sleep,
    max_cycles: Optional[int] = None,
) -> None:
    """Run cycles forever (or ``max_cycles``), surviving ordinary failures.

    A failed cycle is logged and retried next interval, but losing runtime
    tenure or being disabled ends the loop: continuing would trade without
    the exclusive ownership the role requires.
    """
    from .ops.tenure import TenureLost

    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        try:
            results = autopilot.run_once()
            if results:
                log.info("autopilot placed %d order(s)", len(results))
        except (AutopilotDisabled, TenureLost):
            raise
        except Exception:
            log.exception("autopilot cycle failed; continuing")
        sleep(interval)


def build_container_with_retry(
    build: Callable[[], object],
    *,
    attempts: int,
    retry_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
):
    """Build the runtime container, retrying only transient broker outages.

    A once-a-day run that dies on a momentary broker blip loses the whole day.
    Startup reconciliation that fails because the broker was *unreachable* is
    retried; any other failure (including real drift) fails closed at once. A
    failed attempt has already released its runtime tenure.
    """
    from .orders.startup import StartupReconciliationFailed

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


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Autonomous deterministic paper-trading loop (paper-only)."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single evaluation cycle and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="decide and log intended orders without placing any (safe check)",
    )
    parser.add_argument(
        "--startup-attempts",
        type=_positive_int,
        default=3,
        help="attempts when the broker is unreachable at startup (default 3)",
    )
    parser.add_argument(
        "--startup-retry-seconds",
        type=_nonnegative_float,
        default=60.0,
        help="delay between startup attempts (default 60)",
    )
    args = parser.parse_args(argv)

    from . import bootstrap
    from .config import load_config
    from .logging import runtime_startup
    from .security.secrets import load_role_secrets

    config = load_config()
    if not config.autopilot.enabled:
        raise AutopilotDisabled(
            "autopilot is disabled; set autopilot.enabled: true in config.yaml"
        )
    require_paper(config)
    if config.trading.broker is not BrokerKind.ALPACA:
        raise AutopilotDisabled(
            "autopilot requires trading.broker=alpaca (paper) to place real "
            "paper orders"
        )
    problems = autopilot_config_problems(config)
    if problems:
        raise AutopilotDisabled(
            "autopilot configuration cannot trade: " + "; ".join(problems)
        )

    secrets = load_role_secrets(_RUNTIME_ROLE, config=config)
    # runtime_startup installs the redacted, owner-only, rotating role log
    # (logs/paper-drill.runtime.log); no other handler is added here so lines
    # are not duplicated into an unbounded launchd stream file.
    with runtime_startup(_RUNTIME_ROLE, secrets):
        container = build_container_with_retry(
            lambda: bootstrap.build_container(
                config, secrets, runtime_role=_RUNTIME_ROLE
            ),
            attempts=args.startup_attempts,
            retry_seconds=args.startup_retry_seconds,
        )
        primary_failure = False
        try:
            autopilot = build_autopilot(
                config, secrets, container, dry_run=args.dry_run
            )
            if args.once or args.dry_run:
                results = autopilot.run_once()
                degraded = sorted(
                    {
                        d.reason
                        for d in autopilot.last_decisions
                        if d.reason in DEGRADED_REASONS
                    }
                )
                summary = (
                    f"autopilot cycle placed {len(results)} order(s); "
                    f"evaluated {len(autopilot.last_decisions)} symbol(s)"
                    + (f"; degraded: {','.join(degraded)}" if degraded else "")
                )
                log.info(summary)
                print(f"{summary}: {results}")
                return 1 if degraded else 0
            interval = config.autopilot.poll_interval_seconds
            log.info("autopilot loop starting; interval=%ss", interval)
            run_loop(autopilot, interval=interval)
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
                        raise RuntimeError(
                            "runtime_tenure_cleanup_uncertain"
                        ) from None
                else:
                    if not released and not primary_failure:
                        raise RuntimeError("runtime_tenure_cleanup_uncertain")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
