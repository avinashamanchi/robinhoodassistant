"""Security response policy for success and failure paths."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.app_factory import create_app

TOKEN = "task-7-operator-secret"

EXPECTED_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)


def _assert_hardened(response, *, cache_control="no-store"):
    assert response.headers.get_list("Content-Security-Policy") == [
        EXPECTED_CSP
    ]
    assert response.headers.get_list("X-Content-Type-Options") == ["nosniff"]
    assert response.headers.get_list("X-Frame-Options") == ["DENY"]
    assert response.headers.get_list("Referrer-Policy") == ["no-referrer"]
    assert response.headers.get_list("Permissions-Policy") == [
        "camera=(), microphone=(), geolocation=(), payment=()"
    ]
    assert response.headers.get_list("X-Request-ID") == [
        response.json()["error"]["request_id"]
    ]
    assert response.headers.get_list("Cache-Control") == [cache_control]


class _StubAgent:
    def chat(self, message, **context):
        return {"reply": "ok", "tool_calls": []}


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


@pytest.mark.parametrize(
    ("path", "cache_control"),
    [
        ("/health/live", None),
        ("/positions", "no-store"),
        ("/does-not-exist", "no-store"),
    ],
)
def test_security_headers_cover_success_auth_failure_and_not_found(
    client, path, cache_control
):
    response = client.get(path)

    assert response.headers["Content-Security-Policy"] == EXPECTED_CSP
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert (
        response.headers["Permissions-Policy"]
        == "camera=(), microphone=(), geolocation=(), payment=()"
    )
    assert response.headers.get("Cache-Control") == cache_control
    assert response.headers["X-Request-ID"]


def test_provider_exception_text_is_not_returned(make_service):
    class ExplodingAgent:
        def chat(self, message, **context):
            raise RuntimeError("provider-secret-response")

    app = create_app(
        service=make_service(),
        agent=ExplodingAgent(),
        api_token=TOKEN,
        planning=None,
    )
    with TestClient(
        app,
        base_url="https://localhost:8020",
        raise_server_exceptions=False,
    ) as client:
        login = client.post("/auth/login", json={"secret": TOKEN})
        csrf = login.json()["csrf_token"]
        response = client.post("/chat", json={"message": "hi"}, headers={"X-CSRF-Token": csrf})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "provider-secret-response" not in response.text
    _assert_hardened(response)


def test_login_page_and_its_asset_are_anonymous(client):
    page = client.get("/login")
    script = client.get("/static/js/login.js")

    assert page.status_code == 200
    assert "/static/js/login.js" in page.text
    assert script.status_code == 200
    assert "localStorage" not in script.text


@pytest.mark.parametrize(
    ("page", "script"),
    [
        ("index.html", "index.js"),
        ("plans.html", "plans.js"),
        ("backtests.html", "backtests.js"),
    ],
)
def test_ui_removes_api_key_and_plaintext_browser_storage(page, script):
    path = Path("src/trading_assistant/app/static") / page
    text = path.read_text(encoding="utf-8")
    script_text = (
        Path("src/trading_assistant/app/static/js") / script
    ).read_text(encoding="utf-8")
    auth_text = Path(
        "src/trading_assistant/app/static/js/auth.js"
    ).read_text(encoding="utf-8")
    combined = text + script_text + auth_text

    assert "X-API-Key" not in combined
    assert "api_key" not in combined
    assert "localStorage" not in combined
    assert "/auth/session" in auth_text
    assert "X-CSRF-Token" in auth_text
    assert "/static/js/auth.js" in script_text
