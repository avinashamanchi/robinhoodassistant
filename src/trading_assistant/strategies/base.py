"""Strategy interface + Signal type."""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass
from typing import Optional

from ..signals.models import MarketFeatures


class SignalAction(str, enum.Enum):
    BUY = "buy"     # desired state: long
    SELL = "sell"   # desired state: flat (exit long)
    HOLD = "hold"   # no change


@dataclass(frozen=True)
class Signal:
    action: SignalAction
    size_hint: Optional[float] = None   # fraction of allowed size, 0-1; None = default
    reason: str = ""


class Strategy(abc.ABC):
    @property
    @abc.abstractmethod
    def name(self) -> str: ...

    @abc.abstractmethod
    def on_bar(self, features: MarketFeatures) -> Signal: ...


def hold(reason: str = "") -> Signal:
    return Signal(SignalAction.HOLD, reason=reason)
