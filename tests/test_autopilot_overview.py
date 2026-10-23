"""The console's read-only view of the autopilot: settings, readiness, sessions."""

from __future__ import annotations

from datetime import date

from trading_assistant.autopilot.evidence import EvidenceStore


def _record(service, session: date, result: str, decisions):
    store = EvidenceStore(service.session_factory, actor="autopilot:sma_trend")
    store.record_cycle(
        key=f"cycle-{session.isoformat()}",
        session=session,
        result_code=result,
        detail={
            "session": session.isoformat(),
            "execution": "paper",
            "strategy": "sma_trend",
            "decisions": decisions,
            "orders": [{"symbol": "AAPL", "side": "buy", "order_id": 1, "status": "submitted"}],
        },
    )


def test_overview_lists_settings_and_the_newest_session_first(make_service):
    service = make_service()
    _record(service, date(2026, 10, 20), "observed", [
        {"symbol": "AAPL", "signal": "long", "action": "none", "reason": "already_long"},
    ])
    _record(service, date(2026, 10, 21), "executed", [
        {"symbol": "AAPL", "signal": "long", "action": "buy", "reason": "enter_long"},
        {"symbol": "MSFT", "signal": "flat", "action": "none", "reason": "already_flat"},
    ])

    overview = service.autopilot_overview()

    assert overview["strategy"] == service.config.autopilot.strategy
    assert "stop-loss" in overview["exit_policy"]
    assert [s["session"] for s in overview["sessions"]] == ["2026-10-21", "2026-10-20"]
    newest = overview["sessions"][0]
    assert newest["result"] == "executed"
    assert newest["decisions"][0] == {"symbol": "AAPL", "signal": "long", "action": "buy", "reason": "enter_long"}
    assert newest["orders"][0]["status"] == "submitted"
    assert "readiness" in overview


def test_the_overview_route_needs_a_session(authenticated_client):
    from trading_assistant.app.policy import ROUTE_POLICIES, AuthLevel

    client, _csrf = authenticated_client
    response = client.get("/autopilot")
    assert response.status_code == 200
    assert response.json()["sessions"] == []
    (policy,) = [p for p in ROUTE_POLICIES if (p.method, p.path) == ("GET", "/autopilot")]
    assert policy.auth is AuthLevel.SESSION
