"""One API-facing facade for panic, breaker reset, and health."""

from __future__ import annotations

from ..assets import AssetClass
from .audit import AuditRecorder, MutationContext
from .health import (
    OperationalHealthReport,
    build_liveness,
    build_operational_health,
)


class OperationsService:
    def __init__(self, service, audit: AuditRecorder | None = None) -> None:
        self.service = service
        self.audit = audit or AuditRecorder(service.session_factory)

    def panic(self, context: MutationContext) -> dict[str, object]:
        report = self.service.panic(
            actor=context.actor,
            reason=context.reason,
            request_id=context.request_id,
        )
        self.audit.record(
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
        asset_class: AssetClass | str,
        *,
        expected_generation: int,
        context: MutationContext,
    ) -> dict[str, object]:
        result = self.service.reset_killswitch(
            asset_class,
            actor=context.actor,
            reason=context.reason,
            expected_generation=expected_generation,
            request_id=context.request_id,
        )
        self.audit.record(
            context,
            "operations.breaker_reset",
            "circuit_breaker",
            str(result["asset_class"]),
            "reset",
            {"generation": result["generation"]},
        )
        return result

    def health(self) -> OperationalHealthReport:
        return build_operational_health(self.service)

    def liveness(self):
        return build_liveness(self.service.session_factory)
