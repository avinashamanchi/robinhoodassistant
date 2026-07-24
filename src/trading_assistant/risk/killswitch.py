"""Compatibility facade mapping legacy kill switches to scoped breakers."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..assets import AssetClass
from ..db.models import CircuitBreakerState, RiskEvent
from .breakers import (
    BreakerScope,
    reset_in_session,
    trip_in_session,
)


def _scope(asset_class: AssetClass | str) -> BreakerScope:
    if isinstance(asset_class, AssetClass):
        return BreakerScope.loss(asset_class)
    if str(asset_class) == "operator_global":
        return BreakerScope.operator_global()
    return BreakerScope.loss(AssetClass(str(asset_class)))


class KillSwitch:
    """Legacy session-oriented API over loss and operator-global scopes."""

    @staticmethod
    def is_tripped(
        session: Session, asset_class: AssetClass | str = AssetClass.EQUITY
    ) -> bool:
        return bool(
            session.scalar(
                select(CircuitBreakerState.tripped).where(
                    CircuitBreakerState.scope_key == _scope(asset_class).key
                )
            )
        )

    @staticmethod
    def trip(
        session: Session,
        reason: str,
        asset_class: AssetClass | str = AssetClass.EQUITY,
    ) -> None:
        scope = _scope(asset_class)
        _state, changed = trip_in_session(
            session,
            scope,
            reason,
            "compat:killswitch",
        )
        if changed:
            session.add(
                RiskEvent(
                    event_type="killswitch_trip",
                    reason=f"[{scope.key}] {reason}",
                )
            )

    @staticmethod
    def reset(
        session: Session,
        note: str = "manual reset",
        asset_class: AssetClass | str = AssetClass.EQUITY,
    ) -> None:
        scope = _scope(asset_class)
        row = session.get(CircuitBreakerState, scope.key)
        if row is None:
            raise ValueError("cannot reset a breaker that has not been tripped")
        reset_in_session(
            session,
            scope,
            "compat:killswitch",
            note,
            {"compatibility_facade": True},
            expected_generation=row.generation,
        )
        session.add(
            RiskEvent(
                event_type="killswitch_reset",
                reason=f"[{scope.key}] {note}",
            )
        )

    @staticmethod
    def evaluate_daily_loss(
        session: Session,
        realized_pnl_today: Decimal,
        loss_limit: Decimal,
        asset_class: AssetClass | str = AssetClass.EQUITY,
    ) -> bool:
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
