"""The risk engine — deterministic, pure, the final authority on every order (A1, A3).

It performs NO I/O. The caller assembles a :class:`PortfolioSnapshot`, resolves
whether the kill switch is tripped (DB) and whether the market is open (clock),
and passes those in. Every order — LLM-proposed or execution-time re-check —
goes through :meth:`RiskEngine.check`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..broker.models import OrderRequest, PortfolioSnapshot
from ..config import RiskConfig
from . import rules


@dataclass(frozen=True)
class RiskResult:
    approved: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return not self.approved

    def reason_text(self) -> str:
        return "; ".join(self.reasons)


class RiskEngine:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    def check(
        self,
        order: OrderRequest,
        snapshot: PortfolioSnapshot,
        *,
        killswitch_tripped: bool,
        market_open: bool,
    ) -> RiskResult:
        reasons: list[str] = []

        # Kill switch blocks everything, first and unconditionally.
        if killswitch_tripped:
            reasons.append("kill switch is tripped; all new orders are blocked")

        checks = [
            rules.check_allowlist(order, self.config),
            rules.check_market_hours(order, self.config, market_open),
            rules.check_max_notional(order, snapshot, self.config),
            rules.check_max_position(order, snapshot, self.config),
            rules.check_portfolio_exposure(order, snapshot, self.config),
            rules.check_price_sanity(order, snapshot, self.config),
        ]
        reasons.extend(r for r in checks if r is not None)

        return RiskResult(approved=not reasons, reasons=reasons)
