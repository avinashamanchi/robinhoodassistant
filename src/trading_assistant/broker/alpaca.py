"""Paper-only Alpaca implementation of BrokerClient + AlpacaClock (Phase 2).

Constructed execution clients use only official Alpaca paper; read-only data
and the optional WSS boundary use separately committed origins. The clients are
injected so mapping logic is unit-testable without network access;
:meth:`AlpacaBroker.from_credentials` rejects non-paper use.

Idempotency: every order carries ``client_order_id == idempotency_key``. Before
submitting we look the key up at the broker and, if it already exists, return
that order's status rather than creating a duplicate.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from threading import RLock
from typing import Any, Callable, Optional, TypeVar

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

from ..assets import AssetClass, canonicalize_broker_symbol
from ..risk.clock import MarketClockObservation
from ..security.outbound import (
    OutboundPolicy,
    PinnedWebSocket,
    install_pinned_session,
    require_origin,
)
from .base import (
    BrokerAcceptanceUnknown,
    BrokerClient,
    BrokerDataIntegrityError,
    BrokerSubmissionRejected,
)
from .models import (
    Account,
    BrokerFill,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderTimeInForce,
    OrderType,
    Position,
    Quote,
    normalize_fill_economic,
    valid_cumulative_filled_qty,
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
_OFFICIAL_PAPER_TRADING_URL = "https://paper-api.alpaca.markets"
_ALPACA_DATA_URL = "https://data.alpaca.markets"
_ALPACA_STREAM_URL = "wss://stream.data.alpaca.markets"
_PAPER_TRADING_POLICY = OutboundPolicy(_OFFICIAL_PAPER_TRADING_URL)
_DATA_POLICY = OutboundPolicy(_ALPACA_DATA_URL)
_STREAM_POLICY = OutboundPolicy(_ALPACA_STREAM_URL)


def build_alpaca_stream(
    *,
    runtime_role: str = "daemon",
    open_timeout: float = 5.0,
    ping_timeout: float = 30.0,
    close_timeout: float = 5.0,
) -> PinnedWebSocket:
    """Build the optional stream boundary with the concrete no-redirect adapter."""
    require_origin(
        runtime_role,
        "alpaca.stream",
        _ALPACA_STREAM_URL,
    )
    return PinnedWebSocket(
        _STREAM_POLICY,
        open_timeout=open_timeout,
        ping_timeout=ping_timeout,
        close_timeout=close_timeout,
    )


@dataclass(frozen=True)
class AlpacaExecutionTarget:
    """Immutable execution capability derived from the actual SDK client."""

    sandbox: bool
    base_url: str

    @property
    def is_official_paper(self) -> bool:
        return (
            self.sandbox is True
            and self.base_url == _OFFICIAL_PAPER_TRADING_URL
        )

# A long-running process keeps HTTP keep-alive sockets to Alpaca in a pool. When
# the load balancer closes an idle socket, the NEXT request on it raises before a
# response is read ("Remote end closed connection without response"). Retrying
# grabs/opens a fresh socket. Only these connection-level errors are transient;
# HTTP status errors (4xx/5xx APIError) are surfaced, not retried.
_TRANSIENT = (ReqConnectionError, ReqTimeout)
_T = TypeVar("_T")


def _install_transport_policy(
    client: Any,
    timeout_seconds: float,
    policy: OutboundPolicy,
) -> None:
    """Pin an Alpaca SDK client to its committed origin and bounded transport."""
    install_pinned_session(client, policy, read_timeout=timeout_seconds)


def _retry(fn: Callable[..., _T], *args: Any, attempts: int = 3, base_delay: float = 0.3, **kwargs: Any) -> _T:
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except _TRANSIENT as exc:  # stale socket / transient network blip
            last = exc
            log.warning(
                "alpaca transient failure code=broker_transient_failure "
                "operation=%s attempt=%d/%d",
                getattr(fn, "__name__", "broker_operation"),
                i + 1,
                attempts,
            )
            if i + 1 < attempts:
                time.sleep(base_delay * (i + 1))
    assert last is not None
    raise last


def _d(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    return Decimal(str(value))


def _finite_d(value: Any) -> Optional[Decimal]:
    parsed = _d(value)
    if parsed is None or not parsed.is_finite():
        return None
    return parsed


def _required_position_decimal(
    value: Any,
    *,
    symbol: str,
    field: str,
    positive: bool,
) -> Decimal:
    try:
        parsed = _d(value)
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise BrokerDataIntegrityError(
            f"invalid Alpaca position {field} for {symbol}"
        ) from exc
    if (
        parsed is None
        or not parsed.is_finite()
        or (positive and parsed <= 0)
        or (not positive and parsed == 0)
    ):
        raise BrokerDataIntegrityError(
            f"invalid Alpaca position {field} for {symbol}"
        )
    return parsed


def _utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
        self._mutation_lock = RLock()
        self._paper_only_mutations_armed = False

    @property
    def execution_target(self) -> AlpacaExecutionTarget | None:
        """Derive the capability from the SDK client at inspection time."""
        with self._mutation_lock:
            return self._execution_target_unlocked()

    def _execution_target_unlocked(self) -> AlpacaExecutionTarget | None:
        raw_sandbox = getattr(self._trading, "_sandbox", None)
        raw_base_url = getattr(self._trading, "_base_url", None)
        raw_base_url = getattr(raw_base_url, "value", raw_base_url)
        return (
            AlpacaExecutionTarget(
                sandbox=raw_sandbox,
                base_url=raw_base_url,
            )
            if type(raw_sandbox) is bool
            and isinstance(raw_base_url, str)
            else None
        )

    def arm_paper_only_mutations(self) -> None:
        """Permanently fail closed if a later mutation is not official paper."""
        with self._mutation_lock:
            self._require_armed_paper_target_unlocked(force=True)
            self._paper_only_mutations_armed = True

    def validate_armed_paper_target(self) -> None:
        """Dynamically prove the currently armed SDK client is official paper."""
        with self._mutation_lock:
            if not self._paper_only_mutations_armed:
                raise BrokerSubmissionRejected(
                    "paper_mutation_guard_unarmed",
                    "paper-only mutation guard is not armed",
                )
            self._require_armed_paper_target_unlocked(force=True)

    def _require_armed_paper_target_unlocked(
        self,
        *,
        force: bool = False,
    ) -> None:
        if not force and not self._paper_only_mutations_armed:
            return
        target = self._execution_target_unlocked()
        if target is None or target.is_official_paper is not True:
            raise BrokerSubmissionRejected(
                "unsafe_execution_target",
                "broker mutation target is not official Alpaca paper",
            )

    @classmethod
    def from_credentials(
        cls,
        api_key: str,
        secret_key: str,
        *,
        paper: bool = True,
        timeout_seconds: float = 10.0,
        runtime_role: str = "app",
    ) -> "AlpacaBroker":
        if paper is not True:
            raise ValueError("paper-only Alpaca client required")
        require_origin(
            runtime_role,
            "alpaca.trading",
            _OFFICIAL_PAPER_TRADING_URL,
        )
        require_origin(
            runtime_role,
            "alpaca.historical",
            _ALPACA_DATA_URL,
        )
        trading = TradingClient(
            api_key,
            secret_key,
            paper=True,
            url_override=_OFFICIAL_PAPER_TRADING_URL,
        )
        data = StockHistoricalDataClient(
            api_key,
            secret_key,
            url_override=_ALPACA_DATA_URL,
        )
        crypto_data = CryptoHistoricalDataClient(
            api_key,
            secret_key,
            url_override=_ALPACA_DATA_URL,
        )
        _install_transport_policy(trading, timeout_seconds, _PAPER_TRADING_POLICY)
        _install_transport_policy(data, timeout_seconds, _DATA_POLICY)
        _install_transport_policy(crypto_data, timeout_seconds, _DATA_POLICY)
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
        latest_trade = getattr(snap, "latest_trade", None)
        latest_quote = getattr(snap, "latest_quote", None)
        last = _d(getattr(latest_trade, "price", None))
        bid = _d(getattr(latest_quote, "bid_price", None))
        ask = _d(getattr(latest_quote, "ask_price", None))
        prev_close = _d(snap.previous_daily_bar.close) if snap.previous_daily_bar else None
        if last is None or bid is None or ask is None:
            raise ValueError(
                f"invalid Alpaca quote for {symbol}: missing price component"
            )
        book_as_of = _utc_timestamp(getattr(latest_quote, "timestamp", None))
        trade_as_of = _utc_timestamp(getattr(latest_trade, "timestamp", None))
        if book_as_of is None or trade_as_of is None:
            raise ValueError(
                f"invalid Alpaca quote for {symbol}: missing component timestamp"
            )
        quote = Quote(
            ticker=symbol,
            bid=bid,
            ask=ask,
            last=last,
            prev_close=prev_close,
            as_of=min(book_as_of, trade_as_of),
            book_as_of=book_as_of,
            trade_as_of=trade_as_of,
        )
        if not quote.is_valid:
            raise ValueError(
                f"invalid Alpaca quote for {symbol}: invalid price component"
            )
        return quote

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
        seen_symbols: set[str] = set()
        for p in _retry(self._trading.get_all_positions):
            raw_symbol = getattr(p, "symbol", None)
            if (
                not isinstance(raw_symbol, str)
                or not raw_symbol.strip()
            ):
                raise BrokerDataIntegrityError(
                    "invalid Alpaca position symbol"
                )
            try:
                symbol = canonicalize_broker_symbol(
                    raw_symbol,
                    asset_class=getattr(p, "asset_class", None),
                )
            except ValueError as exc:
                raise BrokerDataIntegrityError(
                    "invalid Alpaca position symbol"
                ) from exc
            if symbol in seen_symbols:
                raise BrokerDataIntegrityError(
                    f"duplicate Alpaca position symbol {symbol}"
                )
            seen_symbols.add(symbol)
            out.append(
                Position(
                    ticker=symbol,
                    qty=_required_position_decimal(
                        getattr(p, "qty", None),
                        symbol=symbol,
                        field="quantity",
                        positive=False,
                    ),
                    avg_entry_price=_required_position_decimal(
                        getattr(p, "avg_entry_price", None),
                        symbol=symbol,
                        field="average entry price",
                        positive=True,
                    ),
                    current_price=_required_position_decimal(
                        getattr(p, "current_price", None),
                        symbol=symbol,
                        field="current price",
                        positive=True,
                    ),
                    unrealized_intraday_pnl=_finite_d(
                        getattr(p, "unrealized_intraday_pl", None)
                    ),
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
                raw_order_id = raw.get("order_id")
                broker_order_id = (
                    str(raw_order_id)
                    if raw_order_id is not None
                    else None
                )
                if not broker_order_id or not broker_order_id.strip():
                    raise BrokerDataIntegrityError(
                        "invalid Alpaca fill broker order identity"
                    )
                raw_activity_id = raw.get("id")
                if (
                    raw_activity_id is None
                    or (
                        isinstance(raw_activity_id, str)
                        and not raw_activity_id.strip()
                    )
                ):
                    raise BrokerDataIntegrityError(
                        "invalid Alpaca fill activity identity",
                        broker_order_id=broker_order_id,
                    )
                broker_fill_id = str(raw_activity_id)
                timestamp = datetime.fromisoformat(
                    str(raw["transaction_time"]).replace("Z", "+00:00")
                ).astimezone(timezone.utc)
                raw_side = str(raw["side"]).lower()
                if raw_side in {"buy", "buy_to_cover"}:
                    side = "buy"
                elif raw_side in {"sell", "sell_short"}:
                    side = "sell"
                else:
                    raise BrokerDataIntegrityError(
                        f"invalid Alpaca fill side: {raw_side}",
                        broker_order_id=broker_order_id,
                    )
                try:
                    qty = Decimal(str(raw["qty"]))
                    price = Decimal(str(raw["price"]))
                except (ArithmeticError, TypeError, ValueError) as exc:
                    raise BrokerDataIntegrityError(
                        "invalid Alpaca fill quantity or price",
                        broker_order_id=broker_order_id,
                    ) from exc
                normalized_qty = normalize_fill_economic(qty)
                normalized_price = normalize_fill_economic(price)
                if normalized_qty is None or normalized_price is None:
                    raise BrokerDataIntegrityError(
                        "invalid Alpaca fill quantity or price",
                        broker_order_id=broker_order_id,
                    )
                try:
                    symbol = canonicalize_broker_symbol(
                        raw.get("symbol"),
                        asset_class=raw.get("asset_class"),
                    )
                except ValueError as exc:
                    raise BrokerDataIntegrityError(
                        "invalid Alpaca fill symbol",
                        broker_order_id=broker_order_id,
                    ) from exc
                fills.append(
                    BrokerFill(
                        broker_fill_id=broker_fill_id,
                        broker_order_id=broker_order_id,
                        ticker=symbol,
                        side=side,
                        qty=normalized_qty,
                        price=normalized_price,
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
        except BrokerSubmissionRejected:
            raise
        except BrokerDataIntegrityError:
            raise
        except APIError as exc:
            raise self._submission_exception(exc) from None
        except Exception:
            # The POST was attempted. A timeout, malformed response, or connection
            # reset cannot prove that Alpaca did not accept the client id.
            raise BrokerAcceptanceUnknown(
                "broker submission acceptance is unknown"
            ) from None

    def _submit_once(self, order: OrderRequest) -> OrderResult:
        side = (
            AlpacaOrderSide.BUY if order.side is OrderSide.BUY else AlpacaOrderSide.SELL
        )
        common = dict(
            symbol=order.ticker.upper(),
            side=side,
            time_in_force=(
                TimeInForce.GTC
                if (
                    AssetClass.for_symbol(order.ticker) is AssetClass.CRYPTO
                    or order.time_in_force is OrderTimeInForce.GTC
                )
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

        with self._mutation_lock:
            self._require_armed_paper_target_unlocked()
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
        except BrokerSubmissionRejected:
            raise
        except BrokerDataIntegrityError:
            raise
        except APIError as exc:
            raise self._submission_exception(exc) from None
        except Exception:
            raise BrokerAcceptanceUnknown(
                "broker submission acceptance is unknown"
            ) from None

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
        with self._mutation_lock:
            self._require_armed_paper_target_unlocked()
            placed = self._trading.submit_order(order_data=req)
        return self._to_result(placed)

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
        # A dropped response after DELETE leaves acceptance unknown. Retrying the
        # write would violate the one-attempt boundary; reconciliation resolves it.
        with self._mutation_lock:
            self._require_armed_paper_target_unlocked()
            self._trading.cancel_order_by_id(order_id)
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
            return BrokerSubmissionRejected(
                f"alpaca_http_{status_code}",
                "broker rejected the submitted order",
            )
        return BrokerAcceptanceUnknown(
            "broker submission acceptance is unknown"
        )

    def _to_result(self, o: Any) -> OrderResult:
        raw_broker_order_id = getattr(o, "id", None)
        broker_order_id = (
            str(raw_broker_order_id)
            if raw_broker_order_id is not None
            else None
        )
        try:
            filled_qty = _d(getattr(o, "filled_qty", 0))
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise BrokerDataIntegrityError(
                "invalid Alpaca filled_qty",
                broker_order_id=broker_order_id,
            ) from exc
        if not valid_cumulative_filled_qty(filled_qty):
            raise BrokerDataIntegrityError(
                "invalid Alpaca filled_qty",
                broker_order_id=broker_order_id,
            )
        raw_symbol = getattr(o, "symbol", None)
        ticker = None
        if raw_symbol is not None:
            try:
                ticker = canonicalize_broker_symbol(
                    raw_symbol,
                    asset_class=getattr(o, "asset_class", None),
                )
            except ValueError as exc:
                raise BrokerDataIntegrityError(
                    "invalid Alpaca order symbol",
                    broker_order_id=broker_order_id,
                ) from exc
        return OrderResult(
            idempotency_key=getattr(o, "client_order_id", "") or "",
            broker_order_id=broker_order_id,
            status=_map_status(o.status),
            filled_qty=filled_qty,
            avg_fill_price=_d(getattr(o, "filled_avg_price", None)),
            ticker=ticker,
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
        runtime_role: str = "app",
    ) -> "AlpacaClock":
        if paper is not True:
            raise ValueError("paper-only Alpaca client required")
        require_origin(
            runtime_role,
            "alpaca.trading",
            _OFFICIAL_PAPER_TRADING_URL,
        )
        client = TradingClient(
            api_key,
            secret_key,
            paper=True,
            url_override=_OFFICIAL_PAPER_TRADING_URL,
        )
        _install_transport_policy(client, timeout_seconds, _PAPER_TRADING_POLICY)
        return cls(client)

    def is_open(self, at=None) -> bool:
        if at is not None:
            return self.observe(at).is_open
        value = _retry(self._trading.get_clock).is_open
        if type(value) is not bool:
            raise BrokerDataIntegrityError(
                "invalid Alpaca market clock state"
            )
        return value

    def next_open(self, at=None):
        return _retry(self._trading.get_clock).next_open

    def next_close(self, at=None):
        return _retry(self._trading.get_clock).next_close

    def observe(self, at: datetime) -> MarketClockObservation:
        observed_at = self._observation_time(at)
        exchange_timezone = ZoneInfo("America/New_York")
        local_date = observed_at.astimezone(exchange_timezone).date()
        start_date = local_date - timedelta(days=14)
        # Alpaca's current-clock endpoint would be sampled after ``at`` and
        # therefore cannot be reconciled to this exact historical instant.
        # The official exchange calendar is the sole source for this path.
        raw_calendar = _retry(
            self._trading.get_calendar,
            GetCalendarRequest(start=start_date, end=local_date),
        )
        sessions = self._validated_sessions(
            raw_calendar,
            exchange_timezone=exchange_timezone,
            start_date=start_date,
            end_date=local_date,
        )
        prior_opens = [
            session_open
            for _, session_open, _ in sessions
            if session_open <= observed_at
        ]
        if not prior_opens:
            raise BrokerDataIntegrityError(
                "invalid Alpaca market calendar"
            )
        market_open = any(
            session_open <= observed_at < session_close
            for _, session_open, session_close in sessions
        )
        return MarketClockObservation(
            is_open=market_open,
            most_recent_open=max(prior_opens),
        )

    def most_recent_open(self, at=None) -> datetime:
        observed_at = at or datetime.now(timezone.utc)
        return self.observe(observed_at).most_recent_open

    @staticmethod
    def _observation_time(at: datetime) -> datetime:
        if (
            not isinstance(at, datetime)
            or at.tzinfo is None
            or at.utcoffset() is None
        ):
            raise BrokerDataIntegrityError(
                "invalid Alpaca market calendar"
            )
        return at.astimezone(timezone.utc)

    @classmethod
    def _validated_sessions(
        cls,
        raw_calendar,
        *,
        exchange_timezone: ZoneInfo,
        start_date: date,
        end_date: date,
    ) -> list[tuple[date, datetime, datetime]]:
        try:
            calendar = list(raw_calendar)
        except Exception:
            raise BrokerDataIntegrityError(
                "invalid Alpaca market calendar"
            ) from None

        sessions: list[tuple[date, datetime, datetime]] = []
        seen_dates: set[date] = set()
        try:
            for session in calendar:
                session_date = session.date
                if (
                    type(session_date) is not date
                    or session_date < start_date
                    or session_date > end_date
                    or session_date in seen_dates
                ):
                    raise ValueError
                session_open = cls._session_boundary(
                    session.open,
                    session_date=session_date,
                    exchange_timezone=exchange_timezone,
                )
                session_close = cls._session_boundary(
                    session.close,
                    session_date=session_date,
                    exchange_timezone=exchange_timezone,
                )
                if session_open >= session_close:
                    raise ValueError
                seen_dates.add(session_date)
                sessions.append(
                    (session_date, session_open, session_close)
                )
        except Exception:
            raise BrokerDataIntegrityError(
                "invalid Alpaca market calendar"
            ) from None
        return sessions

    @staticmethod
    def _session_boundary(
        boundary,
        *,
        session_date: date,
        exchange_timezone: ZoneInfo,
    ) -> datetime:
        if not isinstance(boundary, datetime):
            raise ValueError
        if boundary.tzinfo is None:
            local_boundary = boundary.replace(
                tzinfo=exchange_timezone
            )
        else:
            if boundary.utcoffset() is None:
                raise ValueError
            local_boundary = boundary.astimezone(exchange_timezone)
        if local_boundary.date() != session_date:
            raise ValueError
        return local_boundary.astimezone(timezone.utc)
