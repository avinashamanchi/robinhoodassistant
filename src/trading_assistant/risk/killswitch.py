"""Kill switch — DB-backed so a restart returns tripped (A3).

When the daily realized loss breaches the limit, the switch trips and blocks all
new orders until a human resets it. Both trip and reset write an audit row to
``risk_events``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import KillSwitchState, RiskEvent, utcnow


def _get_or_create_state(session: Session) -> KillSwitchState:
    state = session.get(KillSwitchState, 1)
    if state is None:
        state = KillSwitchState(id=1, tripped=False, reason="")
        session.add(state)
        session.flush()
    return state


class KillSwitch:
    """All methods read/write the singleton ``killswitch_state`` row."""

    @staticmethod
    def is_tripped(session: Session) -> bool:
        state = session.get(KillSwitchState, 1)
        return bool(state and state.tripped)

    @staticmethod
    def trip(session: Session, reason: str) -> None:
        state = _get_or_create_state(session)
        if state.tripped:
            return  # already tripped; keep the original reason/time
        state.tripped = True
        state.tripped_at = utcnow()
        state.reason = reason
        state.updated_at = utcnow()
        session.add(RiskEvent(event_type="killswitch_trip", reason=reason))

    @staticmethod
    def reset(session: Session, note: str = "manual reset") -> None:
        state = _get_or_create_state(session)
        state.tripped = False
        state.tripped_at = None
        state.reason = ""
        state.updated_at = utcnow()
        session.add(RiskEvent(event_type="killswitch_reset", reason=note))

    @staticmethod
    def evaluate_daily_loss(
        session: Session, realized_pnl_today: Decimal, loss_limit: Decimal
    ) -> bool:
        """Trip if today's realized loss meets/exceeds the limit. Returns tripped state.

        ``loss_limit`` is a positive USD amount; a realized P&L of -loss_limit or
        worse trips the switch.
        """
        if realized_pnl_today <= -abs(loss_limit):
            KillSwitch.trip(
                session,
                reason=(
                    f"daily realized loss {realized_pnl_today} breached limit "
                    f"-{abs(loss_limit)}"
                ),
            )
        return KillSwitch.is_tripped(session)
