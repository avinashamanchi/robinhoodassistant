"""Section A security: operator sessions + CORS (A1), no-innerHTML XSS guard (A2),
redaction (A3), daemon backoff + staleness (A4)."""

from __future__ import annotations

from html.parser import HTMLParser
import pathlib
import subprocess
import textwrap
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from random import Random

import pytest
from fastapi.testclient import TestClient

from trading_assistant.app.main import create_app
from trading_assistant.app.ratelimit import RateLimiter
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import Quote

TOKEN = "s3cret-token"
_STATIC = pathlib.Path("src/trading_assistant/app/static")
_PAGES = ("index.html", "plans.html", "backtests.html", "login.html")
_SCRIPTS = (
    "js/auth.js",
    "js/login.js",
    "js/index.js",
    "js/plans.js",
    "js/backtests.js",
)


class _StubAgent:
    def chat(self, message, **context):
        return {"reply": "ok", "tool_calls": []}


class _CspParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.errors: list[str] = []
        self.scripts: list[dict[str, str | None]] = []
        self.stylesheets: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script":
            self.scripts.append(attributes)
            if not attributes.get("src"):
                self.errors.append("inline script")
        if tag == "style":
            self.errors.append("inline style block")
        if tag == "link" and attributes.get("rel") == "stylesheet":
            self.stylesheets.append(attributes.get("href", ""))
        for name, _value in attrs:
            if name.lower().startswith("on"):
                self.errors.append(f"inline handler {name}")
            if name.lower() == "style":
                self.errors.append("inline style attribute")


def _run_module(path: pathlib.Path, scenario: str) -> None:
    loader = """
        import fs from "node:fs";
        const source = fs.readFileSync(process.argv[1], "utf8");
        const encoded = Buffer.from(source).toString("base64");
        const module = await import(`data:text/javascript;base64,${encoded}`);
    """
    completed = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            textwrap.dedent(loader + scenario),
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.fixture
def client(make_service):
    service = make_service()
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    test_client = TestClient(app)
    test_client.trading_service = service
    return test_client


# ── A1: fail-closed sessions + CSRF ─────────────────────────────
def test_mutating_without_session_401(client):
    assert client.post("/approve/1", json={"reason": "reviewed"}).status_code == 401
    assert client.post("/killswitch/reset", json={}).status_code == 401
    assert client.post(
        "/reconcile", json={"reason": "reviewed positions"}
    ).status_code == 401
    assert client.post("/chat", json={"message": "hi"}).status_code == 401


def test_x_api_key_is_ignored(client):
    response = client.get("/positions", headers={"X-API-Key": TOKEN})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_session"


def test_authenticated_session_and_csrf_allow_mutation(
    client, authenticate_client
):
    from trading_assistant.assets import AssetClass
    from trading_assistant.risk.breakers import BreakerScope

    observed = client.trading_service.breakers.trip(
        BreakerScope.loss(AssetClass.EQUITY),
        "security drill",
        "daemon",
        request_id="security-breaker-drill",
    )
    client, csrf = authenticate_client(client, TOKEN)
    r = client.post(
        "/killswitch/reset",
        headers={"X-CSRF-Token": csrf},
        json={
            "asset_class": "equity",
            "reason": "authenticated health review",
            "expected_generation": observed.generation,
        },
    )
    assert r.status_code == 200 and r.json()["tripped"] is False


def test_paid_analysis_and_backtest_endpoints_are_rate_limited(
    make_service, authenticate_client
):
    class StubPlanning:
        def analyze(self, symbol):
            return {"symbol": symbol}

    blocked = RateLimiter(max_requests=0, window_seconds=60)
    app = create_app(
        service=make_service(),
        agent=_StubAgent(),
        api_token=TOKEN,
        planning=StubPlanning(),
        analysis_rate=blocked,
        backtest_rate=blocked,
    )
    limited, csrf = authenticate_client(TestClient(app), TOKEN)
    headers = {"X-CSRF-Token": csrf}

    assert limited.post(
        "/analyze",
        json={"symbol": "AAPL", "reason": "rate limit test"},
        headers=headers,
    ).status_code == 429
    assert limited.post(
        "/propose",
        json={"n": 1, "reason": "rate limit test"},
        headers=headers,
    ).status_code == 429
    assert limited.post(
        "/backtests/run",
        json={"symbols": [], "reason": "rate limit test"},
        headers=headers,
    ).status_code == 429


