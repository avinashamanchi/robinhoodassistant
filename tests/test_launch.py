"""Launch features: health/heartbeat (D3), preflight helpers (B3), and a
full order lifecycle integration (B2)."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from trading_assistant.app.main import create_app
from trading_assistant.config import Secrets
from trading_assistant.db.models import Fill, Order


class _StubAgent:
    def chat(self, message):
        return {"reply": "ok", "tool_calls": []}


# ── D3 health + heartbeat ───────────────────────────────────────
def test_health_reflects_heartbeat(make_service):
    svc = make_service()
    assert svc.health()["daemon_alive"] is False        # no heartbeat yet
    svc.write_heartbeat("daemon")
    h = svc.health()
    assert h["db_ok"] is True and h["daemon_alive"] is True
    assert h["heartbeat_age_seconds"] < 5


def test_health_endpoint_no_auth(make_service):
    app = create_app(service=make_service(), agent=_StubAgent(), api_token="tok", planning=None)
    r = TestClient(app).get("/health")           # no X-API-Key required
    assert r.status_code == 200 and r.json()["db_ok"] is True


# ── B3 preflight helpers (keyless) ──────────────────────────────
def test_preflight_config_and_live_checks(app_config):
    from trading_assistant import preflight

    assert preflight._config_parses().status == "PASS"
    assert preflight._live_off(app_config, Secrets()).status == "PASS"


def test_preflight_env_flags_missing_token():
    from trading_assistant import preflight

    r = preflight._env_present(Secrets(app_api_token="short"))
    assert r.status == "FAIL" and "APP_API_TOKEN" in r.detail


# ── B2 full order lifecycle ─────────────────────────────────────
def test_order_lifecycle_propose_approve_fill(make_service):
    svc = make_service()  # AAPL @ 100
    oid = svc.propose_order("AAPL", "buy", "market", notional="400")["order_id"]
    assert svc.get_order_status(oid)["status"] == "proposed"

    approve = svc.approve_order(oid)
    assert approve["executed"] is True and approve["status"] == "submitted"

    filled = svc.record_fill(oid, qty="4", price="100")
    assert filled["status"] == "filled"

    with svc.session_factory() as s:
        assert s.execute(select(func.count()).select_from(Fill)).scalar_one() == 1
        assert s.get(Order, oid).status == "filled"
    # The execution shows up in the log feed the UI reads.
    assert "risk_events" in svc.get_log()
