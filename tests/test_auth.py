"""Fail-closed operator session behavior."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from trading_assistant.app.auth import (
    CsrfRejected,
    InvalidSession,
    SessionAuth,
    SessionExpired,
)
from trading_assistant.app.main import create_app
from trading_assistant.app.ratelimit import RateLimiter
from trading_assistant.assets import AssetClass
from trading_assistant.db.models import AuthSession
from trading_assistant.risk.breakers import BreakerScope

TOKEN = "task-7-operator-secret"


class _StubAgent:
    def chat(self, message):
        return {"reply": "ok", "tool_calls": []}


class _FakeNow:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, **delta):
        self.value += timedelta(**delta)


@pytest.fixture
def fake_now():
    return _FakeNow()


@pytest.fixture
def session_auth(session_factory, fake_now):
    return SessionAuth(session_factory, now=fake_now)


@pytest.fixture
def client(make_service):
    return TestClient(
        create_app(
            service=make_service(),
            agent=_StubAgent(),
            api_token=TOKEN,
            planning=None,
        )
    )


@pytest.mark.parametrize("missing_secret", ["", "   "])
def test_missing_operator_secret_fails_startup(
    make_service, missing_secret
):
    with pytest.raises(RuntimeError, match="APP_API_TOKEN"):
        create_app(
            service=make_service(),
            agent=_StubAgent(),
            api_token=missing_secret,
        )


def test_all_non_liveness_routes_require_session(client):
    assert client.get("/health/live").status_code == 200
    for path in [
        "/",
        "/health",
        "/pending",
        "/positions",
        "/log",
        "/plans",
        "/plans/ui",
        "/backtests",
        "/backtests/ui",
    ]:
        assert client.get(path).status_code == 401, path


def test_login_sets_http_only_same_site_cookie(client):
    response = client.post("/auth/login", json={"secret": TOKEN})

    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "trading_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Max-Age=" in cookie
    assert "Secure" not in cookie
    assert response.json()["csrf_token"]


def test_x_api_key_never_bypasses_session(client):
    response = client.get("/positions", headers={"X-API-Key": TOKEN})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_session"


def test_login_error_is_stable_and_has_request_id(client):
    response = client.post("/auth/login", json={"secret": "wrong"})

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "invalid_credentials",
            "message": "Invalid operator credentials",
            "request_id": response.headers["X-Request-ID"],
        }
    }


def test_session_tokens_and_csrf_are_only_persisted_as_hashes(
    session_auth, session_factory
):
    issued = session_auth.login(TOKEN, TOKEN)

    with session_factory() as session:
        row = session.query(AuthSession).one()
        assert row.token_hash == hashlib.sha256(
            issued.token.encode("utf-8")
        ).hexdigest()
        assert row.csrf_hash == hashlib.sha256(
            issued.csrf.encode("utf-8")
        ).hexdigest()
        assert issued.token not in {
            row.token_hash,
            row.csrf_hash,
            row.actor,
        }
        assert issued.csrf not in {
            row.token_hash,
            row.csrf_hash,
            row.actor,
        }


def test_expired_or_revoked_session_is_rejected(session_auth, fake_now):
    issued = session_auth.login(TOKEN, TOKEN)
    fake_now.advance(hours=9)
    with pytest.raises(SessionExpired):
        session_auth.authenticate(issued.token)

    issued = session_auth.login(TOKEN, TOKEN)
    session_auth.logout(issued.token)
    with pytest.raises(InvalidSession):
        session_auth.authenticate(issued.token)


def test_wrong_csrf_is_rejected(session_auth):
    issued = session_auth.login(TOKEN, TOKEN)

    with pytest.raises(CsrfRejected):
        session_auth.require_csrf(issued.token, "wrong")
    principal = session_auth.require_csrf(issued.token, issued.csrf)
    assert principal.actor == "operator:local"


def test_session_endpoint_rotates_csrf_for_in_memory_clients(client):
    login = client.post("/auth/login", json={"secret": TOKEN})
    original_csrf = login.json()["csrf_token"]

    session = client.get("/auth/session")
    refreshed_csrf = session.json()["csrf_token"]

    assert refreshed_csrf
    assert refreshed_csrf != original_csrf
    assert (
        client.post(
            "/chat",
            json={"message": "old token"},
            headers={"X-CSRF-Token": original_csrf},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/chat",
            json={"message": "new token"},
            headers={"X-CSRF-Token": refreshed_csrf},
        ).status_code
        == 200
    )


def test_tls_cookie_uses_host_prefix_and_secure_flag(make_service):
    service = make_service()
    service.config = service.config.model_copy(
        update={
            "security": service.config.security.model_copy(
                update={"cookie_secure": True}
            )
        }
    )
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token=TOKEN,
        planning=None,
        bind_host="trading.internal",
    )
    client = TestClient(app, base_url="https://trading.internal")

    response = client.post("/auth/login", json={"secret": TOKEN})

    cookie = response.headers["set-cookie"]
    assert "__Host-trading_session=" in cookie
    assert "Secure" in cookie
    assert "Path=/" in cookie


def test_insecure_cookie_rejects_non_loopback_bind(make_service):
    with pytest.raises(RuntimeError, match="cookie_secure"):
        create_app(
            service=make_service(),
            agent=_StubAgent(),
            api_token=TOKEN,
            planning=None,
            bind_host="0.0.0.0",
        )


def test_mutation_requires_csrf(authenticated_client):
    client, csrf = authenticated_client
    service = client.trading_service
    observed = service.breakers.trip(
        BreakerScope.loss(AssetClass.EQUITY),
        reason="drill",
        actor="daemon:test",
    )
    body = {
        "asset_class": "equity",
        "reason": "drill complete",
        "expected_generation": observed.generation,
    }

    missing = client.post("/killswitch/reset", json=body)
    accepted = client.post(
        "/killswitch/reset",
        headers={"X-CSRF-Token": csrf},
        json=body,
    )

    assert missing.status_code == 403
    assert missing.json()["error"]["code"] == "csrf_required"
    assert accepted.status_code == 200


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/chat", {"message": "status"}),
        ("/reject/999", None),
        ("/orders/999/cancel", None),
        ("/sync", None),
        ("/analyze", {"symbol": "AAPL"}),
        ("/plans/999/cancel", None),
        ("/screen", None),
        ("/propose", {"n": 1}),
        ("/backtests/run", {"symbols": []}),
    ],
)
def test_each_non_login_mutation_rejects_missing_csrf(
    authenticated_client, path, body
):
    client, _ = authenticated_client

    response = client.post(path, json=body)

    assert response.status_code == 403, path
    assert response.json()["error"]["code"] == "csrf_required"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/approve/999", {"reason": "reviewed"}),
        (
            "/killswitch/reset",
            {
                "asset_class": "equity",
                "reason": "reviewed",
                "expected_generation": 1,
            },
        ),
        ("/plans/999/approve", {"reason": "reviewed"}),
        ("/panic", {"reason": "manual drill"}),
        ("/reconcile", {"reason": "reviewed positions"}),
    ],
)
def test_high_consequence_routes_require_recent_reauthentication(
    make_service, fake_now, authenticate_client, path, body
):
    app = create_app(
        service=make_service(),
        agent=_StubAgent(),
        api_token=TOKEN,
        planning=None,
        auth_now=fake_now,
    )
    client, csrf = authenticate_client(TestClient(app), TOKEN)
    fake_now.advance(minutes=6)

    response = client.post(
        path,
        json=body,
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 403, path
    assert (
        response.json()["error"]["code"]
        == "recent_authentication_required"
    )


def test_reauthentication_restores_high_consequence_access(
    make_service, fake_now, authenticate_client
):
    app = create_app(
        service=make_service(),
        agent=_StubAgent(),
        api_token=TOKEN,
        planning=None,
        auth_now=fake_now,
    )
    client, csrf = authenticate_client(TestClient(app), TOKEN)
    fake_now.advance(minutes=6)
    assert (
        client.post(
            "/reconcile",
            json={"reason": "reviewed positions"},
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 403
    )

    reauth = client.post(
        "/auth/reauth",
        json={"secret": TOKEN},
        headers={"X-CSRF-Token": csrf},
    )

    assert reauth.status_code == 200
    assert (
        client.post(
            "/reconcile",
            json={"reason": "reviewed positions"},
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 200
    )


def test_logout_requires_csrf_and_revokes_session(authenticated_client):
    client, csrf = authenticated_client

    assert client.post("/auth/logout").status_code == 403
    assert (
        client.post(
            "/auth/logout", headers={"X-CSRF-Token": csrf}
        ).status_code
        == 200
    )
    assert client.get("/positions").status_code == 401


def test_login_has_separate_tight_source_rate_limit(make_service):
    app = create_app(
        service=make_service(),
        agent=_StubAgent(),
        api_token=TOKEN,
        planning=None,
        login_rate=RateLimiter(max_requests=1, window_seconds=60),
    )
    client = TestClient(app)

    assert (
        client.post("/auth/login", json={"secret": "wrong"}).status_code
        == 401
    )
    limited = client.post("/auth/login", json={"secret": TOKEN})
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limit_exceeded"


def test_authenticated_rate_limits_are_keyed_by_session(
    make_service, authenticate_client
):
    app = create_app(
        service=make_service(),
        agent=_StubAgent(),
        api_token=TOKEN,
        planning=None,
        chat_rate=RateLimiter(max_requests=1, window_seconds=60),
    )
    first, first_csrf = authenticate_client(TestClient(app), TOKEN)
    second, second_csrf = authenticate_client(TestClient(app), TOKEN)

    assert (
        first.post(
            "/chat",
            json={"message": "one"},
            headers={"X-CSRF-Token": first_csrf},
        ).status_code
        == 200
    )
    assert (
        second.post(
            "/chat",
            json={"message": "two"},
            headers={"X-CSRF-Token": second_csrf},
        ).status_code
        == 200
    )
    limited = first.post(
        "/chat",
        json={"message": "three"},
        headers={"X-CSRF-Token": first_csrf},
    )
    assert limited.status_code == 429
