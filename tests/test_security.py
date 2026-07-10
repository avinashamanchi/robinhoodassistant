"""Section A security: API-token auth + CORS (A1), no-innerHTML XSS guard (A2),
redaction (A3), daemon backoff + staleness (A4)."""

from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from random import Random

import pytest
from fastapi.testclient import TestClient

from trading_assistant.app.main import create_app
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import Quote

TOKEN = "s3cret-token"
_STATIC = pathlib.Path("src/trading_assistant/app/static")


class _StubAgent:
    def chat(self, message):
        return {"reply": "ok", "tool_calls": []}


@pytest.fixture
def client(make_service):
    app = create_app(service=make_service(), agent=_StubAgent(), api_token=TOKEN, planning=None)
    return TestClient(app)


# ── A1: auth on mutating endpoints ──────────────────────────────
def test_mutating_without_token_401(client):
    assert client.post("/approve/1").status_code == 401
    assert client.post("/killswitch/reset").status_code == 401
    assert client.post("/reconcile").status_code == 401
    assert client.post("/chat", json={"message": "hi"}).status_code == 401


def test_wrong_token_401(client):
    assert client.post("/killswitch/reset", headers={"X-API-Key": "nope"}).status_code == 401


def test_correct_token_allows(client):
    r = client.post("/killswitch/reset", headers={"X-API-Key": TOKEN})
    assert r.status_code == 200 and r.json()["tripped"] is False


def test_get_endpoints_stay_open(client):
    assert client.get("/pending").status_code == 200
    assert client.get("/positions").status_code == 200
    assert client.get("/log").status_code == 200


def test_cors_preflight_blocks_cross_origin(client):
    hdr = {"Origin": "http://evil.example", "Access-Control-Request-Method": "POST",
           "Access-Control-Request-Headers": "x-api-key"}
    r = client.options("/approve/1", headers=hdr)
    assert r.headers.get("access-control-allow-origin") != "http://evil.example"
    ok = client.options("/approve/1", headers={**hdr, "Origin": "http://127.0.0.1:8000"})
    assert ok.headers.get("access-control-allow-origin") == "http://127.0.0.1:8000"


# ── A2: no dynamic innerHTML in the UIs ─────────────────────────
@pytest.mark.parametrize("page", ["index.html", "backtests.html", "plans.html"])
def test_ui_has_no_innerhtml(page):
    text = (_STATIC / page).read_text()
    assert "innerHTML" not in text  # all dynamic values go through textContent


# ── A3: redaction of new secrets ────────────────────────────────
def test_new_secrets_redacted():
    from trading_assistant.config import Secrets
    from trading_assistant.logging import redact, register_all_secrets

    sec = Secrets(app_api_token="APPTOK123", gemini_api_key="GEMKEY456",
                  groq_api_key="GROQKEY789", openrouter_api_key="ORKEY000")
    register_all_secrets(sec)
    out = redact("app=APPTOK123 gem=GEMKEY456 groq=GROQKEY789 or=ORKEY000")
    for leaked in ("APPTOK123", "GEMKEY456", "GROQKEY789", "ORKEY000"):
        assert leaked not in out


# ── A4: backoff + staleness gate ────────────────────────────────
def test_backoff_grows_and_caps():
    from trading_assistant.daemon.backoff import next_delay

    assert next_delay(1, jitter_frac=0) == 1.0
    assert next_delay(3, jitter_frac=0) == 4.0
    assert next_delay(20, jitter_frac=0) == 60.0        # capped
    # jitter stays within bounds and non-negative
    d = next_delay(2, rng=Random(0))
    assert 0.0 <= d <= 60.0


class _StaleBroker(MockBroker):
    def get_quote(self, ticker: str) -> Quote:
        q = super().get_quote(ticker)
        old = datetime.now(timezone.utc) - timedelta(seconds=600)
        return Quote(q.ticker, q.bid, q.ask, q.last, q.prev_close, as_of=old)


def test_stale_quote_does_not_fire(make_service):
    import json

    from trading_assistant.daemon.monitor import Monitor
    from trading_assistant.db.models import Rule

    broker = _StaleBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    with svc.session_factory() as s:
        s.add(Rule(ticker="AAPL", kind="price", state="active",
                   condition_json=json.dumps({"price_below": 175}),
                   action_json=json.dumps({"side": "buy", "notional": "100"})))
        s.commit()
    # Price 100 < 175 would fire, but the quote is 600s stale -> skipped.
    assert Monitor(svc, max_quote_age_seconds=60).tick() == []
