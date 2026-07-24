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
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Optional, TypeVar

import requests
from alpaca.common.exceptions import APIError
from alpaca.data.historical import CryptoHistoricalDataClient
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import Timeout as ReqTimeout

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import CryptoSnapshotRequest, StockSnapshotRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetCalendarRequest,
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
)
from zoneinfo import ZoneInfo

from ..assets import AssetClass
from .base import BrokerAcceptanceUnknown, BrokerClient, BrokerSubmissionRejected
from .models import (
    Account,
    BrokerFill,
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


class _TimeoutSession(requests.Session):
    """Requests session with a finite default timeout on every Alpaca SDK call."""

    def __init__(self, timeout_seconds: float) -> None:
        super().__init__()
        self._default_timeout = timeout_seconds

    def request(self, method: str, url: str, **kwargs: Any):
        kwargs.setdefault("timeout", self._default_timeout)
        return super().request(method, url, **kwargs)


def _install_timeout(client: Any, timeout_seconds: float) -> None:
    client._session = _TimeoutSession(timeout_seconds)


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
    reconciliation_key = "alpaca"

    def __init__(
        self,
        trading_client: TradingClient,
        data_client: StockHistoricalDataClient,
        crypto_data_client: CryptoHistoricalDataClient | None = None,
    ) -> None:
        self._trading = trading_client
        self._data = data_client
        self._crypto_data = crypto_data_client

    @classmethod
    def from_credentials(
        cls,
        api_key: str,
        secret_key: str,
        *,
        paper: bool = True,
        timeout_seconds: float = 10.0,
    ) -> "AlpacaBroker":
        trading = TradingClient(api_key, secret_key, paper=paper)
        data = StockHistoricalDataClient(api_key, secret_key)
        crypto_data = CryptoHistoricalDataClient(api_key, secret_key)
        _install_timeout(trading, timeout_seconds)
        _install_timeout(data, timeout_seconds)
        _install_timeout(crypto_data, timeout_seconds)
        return cls(trading, data, crypto_data)

    # ── market data ────────────────────────────────────────────
    def get_quote(self, ticker: str) -> Quote:
        symbol = ticker.upper()
        if AssetClass.for_symbol(symbol) is AssetClass.CRYPTO:
            if self._crypto_data is None:
                raise RuntimeError("Alpaca crypto market-data client is not configured")
            snap = _retry(
                self._crypto_data.get_crypto_snapshot,
                CryptoSnapshotRequest(symbol_or_symbols=symbol),
            )[symbol]
        else:
            snap = _retry(
                self._data.get_stock_snapshot,
                StockSnapshotRequest(symbol_or_symbols=symbol),
            )[symbol]
        last = _d(snap.latest_trade.price) if snap.latest_trade else None
        bid = _d(snap.latest_quote.bid_price) if snap.latest_quote else None
        ask = _d(snap.latest_quote.ask_price) if snap.latest_quote else None
        prev_close = _d(snap.previous_daily_bar.close) if snap.previous_daily_bar else None
        source_time = (
            getattr(snap.latest_quote, "timestamp", None)
            if snap.latest_quote
            else None
        ) or (
            getattr(snap.latest_trade, "timestamp", None)
            if snap.latest_trade
            else None
        )
        if source_time is None:
            source_time = datetime.now(timezone.utc)
        elif source_time.tzinfo is None:
            source_time = source_time.replace(tzinfo=timezone.utc)
        else:
            source_time = source_time.astimezone(timezone.utc)
        # Fall back sensibly if a field is momentarily missing.
        last = last or bid or ask or Decimal(0)
        return Quote(
            ticker=symbol,
            bid=bid or last,
            ask=ask or last,
            last=last,
            prev_close=prev_close,
            as_of=source_time,
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

    def get_fill_activities(
        self, after: datetime | None = None
    ) -> list[BrokerFill]:
        """Return exact Alpaca FILL activities, including IDs and exchange times."""
        params: dict[str, Any] = {
            "direction": "asc",
            "page_size": 100,
        }
        if after is not None:
            normalized = (
                after.replace(tzinfo=timezone.utc)
                if after.tzinfo is None
                else after.astimezone(timezone.utc)
            )
            normalized -= timedelta(seconds=1)
            params["after"] = normalized.isoformat().replace("+00:00", "Z")

        fills: list[BrokerFill] = []
        while True:
            page = _retry(
                self._trading.get,
                "/account/activities/FILL",
                params,
            )
            for raw in page:
                timestamp = datetime.fromisoformat(
                    str(raw["transaction_time"]).replace("Z", "+00:00")
                ).astimezone(timezone.utc)
                raw_side = str(raw["side"]).lower()
                fills.append(
                    BrokerFill(
                        broker_fill_id=str(raw["id"]),
                        broker_order_id=str(raw["order_id"]),
                        ticker=str(raw["symbol"]).upper(),
                        side="buy" if raw_side.startswith("buy") else "sell",
                        qty=Decimal(str(raw["qty"])),
                        price=Decimal(str(raw["price"])),
                        filled_at=timestamp,
                    )
                )
            if len(page) < 100:
                break
            params["page_token"] = page[-1]["id"]
        return fills

    # ── orders (idempotent) ────────────────────────────────────
    def submit_order(self, order: OrderRequest) -> OrderResult:
        existing = self.get_order_by_client_id(order.idempotency_key)
        if existing is not None:
            return existing
        try:
            return self._submit_once(order)
        except APIError as exc:
            raise self._submission_exception(exc) from exc
        except Exception as exc:
            # The POST was attempted. A timeout, malformed response, or connection
            # reset cannot prove that Alpaca did not accept the client id.
            raise BrokerAcceptanceUnknown(str(exc)) from exc

    def _submit_once(self, order: OrderRequest) -> OrderResult:
        side = (
            AlpacaOrderSide.BUY if order.side is OrderSide.BUY else AlpacaOrderSide.SELL
        )
        common = dict(
            symbol=order.ticker.upper(),
            side=side,
            time_in_force=(
                TimeInForce.GTC
                if AssetClass.for_symbol(order.ticker) is AssetClass.CRYPTO
                else TimeInForce.DAY
            ),
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
        if AssetClass.for_symbol(order.ticker) is AssetClass.CRYPTO:
            raise ValueError("crypto bracket orders are not supported by Alpaca")
        existing = self.get_order_by_client_id(order.idempotency_key)
        if existing is not None:
            return existing
        try:
            return self._submit_bracket_once(order, take_profit, stop_loss)
        except APIError as exc:
            raise self._submission_exception(exc) from exc
        except Exception as exc:
            raise BrokerAcceptanceUnknown(str(exc)) from exc

    def _submit_bracket_once(self, order: OrderRequest, take_profit, stop_loss) -> OrderResult:
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

    def get_order_by_client_id(self, client_order_id: str) -> OrderResult | None:
        found = self._find_by_client_id(client_order_id)
        return self._to_result(found) if found is not None else None

    def get_open_orders(self) -> list[OrderResult]:
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        return [
            self._to_result(order)
            for order in _retry(self._trading.get_orders, filter=request)
        ]

    def cancel_order(self, order_id: str) -> OrderResult:
        _retry(self._trading.cancel_order_by_id, order_id)
        return self._to_result(_retry(self._trading.get_order_by_id, order_id))

    # ── helpers ────────────────────────────────────────────────
    def _find_by_client_id(self, client_order_id: str):
        try:
            return _retry(self._trading.get_order_by_client_id, client_order_id)
        except _TRANSIENT:
            # A transient network error must NOT be read as "no such order" — that
            # would risk a duplicate submit. Propagate so the caller's retry re-tries.
            raise
        except APIError as exc:
            # Only a confirmed 404 means this client id has not been submitted.
            # Authentication errors, rate limits and server failures must fail
            # closed or a retry could create a duplicate order.
            if exc.status_code == 404:
                return None
            raise

    @staticmethod
    def _submission_exception(exc: APIError) -> RuntimeError:
        status_code = getattr(exc, "status_code", None)
        # Alpaca documents 400/422 as validation failures, which prove no order
        # was accepted. Other 4xx responses can be transport/auth/rate-limit
        # edge cases, so preserve the safer indeterminate outcome.
        if status_code in {400, 422}:
            return BrokerSubmissionRejected(f"alpaca_http_{status_code}", str(exc))
        return BrokerAcceptanceUnknown(str(exc))

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
        cls,
        api_key: str,
        secret_key: str,
        *,
        paper: bool = True,
        timeout_seconds: float = 10.0,
    ) -> "AlpacaClock":
        client = TradingClient(api_key, secret_key, paper=paper)
        _install_timeout(client, timeout_seconds)
        return cls(client)

    def is_open(self, at=None) -> bool:
        return bool(_retry(self._trading.get_clock).is_open)

    def next_open(self, at=None):
        return _retry(self._trading.get_clock).next_open

    def next_close(self, at=None):
        return _retry(self._trading.get_clock).next_close

    def most_recent_open(self, at=None) -> datetime:
        now = at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        ny = ZoneInfo("America/New_York")
        local_date = now.astimezone(ny).date()
        calendar = _retry(
            self._trading.get_calendar,
            GetCalendarRequest(
                start=local_date - timedelta(days=14),
                end=local_date,
            ),
        )
        candidates: list[datetime] = []
        for session in calendar:
            session_open = session.open
            if session_open.tzinfo is None:
                session_open = session_open.replace(tzinfo=ny)
            session_open = session_open.astimezone(timezone.utc)
            if session_open <= now:
                candidates.append(session_open)
        if not candidates:
            raise RuntimeError("Alpaca calendar returned no prior market session")
        return max(candidates)
