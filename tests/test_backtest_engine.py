"""Sim engine: no-lookahead DataView (guardrail #4), fill model, end-to-end run."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from trading_assistant.backtest.data import DataSource, LookaheadError
from trading_assistant.backtest.engine import run_backtest
from trading_assistant.backtest.sim_broker import SimBroker
from trading_assistant.backtest.synthetic import make_bars, make_trend
from trading_assistant.broker.models import OrderRequest, OrderSide, OrderType
from trading_assistant.config import BacktestConfig
from trading_assistant.strategies.buy_and_hold import BuyAndHold
from trading_assistant.strategies.sma_crossover import SmaCrossover


def _order(symbol, side, notional=None, qty=None, limit=None, otype=OrderType.MARKET):
    return OrderRequest(
        ticker=symbol,
        side=side,
        order_type=otype,
        idempotency_key=f"k-{symbol}-{notional}-{qty}",
        notional=Decimal(str(notional)) if notional is not None else None,
        qty=Decimal(str(qty)) if qty is not None else None,
        limit_price=Decimal(str(limit)) if limit is not None else None,
    )


def _bar(o, h, l, c, v):
    return pd.Series({"open": o, "high": h, "low": l, "close": c, "volume": v})


# ── no-lookahead (guardrail #4, permanent) ──────────────────────
def test_dataview_only_returns_past():
    source = DataSource({"AAPL": make_bars(50, seed=1)})
    timeline = source.timeline(["AAPL"])
    t = timeline[20]
    view = source.view(t)
    hist = view.history("AAPL")
    assert hist.index.max().to_pydatetime() <= t


def test_dataview_future_access_raises():
    source = DataSource({"AAPL": make_bars(50, seed=1)})
    timeline = source.timeline(["AAPL"])
    view = source.view(timeline[20])
    with pytest.raises(LookaheadError):
        view.get_at("AAPL", timeline[30])  # future relative to t


def test_spy_context_cannot_see_future():
    """Market context flows through the same view — future SPY must be unreachable."""
    source = DataSource({"AAPL": make_bars(50, seed=1), "SPY": make_bars(50, seed=2)})
    timeline = source.timeline(["AAPL"])
    view = source.view(timeline[25])
    assert view.history("SPY").index.max().to_pydatetime() <= timeline[25]
    with pytest.raises(LookaheadError):
        view.get_at("SPY", timeline[40])


# ── fill model ──────────────────────────────────────────────────
def test_market_fill_next_open_with_slippage():
    broker = SimBroker(BacktestConfig(), starting_cash=100_000)
    broker.submit_order(_order("AAPL", OrderSide.BUY, notional=1000))
    broker.process_bar("t1", {"AAPL": _bar(100, 101, 99, 100, 1_000_000)})
    fill = broker.fills[0]
    assert fill.price == pytest.approx(100 * 1.0005)  # 5 bps equity slippage
    assert fill.fee == 0.0                            # equities commission-free
    assert broker.cash == pytest.approx(99_000, abs=1)


def test_partial_fill_caps_at_participation_and_carries():
    broker = SimBroker(BacktestConfig(), starting_cash=100_000)  # 10% participation
    broker.submit_order(_order("AAPL", OrderSide.BUY, qty=10))
    # Bar volume 50 -> cap = 5 shares this bar.
    broker.process_bar("t1", {"AAPL": _bar(100, 101, 99, 100, 50)})
    assert broker.fills[0].qty == pytest.approx(5.0)
    assert len(broker._pending) == 1  # remainder carried
    broker.process_bar("t2", {"AAPL": _bar(100, 101, 99, 100, 50)})
    assert len(broker.fills) == 2      # rest fills next bar


def test_crypto_charges_fee_and_slippage():
    broker = SimBroker(BacktestConfig(), starting_cash=100_000)
    broker.submit_order(_order("BTC/USD", OrderSide.BUY, notional=1000))
    broker.process_bar("t1", {"BTC/USD": _bar(100, 101, 99, 100, 1_000_000)})
    fill = broker.fills[0]
    assert fill.price == pytest.approx(100 * 1.0020)   # 20 bps crypto slippage
    assert fill.fee == pytest.approx(1000 * 0.0025, rel=1e-3)  # 25 bps taker fee
    assert broker.cash < 99_000                         # both costs applied


def test_limit_order_only_fills_when_crossed():
    broker = SimBroker(BacktestConfig(), starting_cash=100_000)
    broker.submit_order(_order("AAPL", OrderSide.BUY, qty=1, limit=95, otype=OrderType.LIMIT))
    broker.process_bar("t1", {"AAPL": _bar(100, 101, 99, 100, 1_000_000)})  # low 99 > 95
    assert broker.fills == []                            # not crossed
    broker.process_bar("t2", {"AAPL": _bar(96, 97, 94, 95, 1_000_000)})     # low 94 <= 95
    assert broker.fills[0].price == 95.0


# ── end-to-end ──────────────────────────────────────────────────
def test_buy_and_hold_grows_in_uptrend():
    source = DataSource({"AAPL": make_trend(n_base=5, n_move=200, start=100.0, end=200.0)})
    res = run_backtest(BuyAndHold(), source, "AAPL", backtest_config=BacktestConfig())
    assert len(res.fills) >= 1
    assert res.ending_equity > res.starting_equity   # captured the uptrend
    assert res.total_return_pct > 50                 # ~doubling of the invested slice


def test_sma_crossover_trades_and_is_deterministic():
    source = DataSource({"AAPL": make_bars(400, seed=7)})
    r1 = run_backtest(SmaCrossover(), source, "AAPL", backtest_config=BacktestConfig())
    r2 = run_backtest(SmaCrossover(), source, "AAPL", backtest_config=BacktestConfig())
    assert r1.ending_equity == r2.ending_equity       # deterministic
    assert len(r1.equity_curve) == len(source.timeline(["AAPL"]))