def test_financial_get_endpoints_fail_closed(client):
    assert client.get("/pending").status_code == 401
    assert client.get("/positions").status_code == 401
    assert client.get("/log").status_code == 401


def test_allowed_cors_preflight_has_security_headers_and_request_id(client):
    response = client.options(
        "/approve/1",
        headers={
            "Origin": "http://127.0.0.1:8000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-csrf-token",
        },
    )

    assert response.status_code == 200
    assert (
        response.headers["access-control-allow-origin"]
        == "http://127.0.0.1:8000"
    )
    assert (
        response.headers["access-control-allow-methods"]
        == "GET, POST, OPTIONS"
    )
    assert {
        value.strip().lower()
        for value in response.headers[
            "access-control-allow-headers"
        ].split(",")
    } == {
        "accept",
        "accept-language",
        "content-language",
        "content-type",
        "x-csrf-token",
    }
    assert response.headers.get("access-control-allow-credentials") is None
    assert response.headers["Content-Security-Policy"]
    assert response.headers["X-Request-ID"]
    assert response.headers["Cache-Control"] == "no-store"


def test_rejected_cors_preflight_has_stable_hardened_error(client):
    response = client.options(
        "/approve/1",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-csrf-token",
        },
    )

    assert response.status_code == 403
    assert response.headers.get("access-control-allow-origin") is None
    assert response.headers.get("access-control-allow-credentials") is None
    assert response.headers["Content-Security-Policy"]
    assert response.headers["X-Request-ID"]
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "error": {
            "code": "cors_rejected",
            "message": "CORS preflight was rejected",
            "request_id": response.headers["X-Request-ID"],
        }
    }


def test_allowed_cross_origin_response_grants_no_cookie_credentials(client):
    origin = "http://localhost:8000"
    login = client.post(
        "/auth/login",
        json={"secret": TOKEN},
    )

    assert login.status_code == 200
    assert "SameSite=strict" in login.headers["set-cookie"]

    financial = client.get("/pending", headers={"Origin": origin})

    assert financial.status_code == 200
    assert financial.headers["access-control-allow-origin"] == origin
    assert financial.headers.get("access-control-allow-credentials") is None
    assert financial.headers["Content-Security-Policy"]
    assert financial.headers["X-Request-ID"]
    assert financial.headers["Cache-Control"] == "no-store"


# ── A2: strict CSP-safe, credential-safe static UIs ─────────────
@pytest.mark.parametrize("page", _PAGES)
def test_pages_have_no_inline_script_style_or_handler(page):
    parser = _CspParser()
    parser.feed((_STATIC / page).read_text(encoding="utf-8"))

    assert parser.errors == []
    assert parser.stylesheets == ["/static/css/console.css"]
    assert len(parser.scripts) == 1
    assert parser.scripts[0]["type"] == "module"
    assert parser.scripts[0]["src"] == (
        f"/static/js/{page.removesuffix('.html')}.js"
    )


@pytest.mark.parametrize("source", (*_PAGES, *_SCRIPTS))
def test_ui_sources_forbid_browser_secrets_and_html_sinks(source):
    text = (_STATIC / source).read_text(encoding="utf-8")

    for forbidden in (
        "localStorage",
        "sessionStorage",
        "X-API-Key",
        "innerHTML",
        "unsafe-inline",
    ):
        assert forbidden not in text


def test_static_assets_are_anonymous_but_operator_pages_remain_protected(client):
    for path in (
        "/static/css/console.css",
        "/static/js/auth.js",
        "/static/js/login.js",
        "/static/js/index.js",
        "/static/js/plans.js",
        "/static/js/backtests.js",
    ):
        assert client.get(path).status_code == 200, path
    assert client.get("/login").status_code == 200
    assert client.get("/").status_code == 401
    assert client.get("/plans/ui").status_code == 401
    assert client.get("/backtests/ui").status_code == 401
    for path in (
        "/static/index.html",
        "/static/plans.html",
        "/static/backtests.html",
        "/static/login.html",
    ):
        assert client.get(path).status_code == 404, path


