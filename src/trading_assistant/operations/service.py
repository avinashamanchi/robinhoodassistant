"""One API-facing facade for panic, breaker reset, and health."""

from __future__ import annotations

import logging

from .audit import AuditRecorder, MutationContext
from .health import (
    OperationalHealthReport,
    build_liveness,
    build_operational_health,
)

_LOG = logging.getLogger(__name__)


class OperationsService:
    def __init__(
        self,
        service,
        audit: AuditRecorder | None = None,
        *,
        rate_limiter=None,
        leases=None,
        provider_budget=None,
        policy_store_maintenance=None,
    ) -> None:
        self.service = service
        self.audit = audit or AuditRecorder(service.session_factory)
        self.rate_limiter = rate_limiter
        self.leases = leases
        self.provider_budget = provider_budget
        self.policy_store_maintenance = policy_store_maintenance

    def _record_best_effort(
        self,
        context: MutationContext,
        action: str,
        *args,
    ) -> None:
        try:
            self.audit.record(
                context,
                action,
                *args,
            )
        except Exception:
            stable_action = {
                "operations.breaker_reset": "operations.reset",
            }.get(action, action)
            _LOG.disabled = False
            _LOG.error(
                "boundary_audit_unavailable action=%s request_id=%s",
                stable_action,
                context.request_id,
            )

    def panic(self, context: MutationContext) -> dict[str, object]:
        report = self.service.panic(
            actor=context.actor,
            reason=context.reason,
            request_id=context.request_id,
        )
        self._record_best_effort(
            context,
            "operations.panic",
            "account",
            "alpaca-paper",
            "safe" if report.get("safe") is True else "unconfirmed",
            {
                "unconfirmed_order_ids": report.get(
                    "unconfirmed_order_ids",
                    [],
                )
            },
        )
        return report

    def reset_breaker(
        self,
        scope: str,
        *,
        expected_generation: int,
        context: MutationContext,
    ) -> dict[str, object]:
        result = self.service.reset_killswitch(
            scope,
            actor=context.actor,
            reason=context.reason,
            expected_generation=expected_generation,
            request_id=context.request_id,
        )
        self._record_best_effort(
            context,
            "operations.breaker_reset",
            "circuit_breaker",
            str(result["scope"]),
            "reset",
            {"generation": result["generation"]},
        )
        return result

    def health(self) -> OperationalHealthReport:
        report = build_operational_health(self.service)
        if self.policy_store_maintenance is None:
            return report
        payload = report.as_dict()
        payload["policy_store_pruning"] = (
            self.policy_store_maintenance.posture().as_dict()
        )
        return OperationalHealthReport(payload)

    def liveness(self):
        return build_liveness(self.service.session_factory)
