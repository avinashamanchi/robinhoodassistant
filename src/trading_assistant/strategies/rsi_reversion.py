"""RSI mean-reversion — buy oversold in a non-downtrend, exit on recovery.

Encodes the playbook caveat directly: RSI-oversold in a TRENDING_DOWN regime is a
falling knife, so entries are suppressed there.
"""

from __future__ import annotations

from ..signals.models import MarketFeatures, Regime
from .base import Signal, SignalAction, Strategy, hold

OVERSOLD = 30.0
EXIT = 55.0


class RsiReversion(Strategy):
    name = "rsi_reversion"

    def on_bar(self, features: MarketFeatures) -> Signal:
        rsi = features.rsi_14
        if rsi is None:
            return hold("no rsi")
        if rsi < OVERSOLD and features.regime is not Regime.TRENDING_DOWN:
            return Signal(SignalAction.BUY, reason=f"rsi {rsi:.1f} oversold, non-downtrend")
        if rsi > EXIT:
            return Signal(SignalAction.SELL, reason=f"rsi {rsi:.1f} recovered")
        return hold()
