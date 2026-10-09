"""One autopilot decision cycle over the universe.

The engine decides and, when allowed, places Alpaca *paper* orders through
:meth:`TradingService.propose_order` then :meth:`TradingService.approve_order`,
so the deterministic risk engine runs on every order and stays the final
authority. It never runs on its own: the daemon (``autopilot.runner``) owns
scheduling, runtime tenure and the readiness gate; ``dry_run`` cycles decide
without touching any order.

Before acting on a symbol it requires:

* broker order truth synced first (``sync_open_orders``), so fills from
  earlier cycles are on the local ledger before anything is decided;
* fresh features (newest bar within ``max_feature_age``) on completed
  sessions only (``signals.sessions``);
* no order for that symbol already in flight, including an uncertain
  submission still awaiting reconciliation;
* ownership: it exits only the quantity its own fills bought, and never buys
  into or sells out of a position another workflow holds.

Duplicate safety: every order's idempotency key is derived from the intended
action (strategy, symbol, side, session; see ``identity.action_key``) and is
sent to the broker as ``client_order_id``. A restarted or repeated cycle
therefore finds the same order. A still-``PROPOSED`` replay is approved (the
earlier run stopped between recording intent and approving); any other
replayed state is reported and left alone. A submission whose response was
lost is recorded by the outbox as ``acceptance_unknown`` and is never
resubmitted; reconciliation resolves it by ``client_order_id``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import logging
import threading
from typing import Callable, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from ..broker.models import OrderSide, OrderStatus
from ..db.models import Fill, Order, fill_has_trusted_identity
from ..dependencies import RequiredDependencyUnavailable
from ..signals.models import MarketFeatures
from .decisions import (
    ACTOR_PREFIX,
    FLAT,
    IN_FLIGHT_STATUSES,
    LONG,
    STRATEGIES,
    Decision,
    require_paper,
    strategy_decision,
)
from .identity import action_key

log = logging.getLogger("trading_assistant.autopilot")

_NEW_YORK = ZoneInfo("America/New_York")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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

    # ── ownership of the runtime ──────────────────────────────────────────────
    def _require_tenure(self) -> None:
        """Fail closed (``TenureLost``) once this runtime lost its tenure."""
        if self.tenure_guard is not None:
            self.tenure_guard.ensure_owned()

    # ── ledger and broker truth ───────────────────────────────────────────────
    def _orders_today(self) -> int:
        """Orders this autopilot approved since UTC midnight."""
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
        try:
            positions = self.service.get_positions()
        except RequiredDependencyUnavailable:
            return None
        return {
            str(p["ticker"]).upper(): Decimal(str(p["qty"])) for p in positions
        }

    def _has_in_flight_order(self, session, symbol: str) -> bool:
        return (
            session.execute(
                select(Order.id)
                .where(
                    Order.ticker == symbol,
                    Order.status.in_(IN_FLIGHT_STATUSES),
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

    def _feature_age_hours(self, features: MarketFeatures) -> float:
        as_of = features.as_of
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        return round((self.now() - as_of).total_seconds() / 3600, 1)

    def _features_stale(self, features: MarketFeatures) -> bool:
        if self.max_feature_age is None:
            return False
        return self._feature_age_hours(features) > (
            self.max_feature_age.total_seconds() / 3600
        )

    def _sync_broker_orders(self) -> bool:
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

    # ── orders ────────────────────────────────────────────────────────────────
    def _submit(
        self,
        symbol: str,
        side: str,
        *,
        session: date,
        qty: Optional[Decimal] = None,
        notional: Optional[Decimal] = None,
    ) -> tuple[Optional[dict], str]:
        """Place (or resume) the intended order. Returns (result, reason)."""
        key = action_key(self.strategy, symbol, side, session)
        if self.dry_run:
            intended = {
                "symbol": symbol,
                "side": side,
                "qty": str(qty) if qty is not None else None,
                "notional": str(notional) if notional is not None else None,
                "dry_run": True,
                "intent_key": key,
            }
            log.info("autopilot DRY-RUN would place %s", intended)
            return intended, "intended"
        reason = f"autopilot {side} via {self.strategy}"
        request_id = uuid4().hex
        self._require_tenure()
        proposal = self.service.propose_order(
            symbol,
            side,
            "market",
            qty=str(qty) if qty is not None else None,
            notional=str(notional) if notional is not None else None,
            idempotency_key=key,
            actor=self.actor,
            reason=reason,
            request_id=request_id,
        )
        status = proposal.get("status")
        replayed = bool(proposal.get("idempotent_replay"))
        if status != OrderStatus.PROPOSED.value:
            if replayed:
                log.info("autopilot intent %s %s already %s", side, symbol, status)
                return None, f"intent_already_{status}"
            log.info(
                "autopilot skip %s %s: %s",
                side,
                symbol,
                proposal.get("risk_reasons") or status,
            )
            return None, "risk_rejected"
        self._require_tenure()
        approved = self.service.approve_order(
            proposal["order_id"],
            actor=self.actor,
            reason=reason,
            request_id=request_id,
        )
        final_status = approved.get("status")
        result = {
            "symbol": symbol,
            "side": side,
            "order_id": proposal["order_id"],
            "status": final_status,
            "executed": bool(approved.get("executed")),
            "broker_order_id": approved.get("broker_order_id"),
            "replayed": replayed,
        }
        log.info("autopilot placed %s", result)
        if final_status == OrderStatus.ACCEPTANCE_UNKNOWN.value:
            return result, "submission_uncertain"
        if final_status == OrderStatus.EXPIRED.value:
            return None, "intent_expired"
        if final_status in (OrderStatus.REJECTED.value, OrderStatus.CANCELED.value):
            return None, "risk_rejected"
        if not approved.get("executed") and approved.get("error"):
            return None, f"intent_already_{final_status}"
        return result, ("resumed_intent" if replayed else "submitted")

    def _plan(
        self, signal: str, held: Decimal, owned: Decimal
    ) -> tuple[str, Optional[Decimal], str]:
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

    def _heal_transient_breakers(self, open_cache: Optional[dict] = None) -> None:
        """Clear latched data/liquidity breakers for open markets only.

        Never touches broker drift, loss, drawdown or operator-global latches.
        """
        from ..assets import AssetClass
        from ..risk.breakers import BreakerScope

        cache = {} if open_cache is None else open_cache
        seen_classes: set = set()
        scopes = []
        for symbol in self.universe:
            ac = AssetClass.for_symbol(symbol)
            if ac not in seen_classes:
                seen_classes.add(ac)
                scopes.append((symbol, BreakerScope.data(ac)))
            scopes.append((symbol, BreakerScope.liquidity(symbol)))
        for symbol, scope in scopes:
            if not self._market_open(symbol, cache):
                continue
            try:
                state = self.service.breakers.get(scope)
            except Exception:
                continue
            if state is None or not state.tripped:
                continue
            try:
                self._require_tenure()
                self.service.reset_killswitch(
                    scope,
                    actor=self.actor,
                    reason="autopilot transient-breaker self-heal",
                    expected_generation=state.generation,
                    request_id=uuid4().hex,
                )
                log.warning("autopilot cleared transient breaker %s", scope.key)
            except Exception as exc:
                if type(exc).__name__ == "TenureLost":
                    raise
                log.info("autopilot left breaker %s tripped (%s)", scope.key, exc)

    def _market_open(self, symbol: str, cache: dict) -> bool:
        from ..assets import AssetClass

        ac = AssetClass.for_symbol(symbol)
        if ac not in cache:
            try:
                cache[ac] = bool(self.service.market_is_open(symbol))
            except Exception:
                cache[ac] = False
        return cache[ac]

    # ── one evaluation pass ───────────────────────────────────────────────────
    def run_once(
        self,
        *,
        session: Optional[date] = None,
        cancel: Optional[threading.Event] = None,
    ) -> list[dict]:
        """Evaluate the universe once; place orders unless ``dry_run``.

        ``session`` is the market session the actions belong to (it is part
        of every intent key); it defaults to today's New York date.
        ``cancel`` stops the cycle between symbols (daemon shutdown).
        """
        require_paper(self.service.config)
        self._require_tenure()
        session = session or self.now().astimezone(_NEW_YORK).date()
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
        for index, symbol in enumerate(self.universe):
            if cancel is not None and cancel.is_set():
                decisions.extend(
                    Decision(rest, "n/a", "none", "cancelled")
                    for rest in self.universe[index:]
                )
                break
            if placed >= self.max_orders_per_day:
                decisions.append(Decision(symbol, "n/a", "none", "daily_cap_reached"))
                break
            if not self._market_open(symbol, open_cache):
                decisions.append(Decision(symbol, "n/a", "none", "market_closed"))
                continue
            try:
                features = self.feature_provider(symbol)
            except Exception:
                log.warning("autopilot features unavailable for %s; skipping", symbol)
                decisions.append(Decision(symbol, "n/a", "none", "features_unavailable"))
                continue
            as_of = features.as_of.isoformat()
            age = self._feature_age_hours(features)
            if self._features_stale(features):
                log.warning(
                    "autopilot features for %s are stale (as_of=%s); skipping",
                    symbol,
                    as_of,
                )
                decisions.append(
                    Decision(symbol, "n/a", "none", "features_stale", as_of=as_of, feature_age_hours=age)
                )
                continue
            signal = self.decide(features)
            if signal not in (LONG, FLAT):
                decisions.append(
                    Decision(symbol, signal, "none", "signal_hold", as_of=as_of, feature_age_hours=age)
                )
                continue
            if positions is None:
                positions = self._positions()
                if positions is None:
                    log.warning("autopilot positions unavailable; ending cycle")
                    decisions.append(
                        Decision(symbol, signal, "none", "positions_unavailable", as_of=as_of, feature_age_hours=age)
                    )
                    break
            held = positions.get(symbol, Decimal(0))
            with self.service.session_factory() as s:
                in_flight = self._has_in_flight_order(s, symbol)
                owned = self._owned_qty(s, symbol)
            if in_flight:
                decisions.append(
                    Decision(symbol, signal, "none", "order_in_flight", held, owned, as_of, age)
                )
                continue
            action, sell_qty, reason = self._plan(signal, held, owned)
            result: Optional[dict] = None
            order_status: Optional[str] = None
            if action == "buy":
                result, outcome = self._submit(
                    symbol, "buy", session=session, notional=self.notional_per_trade
                )
                reason = reason if outcome in ("submitted", "intended") else outcome
            elif action == "sell":
                result, outcome = self._submit(symbol, "sell", session=session, qty=sell_qty)
                reason = reason if outcome in ("submitted", "intended") else outcome
            if result is not None:
                order_status = result.get("status")
                executed.append(result)
                placed += 1
            decisions.append(
                Decision(symbol, signal, action, reason, held, owned, as_of, age, order_status)
            )
        for decision in decisions:
            log.info(
                "autopilot decision symbol=%s signal=%s action=%s reason=%s "
                "held=%s owned=%s as_of=%s",
                decision.symbol,
                decision.signal,
                decision.action,
                decision.reason,
                decision.held,
                decision.owned,
                decision.as_of,
            )
        self.last_decisions = decisions
        return executed
