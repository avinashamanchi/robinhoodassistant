"""Buy-and-hold — the benchmark every other approach must beat."""

from __future__ import annotations

from ..signals.models import MarketFeatures
from .base import Signal, SignalAction, Strategy, hold


class BuyAndHold(Strategy):
    name = "buy_and_hold"

    def __init__(self) -> None:
        self._entered = False

    def on_bar(self, features: MarketFeatures) -> Signal:
        if not self._entered and features.last_close is not None:
            self._entered = True
            return Signal(SignalAction.BUY, size_hint=1.0, reason="initial entry")
        return hold()
