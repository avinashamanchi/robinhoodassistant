"""The daemon-hosted autopilot: single owner of scheduling and execution.

Ownership model
---------------
The daemon process is the only owner of autopilot decisions and autopilot
orders. It holds the ``runtime:daemon`` tenure (one daemon per database), its
broker is wrapped by ``TenureGuardedBroker``, and this runner adds:

* at most one cycle per market session, ``run_after_open_minutes`` after the
  open, recorded durably (``evidence.cycle_key``), so a restart or a second
  scheduler finds the session already done; failed attempts are bounded;
* the readiness gate before every cycle. Orders are placed only in
  ``mode: paper`` with every requirement met; otherwise the cycle decides
  and records evidence without touching an order;
* tenure checks before the cycle, before every order (in the engine) and
  before recording evidence; a lost tenure raises ``TenureLost`` and stops
  the daemon instead of writing anything else;
* cancellation between symbols on shutdown or timeout. A cancelled cycle is
  not recorded as complete, and re-running it is safe because every order
  intent has a stable idempotency key.

The app and MCP server never run the autopilot. They may read evidence
through the audit log; human approvals they make are ordinary orders that
the autopilot's ownership rules leave alone.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
from pathlib import Path
import threading
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from ..assets import AssetClass
from .decisions import DEGRADED_REASONS
from .engine import Autopilot
from .evidence import EvidenceStore, cycle_key, decision_counts
from .identity import approval_fingerprint, config_fingerprint, decision_code_fingerprint
from .readiness import evaluate_readiness, write_report

log = logging.getLogger("trading_assistant.autopilot")

_NEW_YORK = ZoneInfo("America/New_York")
MAX_FAILED_ATTEMPTS_PER_SESSION = 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AutopilotRunner:
    def __init__(
        self,
        *,
        config,
        service,
        autopilot: Autopilot,
        store: EvidenceStore,
        root: Path,
        tenure_guard=None,
        now: Callable[[], datetime] = _utcnow,
        installation_check: Optional[Callable[[Path], object]] = None,
    ) -> None:
        self.config = config
        self.service = service
        self.autopilot = autopilot
        self.store = store
        self.root = root
        self.tenure_guard = tenure_guard
        self.now = now
        self.installation_check = installation_check
        self._cancel = threading.Event()
        self.last_result: Optional[dict] = None

    def request_cancel(self) -> None:
        """Stop the running cycle between symbols (shutdown or timeout)."""
        self._cancel.set()

    def _require_tenure(self) -> None:
        if self.tenure_guard is not None:
            self.tenure_guard.ensure_owned()

    def due_session(self, now: datetime) -> Optional[date]:
        """The open session a cycle is due for, or None."""
        if self.config.autopilot.mode == "off":
            return None
        observation = self.service.market_clock(AssetClass.EQUITY).observe(now)
        if not observation.is_open:
            return None
        opened = observation.most_recent_open
        if now < opened + timedelta(minutes=self.config.autopilot.run_after_open_minutes):
            return None
        return opened.astimezone(_NEW_YORK).date()

    def _readiness(self, now: datetime):
        return evaluate_readiness(
            config=self.config,
            store=self.store,
            root=self.root,
            now=now,
            tenure_guard=self.tenure_guard,
            breakers=getattr(self.service, "breakers", None),
            installation_check=self.installation_check,
        )

    def run_if_due(self) -> Optional[dict]:
        """Run this session's cycle if it is due and not yet recorded."""
        from ..ops.tenure import TenureLost

        now = self.now()
        try:
            session = self.due_session(now)
        except Exception:
            log.warning("autopilot market clock unavailable; no cycle this tick")
            return None
        if session is None:
            return None
        mode = self.config.autopilot.mode
        config_fp = config_fingerprint(self.config)
        code_fp = decision_code_fingerprint(self.config.autopilot.strategy)
        key = cycle_key(mode, session, approval_fingerprint(config_fp, code_fp))
        if self.store.session_completed(key):
            return None
        if self.store.failed_attempts(key) >= MAX_FAILED_ATTEMPTS_PER_SESSION:
            return None

        self._cancel.clear()
        self._require_tenure()
        report = self._readiness(now)
        execute = mode == "paper" and report.ready
        self.autopilot.dry_run = not execute
        base_detail = {
            "session": session.isoformat(),
            "started_at": now.isoformat(),
            "mode": mode,
            "execution": "paper" if execute else "simulated",
            "strategy": self.config.autopilot.strategy,
            "config_fingerprint": config_fp,
            "code_fingerprint": code_fp,
            "code_identity": report.code_identity,
            "readiness": {
                "ready": report.ready,
                "unmet": [r.name for r in report.unmet()],
            },
        }
        try:
            results = self.autopilot.run_once(session=session, cancel=self._cancel)
        except TenureLost:
            raise
        except Exception as error:
            log.error("autopilot cycle failed code=autopilot_cycle_failed")
            self._require_tenure()
            self.store.record_cycle(
                key=key,
                session=session,
                result_code="failed",
                detail={**base_detail, "error": type(error).__name__},
            )
            self._write_report(now)
            self.last_result = {"session": session.isoformat(), "result": "failed"}
            return self.last_result

        decisions = [d.evidence() for d in self.autopilot.last_decisions]
        if any(d["reason"] == "cancelled" for d in decisions):
            log.warning("autopilot cycle cancelled; session %s will be retried", session)
            self.last_result = {"session": session.isoformat(), "result": "cancelled"}
            return self.last_result
        if any(d["reason"] in DEGRADED_REASONS for d in decisions):
            result_code = "degraded"
        elif execute:
            result_code = "executed"
        elif mode == "paper":
            result_code = "blocked"
        else:
            result_code = "observed"
        detail = {
            **base_detail,
            "finished_at": self.now().isoformat(),
            "decisions": decisions,
            "counts": decision_counts(decisions),
            "risk_rejections": sum(1 for d in decisions if d["reason"] == "risk_rejected"),
            "intended_actions": [
                {"symbol": r.get("symbol"), "side": r.get("side"), "qty": r.get("qty"), "notional": r.get("notional")}
                for r in results
                if r.get("dry_run")
            ],
            "orders": [
                {"symbol": r.get("symbol"), "side": r.get("side"), "order_id": r.get("order_id"), "status": r.get("status")}
                for r in results
                if not r.get("dry_run")
            ],
        }
        self._require_tenure()
        self.store.record_cycle(key=key, session=session, result_code=result_code, detail=detail)
        self._write_report(self.now())
        log.info(
            "autopilot session=%s mode=%s result=%s ready=%s unmet=%s",
            session,
            mode,
            result_code,
            report.ready,
            ",".join(r.name for r in report.unmet()) or "-",
        )
        self.last_result = {"session": session.isoformat(), "result": result_code}
        return self.last_result

    def _write_report(self, now: datetime) -> None:
        try:
            write_report(self._readiness(now), self.root)
        except Exception:
            log.warning("autopilot readiness report could not be written")
