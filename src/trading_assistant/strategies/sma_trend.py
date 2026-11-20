"""SMA trend — long while the 20-day leads the 50-day above the 200-day trend.

This is the rule the autopilot has traded since it shipped (previously under the
``sma_crossover`` name, which the backtester uses for a different 50/200 rule).
Keeping it here means the backtest harness and the autopilot evaluate the exact
same ``on_bar`` code, so a backtest of ``sma_trend`` is evidence about what the
autopilot actually does.

Missing 20/50-day averages return HOLD (no change) rather than SELL: an
incomplete feature bundle must never liquidate a position. A missing 200-day
average or last close only skips the long-term trend filter, matching the
original autopilot rule.
"""

from __future__ import annotations

from ..signals.models import MarketFeatures
from .base import Signal, SignalAction, Strategy, hold


class SmaTrend(Strategy):
    name = "sma_trend"

    def on_bar(self, features: MarketFeatures) -> Signal:
        if features.sma_20 is None or features.sma_50 is None:
            return hold("insufficient history")
        if features.sma_20 <= features.sma_50:
            return Signal(SignalAction.SELL, reason="20<=50 (no short-term lead)")
        if (
            features.sma_200 is not None
            and features.last_close is not None
            and features.last_close < features.sma_200
        ):
            return Signal(SignalAction.SELL, reason="close<200 (below trend)")
        return Signal(SignalAction.BUY, reason="20>50 and close>=200 (uptrend)")
