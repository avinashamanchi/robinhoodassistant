"""Position exits and re-entry spacing shared by the backtester and the autopilot.

The trend rules (``sma_trend``) only say whether to be long or flat; they have
no notion of the price a position was bought at. This policy adds the two
things a rule cannot express statelessly, and applies them identically in
``backtest.engine.run_backtest`` and ``autopilot.engine.Autopilot`` so a
backtest of the configured policy is evidence about what the autopilot does:

* ``stop_loss_pct``: exit a held position once the last completed close is at
  or below the average entry price less this percentage. It is checked on
  completed daily bars only, like every other decision, so the exit fills at
  the next session's open (backtest) or at market after the cycle (paper): a
  gap down can fill well below the stop price. ``None`` disables it.
* ``reentry_cooldown_days``: after an exit, do not enter the same symbol again
  until this many calendar days have passed since the exit fill's session.
  It stops a rule that hovers around its threshold from buying and selling
  the same name session after session. ``0`` disables it.

Both inputs are derived from fills (entry price, exit date), never from
remembered state, so a restarted daemon decides exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class ExitPolicy:
    stop_loss_pct: Optional[float] = None
    reentry_cooldown_days: int = 0

    def __post_init__(self) -> None:
        if self.stop_loss_pct is not None and not 0 < self.stop_loss_pct < 100:
            raise ValueError("stop_loss_pct must be between 0 and 100")
        if self.reentry_cooldown_days < 0:
            raise ValueError("reentry_cooldown_days must not be negative")

    def stop_price(self, entry_price: Optional[float]) -> Optional[float]:
        if self.stop_loss_pct is None or not entry_price or entry_price <= 0:
            return None
        return entry_price * (1 - self.stop_loss_pct / 100)

    def stop_triggered(
        self, entry_price: Optional[float], last_close: Optional[float]
    ) -> bool:
        stop = self.stop_price(entry_price)
        return stop is not None and last_close is not None and last_close <= stop

    def cooling_down(self, last_exit: Optional[date], session: date) -> bool:
        if last_exit is None or self.reentry_cooldown_days <= 0:
            return False
        return (session - last_exit).days < self.reentry_cooldown_days

    def describe(self) -> str:
        stop = (
            f"stop-loss {self.stop_loss_pct:g}% below the average entry price, "
            "checked on completed daily closes"
            if self.stop_loss_pct is not None
            else "no stop-loss"
        )
        cooldown = (
            f"no re-entry for {self.reentry_cooldown_days} calendar days after an exit"
            if self.reentry_cooldown_days
            else "no re-entry cooldown"
        )
        return f"{stop}; {cooldown}"
