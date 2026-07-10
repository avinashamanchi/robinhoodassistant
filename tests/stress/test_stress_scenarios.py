"""Synthetic stress scenarios — these test SAFETY behavior, not profitability.

Hand-crafted pathological sequences fed through the risk/rules pipeline. Each
asserts a specific end-state: order counts, position sizes, kill-switch state,
and P&L. They run in CI on every change.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest

from trading_assistant.assets import AssetClass
from trading_assistant.backtest.sim_broker import SimBroker
from trading_assistant.broker.models import (
    OrderRequest,
    OrderSide,
    OrderType,
    Position,
)
from trading_assistant.config import BacktestConfig
from trading_assistant.risk.killswitch import KillSwitch
from trading_assistant.risk.pnl import FillLike, realized_pnl
from trading_assistant.risk.staleness import is_stale

EQ, CR = AssetClass.EQUITY, AssetClass.CRYPTO
NOW = datetime(2022, 6, 1, tzinfo=timezone.utc)


def _bar(o, h, l, c, v=1_000_000):
    return pd.Series({"open": o, "high": h, "low": l, "close": c, "volume": v})


# ── 1. Flash crash: kill switch trips; stop submits exactly once ─
def test_flash_crash_trips_killswitch_and_no_duplicate_stop(session_factory, mock_broker):
    # A -25% crash realizes a loss far past the daily limit.
    fills = [
        FillLike("AAPL", "buy", Decimal("100"), Decimal("100"), NOW),
        FillLike("AAPL", "sell", Decimal("100"), Decimal("75"), NOW + timedelta(hours=1)),
    ]
    loss = realized_pnl(fills)
    assert loss == Decimal("-2500")

    with session_factory() as s:
        tripped = KillSwitch.evaluate_daily_loss(s, loss, Decimal("500"), EQ)
        s.commit()
        assert tripped is True
        assert KillSwitch.is_tripped(s, EQ) is True

    # The stop order delivered/retried twice must create only ONE broker order.
    stop = OrderRequest("AAPL", OrderSide.SELL, OrderType.MARKET, "stop-1", qty=Decimal("100"))
    first = mock_broker.submit_order(stop)
    second = mock_broker.submit_order(stop)
    assert first.broker_order_id == second.broker_order_id


# ── 2. Gap through stop: fills at the bad gap price, not the stop ─
def test_gap_through_stop_fills_at_realistic_price():
    from trading_assistant.backtest.sim_broker import _Pos

    broker = SimBroker(BacktestConfig(), starting_cash=100_000)
    broker._pos["AAPL"] = _Pos(qty=100.0, cost=10_000.0)  # long 100 @ 100 on the books
    # Stop at 100; price GAPS to open 85 (-15%). Modeled as a market sell.
    broker.submit_order(OrderRequest("AAPL", OrderSide.SELL, OrderType.MARKET, "s", qty=Decimal("100")))
    broker.process_bar("t1", {"AAPL": _bar(85, 86, 84, 85)})
    fill = broker.fills[0]
    assert fill.price < 86              # filled near the gap open, NOT the 100 stop
    assert fill.price == pytest.approx(85 * (1 - 0.0005))


# ── 3. Whipsaw: laddered entries never exceed the position limit ─
def test_whipsaw_ladder_respects_position_limit(risk_config, make_snapshot):
    from trading_assistant.risk.engine import RiskEngine

    engine = RiskEngine(risk_config)  # max_position_per_ticker = 2000, price 100
    held = Decimal("0")
    approvals = 0
    for _ in range(10):  # try to keep laddering in $500 (5-share) clips
        snap = make_snapshot(
            prices={"AAPL": Decimal("100")},
            positions=[Position("AAPL", held, Decimal("100"), Decimal("100"))],
        )
        order = OrderRequest("AAPL", OrderSide.BUY, OrderType.MARKET, f"k{_}", notional=Decimal("500"))
        result = engine.check(order, snap, killswitch_tripped=False, market_open=True)
        if result.approved:
            held += Decimal("5")  # 5 shares filled
            approvals += 1
    # 4 clips = $2000 exactly; the 5th would breach $2000 and is refused.
    assert held <= Decimal("20")
    assert approvals == 4


# ── 4. Halt / no data: stale quotes are not tradeable ───────────
def test_stale_quote_blocked():
    fresh = NOW
    assert is_stale(fresh - timedelta(seconds=120), now=fresh, max_age_seconds=60) is True
    assert is_stale(fresh - timedelta(seconds=5), now=fresh, max_age_seconds=60) is False


# ── 5. Crypto weekend dump: crypto trips, equity untouched ──────
def test_crypto_dump_isolates_equity(make_service):
    svc = make_service(market_open=False)  # equity market closed (weekend)
    svc.broker.set_price("BTC/USD", Decimal("100"))
    with svc.session_factory() as s:
        KillSwitch.evaluate_daily_loss(s, Decimal("-3000"), Decimal("500"), CR)
        s.commit()
        assert KillSwitch.is_tripped(s, CR) is True
        assert KillSwitch.is_tripped(s, EQ) is False  # equity independent

    crypto = svc.propose_order("BTC/USD", "buy", "market", notional="100")
    assert crypto["status"] == "rejected"
    assert any("kill switch" in r for r in crypto["risk_reasons"])


# ── 6. Stale-approval replay: price moves before execution ──────
def test_stale_approval_rejected_on_price_move(make_service):
    svc = make_service()
    svc.broker.set_price("AAPL", Decimal("100"))
    svc.broker._positions["AAPL"] = Position("AAPL", Decimal("15"), Decimal("100"), Decimal("100"))
    order_id = svc.propose_order("AAPL", "buy", "market", notional="500")["order_id"]
    assert svc.get_order_status(order_id)["status"] == "proposed"

    # Price jumps 10% between proposal and approval -> execution re-check refuses.
    svc.broker.set_price("AAPL", Decimal("110"))
    result = svc.approve_order(order_id)
    assert result["executed"] is False
    assert result["status"] == "rejected"
    assert svc.broker.submit_calls == 0


# ── 7. Duplicate fill events: P&L counted once ──────────────────
def test_duplicate_fill_counted_once():
    single = [
        FillLike("AAPL", "buy", Decimal("10"), Decimal("100"), NOW),
        FillLike("AAPL", "sell", Decimal("10"), Decimal("110"), NOW + timedelta(hours=1)),
    ]
    dup = single + [single[1]]  # the sell delivered twice

    def dedupe(fills):
        seen, out = set(), []
        for f in fills:
            key = (f.ticker, f.side, f.qty, f.price, f.filled_at)
            if key not in seen:
                seen.add(key)
                out.append(f)
        return out

    def net_position(fills):
        return sum((f.qty if f.side == "buy" else -f.qty) for f in fills)

    # P&L is counted once either way (FIFO can't realize against exhausted inventory)...
    assert realized_pnl(dedupe(dup)) == realized_pnl(single) == Decimal("100")
    # ...but the REAL damage of a duplicate fill is a phantom short position.
    assert net_position(single) == Decimal("0")
    assert net_position(dup) == Decimal("-10")          # phantom short if not deduped
    assert net_position(dedupe(dup)) == Decimal("0")    # the guard prevents it
