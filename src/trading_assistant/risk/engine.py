"""The pure, deterministic final authority on every order."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..broker.models import OrderRequest, OrderSide, PortfolioSnapshot
from ..assets import AssetClass
from ..config import RiskConfig
from .breakers import BreakerScope
from . import rules


@dataclass(frozen=True)
class BreakerTripIntent:
    scope: BreakerScope
    reason: str


@dataclass(frozen=True)
class RiskResult:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    # Non-blocking advisories (e.g. cross-broker concentration). Never affect approval.
    warnings: list[str] = field(default_factory=list)
    breaker_trips: tuple[BreakerTripIntent, ...] = ()
    # Breakers deliberately bypassed for a freshly proven reduce-only order.
    # The submission claim uses this exact set while retaining every data,
    # liquidity, and broker-drift latch.
    bypassed_breakers: tuple[str, ...] = ()

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
            reason = f"quote for {symbol} is invalid"
            return RiskResult(
                approved=False,
                reasons=[reason],
                breaker_trips=(
                    BreakerTripIntent(
                        BreakerScope.data(
                            AssetClass.for_symbol(symbol)
                        ),
                        reason,
                    ),
                ),
            )
        reasons: list[str] = []
        breaker_trips: list[BreakerTripIntent] = []
        position = snapshot.positions.get(symbol)
        position_qty = (
            position.qty
            if position is not None
            and isinstance(position.qty, Decimal)
            and position.qty.is_finite()
            else Decimal(0)
        )
        held = max(position_qty, Decimal(0))
        reserved = snapshot.reserved_sell_qty_by_ticker.get(
            symbol,
            Decimal(0),
        )
        requested_sell_qty = (
            order.qty
            if order.qty is not None
            else (
                order.notional / quote.last
                if quote is not None and quote.is_valid
                else Decimal("Infinity")
            )
        )
        reserved_cover_qty = Decimal(0)
        if quote is not None and quote.is_valid:
            reserved_cover_qty = (
                snapshot.pending_buy_notional_by_ticker.get(
                    symbol,
                    Decimal(0),
                )
                / quote.last
            )
        available_reduce_qty = (
            max(held - reserved, Decimal(0))
            if order.side is OrderSide.SELL
            else max(
                -position_qty - reserved_cover_qty,
                Decimal(0),
            )
        )
        strictly_reduce_only = bool(
            snapshot.broker_reconciled
            and snapshot.pending_exposure_complete
            and requested_sell_qty > 0
            and requested_sell_qty <= available_reduce_qty
        )
        market_hours_reason = (
            "market clock snapshot is incomplete"
            if snapshot.market_clock_complete is not True
            else rules.check_market_hours(
                order,
                self.config,
                snapshot.market_open,
            )
        )
        base_checks = [
            rules.check_allowlist(order, self.config),
            rules.check_pending_exposure_known(snapshot),
            market_hours_reason,
            rules.check_max_notional(order, snapshot, self.config),
            rules.check_max_position(order, snapshot, self.config),
            rules.check_portfolio_exposure(order, snapshot, self.config),
            rules.check_price_sanity(order, snapshot, self.config),
        ]
        reasons.extend(reason for reason in base_checks if reason is not None)

        if not snapshot.quote_fresh:
            stale_reason = "quote is stale"
            reasons.append(stale_reason)
            breaker_trips.append(
                BreakerTripIntent(
                    BreakerScope.data(
                        AssetClass.for_symbol(symbol)
                    ),
                    stale_reason,
                )
            )
        blocking_breakers = set(snapshot.active_breakers)
        bypassed_breakers: set[str] = set()
        bypassable_breakers: set[str] = set()
        if strictly_reduce_only:
            asset_class = AssetClass.for_symbol(symbol)
            bypassable_breakers = {
                BreakerScope.operator_global().key,
                BreakerScope.loss(asset_class).key,
                BreakerScope.drawdown(asset_class).key,
            }
            bypassed_breakers = (
                blocking_breakers & bypassable_breakers
            )
            blocking_breakers -= bypassed_breakers
        if blocking_breakers:
            scopes = ",".join(sorted(blocking_breakers))
            reasons.append(f"active circuit breaker: {scopes}")
        if self.config.require_broker_reconciled and not snapshot.broker_reconciled:
            reasons.append("broker reconciliation is not current")
        account_complete = snapshot.account_complete and all(
            isinstance(value, Decimal)
            and value.is_finite()
            and value > 0
            for value in (
                snapshot.buying_power,
                snapshot.cash,
                snapshot.account_equity,
                snapshot.account_high_water_mark,
            )
        )
        if not account_complete:
            reasons.append("account snapshot is incomplete")
        pnl_is_finite = (
            snapshot.realized_pnl_today.is_finite()
            and snapshot.unrealized_pnl_today.is_finite()
        )
        if not snapshot.daily_pnl_complete or not pnl_is_finite:
            reasons.append("daily P&L snapshot is incomplete")

        if quote is not None:
            if order.side is OrderSide.BUY and account_complete:
                estimated = order.risk_notional(quote)
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
                loss_reason = "daily total-loss limit reached"
                if not strictly_reduce_only:
                    reasons.append(loss_reason)
                breaker_trips.append(
                    BreakerTripIntent(
                        BreakerScope.loss(
                            AssetClass.for_symbol(symbol)
                        ),
                        loss_reason,
                    )
                )
        if account_complete:
            drawdown = (
                snapshot.account_high_water_mark - snapshot.account_equity
            ) / snapshot.account_high_water_mark * Decimal(100)
            if drawdown >= Decimal(str(self.config.max_account_drawdown_pct)):
                drawdown_reason = "account drawdown limit reached"
                if not strictly_reduce_only:
                    reasons.append(drawdown_reason)
                breaker_trips.append(
                    BreakerTripIntent(
                        BreakerScope.drawdown(
                            AssetClass.for_symbol(symbol)
                        ),
                        drawdown_reason,
                    )
                )
        spread = snapshot.spread_pct_by_ticker.get(symbol)
        if (
            spread is not None
            and spread > Decimal(str(self.config.max_spread_pct))
        ):
            spread_reason = "spread exceeds configured maximum"
            reasons.append(spread_reason)
            breaker_trips.append(
                BreakerTripIntent(
                    BreakerScope.liquidity(symbol),
                    spread_reason,
                )
            )

        warnings: list[str] = []
        if self.config.warn_on_cross_broker_concentration:
            warning = rules.check_cross_broker_concentration(
                order, snapshot, self.config
            )
            if warning is not None:
                warnings.append(warning)
        if strictly_reduce_only:
            bypassed_breakers.update(
                intent.scope.key
                for intent in breaker_trips
                if intent.scope.key in bypassable_breakers
            )
        return RiskResult(
            approved=not reasons,
            reasons=reasons,
            warnings=warnings,
            breaker_trips=tuple(breaker_trips),
            bypassed_breakers=tuple(sorted(bypassed_breakers)),
        )
