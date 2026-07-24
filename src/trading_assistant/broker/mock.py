"""Deterministic in-memory broker for tests and local dev.

Prices are derived deterministically from the ticker symbol so tests never
depend on wall-clock or network. Idempotency is enforced: submitting the same
key twice returns the first order rather than creating a second.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from decimal import Decimal
from typing import Optional

from .base import BrokerClient
from .models import (
    Account,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Position,
    Quote,
)


def _deterministic_price(ticker: str) -> Decimal:
    """Stable pseudo-price in [50, 550) derived from the ticker."""
    seed = sum(ord(c) for c in ticker.upper())
    return Decimal(50 + (seed * 7) % 500)


class MockBroker(BrokerClient):
    reconciliation_key = "mock"

    def __init__(
        self,
        prices: Optional[dict[str, Decimal]] = None,
        positions: Optional[list[Position]] = None,
        buying_power: Decimal = Decimal(100_000),
    ) -> None:
        self._prices = {k.upper(): v for k, v in (prices or {}).items()}
        self._positions = {p.ticker.upper(): p for p in (positions or [])}
        self._session_open_prices = {
            p.ticker.upper(): p.current_price for p in (positions or [])
        }
        self._buying_power = buying_power
        # idempotency_key -> OrderResult (the authoritative record)
        self._orders_by_key: dict[str, OrderResult] = {}
        self._orders_by_id: dict[str, OrderResult] = {}
        self._id_counter = itertools.count(1)
        self.brackets: list[dict] = []

    # ── market data ────────────────────────────────────────────
    def price_of(self, ticker: str) -> Decimal:
        return self._prices.get(ticker.upper(), _deterministic_price(ticker))

    def set_price(self, ticker: str, price: Decimal) -> None:
        self._prices[ticker.upper()] = price

    def set_session_open_price(self, ticker: str, price: Decimal) -> None:
        self._session_open_prices[ticker.upper()] = price

    def get_quote(self, ticker: str) -> Quote:
        last = self.price_of(ticker)
        spread = (last * Decimal("0.001")).quantize(Decimal("0.01"))
        return Quote(
            ticker=ticker.upper(),
            bid=last - spread,
            ask=last + spread,
            last=last,
            prev_close=last,  # flat prev close keeps day_change deterministic (0%)
        )

    # ── account / positions ────────────────────────────────────
    def get_account(self) -> Account:
        equity = self._buying_power + sum(
            (
                position.qty * self.price_of(symbol)
                for symbol, position in self._positions.items()
            ),
            Decimal(0),
        )
        return Account(
            buying_power=self._buying_power, equity=equity, cash=self._buying_power
        )

    def get_positions(self) -> list[Position]:
        return [
            replace(
                position,
                current_price=self.price_of(symbol),
                unrealized_intraday_pnl=(
                    position.qty
                    * (
                        self.price_of(symbol)
                        - self._session_open_prices.get(
                            symbol, position.current_price
                        )
                    )
                ),
            )
            for symbol, position in self._positions.items()
        ]

    # ── orders (idempotent) ────────────────────────────────────
    def submit_order(self, order: OrderRequest) -> OrderResult:
        existing = self._orders_by_key.get(order.idempotency_key)
        if existing is not None:
            # Never resubmit; return the authoritative status.
            return existing

        broker_id = f"mock-{next(self._id_counter)}"
        result = OrderResult(
            idempotency_key=order.idempotency_key,
            broker_order_id=broker_id,
            status=OrderStatus.SUBMITTED,
        )
        self._orders_by_key[order.idempotency_key] = result
        self._orders_by_id[broker_id] = result
        return result

    def get_order_by_client_id(self, client_order_id: str) -> OrderResult | None:
        return self._orders_by_key.get(client_order_id)

    def get_open_orders(self) -> list[OrderResult]:
        return [
            order
            for order in self._orders_by_id.values()
            if order.status in {
                OrderStatus.SUBMITTED,
                OrderStatus.PARTIALLY_FILLED,
            }
        ]

    def submit_bracket(self, order: OrderRequest, take_profit, stop_loss) -> OrderResult:
        result = self.submit_order(order)
        self.brackets.append(
            {"order": order, "take_profit": take_profit, "stop_loss": stop_loss}
        )
        return result

    def get_order_status(self, order_id: str) -> OrderResult:
        result = self._orders_by_id.get(order_id)
        if result is None:
            raise KeyError(f"unknown order id: {order_id}")
        return result

    def cancel_order(self, order_id: str) -> OrderResult:
        result = self.get_order_status(order_id)
        canceled = OrderResult(
            idempotency_key=result.idempotency_key,
            broker_order_id=result.broker_order_id,
            status=OrderStatus.CANCELED,
            filled_qty=result.filled_qty,
            avg_fill_price=result.avg_fill_price,
        )
        self._orders_by_id[order_id] = canceled
        self._orders_by_key[result.idempotency_key] = canceled
        return canceled
