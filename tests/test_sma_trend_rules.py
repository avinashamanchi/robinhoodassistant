"""sma_trend boundaries, and its documented lack of a stop-loss."""

from __future__ import annotations

from tests.test_strategies import _feat
from trading_assistant.strategies.base import SignalAction
from trading_assistant.strategies.sma_trend import SmaTrend


def test_boundaries_follow_the_written_rule():
    rule = SmaTrend()
    # 20-day equal to the 50-day is not a lead: flat.
    assert rule.on_bar(_feat(sma_20=100, sma_50=100, sma_200=90)).action is SignalAction.SELL
    # Close exactly at the 200-day is not below the trend: long.
    assert rule.on_bar(_feat(sma_20=105, sma_50=100, sma_200=100, last_close=100)).action is SignalAction.BUY
    assert rule.on_bar(_feat(sma_20=105, sma_50=100, sma_200=100, last_close=99.99)).action is SignalAction.SELL


def test_there_is_no_stop_loss_a_deep_drawdown_alone_never_exits():
    # Price has fallen far below a hypothetical entry, but the trend conditions
    # still hold, so the rule stays long. Exits come only from the trend rule.
    rule = SmaTrend()
    assert rule.on_bar(_feat(sma_20=150, sma_50=140, sma_200=100, last_close=101)).action is SignalAction.BUY
