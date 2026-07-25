"""Complete execution snapshots assembled before any durable order write."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, sessionmaker

from trading_assistant.assets import (
    AssetClass,
    broker_symbol_matches_local,
    canonicalize_broker_symbol,
)
from trading_assistant.broker.models import (
    Account,
    OrderSide,
    OrderStatus,
    PortfolioSnapshot,
    Position,
    Quote,
    conservative_buy_price,
)
from trading_assistant.config import RiskConfig
from trading_assistant.db.models import (
    AccountRiskState,
    FILL_RECONCILIATION_REQUIRED,
    Fill,
    Order,
    RuleGroup,
    fill_has_trusted_identity,
    fill_requires_reconciliation,
)
from trading_assistant.dependencies import (
    RequiredDependencyUnavailable,
    RequiredQuoteUnavailable,
)
from trading_assistant.risk.breakers import BreakerScope, BreakerService
from trading_assistant.risk.pnl import FillLike, realized_pnl_today
from trading_assistant.risk.staleness import is_stale
from trading_assistant.risk.submission_barrier import SubmissionBarrier


_PENDING_STATUSES = (
    OrderStatus.APPROVAL_RECORDED.value,
    OrderStatus.SUBMITTING.value,
    OrderStatus.ACCEPTANCE_UNKNOWN.value,
    OrderStatus.SUBMITTED.value,
    OrderStatus.PARTIALLY_FILLED.value,
)
_UNRECONCILED_STATUSES = (
    OrderStatus.SUBMITTING.value,
    OrderStatus.ACCEPTANCE_UNKNOWN.value,
)


class PortfolioSnapshotService:
    """Build immutable risk inputs without broker I/O in SQLite transactions."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker,
        clock_for_asset: Callable[[AssetClass], object],
        external_positions: Callable[[], dict],
        risk_config_for_asset: Callable[[AssetClass], RiskConfig] | None = None,
        breakers: BreakerService | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.session_factory = session_factory
        self.broker = broker
        self.clock_for_asset = clock_for_asset
        self.external_positions = external_positions
        self.risk_config_for_asset = risk_config_for_asset
        self.breakers = breakers or BreakerService(session_factory)
        self.now = now
        self.submission_barrier = SubmissionBarrier(session_factory)

    def assemble_for_execution(
        self, ticker: str, *, exclude_order_id: int | None = None
    ) -> PortfolioSnapshot:
        asset_class = AssetClass.for_symbol(ticker)
        (
            account,
            positions,
            position_exposure_complete,
            external,
            quotes,
            wanted,
            market_open,
            market_clock_complete,
            boundary,
        ) = self._provider_values(
            [ticker],
            asset_class,
            required_dependencies=True,
        )

        with self.session_factory() as discovery_session:
            pending_symbols, discovery_complete = self._pending_symbols(
                discovery_session, exclude_order_id
            )
        wanted.update(pending_symbols)
        self._fetch_missing_quotes(
            quotes,
            wanted,
            required=True,
        )

        captured_at = self._captured_at()
        high_water_mark = self._account_high_water_mark(
            asset_class, account, captured_at
        )
        active_breakers = frozenset(
            state.scope.key
            for state in self.breakers.active_for_symbol(ticker)
        )
        with self.session_factory() as session:
            snapshot = self._assemble_local(
                session,
                ticker=ticker,
                asset_class=asset_class,
                exclude_order_id=exclude_order_id,
                account=account,
                positions=positions,
                position_exposure_complete=position_exposure_complete,
                external=external,
                quotes=quotes,
                wanted=wanted,
                market_open=market_open,
                market_clock_complete=market_clock_complete,
                boundary=boundary,
                captured_at=captured_at,
                high_water_mark=high_water_mark,
                active_breakers=active_breakers,
                discovery_complete=discovery_complete,
            )
        if not snapshot.quote_fresh:
            raise RequiredQuoteUnavailable
        return snapshot

    def assemble(
        self,
        session: Session,
        tickers: list[str],
        asset_class: AssetClass = AssetClass.EQUITY,
        *,
        exclude_order_id: int | None = None,
        quote_overrides: dict[str, object] | None = None,
        required_dependencies: bool = False,
    ) -> PortfolioSnapshot:
        (
            account,
            positions,
            position_exposure_complete,
            external,
            quotes,
            wanted,
            market_open,
            market_clock_complete,
            boundary,
        ) = self._provider_values(
            tickers,
            asset_class,
            quote_overrides=quote_overrides,
            required_dependencies=required_dependencies,
        )
        pending_symbols, discovery_complete = self._pending_symbols(
            session, exclude_order_id
        )
        wanted.update(pending_symbols)
        self._fetch_missing_quotes(
            quotes,
            wanted,
            required=required_dependencies,
        )

        captured_at = self._captured_at()
        high_water_mark = self._account_high_water_mark(
            asset_class, account, captured_at
        )
        active_breakers = frozenset(
            state.scope.key
            for state in self.breakers.active_for_symbol(tickers[0])
        )
        snapshot = self._assemble_local(
            session,
            ticker=tickers[0],
            asset_class=asset_class,
            exclude_order_id=exclude_order_id,
            account=account,
            positions=positions,
            position_exposure_complete=position_exposure_complete,
            external=external,
            quotes=quotes,
            wanted=wanted,
            market_open=market_open,
            market_clock_complete=market_clock_complete,
            boundary=boundary,
            captured_at=captured_at,
            high_water_mark=high_water_mark,
            active_breakers=active_breakers,
            discovery_complete=discovery_complete,
        )
        if required_dependencies and not snapshot.quote_fresh:
            raise RequiredQuoteUnavailable
        return snapshot

    def _provider_values(
        self,
        tickers: list[str],
        asset_class: AssetClass,
        *,
        quote_overrides: dict[str, object] | None = None,
        required_dependencies: bool = False,
    ) -> tuple[
        Account,
        list[Position],
        bool,
        dict,
        dict[str, Quote],
        set[str],
        bool,
        bool,
        datetime | None,
    ]:
        try:
            account = self.broker.get_account()
            positions = self.broker.get_positions()
            if any(
                not position.risk_values_valid
                for position in positions
            ):
                raise ValueError("invalid broker position payload")
        except Exception:
            if required_dependencies:
                raise RequiredDependencyUnavailable from None
            raise
        positions, position_exposure_complete = self._canonical_positions(
            positions,
            tickers,
        )
        external = self.external_positions()
        (
            market_open,
            market_clock_complete,
            boundary,
        ) = self._market_clock_values(
            asset_class,
            required_dependencies=required_dependencies,
        )
        quotes = {
            symbol.upper(): quote
            for symbol, quote in (quote_overrides or {}).items()
        }
        wanted = (
            {ticker.upper() for ticker in tickers}
            | {position.ticker.upper() for position in positions}
        )
        self._fetch_missing_quotes(
            quotes,
            wanted,
            required=required_dependencies,
        )
        return (
            account,
            positions,
            position_exposure_complete,
            external,
            quotes,
            wanted,
            market_open,
            market_clock_complete,
            boundary,
        )

    def _market_clock_values(
        self,
        asset_class: AssetClass,
        *,
        required_dependencies: bool,
    ) -> tuple[bool, bool, datetime | None]:
        try:
            clock = self.clock_for_asset(asset_class)
            market_open = clock.is_open()
            boundary = clock.most_recent_open()
            if type(market_open) is not bool:
                raise ValueError("invalid market clock state")
            if (
                not isinstance(boundary, datetime)
                or boundary.tzinfo is None
                or boundary.utcoffset() is None
            ):
                raise ValueError("invalid market session boundary")
            boundary = boundary.astimezone(timezone.utc)
        except Exception:
            if required_dependencies:
                raise RequiredDependencyUnavailable from None
            return False, False, None
        return market_open, True, boundary

    @staticmethod
    def _canonical_positions(
        positions: list[Position],
        local_symbols: list[str],
    ) -> tuple[list[Position], bool]:
        normalized = [
            canonicalize_broker_symbol(position.ticker)
            for position in positions
        ]
        local_crypto_symbols = {
            canonicalize_broker_symbol(symbol)
            for symbol in (
                list(local_symbols)
                + [
                    symbol
                    for symbol in normalized
                    if "/" in symbol
                ]
            )
            if "/" in symbol
        }
        canonical_positions: list[Position] = []
        seen: set[str] = set()
        complete = True
        for position, broker_symbol in zip(
            positions,
            normalized,
            strict=True,
        ):
            matching_local = [
                local_symbol
                for local_symbol in local_crypto_symbols
                if broker_symbol_matches_local(
                    broker_symbol,
                    local_symbol,
                )
            ]
            canonical_symbol = (
                matching_local[0]
                if len(matching_local) == 1
                else broker_symbol
            )
            if len(matching_local) > 1 or canonical_symbol in seen:
                complete = False
                continue
            seen.add(canonical_symbol)
            canonical_positions.append(
                replace(position, ticker=canonical_symbol)
            )
        return canonical_positions, complete

    def _fetch_missing_quotes(
        self,
        quotes: dict[str, Quote],
        wanted: set[str],
        *,
        required: bool = False,
    ) -> None:
        for symbol in sorted(wanted):
            if symbol in quotes:
                continue
            try:
                quotes[symbol] = self.broker.get_quote(symbol)
            except Exception:
                if required:
                    raise RequiredQuoteUnavailable from None
                continue

    @staticmethod
    def _pending_symbols(
        session: Session, exclude_order_id: int | None
    ) -> tuple[set[str], bool]:
        query = select(Order.ticker).where(Order.status.in_(_PENDING_STATUSES))
        if exclude_order_id is not None:
            query = query.where(Order.id != exclude_order_id)
        try:
            return {
                ticker.upper() for ticker in session.scalars(query).all()
            }, True
        except Exception:
            session.rollback()
            return set(), False

    def _captured_at(self) -> datetime:
        captured_at = self.now()
        if captured_at.tzinfo is None:
            return captured_at.replace(tzinfo=timezone.utc)
        return captured_at.astimezone(timezone.utc)

    def _account_high_water_mark(
        self,
        asset_class: AssetClass,
        account: Account,
        captured_at: datetime,
    ) -> Decimal:
        if not account.is_valid:
            with self.session_factory() as session:
                state = session.get(
                    AccountRiskState, asset_class.value
                )
                return (
                    state.high_water_mark
                    if state is not None
                    else Decimal(0)
                )
        equity = account.equity
        statement = insert(AccountRiskState).values(
            asset_class=asset_class.value,
            high_water_mark=equity,
            last_equity=equity,
            updated_at=captured_at,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[AccountRiskState.asset_class],
            set_={
                "high_water_mark": func.max(
                    AccountRiskState.high_water_mark,
                    statement.excluded.high_water_mark,
                ),
                "last_equity": statement.excluded.last_equity,
                "updated_at": statement.excluded.updated_at,
            },
        )
        with self.submission_barrier.hold_writer():
            with self.session_factory() as session:
                session.execute(statement)
                session.flush()
                state = session.get(AccountRiskState, asset_class.value)
                assert state is not None
                high_water_mark = state.high_water_mark
                session.commit()
                return high_water_mark

    def _max_quote_age(self, asset_class: AssetClass) -> Decimal:
        if self.risk_config_for_asset is None:
            return Decimal("60")
        return Decimal(
            str(
                self.risk_config_for_asset(
                    asset_class
                ).max_quote_age_seconds
            )
        )

    def _quote_is_fresh(
        self, quote: Quote, captured_at: datetime, asset_class: AssetClass
    ) -> bool:
        if not quote.is_valid:
            return False
        max_age_seconds = float(self._max_quote_age(asset_class))
        return all(
            not is_stale(
                component_time,
                now=captured_at,
                max_age_seconds=max_age_seconds,
            )
            for component_time in (
                quote.book_as_of,
                quote.trade_as_of,
            )
        )

    def _assemble_local(
        self,
        session: Session,
        *,
        ticker: str,
        asset_class: AssetClass,
        exclude_order_id: int | None,
        account: Account,
        positions: list[Position],
        position_exposure_complete: bool,
        external: dict,
        quotes: dict[str, Quote],
        wanted: set[str],
        market_open: bool,
        market_clock_complete: bool,
        boundary: datetime | None,
        captured_at: datetime,
        high_water_mark: Decimal,
        active_breakers: frozenset[str],
        discovery_complete: bool,
    ) -> PortfolioSnapshot:
        pos_map = {
            position.ticker.upper(): position for position in positions
        }
        spread_pct_by_ticker = {
            symbol: (quote.ask - quote.bid) / quote.last * Decimal(100)
            for symbol, quote in quotes.items()
            if quote.is_valid
        }
        quote_fresh = (
            wanted.issubset(quotes)
            and all(
                self._quote_is_fresh(
                    quotes[symbol], captured_at, asset_class
                )
                for symbol in wanted
            )
        )
        relevant_positions = [
            position
            for position in positions
            if AssetClass.for_symbol(position.ticker) is asset_class
        ]
        broker_drift_active = (
            BreakerScope.broker_drift().key in active_breakers
        )
        finite_unrealized = [
            position.unrealized_intraday_pnl
            for position in relevant_positions
            if (
                position.unrealized_intraday_pnl is not None
                and position.unrealized_intraday_pnl.is_finite()
            )
        ]
        daily_pnl_complete = (
            market_clock_complete
            and position_exposure_complete
            and len(finite_unrealized) == len(relevant_positions)
            and not broker_drift_active
        )
        unrealized_pnl_today = sum(
            finite_unrealized,
            Decimal(0),
        )
        account_complete = (
            account.is_valid
            and isinstance(high_water_mark, Decimal)
            and high_water_mark.is_finite()
            and high_water_mark > 0
        )

        try:
            pending_query = select(Order).where(
                Order.status.in_(_PENDING_STATUSES)
            )
            if exclude_order_id is not None:
                pending_query = pending_query.where(
                    Order.id != exclude_order_id
                )
            pending_orders = session.scalars(pending_query).all()
            all_fills = session.scalars(select(Fill)).all()
            fills = [
                fill
                for fill in all_fills
                if fill_has_trusted_identity(fill)
            ]
            legacy_fill_reconciliation_required = any(
                fill_requires_reconciliation(fill)
                for fill in all_fills
            )
            fill_reconciliation_required = (
                session.scalar(
                    select(Order.id)
                    .where(
                        Order.acceptance_state
                        == FILL_RECONCILIATION_REQUIRED
                    )
                    .limit(1)
                )
                is not None
                or legacy_fill_reconciliation_required
            )
            broker_reconciled = (
                position_exposure_complete
                and session.scalar(
                    select(Order.id)
                    .where(Order.status.in_(_UNRECONCILED_STATUSES))
                    .limit(1)
                )
                is None
                and not fill_reconciliation_required
                and not broker_drift_active
                and session.scalar(
                    select(RuleGroup.id)
                    .where(RuleGroup.reconciliation_required.is_(True))
                    .limit(1)
                )
                is None
            )
            pending_buy_notional: dict[str, Decimal] = {}
            reserved_sell_qty: dict[str, Decimal] = {}
            pending_exposure_complete = (
                discovery_complete
                and position_exposure_complete
                and not legacy_fill_reconciliation_required
            )
            fills_by_order: dict[int, list[Fill]] = {}
            for fill in fills:
                if fill.order_id is not None:
                    fills_by_order.setdefault(fill.order_id, []).append(fill)
            for pending_order in pending_orders:
                symbol = pending_order.ticker.upper()
                quote = quotes.get(symbol)
                if quote is None or not quote.is_valid:
                    pending_exposure_complete = False
                    continue
                order_fills = fills_by_order.get(pending_order.id, [])
                recorded_qty = sum(
                    (fill.qty for fill in order_fills), Decimal(0)
                )
                recorded_notional = sum(
                    (fill.qty * fill.price for fill in order_fills),
                    Decimal(0),
                )
                if pending_order.qty is not None:
                    remaining_qty = max(
                        pending_order.qty - recorded_qty, Decimal(0)
                    )
                    if pending_order.side == OrderSide.BUY.value:
                        reservation_price = conservative_buy_price(
                            pending_order.order_type,
                            pending_order.limit_price,
                            quote,
                        )
                    else:
                        reservation_price = quote.last
                    remaining_notional = (
                        remaining_qty * reservation_price
                    )
                else:
                    remaining_notional = max(
                        (pending_order.notional or Decimal(0))
                        - recorded_notional,
                        Decimal(0),
                    )
                    remaining_qty = remaining_notional / quote.last
                if pending_order.side == OrderSide.BUY.value:
                    pending_buy_notional[symbol] = (
                        pending_buy_notional.get(symbol, Decimal(0))
                        + remaining_notional
                    )
                else:
                    reserved_sell_qty[symbol] = (
                        reserved_sell_qty.get(symbol, Decimal(0))
                        + remaining_qty
                    )
            class_fills = [
                FillLike(
                    row.ticker,
                    row.side,
                    row.qty,
                    row.price,
                    row.filled_at,
                )
                for row in fills
                if AssetClass.for_symbol(row.ticker) is asset_class
            ]
            if boundary is None:
                realized = Decimal(0)
                daily_pnl_complete = False
            else:
                realized = realized_pnl_today(
                    class_fills,
                    asset_class=asset_class,
                    boundary=boundary,
                )
            if not realized.is_finite():
                daily_pnl_complete = False
                realized = Decimal(0)
            if fill_reconciliation_required:
                daily_pnl_complete = False
        except Exception:
            session.rollback()
            pending_buy_notional = {}
            reserved_sell_qty = {}
            pending_exposure_complete = False
            daily_pnl_complete = False
            broker_reconciled = False
            realized = Decimal(0)

        return PortfolioSnapshot(
            positions=pos_map,
            quotes=quotes,
            buying_power=account.buying_power,
            realized_pnl_today=realized,
            cash=account.cash,
            unrealized_pnl_today=unrealized_pnl_today,
            daily_pnl_complete=daily_pnl_complete,
            account_high_water_mark=high_water_mark,
            account_equity=account.equity,
            account_complete=account_complete,
            quote_fresh=quote_fresh,
            market_open=market_open,
            market_clock_complete=market_clock_complete,
            spread_pct_by_ticker=spread_pct_by_ticker,
            pending_buy_notional_by_ticker=pending_buy_notional,
            reserved_sell_qty_by_ticker=reserved_sell_qty,
            broker_reconciled=broker_reconciled,
            active_breakers=active_breakers,
            as_of=captured_at,
            external_positions=external,
            pending_signed_notional=dict(pending_buy_notional),
            pending_exposure_complete=pending_exposure_complete,
        )
