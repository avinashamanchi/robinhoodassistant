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
from tests.app_factory import create_app
from trading_assistant.assets import AssetClass
from trading_assistant.db.models import AuthSession
from trading_assistant.risk.breakers import BreakerScope

TOKEN = "task-7-operator-secret"


class _StubAgent:
    def chat(self, message, **context):
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
    return SessionAuth(
        session_factory,
        application_secret=TOKEN,
        now=fake_now,
    )


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
        "/backtests/v1",
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
    assert "Secure" in cookie
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
        assert row.token_hash != hashlib.sha256(
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


def test_session_endpoint_is_non_mutating_and_multi_tab_safe(client):
    login = client.post("/auth/login", json={"secret": TOKEN})
    original_csrf = login.json()["csrf_token"]
    session_factory = client.app.state.session_auth.session_factory
    with session_factory() as db:
        original_hash = db.query(AuthSession).one().csrf_hash

    first_tab = client.get("/auth/session")
    second_tab = client.get("/auth/session")
    first_csrf = first_tab.json()["csrf_token"]
    second_csrf = second_tab.json()["csrf_token"]

    assert first_csrf == original_csrf
    assert second_csrf == original_csrf
    with session_factory() as db:
        assert db.query(AuthSession).one().csrf_hash == original_hash
    assert (
        client.post(
            "/chat",
            json={"message": "first tab"},
            headers={"X-CSRF-Token": first_csrf},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/chat",
            json={"message": "second tab"},
            headers={"X-CSRF-Token": second_csrf},
        ).status_code
        == 200
    )


def test_session_and_csrf_share_versioned_secret_lifecycle_across_restarts(
    make_service,
):
    service = make_service()
    first_app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    first = TestClient(first_app)
    login = first.post("/auth/login", json={"secret": TOKEN})
    cookie_name = first.app.state.session_auth.cookie_name()
    token = first.cookies.get(cookie_name)
    csrf = login.json()["csrf_token"]

    same_key_app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    same_key = TestClient(same_key_app)
    same_key.cookies.set(cookie_name, token)

    assert same_key.get("/positions").status_code == 200
    assert same_key.get("/auth/session").json()["csrf_token"] == csrf
    assert (
        same_key.post(
            "/chat",
            json={"message": "same-key restart"},
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 200
    )

    rotated_app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="rotated-task-7-operator-secret",
        planning=None,
    )
    rotated = TestClient(rotated_app)
    rotated.cookies.set(cookie_name, token)

    for response in (
        rotated.get("/positions"),
        rotated.get("/auth/session"),
        rotated.post(
            "/chat",
            json={"message": "rotated key"},
            headers={"X-CSRF-Token": csrf},
        ),
    ):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_session"


def test_tls_cookie_uses_host_prefix_and_secure_flag(make_service):
    service = make_service()
    service.config = service.config.model_copy(
        update={
            "server": service.config.server.model_copy(
                update={"secure_cookies": True}
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
    client = TestClient(app, base_url="https://localhost:8020")

    response = client.post("/auth/login", json={"secret": TOKEN})

    cookie = response.headers["set-cookie"]
    assert "__Host-trading_session=" in cookie
    assert "Secure" in cookie
    assert "Path=/" in cookie


def test_insecure_cookie_rejects_non_loopback_bind(make_service):
    service = make_service()
    service.config = service.config.model_copy(
        update={
            "server": service.config.server.model_copy(
                update={"secure_cookies": False}
            )
        }
    )
    with pytest.raises(RuntimeError, match="secure_cookies"):
        create_app(
            service=service,
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
        request_id="auth-killswitch-drill",
    )
    body = {
        "scope": "loss:equity",
        "reason": "drill complete",
        "expected_generation": observed.generation,
    }

    missing = client.post("/killswitch/reset", json=body)
    accepted = client.post(
        "/killswitch/reset",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "auth-killswitch-reset",
        },
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
        ("/analyze", {"symbol": "AAPL", "reason": "review analysis"}),
        ("/plans/999/cancel", None),
        ("/screen", None),
        ("/propose", {"n": 1, "reason": "review candidates"}),
        (
            "/backtests/run",
            {"symbols": [], "reason": "review strategies"},
        ),
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
                "scope": "loss:equity",
                "reason": "reviewed",
                "expected_generation": 1,
            },
        ),
        (
            "/plans/999/approve",
            {
                "reason": "reviewed",
                "review_token": "plan:999:authority:v1:test-digest",
            },
        ),
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
                headers={
                    "X-CSRF-Token": csrf,
                    "Idempotency-Key": "auth-reconcile-after-reauth",
                },
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


def test_login_source_rate_limit_survives_restart_before_secret_comparison(
    make_service,
    with_limit,
):
    service = make_service()
    service.config = with_limit(
        service.config,
        "login",
        requests=1,
        window_seconds=60,
    )
    first_app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    first_client = TestClient(first_app)

    assert (
        first_client.post(
            "/auth/login",
            json={"secret": "wrong"},
        ).status_code
        == 401
    )
    restarted_app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    login_calls = 0

    def forbidden_login(*_args, **_kwargs):
        nonlocal login_calls
        login_calls += 1
        raise AssertionError("denied login reached secret comparison")

    restarted_app.state.session_auth.login = forbidden_login
    limited = TestClient(restarted_app).post(
        "/auth/login",
        json={"secret": TOKEN},
    )

    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limit_exceeded"
    assert login_calls == 0


def test_authenticated_rate_limits_are_keyed_by_session(
    make_service,
    authenticate_client,
    with_limit,
):
    service = make_service()
    service.config = with_limit(
        service.config,
        "chat",
        requests=1,
        window_seconds=60,
    )
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token=TOKEN,
        planning=None,
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
