from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from trading_assistant.assets import AssetClass
from trading_assistant.db.models import AuditEvent
from trading_assistant.db.session import create_db_engine, make_session_factory
from trading_assistant.risk.breakers import (
    BreakerKind,
    BreakerScope,
    BreakerService,
)


NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


def test_breaker_scopes_have_stable_targeted_keys():
    assert BreakerScope.data(AssetClass.EQUITY).key == "data:equity"
    assert BreakerScope.loss(AssetClass.CRYPTO).key == "loss:crypto"
    assert BreakerScope.drawdown(AssetClass.EQUITY).key == "drawdown:equity"
    assert BreakerScope.liquidity("aapl").key == "liquidity:AAPL"
    assert BreakerScope.broker_drift().key == "broker_drift"
    assert BreakerScope.operator_global().key == "operator_global"


def test_scoped_breakers_persist_and_reset_independently(session_factory):
    service = BreakerService(session_factory)
    data_scope = BreakerScope.data(AssetClass.EQUITY)
    drift_scope = BreakerScope.broker_drift()

    data_state = service.trip(data_scope, "feed disagreement", "daemon", now=NOW)
    drift_state = service.trip(drift_scope, "position mismatch", "daemon", now=NOW)
    reset_state = service.reset(
        data_scope,
        actor="operator:avi",
        reason="feed healthy",
        prior_health={"provider": "healthy", "age_seconds": 1},
        now=NOW,
    )

    assert data_state.scope == data_scope
    assert data_state.tripped is True
    assert drift_state.scope == drift_scope
    assert reset_state.tripped is False
    assert service.is_tripped(data_scope) is False
    assert service.is_tripped(drift_scope) is True


def test_breaker_trip_survives_restart(db_url, session_factory):
    scope = BreakerScope.loss(AssetClass.CRYPTO)
    BreakerService(session_factory).trip(scope, "daily loss", "daemon", now=NOW)

    restarted = BreakerService(
        make_session_factory(create_db_engine(db_url))
    )

    assert restarted.is_tripped(scope) is True


def test_active_for_symbol_returns_only_relevant_scopes(session_factory):
    service = BreakerService(session_factory)
    expected = (
        BreakerScope.operator_global(),
        BreakerScope.broker_drift(),
        BreakerScope.data(AssetClass.EQUITY),
        BreakerScope.loss(AssetClass.EQUITY),
        BreakerScope.drawdown(AssetClass.EQUITY),
        BreakerScope.liquidity("AAPL"),
    )
    unrelated = (
        BreakerScope.data(AssetClass.CRYPTO),
        BreakerScope.liquidity("MSFT"),
    )
    for scope in expected + unrelated:
        service.trip(scope, f"trip {scope.key}", "daemon", now=NOW)

    active = service.active_for_symbol("aapl")

    assert {state.scope for state in active} == set(expected)
    assert all(state.tripped for state in active)


@pytest.mark.parametrize(
    ("reason", "prior_health"),
    [
        ("", {"provider": "healthy"}),
        ("   ", {"provider": "healthy"}),
        ("feed healthy", {}),
    ],
)
def test_reset_requires_reason_and_prior_health(
    session_factory, reason, prior_health
):
    service = BreakerService(session_factory)
    scope = BreakerScope.data(AssetClass.EQUITY)
    service.trip(scope, "feed disagreement", "daemon", now=NOW)

    with pytest.raises(ValueError):
        service.reset(
            scope,
            actor="operator:avi",
            reason=reason,
            prior_health=prior_health,
            now=NOW,
        )

    assert service.is_tripped(scope) is True


def test_each_breaker_mutation_writes_an_audit_event(session_factory):
    service = BreakerService(session_factory)
    scope = BreakerScope(BreakerKind.LIQUIDITY, "AAPL")
    service.trip(scope, "spread too wide", "daemon", now=NOW)
    service.reset(
        scope,
        actor="operator:avi",
        reason="spread normalized",
        prior_health={"spread_pct": "0.2"},
        now=NOW,
    )

    with session_factory() as session:
        events = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.target_id == scope.key)
            .order_by(AuditEvent.id)
        ).all()

    assert [event.action for event in events] == [
        "circuit_breaker.trip",
        "circuit_breaker.reset",
    ]
    assert [event.actor for event in events] == ["daemon", "operator:avi"]
    assert events[1].reason == "spread normalized"
    assert '"spread_pct": "0.2"' in events[1].detail_json
