"""The pure, deterministic final authority on every order."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..broker.models import OrderRequest, OrderSide, PortfolioSnapshot
from ..config import RiskConfig
from . import rules


@dataclass(frozen=True)
class RiskResult:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    # Non-blocking advisories (e.g. cross-broker concentration). Never affect approval.
    warnings: list[str] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return not self.approved

    def reason_text(self) -> str:
        return "; ".join(self.reasons)

    def warning_text(self) -> str:
        return "; ".join(self.warnings)


class RiskEngine:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    def check(
        self, order: OrderRequest, snapshot: PortfolioSnapshot
    ) -> RiskResult:
        symbol = order.ticker.upper()
        quote = snapshot.quotes.get(symbol)
        if quote is not None and not quote.is_valid:
            return RiskResult(
                approved=False,
                reasons=[f"quote for {symbol} is invalid"],
            )
        reasons: list[str] = []
        base_checks = [
            rules.check_allowlist(order, self.config),
            rules.check_pending_exposure_known(snapshot),
            rules.check_market_hours(order, self.config, snapshot.market_open),
            rules.check_max_notional(order, snapshot, self.config),
            rules.check_max_position(order, snapshot, self.config),
            rules.check_portfolio_exposure(order, snapshot, self.config),
            rules.check_price_sanity(order, snapshot, self.config),
        ]
        reasons.extend(reason for reason in base_checks if reason is not None)

        if not snapshot.quote_fresh:
            reasons.append("quote is stale")
        if snapshot.active_breakers:
            scopes = ",".join(sorted(snapshot.active_breakers))
            reasons.append(f"active circuit breaker: {scopes}")
        if self.config.require_broker_reconciled and not snapshot.broker_reconciled:
            reasons.append("broker reconciliation is not current")
        pnl_is_finite = (
            snapshot.realized_pnl_today.is_finite()
            and snapshot.unrealized_pnl_today.is_finite()
        )
        if not snapshot.daily_pnl_complete or not pnl_is_finite:
            reasons.append("daily P&L snapshot is incomplete")

        if quote is not None:
            if order.side is OrderSide.BUY:
                estimated = order.buying_power_notional(quote)
                reserved_buying_power = sum(
                    (
                        max(notional, Decimal(0))
                        for notional in (
                            snapshot.pending_buy_notional_by_ticker.values()
                        )
                    ),
                    Decimal(0),
                )
                available_buying_power = max(
                    snapshot.buying_power - reserved_buying_power,
                    Decimal(0),
                )
                if estimated > available_buying_power:
                    reasons.append("insufficient buying power")
            if order.side is OrderSide.SELL:
                position = snapshot.positions.get(symbol)
                held = max(position.qty, Decimal(0)) if position else Decimal(0)
                reserved = snapshot.reserved_sell_qty_by_ticker.get(
                    symbol, Decimal(0)
                )
                requested = (
                    order.qty
                    if order.qty is not None
                    else order.notional / quote.last
                )
                if requested > held - reserved:
                    reasons.append("sell quantity exceeds unreserved position")

        if pnl_is_finite:
            daily_total = (
                snapshot.realized_pnl_today
                + snapshot.unrealized_pnl_today
            )
            if daily_total <= -Decimal(
                str(self.config.max_daily_total_loss)
            ):
                reasons.append("daily total-loss limit reached")
        if snapshot.account_high_water_mark > 0:
            drawdown = (
                snapshot.account_high_water_mark - snapshot.account_equity
            ) / snapshot.account_high_water_mark * Decimal(100)
            if drawdown >= Decimal(str(self.config.max_account_drawdown_pct)):
                reasons.append("account drawdown limit reached")
        spread = snapshot.spread_pct_by_ticker.get(symbol)
        if (
            spread is not None
            and spread > Decimal(str(self.config.max_spread_pct))
        ):
            reasons.append("spread exceeds configured maximum")

        warnings: list[str] = []
        if self.config.warn_on_cross_broker_concentration:
            warning = rules.check_cross_broker_concentration(
                order, snapshot, self.config
            )
            if warning is not None:
                warnings.append(warning)
        return RiskResult(approved=not reasons, reasons=reasons, warnings=warnings)
