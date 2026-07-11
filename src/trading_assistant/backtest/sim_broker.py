"""Simulated broker implementing BrokerClient for backtests.

Fill model:
* Market orders fill at the NEXT bar's open (submitted at t, filled at t+1),
  with per-asset-class slippage applied adverse to the trade direction.
* Limit orders fill only if the bar's range crosses the limit price.
* Partial fills are capped at ``max_participation_pct`` of the bar's volume; any
  remainder carries to the next bar.
* Fees are charged separately from slippage (crypto pays both).

Emits SimFill records so downstream code can reconstruct P&L and exercise the
stress scenarios.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from itertools import count
from typing import Optional

import pandas as pd

from ..assets import AssetClass
from ..broker.base import BrokerClient
from ..broker.models import (
    Account,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Position,
    Quote,
)
from ..config import BacktestConfig


@dataclass
class SimFill:
    symbol: str
    side: str
    qty: float
    price: float
    fee: float
    ts: datetime


@dataclass
class _Pending:
    order: OrderRequest
    asset_class: AssetClass
    remaining_qty: Optional[float] = None  # resolved at first fill bar


@dataclass
class _Pos:
    qty: float = 0.0
    cost: float = 0.0  # total cost basis of the open position

    @property
    def avg(self) -> float:
        return self.cost / self.qty if self.qty else 0.0


class SimBroker(BrokerClient):
    def __init__(
        self, config: BacktestConfig, starting_cash: float = 100_000.0
    ) -> None:
        self.config = config
        self.cash = float(starting_cash)
        self._pos: dict[str, _Pos] = {}
        self._last: dict[str, float] = {}
        self._pending: list[_Pending] = []
        self.fills: list[SimFill] = []
        self._ids = count(1)
        self._orders: dict[str, OrderResult] = {}

    # ── cost model ─────────────────────────────────────────────
    def _slippage(self, ac: AssetClass) -> float:
        return self.config.slippage_bps.get(ac.value, 0.0) / 10_000.0

    def _fee_rate(self, ac: AssetClass) -> float:
        return self.config.fees_bps.get(ac.value, 0.0) / 10_000.0

    # ── BrokerClient interface ─────────────────────────────────
    def get_quote(self, ticker: str) -> Quote:
        last = Decimal(str(self._last.get(ticker.upper(), 0.0)))
        return Quote(ticker=ticker.upper(), bid=last, ask=last, last=last, prev_close=last)

    def get_account(self) -> Account:
        equity = Decimal(str(self.equity()))
        cash = Decimal(str(self.cash))
        return Account(buying_power=cash, equity=equity, cash=cash)

    def get_positions(self) -> list[Position]:
        out = []
        for sym, p in self._pos.items():
            if p.qty == 0:
                continue
            last = Decimal(str(self._last.get(sym, p.avg)))
            out.append(
                Position(
                    ticker=sym,
                    qty=Decimal(str(p.qty)),
                    avg_entry_price=Decimal(str(p.avg)),
                    current_price=last,
                )
            )
        return out

    def submit_order(self, order: OrderRequest) -> OrderResult:
        ac = AssetClass.for_symbol(order.ticker)
        self._pending.append(_Pending(order=order, asset_class=ac))
        result = OrderResult(
            idempotency_key=order.idempotency_key,
            broker_order_id=f"sim-{next(self._ids)}",
            status=OrderStatus.SUBMITTED,
        )
        self._orders[result.broker_order_id] = result
        return result

    def submit_bracket(self, order: OrderRequest, take_profit, stop_loss) -> OrderResult:
        """Record a server-side bracket (entry + OCO take-profit/stop). Test double."""
        result = self.submit_order(order)
        self.brackets.append(
            {"order": order, "take_profit": take_profit, "stop_loss": stop_loss,
             "broker_order_id": result.broker_order_id}
        )
        return result

    def get_order_status(self, order_id: str) -> OrderResult:
        return self._orders[order_id]

    def cancel_order(self, order_id: str) -> OrderResult:
        return self._orders[order_id]

    # ── simulation driver ──────────────────────────────────────
    def mark(self, symbol: str, price: float) -> None:
        self._last[symbol.upper()] = float(price)

    def equity(self) -> float:
        mv = sum(p.qty * self._last.get(sym, p.avg) for sym, p in self._pos.items())
        return self.cash + mv

    def process_bar(self, ts: datetime, bars: dict[str, pd.Series]) -> None:
        """Fill pending orders against the newly-arrived bar for each symbol."""
        for sym, bar in bars.items():
            self.mark(sym, float(bar["close"]))

        still_pending: list[_Pending] = []
        for pend in self._pending:
            sym = pend.order.ticker.upper()
            bar = bars.get(sym)
            if bar is None:
                still_pending.append(pend)  # no data this bar; carry
                continue
            carried = self._try_fill(pend, ts, bar)
            if carried is not None:
                still_pending.append(carried)
        self._pending = still_pending

    def _try_fill(self, pend: _Pending, ts: datetime, bar: pd.Series) -> Optional[_Pending]:
        order = pend.order
        is_buy = order.side.value == "buy"
        slip = self._slippage(pend.asset_class)

        if order.order_type is OrderType.LIMIT:
            limit = float(order.limit_price)
            crossed = (is_buy and float(bar["low"]) <= limit) or (
                not is_buy and float(bar["high"]) >= limit
            )
            if not crossed:
                return pend  # not crossed; carry
            fill_price = limit
        else:  # market: next bar open, slippage adverse
            fill_price = float(bar["open"]) * (1 + slip if is_buy else 1 - slip)

        if pend.remaining_qty is None:
            if order.qty is not None:
                pend.remaining_qty = float(order.qty)
            else:
                pend.remaining_qty = float(order.notional) / fill_price

        cap = self.config.fills.max_participation_pct / 100.0 * float(bar["volume"])
        fill_qty = min(pend.remaining_qty, cap) if cap > 0 else pend.remaining_qty
        if fill_qty <= 0:
            return pend

        fee = fill_qty * fill_price * self._fee_rate(pend.asset_class)
        pos = self._pos.setdefault(order.ticker.upper(), _Pos())
        if is_buy:
            self.cash -= fill_qty * fill_price + fee
            pos.qty += fill_qty
            pos.cost += fill_qty * fill_price
        else:
            self.cash += fill_qty * fill_price - fee
            if pos.qty > 0:
                pos.cost -= (fill_qty / pos.qty) * pos.cost
            pos.qty -= fill_qty

        self.fills.append(
            SimFill(order.ticker.upper(), order.side.value, fill_qty, fill_price, fee, ts)
        )

        pend.remaining_qty -= fill_qty
        return pend if pend.remaining_qty > 1e-9 else None
