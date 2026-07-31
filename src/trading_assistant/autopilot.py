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

Decisions come from a deterministic strategy over the same ``MarketFeatures`` the
analyst reads (no LLM in the execution path), so behaviour is reproducible.

    uv run python -m trading_assistant.autopilot --once   # one cycle, then exit
    uv run python -m trading_assistant.autopilot          # run the loop
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional
from uuid import uuid4

from sqlalchemy import func, select

from .broker.models import OrderStatus
from .config import AppConfig, BrokerKind, TradingMode
from .db.models import Order
from .dependencies import RequiredDependencyUnavailable
from .signals.models import MarketFeatures

log = logging.getLogger(__name__)


class AutopilotDisabled(RuntimeError):
    """Refuse to run the autopilot unless it is explicitly enabled on paper."""


# ── deterministic strategies (MarketFeatures -> "long" | "flat") ──────────────
def sma_crossover_decision(f: MarketFeatures) -> str:
    """Trend-following long/flat rule.

    Long while the fast average leads the slow average *and* price holds above the
    long-term trend; otherwise flat. Missing inputs are treated as flat, so the
    autopilot never trades on incomplete data.
    """
    if f.sma_20 is None or f.sma_50 is None:
        return "flat"
    if f.sma_20 <= f.sma_50:
        return "flat"
    if (
        f.sma_200 is not None
        and f.last_close is not None
        and f.last_close < f.sma_200
    ):
        return "flat"
    return "long"


STRATEGIES: dict[str, Callable[[MarketFeatures], str]] = {
    "sma_crossover": sma_crossover_decision,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def require_paper(config: AppConfig) -> None:
    """Fail closed unless the configuration is paper trading."""
    if config.trading.mode is not TradingMode.PAPER:
        raise AutopilotDisabled(
            "autopilot refuses to run unless trading.mode=paper"
        )


class Autopilot:
    def __init__(
        self,
        service,
        feature_provider: Callable[[str], MarketFeatures],
        *,
        universe: list[str],
        notional_per_trade: Decimal,
        max_orders_per_day: int,
        strategy: str = "sma_crossover",
        decide: Optional[Callable[[MarketFeatures], str]] = None,
        now: Callable[[], datetime] = _utcnow,
        dry_run: bool = False,
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
        self.decide = decide or STRATEGIES[strategy]
        self.now = now
        self.dry_run = dry_run
        self.actor = f"autopilot:{strategy}"

    # ── helpers ───────────────────────────────────────────────────────────────
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
                        Order.approval_actor.like("autopilot:%"),
                        Order.approved_at >= start,
                    )
                ).scalar_one()
            )

    def _held_qty(self, symbol: str) -> Optional[Decimal]:
        """Signed quantity currently held, or None if positions can't be read."""
        try:
            positions = self.service.get_positions()
        except RequiredDependencyUnavailable:
            return None
        for p in positions:
            if str(p["ticker"]).upper() == symbol:
                return Decimal(str(p["qty"]))
        return Decimal(0)

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

    # ── one evaluation pass ───────────────────────────────────────────────────
    def run_once(self) -> list[dict]:
        """Evaluate the whole universe once and place any resulting paper orders."""
        require_paper(self.service.config)
        executed: list[dict] = []
        placed = self._orders_today()
        for symbol in self.universe:
            if placed >= self.max_orders_per_day:
                log.info(
                    "autopilot daily order cap reached (%d)",
                    self.max_orders_per_day,
                )
                break
            try:
                features = self.feature_provider(symbol)
            except Exception:
                log.warning(
                    "autopilot features unavailable for %s; skipping", symbol
                )
                continue
            target = self.decide(features)
            held = self._held_qty(symbol)
            if held is None:
                log.warning(
                    "autopilot positions unavailable; skipping %s", symbol
                )
                continue
            result: Optional[dict] = None
            if target == "long" and held <= 0:
                result = self._submit(
                    symbol, "buy", notional=self.notional_per_trade
                )
            elif target == "flat" and held > 0:
                result = self._submit(symbol, "sell", qty=held)
            if result is not None:
                executed.append(result)
                placed += 1
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
    )


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
    args = parser.parse_args(argv)

    from . import bootstrap
    from .config import load_config
    from .logging import runtime_startup
    from .security.secrets import load_role_secrets

    logging.basicConfig(level=logging.INFO)
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

    secrets = load_role_secrets(_RUNTIME_ROLE, config=config)
    with runtime_startup(_RUNTIME_ROLE, secrets):
        container = bootstrap.build_container(
            config, secrets, runtime_role=_RUNTIME_ROLE
        )
        primary_failure = False
        try:
            autopilot = build_autopilot(
                config, secrets, container, dry_run=args.dry_run
            )
            if args.once or args.dry_run:
                results = autopilot.run_once()
                print(
                    f"autopilot cycle placed {len(results)} order(s): {results}"
                )
                return 0
            interval = config.autopilot.poll_interval_seconds
            log.info("autopilot loop starting; interval=%ss", interval)
            while True:
                try:
                    results = autopilot.run_once()
                    if results:
                        log.info("autopilot placed %d order(s)", len(results))
                except AutopilotDisabled:
                    raise
                except Exception:
                    log.exception("autopilot cycle failed; continuing")
                time.sleep(interval)
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
