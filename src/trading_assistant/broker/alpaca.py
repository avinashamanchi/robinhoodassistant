"""Alpaca implementation of BrokerClient + AlpacaClock (Phase 2).

Paper vs live is chosen by the caller (config + double-lock). The clients are
injected so the mapping logic is unit-testable without network access; use
:meth:`AlpacaBroker.from_credentials` to build real SDK clients.

Idempotency: every order carries ``client_order_id == idempotency_key``. Before
submitting we look the key up at the broker and, if it already exists, return
that order's status rather than creating a duplicate.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any, Callable, Optional, TypeVar

from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import Timeout as ReqTimeout

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from .base import BrokerClient
from .models import (
    Account,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Quote,
)

# Alpaca order-status string -> our lifecycle status.
_STATUS_MAP: dict[str, OrderStatus] = {
    "new": OrderStatus.SUBMITTED,
    "accepted": OrderStatus.SUBMITTED,
    "pending_new": OrderStatus.SUBMITTED,
    "accepted_for_bidding": OrderStatus.SUBMITTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "done_for_day": OrderStatus.SUBMITTED,
    "canceled": OrderStatus.CANCELED,
    "pending_cancel": OrderStatus.SUBMITTED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
    "suspended": OrderStatus.SUBMITTED,
}


log = logging.getLogger(__name__)

# A long-running process keeps HTTP keep-alive sockets to Alpaca in a pool. When
# the load balancer closes an idle socket, the NEXT request on it raises before a
# response is read ("Remote end closed connection without response"). Retrying
# grabs/opens a fresh socket. Only these connection-level errors are transient;
# HTTP status errors (4xx/5xx APIError) are surfaced, not retried.
_TRANSIENT = (ReqConnectionError, ReqTimeout)
_T = TypeVar("_T")


def _retry(fn: Callable[..., _T], *args: Any, attempts: int = 3, base_delay: float = 0.3, **kwargs: Any) -> _T:
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except _TRANSIENT as exc:  # stale socket / transient network blip
            last = exc
            log.warning("alpaca transient error on %s (attempt %d/%d): %s",
                        getattr(fn, "__name__", fn), i + 1, attempts, exc)
            if i + 1 < attempts:
                time.sleep(base_delay * (i + 1))
    assert last is not None
    raise last


def _d(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    return Decimal(str(value))


def _map_status(raw: Any) -> OrderStatus:
    key = getattr(raw, "value", raw)
    return _STATUS_MAP.get(str(key), OrderStatus.SUBMITTED)


class AlpacaBroker(BrokerClient):
    def __init__(
        self, trading_client: TradingClient, data_client: StockHistoricalDataClient
    ) -> None:
        self._trading = trading_client
        self._data = data_client

    @classmethod
    def from_credentials(
        cls, api_key: str, secret_key: str, *, paper: bool = True
    ) -> "AlpacaBroker":
        trading = TradingClient(api_key, secret_key, paper=paper)
        data = StockHistoricalDataClient(api_key, secret_key)
        return cls(trading, data)

    # ── market data ────────────────────────────────────────────
    def get_quote(self, ticker: str) -> Quote:
        symbol = ticker.upper()
        snap = _retry(
            self._data.get_stock_snapshot,
            StockSnapshotRequest(symbol_or_symbols=symbol),
        )[symbol]
        last = _d(snap.latest_trade.price) if snap.latest_trade else None
        bid = _d(snap.latest_quote.bid_price) if snap.latest_quote else None
        ask = _d(snap.latest_quote.ask_price) if snap.latest_quote else None
        prev_close = _d(snap.previous_daily_bar.close) if snap.previous_daily_bar else None
        # Fall back sensibly if a field is momentarily missing.
        last = last or bid or ask or Decimal(0)
        return Quote(
            ticker=symbol,
            bid=bid or last,
            ask=ask or last,
            last=last,
            prev_close=prev_close,
        )

    # ── account / positions ────────────────────────────────────
    def get_account(self) -> Account:
        acct = _retry(self._trading.get_account)
        return Account(
            buying_power=_d(acct.buying_power) or Decimal(0),
            equity=_d(acct.equity) or Decimal(0),
            cash=_d(acct.cash) or Decimal(0),
        )

    def get_positions(self) -> list[Position]:
        out: list[Position] = []
        for p in _retry(self._trading.get_all_positions):
            out.append(
                Position(
                    ticker=p.symbol.upper(),
                    qty=_d(p.qty) or Decimal(0),
                    avg_entry_price=_d(p.avg_entry_price) or Decimal(0),
                    current_price=_d(p.current_price) or Decimal(0),
                )
            )
        return out

    # ── orders (idempotent) ────────────────────────────────────
    def submit_order(self, order: OrderRequest) -> OrderResult:
        # Retry the WHOLE attempt: on a dropped connection the retry re-runs the
        # idempotency lookup first, so a submit that actually landed is returned
        # (via client_order_id) instead of double-placed.
        return _retry(self._submit_once, order)

    def _submit_once(self, order: OrderRequest) -> OrderResult:
        existing = self._find_by_client_id(order.idempotency_key)
        if existing is not None:
            return self._to_result(existing)

        side = (
            AlpacaOrderSide.BUY if order.side is OrderSide.BUY else AlpacaOrderSide.SELL
        )
        common = dict(
            symbol=order.ticker.upper(),
            side=side,
            time_in_force=TimeInForce.DAY,
            client_order_id=order.idempotency_key,
        )
        if order.qty is not None:
            common["qty"] = float(order.qty)
        else:
            common["notional"] = float(order.notional)

        if order.order_type is OrderType.LIMIT:
            request = LimitOrderRequest(limit_price=float(order.limit_price), **common)
        else:
            request = MarketOrderRequest(**common)

        placed = self._trading.submit_order(order_data=request)
        return self._to_result(placed)

    def submit_bracket(self, order: OrderRequest, take_profit, stop_loss) -> OrderResult:
        """Server-side OCO bracket: entry + take-profit + stop-loss in one order."""
        return _retry(self._submit_bracket_once, order, take_profit, stop_loss)

    def _submit_bracket_once(self, order: OrderRequest, take_profit, stop_loss) -> OrderResult:
        existing = self._find_by_client_id(order.idempotency_key)
        if existing is not None:
            return self._to_result(existing)
        from alpaca.trading.enums import OrderClass
        from alpaca.trading.requests import (
            LimitOrderRequest,
            StopLossRequest,
            TakeProfitRequest,
        )

        side = AlpacaOrderSide.BUY if order.side is OrderSide.BUY else AlpacaOrderSide.SELL
        req = LimitOrderRequest(
            symbol=order.ticker.upper(),
            qty=float(order.qty),
            side=side,
            time_in_force=TimeInForce.DAY,
            client_order_id=order.idempotency_key,
            limit_price=float(order.limit_price),
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=float(take_profit)),
            stop_loss=StopLossRequest(stop_price=float(stop_loss)),
        )
        return self._to_result(self._trading.submit_order(order_data=req))

    def get_order_status(self, order_id: str) -> OrderResult:
        return self._to_result(_retry(self._trading.get_order_by_id, order_id))

    def cancel_order(self, order_id: str) -> OrderResult:
        _retry(self._trading.cancel_order_by_id, order_id)
        return self._to_result(_retry(self._trading.get_order_by_id, order_id))

    # ── helpers ────────────────────────────────────────────────
    def _find_by_client_id(self, client_order_id: str):
        try:
            return _retry(self._trading.get_order_by_client_order_id, client_order_id)
        except _TRANSIENT:
            # A transient network error must NOT be read as "no such order" — that
            # would risk a duplicate submit. Propagate so the caller's retry re-tries.
            raise
        except Exception:
            # SDK raises a real error when no such order exists: not submitted yet.
            return None

    def _to_result(self, o: Any) -> OrderResult:
        return OrderResult(
            idempotency_key=getattr(o, "client_order_id", "") or "",
            broker_order_id=str(o.id),
            status=_map_status(o.status),
            filled_qty=_d(getattr(o, "filled_qty", 0)) or Decimal(0),
            avg_fill_price=_d(getattr(o, "filled_avg_price", None)),
        )


class AlpacaClock:
    """MarketClock backed by Alpaca's clock API (A7). Satisfies the protocol."""

    def __init__(self, trading_client: TradingClient) -> None:
        self._trading = trading_client

    @classmethod
    def from_credentials(
        cls, api_key: str, secret_key: str, *, paper: bool = True
    ) -> "AlpacaClock":
        return cls(TradingClient(api_key, secret_key, paper=paper))

    def is_open(self, at=None) -> bool:
        return bool(_retry(self._trading.get_clock).is_open)

    def next_open(self, at=None):
        return _retry(self._trading.get_clock).next_open

    def next_close(self, at=None):
        return _retry(self._trading.get_clock).next_close
