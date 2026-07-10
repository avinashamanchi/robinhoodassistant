"""SMA crossover — long while the 50-day is above the 200-day, else flat.

Equivalent to entering on a golden cross and exiting on a death cross, but stated
as a stateless level comparison so it is robust to missing a single crossover bar.
"""

from __future__ import annotations

from ..signals.models import MarketFeatures
from .base import Signal, SignalAction, Strategy, hold


class SmaCrossover(Strategy):
    name = "sma_crossover"

    def on_bar(self, features: MarketFeatures) -> Signal:
        if features.sma_50 is None or features.sma_200 is None:
            return hold("insufficient history")
        if features.sma_50 > features.sma_200:
            return Signal(SignalAction.BUY, reason="50>200 (uptrend)")
        return Signal(SignalAction.SELL, reason="50<=200 (downtrend)")
