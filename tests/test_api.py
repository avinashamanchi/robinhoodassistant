"""FastAPI endpoints: pending/approve/reject/positions/log/killswitch, chat, rate limit."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from trading_assistant.app.main import create_app
from trading_assistant.app.ratelimit import RateLimiter


class StubAgent:
    def __init__(self):
        self.calls = 0

    def chat(self, message: str):
        self.calls += 1
        return {"reply": f"echo: {message}", "tool_calls": []}


@pytest.fixture
def client(make_service):
    svc = make_service()
    agent = StubAgent()
    app = create_app(
        service=svc,
        agent=agent,
        api_token="",  # auth tested separately in test_security.py
        chat_rate=RateLimiter(max_requests=2, window_seconds=60),
        approve_rate=RateLimiter(max_requests=100, window_seconds=60),
    )
    return TestClient(app), svc, agent


def _propose(svc, notional="100"):
    return svc.propose_order("AAPL", "buy", "market", notional=notional)["order_id"]


def test_index_served(client):
    c, _, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert "Trading Assistant" in r.text


def test_pending_approve_flow(client):
    c, svc, _ = client
    order_id = _propose(svc)

    pending = c.get("/pending").json()["pending"]
    assert len(pending) == 1 and pending[0]["order_id"] == order_id

    approve = c.post(f"/approve/{order_id}").json()
    assert approve["executed"] is True
    assert svc.broker.submit_calls == 1

    # No longer pending.
    assert c.get("/pending").json()["pending"] == []


def test_double_approve_returns_409(client):
    c, svc, _ = client
    order_id = _propose(svc)
    assert c.post(f"/approve/{order_id}").status_code == 200
    assert c.post(f"/approve/{order_id}").status_code == 409


def test_reject_endpoint(client):
    c, svc, _ = client
    order_id = _propose(svc)
    r = c.post(f"/reject/{order_id}").json()
    assert r["status"] == "rejected"


def test_positions_and_log(client):
    c, svc, _ = client
    _propose(svc, notional="600")  # rejected -> creates a risk_event
    assert "positions" in c.get("/positions").json()
    log = c.get("/log").json()
    assert len(log["risk_events"]) >= 1


def test_killswitch_reset_endpoint(client):
    c, svc, _ = client
    with svc.session_factory() as s:
        from trading_assistant.risk.killswitch import KillSwitch

        KillSwitch.trip(s, reason="drill")
        s.commit()
    r = c.post("/killswitch/reset").json()
    assert r["tripped"] is False


def test_chat_and_rate_limit(client):
    c, svc, agent = client
    assert c.post("/chat", json={"message": "hi"}).json()["reply"] == "echo: hi"
    c.post("/chat", json={"message": "again"})       # 2nd allowed (limit=2)
    r = c.post("/chat", json={"message": "third"})   # 3rd blocked
    assert r.status_code == 429
