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
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select, update

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
        cycle_timeout_seconds: float = 90.0,
        daily_task_timeout_seconds: float = 120.0,
        shadow=None,
        digest_source=None,
    ) -> None:
        self.service = service
        self.notifier = notifier or NullNotifier()
        self.auto_execute = auto_execute
        self.poll_interval = poll_interval_seconds
        self.max_quote_age_seconds = max_quote_age_seconds
        self.cycle_timeout = cycle_timeout_seconds
        self.daily_task_timeout = daily_task_timeout_seconds
        self.shadow = shadow                 # ShadowRunner or None (D1)
        self.digest_source = digest_source   # screen source for the digest (D2)
        self._last_daily = None              # date of last daily-tasks run
        self._core_task: Optional[asyncio.Task[Any]] = None
        self._daily_task: Optional[asyncio.Task[Any]] = None

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

    def _claim_rule(self, rule_id: int) -> bool:
        """Atomically claim active -> processing exactly once."""
        with self.service.session_factory() as s:
            result = s.execute(
                update(Rule)
                .where(Rule.id == rule_id, Rule.state == "active")
                .values(state="processing")
            )
            s.commit()
            return result.rowcount == 1

    def _finish_claim(self, rule_id: int, state: str) -> None:
        with self.service.session_factory() as s:
            s.execute(
                update(Rule)
                .where(Rule.id == rule_id, Rule.state == "processing")
                .values(state=state)
            )
            s.commit()

    def _effective_action(self, rule: dict, action: dict) -> dict | None:
        if rule["kind"] not in ("target", "stop", "trailing", "time"):
            return dict(action)
        side = action["side"]
        available = self.service.available_reduce_qty(rule["ticker"], side)
        if available <= 0:
            return None
        adjusted = dict(action)
        if adjusted.get("qty") is not None:
            adjusted["qty"] = str(min(Decimal(str(adjusted["qty"])), available))
        else:
            quote = self.service.broker.get_quote(rule["ticker"])
            requested = Decimal(str(adjusted.get("notional", 0))) / quote.last
            adjusted.pop("notional", None)
            adjusted["qty"] = str(min(requested, available))
        return adjusted

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
        quotes: dict[str, Any] = {}
        for rule in self._active_rules():
            rule_id, ticker, action = rule["id"], rule["ticker"], rule["action"]
            # Equity price rules cannot execute while the equity clock is closed.
            # Avoid burning market-data quota on the same stale snapshot all night.
            # Crypto uses its independent always-open clock and continues normally.
            if not self.service.market_is_open(ticker):
                continue
            if ticker not in quotes:
                quotes[ticker] = self.service.broker.get_quote(ticker)
            quote = quotes[ticker]
            if not self._fires(rule, quote):
                continue
            # Compare-and-set claim prevents concurrent monitors from double firing.
            if not self._claim_rule(rule_id):
                continue
            try:
                effective_action = self._effective_action(rule, action)
                if effective_action is None:
                    self._finish_claim(rule_id, "canceled")
                    actions.append(
                        {
                            "rule_id": rule_id,
                            "proposal": None,
                            "executed": None,
                            "oco_canceled": 0,
                            "error": "no unreserved position to exit",
                        }
                    )
                    continue

                proposal = self.service.propose_order(
                    ticker=ticker,
                    side=effective_action["side"],
                    order_type=effective_action.get("order_type", "market"),
                    qty=effective_action.get("qty"),
                    notional=effective_action.get("notional"),
                    limit_price=effective_action.get("limit_price"),
                    idempotency_key=f"rule-{rule_id}",
                )
                self.notifier.send(
                    f"Rule {rule_id} ({rule['kind']}) triggered on {ticker}: "
                    f"proposal #{proposal['order_id']} [{proposal['status']}]"
                )
                if proposal["status"] != "proposed":
                    self._finish_claim(rule_id, "failed")
                    actions.append(
                        {
                            "rule_id": rule_id,
                            "proposal": proposal,
                            "executed": None,
                            "oco_canceled": 0,
                        }
                    )
                    continue

                executed = None
                if self.auto_execute and rule["pre_approved"]:
                    executed = self.service.approve_order(
                        proposal["order_id"],
                        actor=f"rule:{rule_id}",
                        reason="pre-approved rule execution",
                    )
                    if not executed.get("executed"):
                        self._finish_claim(rule_id, "failed")
                        actions.append(
                            {
                                "rule_id": rule_id,
                                "proposal": proposal,
                                "executed": executed,
                                "oco_canceled": 0,
                            }
                        )
                        continue

                self._finish_claim(rule_id, "triggered")
                canceled = 0
                if (
                    executed is not None
                    and executed.get("executed")
                    and rule["kind"] in ("stop", "trailing", "time")
                    and rule["plan_id"] is not None
                ):
                    canceled = self._cancel_siblings(rule["plan_id"], rule_id)

                actions.append(
                    {
                        "rule_id": rule_id,
                        "proposal": proposal,
                        "executed": executed,
                        "oco_canceled": canceled,
                    }
                )
            except Exception as exc:
                self._finish_claim(rule_id, "active")
                log.exception("rule %s execution failed; returned to active", rule_id)
                actions.append(
                    {
                        "rule_id": rule_id,
                        "proposal": None,
                        "executed": None,
                        "oco_canceled": 0,
                        "error": type(exc).__name__,
                    }
                )
        return actions

    def run_daily_tasks(self, today=None) -> dict[str, Any]:
        """Once per day: grade matured shadow calls, run a fresh shadow batch, and
        send the morning digest. Idempotent within a day (guarded by _last_daily)."""
        from datetime import datetime, timezone

        today = today or datetime.now(timezone.utc).date()
        if self._last_daily == today:
            return {"ran": False}
        self._last_daily = today
        result: dict[str, Any] = {"ran": True}
        if self.shadow is not None:
            try:
                result["shadow_graded"] = self.shadow.grade_due()
                result["shadow_new"] = len(self.shadow.run_once())
            except Exception:
                log.exception("shadow tasks failed")
        try:
            from ..analyst.digest import compose_digest

            self.notifier.send(compose_digest(self.service, shadow=self.shadow,
                                              screen_source=self.digest_source))
            result["digest_sent"] = True
        except Exception:
            log.exception("digest failed")
        return result

    # ── reconciliation on restart ──────────────────────────────
    def reconcile(self) -> dict[str, Any]:
        """Synchronize broker truth before resuming persisted rules on startup."""
        report = self.service.reconciliation.reconcile()
        order_sync = self.service.serialize_reconciliation_report(report)
        position_reconciliation = self.service.reconcile_positions()
        with self.service.session_factory() as s:
            recovered = s.execute(
                update(Rule)
                .where(Rule.state == "processing")
                .values(state="active")
            ).rowcount
            s.commit()
            active = s.execute(
                select(Rule).where(Rule.state == "active")
            ).scalars().all()
            triggered = s.execute(
                select(Rule).where(Rule.state == "triggered")
            ).scalars().all()
        summary = {
            "active": len(active),
            "triggered": len(triggered),
            "claims_recovered": recovered,
            "order_sync": order_sync,
            "position_reconciliation": position_reconciliation,
        }
        log.info("daemon reconcile: %s", summary)
        return summary

    def _core_cycle(self) -> None:
        order_sync = self.service.sync_open_orders()
        if order_sync.get("failed", 0):
            self.service.trip_all_killswitches(
                "runtime broker order reconciliation failed"
            )
            raise RuntimeError(
                "runtime broker order reconciliation failed; kill switches tripped"
            )
        self.service.enforce_daily_loss_limits()
        self.tick()

    async def _bounded_core_cycle(self) -> None:
        """Run one safety cycle without ever blocking the asyncio event loop.

        ``wait_for`` cannot kill a Python worker thread, so a timed-out task is
        retained and no replacement cycle is started until it exits. This avoids
        accumulating workers or racing multiple rule evaluators while the
        external launchd watchdog restarts a genuinely wedged process.
        """
        if self._core_task is not None:
            if not self._core_task.done():
                raise TimeoutError("previous daemon core cycle is still running")
            completed = self._core_task
            self._core_task = None
            completed.result()

        self._core_task = asyncio.create_task(asyncio.to_thread(self._core_cycle))
        current = self._core_task
        try:
            await asyncio.wait_for(
                asyncio.shield(current), timeout=self.cycle_timeout
            )
        except BaseException:
            if current.done():
                self._core_task = None
            raise
        self._core_task = None

    async def _bounded_daily_tasks(self) -> None:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self.run_daily_tasks),
                timeout=self.daily_task_timeout,
            )
        except TimeoutError:
            log.error(
                "daily analysis exceeded %.1fs; safety heartbeat loop continues",
                self.daily_task_timeout,
            )
        except Exception:
            log.exception("daily analysis task failed")

    def _schedule_daily_tasks(self) -> None:
        if self._daily_task is not None and not self._daily_task.done():
            return
        if self._daily_task is not None:
            self._daily_task.result()
        self._daily_task = asyncio.create_task(self._bounded_daily_tasks())

    # ── async loop with exponential backoff (A4) ───────────────
    async def run(self, stop_event: Optional[asyncio.Event] = None) -> None:
        startup = await asyncio.wait_for(
            asyncio.to_thread(self.reconcile), timeout=self.cycle_timeout
        )
        if (
            startup["order_sync"].get("failed", 0)
            or not startup["position_reconciliation"].get("reconciled", False)
        ):
            self.service.trip_all_killswitches("startup reconciliation failed")
            raise RuntimeError("startup reconciliation failed; kill switches tripped")
        attempt = 0
        while not (stop_event and stop_event.is_set()):
            try:
                await self._bounded_core_cycle()
                self.service.write_heartbeat("daemon")  # liveness for /health (D3)
                # Shadow analysis and the digest run independently. They can time
                # out or fail without delaying order sync, rules, or heartbeats.
                self._schedule_daily_tasks()
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
