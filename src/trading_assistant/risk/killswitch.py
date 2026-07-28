"""Compatibility facade mapping legacy kill switches to scoped breakers."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..assets import AssetClass
from ..db.models import CircuitBreakerState, RiskEvent
from ..security.sensitive_fields import persist_sensitive
from .breakers import (
    BreakerScope,
    trip_in_session,
)
from .submission_barrier import SubmissionBarrier


def _scope(asset_class: AssetClass | str) -> BreakerScope:
    if isinstance(asset_class, AssetClass):
        return BreakerScope.loss(asset_class)
    if str(asset_class) == "operator_global":
        return BreakerScope.operator_global()
    return BreakerScope.loss(AssetClass(str(asset_class)))


def _require_barrier_before_transaction(session: Session) -> None:
    if session.in_transaction():
        raise RuntimeError(
            "compatibility breaker writes reject an active transaction; "
            "use a fresh session so the process barrier is acquired first"
        )


class KillSwitchResetUnavailable(RuntimeError):
    """Legacy reset is disabled because it cannot collect server health."""


class KillSwitch:
    """Legacy API over loss and operator-global scopes.

    Compatibility writes require a fresh session and own their commit so the
    process barrier is acquired before SQLite and remains held until the new
    breaker state is durable.
    """

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
        *,
        actor: str,
        request_id: str,
    ) -> None:
        scope = _scope(asset_class)
        _require_barrier_before_transaction(session)
        with SubmissionBarrier(session).hold_writer():
            try:
                _state, changed = trip_in_session(
                    session,
                    scope,
                    reason,
                    actor,
                    request_id=request_id,
                )
                if changed:
                    persist_sensitive(
                        session,
                        RiskEvent(
                            event_type="killswitch_trip",
                        ),
                        {"reason": f"[{scope.key}] {reason}"},
                    )
                session.commit()
            except BaseException:
                session.rollback()
                raise

    @staticmethod
    def reset(
        session: Session,
        note: str = "manual reset",
        asset_class: AssetClass | str = AssetClass.EQUITY,
        *,
        actor: str,
        request_id: str,
    ) -> None:
        raise KillSwitchResetUnavailable(
            "compatibility reset is unavailable; "
            "use TradingService.reset_killswitch"
        )

    @staticmethod
    def evaluate_daily_loss(
        session: Session,
        realized_pnl_today: Decimal,
        loss_limit: Decimal,
        asset_class: AssetClass | str = AssetClass.EQUITY,
        *,
        actor: str,
        request_id: str,
    ) -> bool:
        if realized_pnl_today <= -abs(loss_limit):
            KillSwitch.trip(
                session,
                reason=(
                    f"daily realized loss {realized_pnl_today} breached limit "
                    f"-{abs(loss_limit)}"
                ),
                asset_class=asset_class,
                actor=actor,
                request_id=request_id,
            )
        return KillSwitch.is_tripped(session, asset_class)
