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
from ..risk.staleness import is_stale
from ..service import TradingService
from . import rules_engine
from .backoff import next_delay

log = logging.getLogger(__name__)


class Monitor:
    def __init__(
        self,
        service: TradingService,
        notifier: Optional[Notifier] = None,
        *,
        auto_execute: bool = False,
        poll_interval_seconds: float = 15.0,
        max_quote_age_seconds: float = 60.0,
    ) -> None:
        self.service = service
        self.notifier = notifier or NullNotifier()
        self.auto_execute = auto_execute
        self.poll_interval = poll_interval_seconds
        self.max_quote_age_seconds = max_quote_age_seconds

    # ── one evaluation pass (synchronous, testable) ────────────
    def _active_rules(self) -> list[dict[str, Any]]:
        with self.service.session_factory() as s:
            rules = s.execute(select(Rule).where(Rule.state == "active")).scalars().all()
            return [
                {
                    "id": r.id, "ticker": r.ticker, "kind": r.kind,
                    "condition": json.loads(r.condition_json),
                    "action": json.loads(r.action_json),
                    "hwm": r.hwm, "deadline": r.deadline,
                    "pre_approved": r.pre_approved, "plan_id": r.plan_id,
                }
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

    def _persist_hwm(self, rule_id: int, hwm) -> None:
        with self.service.session_factory() as s:
            rule = s.get(Rule, rule_id)
            if rule is not None:
                rule.hwm = hwm
                s.commit()

    def _cancel_siblings(self, plan_id: int, except_id: int) -> int:
        """OCO: atomically cancel all other active rules in the plan group."""
        if plan_id is None:
            return 0
        with self.service.session_factory() as s:
            sibs = s.execute(
                select(Rule).where(
                    Rule.plan_id == plan_id,
                    Rule.state == "active",
                    Rule.id != except_id,
                )
            ).scalars().all()
            for r in sibs:
                r.state = "canceled"
            s.commit()
            return len(sibs)

    def _fires(self, rule: dict, quote) -> bool:
        # Staleness gate (A4): never fire on a quote older than the threshold.
        if is_stale(quote.as_of, max_age_seconds=self.max_quote_age_seconds):
            log.warning("rule %s skipped: quote stale (> %ss)", rule["id"], self.max_quote_age_seconds)
            return False
        kind = rule["kind"]
        if kind == "trailing":
            pct = rule["condition"].get("trailing_stop_pct")
            fires, new_hwm = rules_engine.update_trailing_stop(rule["hwm"], quote.last, pct)
            self._persist_hwm(rule["id"], new_hwm)  # persist HWM every tick
            return fires
        if kind == "time":
            return rules_engine.time_stop_fires(rule["deadline"])
        return rules_engine.evaluate(rule["condition"], quote)

    def tick(self) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for rule in self._active_rules():
            rule_id, ticker, action = rule["id"], rule["ticker"], rule["action"]
            quote = self.service.broker.get_quote(ticker)
            if not self._fires(rule, quote):
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
                f"Rule {rule_id} ({rule['kind']}) triggered on {ticker}: "
                f"proposal #{proposal['order_id']} [{proposal['status']}]"
            )

            executed = None
            # Only PRE-APPROVED plan rules auto-exec, and only when the flag is on;
            # ad-hoc rules always await human approval. Execution still passes the
            # full risk engine.
            if self.auto_execute and rule["pre_approved"] and proposal["status"] == "proposed":
                executed = self.service.approve_order(proposal["order_id"])

            # OCO: a full-exit rule (stop/trailing/time) cancels the plan's siblings.
            canceled = 0
            if rule["kind"] in ("stop", "trailing", "time") and rule["plan_id"] is not None:
                canceled = self._cancel_siblings(rule["plan_id"], rule_id)

            actions.append(
                {"rule_id": rule_id, "proposal": proposal, "executed": executed,
                 "oco_canceled": canceled}
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

    # ── async loop with exponential backoff (A4) ───────────────
    async def run(self, stop_event: Optional[asyncio.Event] = None) -> None:
        self.reconcile()
        attempt = 0
        while not (stop_event and stop_event.is_set()):
            try:
                self.tick()
                if attempt:  # recovered — feed is healthy again
                    log.info("monitor recovered after %d failed attempt(s)", attempt)
                attempt = 0
                await asyncio.sleep(self.poll_interval)
            except Exception:  # a bad tick must not kill the daemon
                attempt += 1
                delay = next_delay(attempt)
                log.exception(
                    "monitor tick failed; reconnecting with backoff %.1fs (attempt %d)",
                    delay, attempt,
                )
                await asyncio.sleep(delay)
