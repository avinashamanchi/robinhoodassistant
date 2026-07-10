"""Baseline strategies produce the expected position intent from features."""

from __future__ import annotations

from datetime import datetime, timezone

from trading_assistant.assets import AssetClass
from trading_assistant.signals.models import (
    Bar,
    EventTag,
    EventType,
    MarketFeatures,
    Regime,
)
from trading_assistant.strategies.base import SignalAction
from trading_assistant.strategies.breakout import Breakout
from trading_assistant.strategies.buy_and_hold import BuyAndHold
from trading_assistant.strategies.rsi_reversion import RsiReversion
from trading_assistant.strategies.sma_crossover import SmaCrossover

TS = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _feat(**kw) -> MarketFeatures:
    base = dict(symbol="AAPL", asset_class=AssetClass.EQUITY, as_of=TS, last_close=100.0)
    base.update(kw)
    return MarketFeatures(**base)


def test_buy_and_hold_enters_once():
    s = BuyAndHold()
    assert s.on_bar(_feat()).action is SignalAction.BUY
    assert s.on_bar(_feat()).action is SignalAction.HOLD


def test_sma_crossover():
    s = SmaCrossover()
    assert s.on_bar(_feat(sma_50=110, sma_200=100)).action is SignalAction.BUY
    assert s.on_bar(_feat(sma_50=90, sma_200=100)).action is SignalAction.SELL
    assert s.on_bar(_feat()).action is SignalAction.HOLD  # no SMAs yet


def test_rsi_reversion():
    s = RsiReversion()
    assert s.on_bar(_feat(rsi_14=25, regime=Regime.RANGING)).action is SignalAction.BUY
    # Oversold but in a downtrend -> do NOT catch the falling knife.
    assert s.on_bar(_feat(rsi_14=25, regime=Regime.TRENDING_DOWN)).action is SignalAction.HOLD
    assert s.on_bar(_feat(rsi_14=60)).action is SignalAction.SELL


def test_breakout_events():
    s = Breakout()
    up = _feat(events=[EventTag(type=EventType.BREAKOUT, ts=TS)])
    assert s.on_bar(up).action is SignalAction.BUY
    down = _feat(events=[EventTag(type=EventType.BREAKDOWN, ts=TS)])
    assert s.on_bar(down).action is SignalAction.SELL


def test_breakout_chandelier_stop():
    s = Breakout()
    bars = [Bar(ts=TS, open=100, high=120, low=99, close=100, volume=1e6)]
    # recent high 120, ATR 5 -> stop 105; last_close 100 < 105 -> exit.
    f = _feat(last_close=100.0, atr_14=5.0, recent_bars=bars)
    assert s.on_bar(f).action is SignalAction.SELL