def test_auth_module_redirects_401_and_parses_stable_error_envelope():
    _run_module(
        _STATIC / "js" / "auth.js",
        """
        const redirects = [];
        globalThis.window = {
          location: { assign: (path) => redirects.push(path) },
        };
        globalThis.fetch = async () => ({
          status: 401,
          ok: false,
          json: async () => ({
            error: {
              code: "invalid_session",
              message: "A valid operator session is required",
              request_id: "request-401",
            },
          }),
        });
        await module.loadSession().then(
          () => { throw new Error("loadSession should reject"); },
          (error) => {
            if (error.code !== "invalid_session") throw error;
            if (error.requestId !== "request-401") throw error;
          },
        );
        if (JSON.stringify(redirects) !== JSON.stringify(["/login"])) {
          throw new Error(`unexpected redirects: ${JSON.stringify(redirects)}`);
        }
        """,
    )

    _run_module(
        _STATIC / "js" / "auth.js",
        """
        globalThis.window = { location: { assign: () => {} } };
        let call = 0;
        globalThis.fetch = async () => {
          call += 1;
          if (call === 1) {
            return {
              status: 200,
              ok: true,
              json: async () => ({
                actor: "operator:local",
                csrf_token: "csrf-memory-only",
              }),
            };
          }
          return {
            status: 422,
            ok: false,
            json: async () => ({
              error: {
                code: "invalid_request",
                message: "Request validation failed",
                request_id: "request-422",
              },
            }),
          };
        };
        await module.loadSession();
        await module.api("/chat", { method: "POST" }).then(
          () => { throw new Error("api should reject"); },
          (error) => {
            if (error.code !== "invalid_request") throw error;
            if (error.message !== "Request validation failed") throw error;
            if (error.requestId !== "request-422") throw error;
            if (error.status !== 422) throw error;
          },
        );
        """,
    )


def test_auth_module_clears_reauth_secret_and_retries_mutation_once():
    _run_module(
        _STATIC / "js" / "auth.js",
        """
        globalThis.window = { location: { assign: () => {} } };
        const secretInput = { value: "fresh-operator-secret" };
        module.configureReauthentication(async () => secretInput);
        const calls = [];
        globalThis.fetch = async (path, options = {}) => {
          calls.push({ path, options });
          if (path === "/auth/session") {
            return {
              status: 200,
              ok: true,
              json: async () => ({
                actor: "operator:local",
                csrf_token: "csrf-memory-only",
              }),
            };
          }
          if (path === "/auth/reauth") {
            if (secretInput.value !== "") {
              throw new Error("reauth secret was not cleared before fetch");
            }
            if (JSON.parse(options.body).secret !== "fresh-operator-secret") {
              throw new Error("reauth request did not copy the secret");
            }
            return { status: 200, ok: true, json: async () => ({}) };
          }
          return {
            status: 403,
            ok: false,
            json: async () => ({
              error: {
                code: "recent_authentication_required",
                message: "Recent operator reauthentication is required",
                request_id: `approval-${calls.length}`,
              },
            }),
          };
        };
        await module.loadSession();
        await module.api("/approve/7", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason: "reviewed exact proof" }),
        }).then(
          () => { throw new Error("retry should surface the second 403"); },
          (error) => {
            if (error.code !== "recent_authentication_required") throw error;
          },
        );
        const paths = calls.map((call) => call.path);
        const expected = [
          "/auth/session",
          "/approve/7",
          "/auth/reauth",
          "/approve/7",
        ];
        if (JSON.stringify(paths) !== JSON.stringify(expected)) {
          throw new Error(`unexpected calls: ${JSON.stringify(paths)}`);
        }
        const mutationCalls = calls.filter((call) => call.path === "/approve/7");
        for (const call of mutationCalls) {
          const headers = new Headers(call.options.headers);
          if (headers.get("X-CSRF-Token") !== "csrf-memory-only") {
            throw new Error("mutation did not carry in-memory CSRF");
          }
        }
        """,
    )


