"""Kill switch — DB-backed so a restart returns tripped (A3).

Phase 7: keyed by asset class. Equity and crypto trip independently. Every method
defaults ``asset_class=EQUITY`` so all pre-Phase-7 call sites and tests are
unchanged. When the daily realized loss for a class breaches its limit, that
class's switch trips and blocks its new orders until a human resets it. Both trip
and reset write an audit row to ``risk_events``.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..assets import AssetClass
from ..db.models import KillSwitchState, RiskEvent, utcnow


def _ac(asset_class: AssetClass | str) -> str:
    return asset_class.value if isinstance(asset_class, AssetClass) else str(asset_class)


def _get_or_create_state(session: Session, asset_class: str) -> KillSwitchState:
    state = session.execute(
        select(KillSwitchState).where(KillSwitchState.asset_class == asset_class)
    ).scalar_one_or_none()
    if state is None:
        state = KillSwitchState(asset_class=asset_class, tripped=False, reason="")
        session.add(state)
        session.flush()
    return state


class KillSwitch:
    """All methods read/write the ``killswitch_state`` row for one asset class."""

    @staticmethod
    def is_tripped(
        session: Session, asset_class: AssetClass | str = AssetClass.EQUITY
    ) -> bool:
        state = session.execute(
            select(KillSwitchState).where(
                KillSwitchState.asset_class == _ac(asset_class)
            )
        ).scalar_one_or_none()
        return bool(state and state.tripped)

    @staticmethod
    def trip(
        session: Session,
        reason: str,
        asset_class: AssetClass | str = AssetClass.EQUITY,
    ) -> None:
        ac = _ac(asset_class)
        state = _get_or_create_state(session, ac)
        if state.tripped:
            return  # already tripped; keep the original reason/time
        state.tripped = True
        state.tripped_at = utcnow()
        state.reason = reason
        state.updated_at = utcnow()
        session.add(RiskEvent(event_type="killswitch_trip", reason=f"[{ac}] {reason}"))

    @staticmethod
    def reset(
        session: Session,
        note: str = "manual reset",
        asset_class: AssetClass | str = AssetClass.EQUITY,
    ) -> None:
        ac = _ac(asset_class)
        state = _get_or_create_state(session, ac)
        state.tripped = False
        state.tripped_at = None
        state.reason = ""
        state.updated_at = utcnow()
        session.add(RiskEvent(event_type="killswitch_reset", reason=f"[{ac}] {note}"))

    @staticmethod
    def evaluate_daily_loss(
        session: Session,
        realized_pnl_today: Decimal,
        loss_limit: Decimal,
        asset_class: AssetClass | str = AssetClass.EQUITY,
    ) -> bool:
        """Trip this class's switch if its realized loss meets/exceeds the limit."""
        if realized_pnl_today <= -abs(loss_limit):
            KillSwitch.trip(
                session,
                reason=(
                    f"daily realized loss {realized_pnl_today} breached limit "
                    f"-{abs(loss_limit)}"
                ),
                asset_class=asset_class,
            )
        return KillSwitch.is_tripped(session, asset_class)
