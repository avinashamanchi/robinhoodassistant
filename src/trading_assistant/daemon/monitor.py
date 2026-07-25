"""Monitoring daemon.

Delegates conditional-rule evaluation to :class:`RuleWorker`. A firing creates
a PENDING proposal routed through the risk engine and sends a notification.
This phase never auto-approves and never submits a broker order.

Crash-safe: rules live in the DB, so a restarted daemon resumes from persisted
state. Rules are one-shot (active -> triggered) to avoid re-firing every tick.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import select

from ..db.models import Rule
from ..notifications.base import Notifier, NullNotifier
from ..operations import MutationContext
from ..rules.worker import RuleWorker
from ..service import TradingService

log = logging.getLogger(__name__)


class _KillSwitchesAlreadyTripped(RuntimeError):
    """Signal that the current failure already durably latched safety."""


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
        rule_worker=None,
    ) -> None:
        self.service = service
        self.notifier = notifier or NullNotifier()
        # Retained as a compatibility argument only. RuleWorker has no approval
        # or submission dependency, so even a stale true setting cannot trade.
        self.auto_execute = False
        self.poll_interval = poll_interval_seconds
        self.max_quote_age_seconds = max_quote_age_seconds
        self.cycle_timeout = cycle_timeout_seconds
        self.daily_task_timeout = daily_task_timeout_seconds
        self.shadow = shadow                 # ShadowRunner or None (D1)
        self.digest_source = digest_source   # screen source for the digest (D2)
        self._last_daily = None              # date of last daily-tasks run
        self._core_task: Optional[asyncio.Task[Any]] = None
        self._daily_task: Optional[asyncio.Task[Any]] = None
        self.rule_worker = rule_worker or RuleWorker(
            service,
            service.rule_repository,
            service.rule_application,
            self.notifier,
            max_quote_age_seconds=max_quote_age_seconds,
        )

    # ── one evaluation pass (synchronous, testable) ────────────
    def tick(self):
        context = MutationContext(
            actor="daemon:rules",
            reason="daemon conditional rule evaluation",
            request_id=uuid4().hex,
        )
        return self.rule_worker.tick(
            actor=context.actor,
            reason=context.reason,
            request_id=context.request_id,
        )

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
                log.error(
                    "shadow tasks failed code=shadow_tasks_failed"
                )
        try:
            from ..analyst.digest import compose_digest

            self.notifier.send(compose_digest(self.service, shadow=self.shadow,
                                              screen_source=self.digest_source))
            result["digest_sent"] = True
        except Exception:
            log.error("digest failed code=digest_failed")
        return result

    # ── reconciliation on restart ──────────────────────────────
    def reconcile(self) -> dict[str, Any]:
        """Synchronize broker truth before resuming persisted rules on startup."""
        context = MutationContext(
            actor="daemon:startup",
            reason="daemon startup reconciliation",
            request_id=uuid4().hex,
        )
        order_sync = self.service.sync_open_orders(
            actor=context.actor,
            reason="daemon startup broker order reconciliation",
            request_id=context.request_id,
        )
        position_reconciliation = self.service.reconcile_positions(
            actor=context.actor,
            reason="daemon startup position reconciliation",
            request_id=context.request_id,
        )
        with self.service.session_factory() as s:
            active = s.execute(
                select(Rule).where(Rule.state == "active")
            ).scalars().all()
            triggered = s.execute(
                select(Rule).where(Rule.state == "triggered")
            ).scalars().all()
        summary = {
            "active": len(active),
            "triggered": len(triggered),
            "claims_recovered": 0,
            "order_sync": order_sync,
            "position_reconciliation": position_reconciliation,
        }
        log.info("daemon reconcile: %s", summary)
        return summary

    def _core_cycle(self) -> None:
        context = MutationContext(
            actor="daemon:monitor",
            reason="daemon runtime safety cycle",
            request_id=uuid4().hex,
        )
        order_sync = self.service.sync_open_orders(
            actor=context.actor,
            reason="daemon runtime broker order reconciliation",
            request_id=context.request_id,
        )
        if order_sync.get("failed", 0):
            self.service.trip_all_killswitches(
                actor=context.actor,
                reason="runtime broker order reconciliation failed",
                request_id=context.request_id,
            )
            raise _KillSwitchesAlreadyTripped(
                "runtime broker order reconciliation failed; kill switches tripped"
            )
        self.service.enforce_daily_loss_limits(
            actor=context.actor,
            reason="daemon scheduled daily loss enforcement",
            request_id=context.request_id,
        )
        self.rule_worker.tick(
            actor=context.actor,
            reason="daemon conditional rule evaluation",
            request_id=context.request_id,
        )

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
            log.error(
                "daily analysis task failed "
                "code=daily_analysis_failed"
            )

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
            self.service.trip_all_killswitches(
                actor="daemon:startup",
                reason="startup reconciliation failed",
                request_id=uuid4().hex,
            )
            raise RuntimeError("startup reconciliation failed; kill switches tripped")
        while not (stop_event and stop_event.is_set()):
            try:
                await self._bounded_core_cycle()
                self.service.write_heartbeat("daemon")  # liveness for /health (D3)
                # Shadow analysis and the digest run independently. They can time
                # out or fail without delaying order sync, rules, or heartbeats.
                self._schedule_daily_tasks()
                await asyncio.sleep(self.poll_interval)
            except Exception as exc:
                if not isinstance(exc, _KillSwitchesAlreadyTripped):
                    self.service.trip_all_killswitches(
                        actor="daemon:monitor",
                        reason="daemon mutating cycle failed",
                        request_id=uuid4().hex,
                    )
                log.error(
                    "monitor tick failed code=monitor_cycle_failed "
                    "result=process_exit",
                )
                raise