def test_login_clears_secret_before_fetch_even_when_network_fails():
    _run_module(
        _STATIC / "js" / "login.js",
        """
        const listeners = {};
        const form = {
          addEventListener: (name, callback) => { listeners[name] = callback; },
        };
        const statusLine = { textContent: "" };
        const secretInput = { value: "login-secret", focus() {} };
        globalThis.document = {
          getElementById: (id) => ({
            "login-form": form,
            "login-status": statusLine,
            "login-secret": secretInput,
          })[id],
        };
        globalThis.window = { location: { assign: () => {} } };
        let requestBody = null;
        globalThis.fetch = async (_path, options) => {
          if (secretInput.value !== "") {
            throw new Error("login secret was not cleared before fetch");
          }
          requestBody = JSON.parse(options.body);
          throw new TypeError("network unavailable");
        };
        await import(`data:text/javascript;base64,${Buffer.from(
          fs.readFileSync(process.argv[1], "utf8"),
        ).toString("base64")}#login`);
        await listeners.submit({ preventDefault() {} });
        if (secretInput.value !== "") {
          throw new Error("login secret remained in the input");
        }
        if (requestBody.secret !== "login-secret") {
          throw new Error("login request did not copy the secret");
        }
        if (statusLine.textContent !== "Sign-in failed. Check the connection and try again.") {
          throw new Error(`unexpected status: ${statusLine.textContent}`);
        }
        """,
    )


def test_console_javascript_requires_truthful_approval_panic_and_scoped_reset():
    text = (_STATIC / "js" / "index.js").read_text(encoding="utf-8")

    assert "/pending/" in text
    assert "/confirmation" in text
    assert "proof.complete" in text
    for field in (
        "broker",
        "mode",
        "expires_at",
        "current_signed_notional",
        "resulting_signed_notional",
    ):
        assert field in text
    assert "approval-confirm-button" in text
    assert "confirmed_canceled" in text
    assert "unconfirmed_order_ids" in text
    assert "remote_open_order_ids" in text
    assert "unsafe_local_state" in text
    assert "unknown_categories" in text
    assert "local_enumeration" in text
    assert "remote_enumeration" in text
    assert "safe === true" in text
    assert "everything halted" not in text.lower()
    assert '["equity", "crypto"]' in text
    assert "expected_generation" in text
    assert "global" not in text.lower()


def test_pages_include_accessible_session_actions_dialogs_and_live_regions():
    index = (_STATIC / "index.html").read_text(encoding="utf-8")

    assert 'href="#main-content"' in index
    assert "<nav" in index
    assert 'aria-label="Primary"' in index
    assert 'id="critical-banner"' in index
    assert 'role="alert"' in index
    assert 'id="approval-dialog"' in index
    assert 'aria-labelledby="approval-title"' in index
    assert 'id="reauth-dialog"' in index
    assert 'aria-labelledby="reauth-title"' in index
    assert 'id="status-region"' in index
    assert 'aria-live="polite"' in index
    assert "Sign out" in index


@pytest.mark.parametrize("script", ["index.js", "plans.js"])
def test_review_dialogs_close_on_escape_and_restore_invoker_focus(script):
    text = (_STATIC / "js" / script).read_text(encoding="utf-8")

    assert 'event.key === "Escape"' in text
    assert "closeDialog(dialog)" in text
    assert "target.focus()" in text
    close_start = text.index("function closeDialog(dialog)")
    target_lookup = text.index(
        "const target = dialogReturnFocus.get(dialog)",
        close_start,
    )
    native_close = text.index("dialog.close()", close_start)
    assert target_lookup < native_close


def test_console_workspace_constrains_mobile_data_without_hiding_it():
    css = (_STATIC / "css" / "console.css").read_text(encoding="utf-8")
    workspace_start = css.index(".workspace {")
    workspace_end = css.index("}", workspace_start)
    workspace_rule = css[workspace_start:workspace_end]

    assert "grid-template-columns: minmax(0, 1fr);" in workspace_rule
    assert (
        ".data-grid,\n"
        "  .plans-layout,\n"
        "  .backtests-layout {\n"
        "    grid-template-columns: minmax(0, 1fr);"
    ) in css
    assert "overflow-wrap: anywhere;" in css


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
        return Quote(
            q.ticker,
            q.bid,
            q.ask,
            q.last,
            q.prev_close,
            as_of=old,
            book_as_of=old,
            trade_as_of=old,
        )


def test_stale_quote_does_not_fire(make_service):
    from trading_assistant.daemon.monitor import Monitor

    broker = _StaleBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    svc.create_conditional_rule(
        "AAPL",
        {"price_below": 175},
        {"side": "buy", "notional": "100"},
        actor="operator:test",
        reason="stale quote rule setup",
        request_id="security-stale-quote-rule",
    )
    # Price 100 < 175 would fire, but the quote is 600s stale -> skipped.
    assert Monitor(svc, max_quote_age_seconds=60).tick() == []
