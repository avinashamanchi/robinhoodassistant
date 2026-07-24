"""Read-only execution snapshot assembly, isolated from durable order writes."""

from __future__ import annotations

from decimal import Decimal
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trading_assistant.assets import AssetClass
from trading_assistant.broker.models import OrderSide, OrderStatus, PortfolioSnapshot
from trading_assistant.db.models import Fill, Order
from trading_assistant.risk.pnl import FillLike, realized_pnl_today


class PortfolioSnapshotService:
    """Build a risk snapshot in a short, read-only session before broker submission."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker,
        clock_for_asset: Callable[[AssetClass], object],
        external_positions: Callable[[], dict],
    ) -> None:
        self.session_factory = session_factory
        self.broker = broker
        self.clock_for_asset = clock_for_asset
        self.external_positions = external_positions

    def assemble_for_execution(
        self, ticker: str, *, exclude_order_id: int | None = None
    ) -> PortfolioSnapshot:
        asset_class = AssetClass.for_symbol(ticker)
        with self.session_factory() as session:
            return self.assemble(
                session, [ticker], asset_class, exclude_order_id=exclude_order_id
            )

    def assemble(
        self,
        session: Session,
        tickers: list[str],
        asset_class: AssetClass = AssetClass.EQUITY,
        *,
        exclude_order_id: int | None = None,
    ) -> PortfolioSnapshot:
        positions = self.broker.get_positions()
        pos_map = {position.ticker.upper(): position for position in positions}
        pending_query = select(Order).where(
            Order.status.in_(
                (
                    OrderStatus.APPROVED.value,
                    OrderStatus.APPROVAL_RECORDED.value,
                    OrderStatus.SUBMITTING.value,
                    OrderStatus.ACCEPTANCE_UNKNOWN.value,
                    OrderStatus.SUBMITTED.value,
                    OrderStatus.PARTIALLY_FILLED.value,
                )
            )
        )
        if exclude_order_id is not None:
            pending_query = pending_query.where(Order.id != exclude_order_id)
        pending_orders = session.execute(pending_query).scalars().all()
        wanted = (
            {ticker.upper() for ticker in tickers}
            | set(pos_map)
            | {order.ticker.upper() for order in pending_orders}
        )
        quotes = {}
        for symbol in wanted:
            try:
                quotes[symbol] = self.broker.get_quote(symbol)
            except Exception:
                # The risk engine receives no quote and rejects fail-closed.
                continue
        account = self.broker.get_account()
        pending_signed_notional: dict[str, Decimal] = {}
        pending_exposure_complete = True
        for pending_order in pending_orders:
            if pending_order.status in (
                OrderStatus.SUBMITTING.value,
                OrderStatus.ACCEPTANCE_UNKNOWN.value,
            ):
                pending_exposure_complete = False
            symbol = pending_order.ticker.upper()
            quote = quotes.get(symbol)
            if quote is None:
                pending_exposure_complete = False
                continue
            recorded_qty = sum((fill.qty for fill in pending_order.fills), Decimal(0))
            recorded_notional = sum(
                (fill.qty * fill.price for fill in pending_order.fills), Decimal(0)
            )
            if pending_order.qty is not None:
                remaining_qty = max(pending_order.qty - recorded_qty, Decimal(0))
                remaining_notional = remaining_qty * quote.last
            else:
                remaining_notional = max(
                    (pending_order.notional or Decimal(0)) - recorded_notional,
                    Decimal(0),
                )
            signed_notional = (
                remaining_notional
                if pending_order.side == OrderSide.BUY.value
                else -remaining_notional
            )
            pending_signed_notional[symbol] = (
                pending_signed_notional.get(symbol, Decimal(0)) + signed_notional
            )
        fills = [
            FillLike(row.ticker, row.side, row.qty, row.price, row.filled_at)
            for row in session.execute(select(Fill)).scalars().all()
            if AssetClass.for_symbol(row.ticker) is asset_class
        ]
        return PortfolioSnapshot(
            positions=pos_map,
            quotes=quotes,
            buying_power=account.buying_power,
            realized_pnl_today=realized_pnl_today(
                fills,
                asset_class=asset_class,
                boundary=self.clock_for_asset(asset_class).most_recent_open(),
            ),
            external_positions=self.external_positions(),
            pending_signed_notional=pending_signed_notional,
            pending_exposure_complete=pending_exposure_complete,
        )
