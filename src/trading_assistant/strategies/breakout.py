"""Breakout — enter on a confirmed breakout, exit on an ATR chandelier stop.

The trailing stop is derived from features (recent high − k·ATR), so the strategy
stays stateless while still approximating an ATR-based trail. A BREAKDOWN event
also forces an exit.
"""

from __future__ import annotations

from ..signals.models import EventType, MarketFeatures
from .base import Signal, SignalAction, Strategy, hold

ATR_MULT = 3.0


class Breakout(Strategy):
    name = "breakout"

    def on_bar(self, features: MarketFeatures) -> Signal:
        types = {e.type for e in features.events}
        if EventType.BREAKOUT in types:
            return Signal(SignalAction.BUY, reason="confirmed breakout")
        if EventType.BREAKDOWN in types:
            return Signal(SignalAction.SELL, reason="breakdown")

        # Chandelier exit: flat out if price falls a multiple of ATR below the
        # recent swing high.
        if features.atr_14 is not None and features.recent_bars and features.last_close:
            recent_high = max(b.high for b in features.recent_bars)
            stop = recent_high - ATR_MULT * features.atr_14
            if features.last_close < stop:
                return Signal(SignalAction.SELL, reason="atr chandelier stop")
        return hold()
