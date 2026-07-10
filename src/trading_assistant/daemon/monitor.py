"""Monitoring daemon.

Polls quotes for tickers with active conditional rules; when a rule triggers it
creates a PENDING proposal (routed through the risk engine like any order) and
sends a notification. It never bypasses the human gate unless
``features.auto_execute_preapproved_rules`` is explicitly on — and even then
execution re-runs the risk engine.

Crash-safe: rules live in the DB, so a restarted daemon resumes from persisted
state. Rules are one-shot (active -> triggered) to avoid re-firing every tick.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from sqlalchemy import select

from ..db.models import Rule
from ..notifications.base import Notifier, NullNotifier
from ..service import TradingService
from . import rules_engine

log = logging.getLogger(__name__)


class Monitor:
    def __init__(
        self,
        service: TradingService,
        notifier: Optional[Notifier] = None,
        *,
        auto_execute: bool = False,
        poll_interval_seconds: float = 15.0,
    ) -> None:
        self.service = service
        self.notifier = notifier or NullNotifier()
        self.auto_execute = auto_execute
        self.poll_interval = poll_interval_seconds

    # ── one evaluation pass (synchronous, testable) ────────────
    def _active_rules(self) -> list[tuple[int, str, dict, dict]]:
        with self.service.session_factory() as s:
            rules = s.execute(select(Rule).where(Rule.state == "active")).scalars().all()
            return [
                (r.id, r.ticker, json.loads(r.condition_json), json.loads(r.action_json))
                for r in rules
            ]

    def _mark_triggered(self, rule_id: int) -> bool:
        """Atomically flip active -> triggered. Returns False if already handled."""
        with self.service.session_factory() as s:
            rule = s.get(Rule, rule_id)
            if rule is None or rule.state != "active":
                return False
            rule.state = "triggered"
            s.commit()
            return True

    def tick(self) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for rule_id, ticker, condition, action in self._active_rules():
            quote = self.service.broker.get_quote(ticker)
            if not rules_engine.evaluate(condition, quote):
                continue
            # Claim the rule first so a concurrent tick can't double-fire it.
            if not self._mark_triggered(rule_id):
                continue

            proposal = self.service.propose_order(
                ticker=ticker,
                side=action["side"],
                order_type=action.get("order_type", "market"),
                qty=action.get("qty"),
                notional=action.get("notional"),
                limit_price=action.get("limit_price"),
            )
            self.notifier.send(
                f"Rule {rule_id} triggered on {ticker} "
                f"({rules_engine.describe(condition)}): proposal #{proposal['order_id']} "
                f"[{proposal['status']}]"
            )

            executed = None
            if self.auto_execute and proposal["status"] == "proposed":
                # Pre-approved auto-exec still passes execution-time risk checks.
                executed = self.service.approve_order(proposal["order_id"])

            actions.append(
                {"rule_id": rule_id, "proposal": proposal, "executed": executed}
            )
        return actions

    # ── reconciliation on restart ──────────────────────────────
    def reconcile(self) -> dict[str, int]:
        """Rules persist in the DB; report the resumable state on startup."""
        with self.service.session_factory() as s:
            active = s.execute(
                select(Rule).where(Rule.state == "active")
            ).scalars().all()
            triggered = s.execute(
                select(Rule).where(Rule.state == "triggered")
            ).scalars().all()
        summary = {"active": len(active), "triggered": len(triggered)}
        log.info("daemon reconcile: %s", summary)
        return summary

    # ── async loop ─────────────────────────────────────────────
    async def run(self, stop_event: Optional[asyncio.Event] = None) -> None:
        self.reconcile()
        while not (stop_event and stop_event.is_set()):
            try:
                self.tick()
            except Exception:  # a bad tick must not kill the daemon
                log.exception("monitor tick failed")
            await asyncio.sleep(self.poll_interval)
