"""Section A security: operator sessions + CORS (A1), no-innerHTML XSS guard (A2),
redaction (A3), daemon backoff + staleness (A4)."""

from __future__ import annotations

import asyncio
from html.parser import HTMLParser
import pathlib
import subprocess
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from random import Random

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.app_factory import create_app
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


def _run_page_module(
    path: pathlib.Path,
    exported_names: tuple[str, ...],
    scenario: str,
) -> None:
    loader = r"""
        import fs from "node:fs";

        class FakeElement {
          constructor(id = "", tagName = "div") {
            this.id = id;
            this.tagName = tagName.toUpperCase();
            this.children = [];
            this.parentNode = null;
            this.listeners = new Map();
            this._textContent = "";
            this.className = "";
            this.value = "";
            this.disabled = false;
            this.hidden = false;
            this.open = false;
            this.type = "";
          }

          get firstChild() {
            return this.children[0] || null;
          }

          get textContent() {
            return this._textContent
              + this.children.map((child) => child.textContent).join("");
          }

          set textContent(value) {
            this._textContent = value === null || value === undefined
              ? ""
              : String(value);
            this.children = [];
          }

          appendChild(child) {
            child.parentNode = this;
            this.children.push(child);
            return child;
          }

          append(...children) {
            children.forEach((child) => this.appendChild(child));
          }

          prepend(child) {
            child.parentNode = this;
            this.children.unshift(child);
          }

          removeChild(child) {
            const index = this.children.indexOf(child);
            if (index >= 0) {
              this.children.splice(index, 1);
              child.parentNode = null;
            }
            return child;
          }

          remove() {
            if (this.parentNode) {
              this.parentNode.removeChild(this);
            }
          }

          querySelector(selector) {
            const className = selector.startsWith(".")
              ? selector.slice(1)
              : null;
            for (const child of this.children) {
              if (
                className
                && child.className.split(/\s+/).includes(className)
              ) {
                return child;
              }
              const nested = child.querySelector(selector);
              if (nested) {
                return nested;
              }
            }
            return null;
          }

          addEventListener(name, callback) {
            const callbacks = this.listeners.get(name) || [];
            callbacks.push(callback);
            this.listeners.set(name, callbacks);
          }

          removeEventListener(name, callback) {
            const callbacks = this.listeners.get(name) || [];
            this.listeners.set(
              name,
              callbacks.filter((candidate) => candidate !== callback),
            );
          }

          emit(name, overrides = {}) {
            const event = {
              preventDefault() {},
              currentTarget: this,
              target: this,
              key: undefined,
              ...overrides,
            };
            return (this.listeners.get(name) || []).map(
              (callback) => callback(event),
            );
          }

          click() {
            return this.emit("click");
          }

          focus() {
            globalThis.document.activeElement = this;
          }

          showModal() {
            this.open = true;
          }

          close() {
            if (!this.open) {
              return;
            }
            this.open = false;
            this.emit("close");
          }
        }

        globalThis.installDom = (ids) => {
          const elements = new Map(
            ids.map((id) => [id, new FakeElement(id)]),
          );
          globalThis.document = {
            activeElement: null,
            createElement: (tagName) => new FakeElement("", tagName),
            getElementById: (id) => elements.get(id) || null,
          };
          globalThis.window = {
            setTimeout: () => 0,
            setInterval: () => 0,
            location: {assign() {}},
          };
          return Object.fromEntries(elements);
        };
        globalThis.deferred = () => {
          let resolve;
          let reject;
          const promise = new Promise((res, rej) => {
            resolve = res;
            reject = rej;
          });
          return {promise, resolve, reject};
        };
        globalThis.flush = async () => {
          await Promise.resolve();
          await new Promise((resolve) => setImmediate(resolve));
        };
        globalThis.findButton = (root, label) => {
          if (root.tagName === "BUTTON" && root.textContent === label) {
            return root;
          }
          for (const child of root.children) {
            const found = globalThis.findButton(child, label);
            if (found) {
              return found;
            }
          }
          return null;
        };

        const authSource = `
          export const api = (...args) => globalThis.__api(...args);
          export const jsonPost = (body) => ({
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(body),
          });
          export const loadSession = () => Promise.resolve({
            actor: "operator:test",
            csrf_token: "csrf-test",
          });
          export const logout = () => Promise.resolve();
        `;
        const authUrl = `data:text/javascript;base64,${
          Buffer.from(authSource).toString("base64")
        }`;
        let source = fs.readFileSync(process.argv[1], "utf8");
        source = source.replace(
          'from "/static/js/auth.js";',
          `from "${authUrl}";`,
        );
        source = source.replace(/\ninitialize\(\);\s*$/, "\n");
        source += `\nexport {${process.argv[2]}};\n`;
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
            ",".join(exported_names),
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
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "security-breaker-reset",
        },
        json={
            "scope": "loss:equity",
            "reason": "authenticated health review",
            "expected_generation": observed.generation,
        },
    )
    assert r.status_code == 200 and r.json()["tripped"] is False


def test_blocked_boundary_audit_does_not_delay_exact_liveness(
    make_service,
):
    app = create_app(
        service=make_service(),
        agent=_StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    blocked = threading.Event()
    release = threading.Event()

    class BlockingAudit:
        def record(self, *_args, **_kwargs):
            blocked.set()
            release.wait(timeout=0.5)

    app.state.audit = BlockingAudit()

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://localhost:8020",
        ) as client:
            login = await client.post(
                "/auth/login",
                json={"secret": TOKEN},
            )
            started_at = time.monotonic()
            mutation = asyncio.create_task(
                client.post(
                    "/chat",
                    json={"message": "boundary audit responsiveness"},
                    headers={
                        "X-CSRF-Token": login.json()["csrf_token"],
                    },
                )
            )
            assert await asyncio.to_thread(blocked.wait, 1)
            try:
                liveness = await asyncio.wait_for(
                    client.get("/health/live"),
                    timeout=0.2,
                )
                elapsed = time.monotonic() - started_at
            finally:
                release.set()
            mutation_response = await mutation
            return liveness, mutation_response, elapsed

    liveness, mutation_response, elapsed = asyncio.run(exercise())

    assert liveness.status_code == 200
    assert mutation_response.status_code == 200
    assert elapsed < 0.25


def test_paid_analysis_and_backtest_endpoints_are_rate_limited(
    make_service,
    authenticate_client,
    with_limit,
    monkeypatch,
):
    class StubPlanning:
        def __init__(self):
            self.calls = 0

        def analyze(self, symbol, **_context):
            self.calls += 1
            return {"symbol": symbol}

    class StubReport:
        def to_dict(self):
            return {"status": "complete"}

    planning = StubPlanning()
    backtest_calls = 0

    def run_stub_backtest(*_args, **_kwargs):
        nonlocal backtest_calls
        backtest_calls += 1
        return 7, StubReport()

    monkeypatch.setattr(
        "trading_assistant.backtest.runner.run_synthetic_backtest",
        run_stub_backtest,
    )
    service = make_service()
    service.config = with_limit(
        service.config,
        "analysis",
        requests=1,
        window_seconds=60,
    )
    service.config = with_limit(
        service.config,
        "backtest",
        requests=1,
        window_seconds=60,
    )
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token=TOKEN,
        planning=planning,
    )
    limited, csrf = authenticate_client(TestClient(app), TOKEN)
    assert limited.post(
        "/analyze",
        json={"symbol": "AAPL", "reason": "rate limit test"},
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "security-rate-analyze",
        },
    ).status_code == 200
    assert limited.post(
        "/propose",
        json={"n": 1, "reason": "rate limit test"},
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "security-rate-propose",
        },
    ).status_code == 429
    assert planning.calls == 1
    assert limited.post(
        "/backtests/run",
        json={"symbols": [], "reason": "rate limit test"},
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "security-rate-backtest",
        },
    ).status_code == 200
    assert limited.post(
        "/backtests/run",
        json={"symbols": [], "reason": "rate limit retry"},
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "security-rate-backtest-retry",
        },
    ).status_code == 429
    assert backtest_calls == 1


def test_financial_get_endpoints_fail_closed(client):
    assert client.get("/pending").status_code == 401
    assert client.get("/account").status_code == 401
    assert client.get("/positions").status_code == 401
    assert client.get("/log").status_code == 401


def test_cross_origin_preflight_is_rejected_without_cors_headers(client):
    response = client.options(
        "/approve/1",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-csrf-token",
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_mismatch"
    assert response.headers.get("access-control-allow-origin") is None
    assert response.headers.get("access-control-allow-credentials") is None


def test_foreign_origin_has_a_stable_perimeter_error(client):
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
    assert response.json()["error"]["code"] == "origin_mismatch"


def test_cross_origin_authenticated_request_is_rejected_before_cookie_use(client):
    origin = "https://evil.example"
    login = client.post(
        "/auth/login",
        json={"secret": TOKEN},
    )

    assert login.status_code == 200
    assert "SameSite=strict" in login.headers["set-cookie"]

    financial = client.get("/pending", headers={"Origin": origin})

    assert financial.status_code == 403
    assert financial.json()["error"]["code"] == "origin_mismatch"
    assert financial.headers.get("access-control-allow-origin") is None
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


@pytest.mark.parametrize("page", _PAGES)
def test_pages_use_the_local_flight_deck_icon(page):
    text = (_STATIC / page).read_text(encoding="utf-8")

    assert '<link rel="icon" href="/static/img/flight-deck.svg">' in text


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
        "/static/img/flight-deck.svg",
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


def test_auth_module_clones_mutable_options_and_preserves_header_precedence():
    _run_module(
        _STATIC / "js" / "auth.js",
        """
        globalThis.window = { location: { assign: () => {} } };
        const calls = [];
        globalThis.fetch = async (path, options = {}) => {
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
          calls.push({ path, options });
          return {
            status: 200,
            ok: true,
            json: async () => ({ accepted: true }),
          };
        };
        const callerHeaders = {
          "Content-Type": "application/vnd.operator+json",
          "Idempotency-Key": "caller-action-key",
          "X-CSRF-Token": "stale-caller-csrf",
          "X-Operator-Trace": "trace-7",
        };
        const callerOptions = {
          method: "post",
          credentials: "omit",
          headers: callerHeaders,
          body: '{"reason":"reviewed exact proof"}',
        };
        const original = JSON.stringify(callerOptions);

        await module.api("/custom-mutation", callerOptions);

        if (JSON.stringify(callerOptions) !== original) {
          throw new Error("mutable caller options changed");
        }
        if (callerOptions.headers !== callerHeaders) {
          throw new Error("mutable caller headers reference changed");
        }
        const internal = calls[0].options;
        if (internal === callerOptions) {
          throw new Error("fetch received caller-owned options");
        }
        if (internal.headers === callerHeaders) {
          throw new Error("fetch received caller-owned headers");
        }
        if (internal.method !== "POST") {
          throw new Error(`method was not normalized: ${internal.method}`);
        }
        if (internal.credentials !== "same-origin") {
          throw new Error(`credentials precedence failed: ${internal.credentials}`);
        }
        const headers = new Headers(internal.headers);
        const expected = {
          contentType: "application/vnd.operator+json",
          csrf: "csrf-memory-only",
          idempotency: "caller-action-key",
          trace: "trace-7",
        };
        const observed = {
          contentType: headers.get("Content-Type"),
          csrf: headers.get("X-CSRF-Token"),
          idempotency: headers.get("Idempotency-Key"),
          trace: headers.get("X-Operator-Trace"),
        };
        if (JSON.stringify(observed) !== JSON.stringify(expected)) {
          throw new Error(`header precedence failed: ${JSON.stringify(observed)}`);
        }
        """,
    )


def test_auth_module_accepts_frozen_options_and_plain_headers():
    _run_module(
        _STATIC / "js" / "auth.js",
        """
        globalThis.window = { location: { assign: () => {} } };
        const calls = [];
        globalThis.fetch = async (path, options = {}) => {
          if (path === "/auth/session") {
            return {
              status: 200,
              ok: true,
              json: async () => ({
                actor: "operator:local",
                csrf_token: "csrf-frozen",
              }),
            };
          }
          calls.push({ path, options });
          return { status: 200, ok: true, json: async () => ({}) };
        };
        const frozenHeaders = Object.freeze({
          "Content-Type": "application/json",
          "Idempotency-Key": "frozen-action-key",
          "X-Frozen": "preserved",
        });
        const frozenOptions = Object.freeze({
          method: "post",
          credentials: "omit",
          headers: frozenHeaders,
          body: "{}",
        });

        await module.api("/frozen-mutation", frozenOptions);

        const internal = calls[0].options;
        if (internal === frozenOptions || internal.headers === frozenHeaders) {
          throw new Error("frozen caller ownership leaked into fetch");
        }
        if (
          frozenOptions.method !== "post"
          || frozenOptions.credentials !== "omit"
          || frozenOptions.headers !== frozenHeaders
        ) {
          throw new Error("frozen caller input changed");
        }
        const headers = new Headers(internal.headers);
        if (
          internal.method !== "POST"
          || internal.credentials !== "same-origin"
          || headers.get("X-CSRF-Token") !== "csrf-frozen"
          || headers.get("Idempotency-Key") !== "frozen-action-key"
          || headers.get("X-Frozen") !== "preserved"
        ) {
          throw new Error("frozen options were not normalized internally");
        }
        """,
    )


def test_auth_module_clones_supplied_headers_without_mutating_them():
    _run_module(
        _STATIC / "js" / "auth.js",
        """
        globalThis.window = { location: { assign: () => {} } };
        const calls = [];
        globalThis.fetch = async (path, options = {}) => {
          if (path === "/auth/session") {
            return {
              status: 200,
              ok: true,
              json: async () => ({
                actor: "operator:local",
                csrf_token: "csrf-owned",
              }),
            };
          }
          calls.push({ path, options });
          return { status: 200, ok: true, json: async () => ({}) };
        };
        const suppliedHeaders = new Headers({
          "Content-Type": "application/custom+json",
          "Idempotency-Key": "headers-action-key",
          "X-CSRF-Token": "caller-csrf",
          "X-Supplied": "preserved",
        });
        const before = JSON.stringify(Array.from(suppliedHeaders.entries()));
        const callerOptions = {
          method: "POST",
          headers: suppliedHeaders,
          body: "{}",
        };

        await module.api("/headers-mutation", callerOptions);

        if (JSON.stringify(Array.from(suppliedHeaders.entries())) !== before) {
          throw new Error("supplied Headers instance was mutated");
        }
        if (callerOptions.headers !== suppliedHeaders) {
          throw new Error("caller lost its supplied Headers instance");
        }
        const internal = calls[0].options;
        if (internal.headers === suppliedHeaders) {
          throw new Error("fetch reused the supplied Headers instance");
        }
        const headers = new Headers(internal.headers);
        if (
          headers.get("Content-Type") !== "application/custom+json"
          || headers.get("Idempotency-Key") !== "headers-action-key"
          || headers.get("X-CSRF-Token") !== "csrf-owned"
          || headers.get("X-Supplied") !== "preserved"
        ) {
          throw new Error("internal header precedence was incorrect");
        }
        """,
    )


def test_auth_module_reuses_internal_retry_options_and_rotates_action_key():
    _run_module(
        _STATIC / "js" / "auth.js",
        """
        globalThis.window = { location: { assign: () => {} } };
        const generatedKeys = ["operator-action-1", "operator-action-2"];
        let uuidCalls = 0;
        Object.defineProperty(globalThis, "crypto", {
          configurable: true,
          value: {
            randomUUID: () => generatedKeys[uuidCalls++],
          },
        });
        const secretInput = { value: "fresh-operator-secret" };
        module.configureReauthentication(async () => secretInput);
        const calls = [];
        let firstActionAttempts = 0;
        globalThis.fetch = async (path, options = {}) => {
          calls.push({ path, options, headers: options.headers });
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
            const headers = new Headers(options.headers);
            if (headers.has("Idempotency-Key")) {
              throw new Error("reauth request inherited a mutation key");
            }
            if (
              headers.get("Content-Type") !== "application/json"
              || headers.get("X-CSRF-Token") !== "csrf-memory-only"
            ) {
              throw new Error("reauth internal headers were incorrect");
            }
            return { status: 200, ok: true, json: async () => ({}) };
          }
          if (path === "/approve/7") {
            firstActionAttempts += 1;
            if (firstActionAttempts === 1) {
              return {
                status: 403,
                ok: false,
                json: async () => ({
                  error: {
                    code: "recent_authentication_required",
                    message: "Recent operator reauthentication is required",
                    request_id: "approval-first",
                  },
                }),
              };
            }
          }
          return {
            status: 200,
            ok: true,
            json: async () => ({ executed: false }),
          };
        };

        const firstCallerOptions = module.jsonPost({
          reason: "reviewed exact proof",
        });
        const firstCallerHeaders = firstCallerOptions.headers;
        await module.api("/approve/7", firstCallerOptions);

        const firstActionCalls = calls.filter(
          (call) => call.path === "/approve/7",
        );
        if (firstActionCalls[0].options === firstCallerOptions) {
          throw new Error("retry object remained caller-owned");
        }
        if (firstActionCalls[0].options !== firstActionCalls[1].options) {
          throw new Error("recent-auth retry replaced internal options");
        }
        if (firstActionCalls[0].headers !== firstActionCalls[1].headers) {
          throw new Error("recent-auth retry replaced internal headers");
        }
        if (
          firstCallerOptions.headers !== firstCallerHeaders
          || Object.hasOwn(firstCallerHeaders, "X-CSRF-Token")
          || Object.hasOwn(firstCallerOptions, "credentials")
        ) {
          throw new Error("jsonPost caller options were mutated");
        }
        for (const call of firstActionCalls) {
          const headers = new Headers(call.headers);
          if (
            headers.get("Content-Type") !== "application/json"
            || headers.get("X-CSRF-Token") !== "csrf-memory-only"
            || headers.get("Idempotency-Key") !== "operator-action-1"
          ) {
            throw new Error("first action retry headers changed");
          }
        }

        const secondCallerOptions = module.jsonPost({
          reason: "second operator action",
        });
        await module.api("/approve/8", secondCallerOptions);
        const secondActionCall = calls.find(
          (call) => call.path === "/approve/8",
        );
        const secondHeaders = new Headers(secondActionCall.headers);
        if (secondActionCall.options === firstActionCalls[0].options) {
          throw new Error("new action reused the previous internal options");
        }
        if (secondHeaders.get("Idempotency-Key") !== "operator-action-2") {
          throw new Error("new action did not receive a new idempotency key");
        }
        if (uuidCalls !== 2) {
          throw new Error(`expected two action UUIDs, got ${uuidCalls}`);
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
          if (new Headers(options.headers).has("Idempotency-Key")) {
            throw new Error("login request carried a mutation key");
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


_APPROVAL_DOM_SETUP = r"""
    const elements = installDom([
      "pending-list",
      "approval-dialog",
      "approval-form",
      "approval-reason",
      "approval-confirm-button",
      "approval-proof-status",
      "approval-broker",
      "approval-mode",
      "approval-symbol",
      "approval-side",
      "approval-order-type",
      "approval-quantity",
      "approval-notional",
      "approval-limit-price",
      "approval-expiry",
      "approval-current-quantity",
      "approval-current-exposure",
      "approval-resulting-exposure",
      "approval-exposure-time",
      "status-region",
      "receipt-panel",
    ]);
    const expiresAt = "2099-07-25T12:00:00+00:00";
    const pendingOrder = (orderId, symbol) => ({
      order_id: orderId,
      ticker: symbol,
      side: "buy",
      order_type: "limit",
      qty: "2.000000",
      notional: null,
      limit_price: "101.000000",
      status: "proposed",
      expires_at: expiresAt,
      expired: false,
    });
    const confirmation = (
      orderId,
      symbol,
      resultingExposure = "302.000000",
    ) => ({
      complete: true,
      missing_proof: [],
      broker: "Alpaca",
      mode: "paper",
      order: {
        order_id: orderId,
        symbol,
        side: "buy",
        order_type: "limit",
        quantity: "2.000000",
        notional: null,
        limit_price: "101.000000",
      },
      expires_at: expiresAt,
      exposure: {
        currency: "USD",
        current_position_quantity: "1.000000",
        current_signed_notional: "100.000000",
        resulting_signed_notional: resultingExposure,
        as_of: "2099-07-25T11:59:00+00:00",
      },
    });
"""


def test_approval_dialog_ignores_out_of_order_proof_for_another_order():
    _run_page_module(
        _STATIC / "js" / "index.js",
        (
            "refreshPending",
            "openApproval",
            "submitApproval",
            "updateApprovalButton",
        ),
        _APPROVAL_DOM_SETUP
        + r"""
        const pending = [pendingOrder(1, "AAPL"), pendingOrder(2, "MSFT")];
        const a = deferred();
        const b = deferred();
        const approval = deferred();
        const calls = [];
        globalThis.__api = (path, options = {}) => {
          calls.push({path, options});
          if (path === "/pending") return Promise.resolve({pending});
          if (path === "/pending/1/confirmation") return a.promise;
          if (path === "/pending/2/confirmation") return b.promise;
          if (path === "/approve/2") return approval.promise;
          throw new Error(`unexpected API path ${path}`);
        };

        await module.refreshPending();
        const first = elements["pending-list"].children[0];
        const second = elements["pending-list"].children[1];
        module.openApproval(1, findButton(first, "Review approval"));
        module.openApproval(2, findButton(second, "Review approval"));
        b.resolve(confirmation(2, "MSFT"));
        await flush();
        a.resolve(confirmation(1, "AAPL"));
        await flush();

        elements["approval-reason"].value = "reviewed MSFT proof";
        module.updateApprovalButton();
        module.submitApproval({preventDefault() {}});
        await flush();

        const submitted = calls.filter((call) => call.path.startsWith("/approve/"));
        const failures = [];
        if (elements["approval-symbol"].textContent !== "MSFT") {
          failures.push(`displayed ${elements["approval-symbol"].textContent}`);
        }
        if (submitted.length !== 1 || submitted[0].path !== "/approve/2") {
          failures.push(`submitted ${submitted.map((call) => call.path)}`);
        }
        if (
          submitted.length === 1
          && JSON.parse(submitted[0].options.body).reason !== "reviewed MSFT proof"
        ) {
          failures.push("submitted the wrong reason body");
        }
        if (failures.length) throw new Error(failures.join("; "));
        """,
    )


def test_approval_dialog_rejects_mismatched_proof_identity_after_switch():
    _run_page_module(
        _STATIC / "js" / "index.js",
        (
            "refreshPending",
            "openApproval",
            "submitApproval",
            "updateApprovalButton",
        ),
        _APPROVAL_DOM_SETUP
        + r"""
        const pending = [pendingOrder(1, "AAPL"), pendingOrder(2, "MSFT")];
        const a = deferred();
        const b = deferred();
        const calls = [];
        globalThis.__api = (path, options = {}) => {
          calls.push({path, options});
          if (path === "/pending") return Promise.resolve({pending});
          if (path === "/pending/1/confirmation") return a.promise;
          if (path === "/pending/2/confirmation") return b.promise;
          if (path.startsWith("/approve/")) return Promise.resolve({});
          throw new Error(`unexpected API path ${path}`);
        };

        await module.refreshPending();
        module.openApproval(1, elements["approval-form"]);
        a.resolve(confirmation(1, "AAPL"));
        await flush();
        module.openApproval(2, elements["approval-form"]);
        elements["approval-reason"].value = "must remain disabled";
        module.updateApprovalButton();
        if (!elements["approval-confirm-button"].disabled) {
          throw new Error("A proof remained enabled after switching to B");
        }
        b.resolve(confirmation("2", "MSFT"));
        await flush();
        module.updateApprovalButton();
        module.submitApproval({preventDefault() {}});
        await flush();

        if (!elements["approval-confirm-button"].disabled) {
          throw new Error("string proof ID was accepted as canonical order 2");
        }
        if (calls.some((call) => call.path.startsWith("/approve/"))) {
          throw new Error("mismatched proof identity submitted an approval");
        }
        """,
    )


def test_approval_dialog_close_reopen_poison_old_same_target_response():
    _run_page_module(
        _STATIC / "js" / "index.js",
        (
            "refreshPending",
            "openApproval",
            "closeDialog",
        ),
        _APPROVAL_DOM_SETUP
        + r"""
        const first = deferred();
        const second = deferred();
        let confirmationCall = 0;
        globalThis.__api = (path) => {
          if (path === "/pending") {
            return Promise.resolve({pending: [pendingOrder(1, "AAPL")]});
          }
          if (path === "/pending/1/confirmation") {
            confirmationCall += 1;
            return confirmationCall === 1 ? first.promise : second.promise;
          }
          throw new Error(`unexpected API path ${path}`);
        };

        await module.refreshPending();
        module.openApproval(1, elements["approval-form"]);
        module.closeDialog(elements["approval-dialog"]);
        module.openApproval(1, elements["approval-form"]);
        second.resolve(confirmation(1, "AAPL", "322.000000"));
        await flush();
        first.resolve(confirmation(1, "AAPL", "311.000000"));
        await flush();

        if (elements["approval-resulting-exposure"].textContent !== "322.000000") {
          throw new Error(
            `old response overwrote reopen: ${
              elements["approval-resulting-exposure"].textContent
            }`,
          );
        }
        """,
    )


def test_approval_dialog_ignores_late_error_after_new_target_is_proven():
    _run_page_module(
        _STATIC / "js" / "index.js",
        (
            "refreshPending",
            "openApproval",
            "updateApprovalButton",
        ),
        _APPROVAL_DOM_SETUP
        + r"""
        const a = deferred();
        const b = deferred();
        globalThis.__api = (path) => {
          if (path === "/pending") {
            return Promise.resolve({
              pending: [pendingOrder(1, "AAPL"), pendingOrder(2, "MSFT")],
            });
          }
          if (path === "/pending/1/confirmation") return a.promise;
          if (path === "/pending/2/confirmation") return b.promise;
          throw new Error(`unexpected API path ${path}`);
        };

        await module.refreshPending();
        module.openApproval(1, elements["approval-form"]);
        module.openApproval(2, elements["approval-form"]);
        b.resolve(confirmation(2, "MSFT"));
        await flush();
        elements["approval-reason"].value = "reviewed MSFT";
        module.updateApprovalButton();
        a.reject(new Error("late A failure"));
        await flush();

        if (elements["approval-symbol"].textContent !== "MSFT") {
          throw new Error("late A error erased B identity");
        }
        if (elements["approval-confirm-button"].disabled) {
          throw new Error("late A error disabled proven B");
        }
        """,
    )


def test_approval_dialog_blocks_double_submit_for_one_target():
    _run_page_module(
        _STATIC / "js" / "index.js",
        (
            "refreshPending",
            "openApproval",
            "submitApproval",
            "updateApprovalButton",
        ),
        _APPROVAL_DOM_SETUP
        + r"""
        const approval = deferred();
        const calls = [];
        globalThis.__api = (path, options = {}) => {
          calls.push({path, options});
          if (path === "/pending") {
            return Promise.resolve({pending: [pendingOrder(1, "AAPL")]});
          }
          if (path === "/pending/1/confirmation") {
            return Promise.resolve(confirmation(1, "AAPL"));
          }
          if (path === "/approve/1") return approval.promise;
          throw new Error(`unexpected API path ${path}`);
        };

        await module.refreshPending();
        await module.openApproval(1, elements["approval-form"]);
        elements["approval-reason"].value = "one deliberate approval";
        module.updateApprovalButton();
        module.submitApproval({preventDefault() {}});
        module.submitApproval({preventDefault() {}});
        await flush();

        const approvals = calls.filter((call) => call.path === "/approve/1");
        if (approvals.length !== 1) {
          throw new Error(`approval submitted ${approvals.length} times`);
        }
        if (elements["approval-reason"].value !== "") {
          throw new Error("approval state was not cleared after submit");
        }
        """,
    )


@pytest.mark.parametrize(
    "proof_mutation",
    (
        'proof.broker = "Mock";',
        'proof.mode = "live";',
        'proof.expires_at = "2000-01-01T00:00:00+00:00";',
        'proof.order.symbol = "MSFT";',
    ),
)
def test_approval_dialog_fails_closed_for_noncanonical_server_proof(
    proof_mutation,
):
    _run_page_module(
        _STATIC / "js" / "index.js",
        (
            "refreshPending",
            "openApproval",
            "updateApprovalButton",
        ),
        _APPROVAL_DOM_SETUP
        + f"""
        const proof = confirmation(1, "AAPL");
        {proof_mutation}
        globalThis.__api = (path) => {{
          if (path === "/pending") {{
            return Promise.resolve({{pending: [pendingOrder(1, "AAPL")]}});
          }}
          if (path === "/pending/1/confirmation") {{
            return Promise.resolve(proof);
          }}
          throw new Error(`unexpected API path ${{path}}`);
        }};

        await module.refreshPending();
        await module.openApproval(1, elements["approval-form"]);
        elements["approval-reason"].value = "must not enable";
        module.updateApprovalButton();
        if (!elements["approval-confirm-button"].disabled) {{
          throw new Error("noncanonical proof enabled approval");
        }}
        """,
    )


def test_approval_dialog_requires_explicit_current_pending_status():
    _run_page_module(
        _STATIC / "js" / "index.js",
        (
            "refreshPending",
            "openApproval",
            "updateApprovalButton",
        ),
        _APPROVAL_DOM_SETUP
        + r"""
        const pending = pendingOrder(1, "AAPL");
        delete pending.expired;
        globalThis.__api = (path) => {
          if (path === "/pending") {
            return Promise.resolve({pending: [pending]});
          }
          if (path === "/pending/1/confirmation") {
            return Promise.resolve(confirmation(1, "AAPL"));
          }
          throw new Error(`unexpected API path ${path}`);
        };

        await module.refreshPending();
        await module.openApproval(1, elements["approval-form"]);
        elements["approval-reason"].value = "missing pending truth";
        module.updateApprovalButton();
        if (!elements["approval-confirm-button"].disabled) {
          throw new Error("missing explicit pending expiry status enabled approval");
        }
        """,
    )


def test_account_refresh_invalidates_proof_and_times_out():
    _run_page_module(
        _STATIC / "js" / "index.js",
        ("refreshAccount",),
        r"""
        const elements = installDom([
          "account-status",
          "account-equity",
          "account-buying-power",
          "account-cash",
          "account-exposure",
          "proof-data",
          "positions",
        ]);
        let timeoutCallback = null;
        window.setTimeout = (callback) => {
          timeoutCallback = callback;
          return 17;
        };
        window.clearTimeout = () => {};
        let requestSignal = null;
        globalThis.__api = (path, options = {}) => {
          if (path !== "/account") {
            throw new Error(`unexpected API path ${path}`);
          }
          requestSignal = options.signal;
          return new Promise((_resolve, reject) => {
            requestSignal.addEventListener(
              "abort",
              () => reject(new Error("account request timed out")),
              {once: true},
            );
          });
        };

        const refresh = module.refreshAccount();
        if (
          !elements["proof-data"].textContent.includes("Refreshing")
          || !elements["proof-data"].className.includes("caution")
        ) {
          throw new Error("account refresh retained stale verified proof");
        }
        if (typeof timeoutCallback !== "function") {
          throw new Error("account refresh did not install a finite timeout");
        }

        timeoutCallback();
        try {
          await refresh;
          throw new Error("timed-out account refresh unexpectedly succeeded");
        } catch (error) {
          if (error.message === "timed-out account refresh unexpectedly succeeded") {
            throw error;
          }
        }

        if (!requestSignal.aborted) {
          throw new Error("account timeout did not abort the broker read");
        }
        if (!elements["proof-data"].className.includes("alarm")) {
          throw new Error("timed-out account proof did not fail closed");
        }
        """,
    )


def test_account_snapshot_renders_positions_from_same_response():
    _run_page_module(
        _STATIC / "js" / "index.js",
        ("refreshAccount",),
        r"""
        const elements = installDom([
          "account-status",
          "account-equity",
          "account-buying-power",
          "account-cash",
          "account-exposure",
          "proof-data",
          "positions",
        ]);
        window.clearTimeout = () => {};
        globalThis.__api = (path) => {
          if (path !== "/account") {
            throw new Error(`unexpected API path ${path}`);
          }
          return Promise.resolve({
            equity: "100",
            buying_power: "200",
            cash: "90",
            gross_exposure: "10",
            observed_at: new Date().toISOString(),
            positions: [{
              ticker: "AAPL",
              qty: "1",
              avg_entry_price: "10",
              current_price: "10",
              market_value: "10",
            }],
          });
        };

        await module.refreshAccount();

        if (!elements.positions.textContent.includes("AAPL")) {
          throw new Error("account snapshot did not render its coherent positions");
        }
        """,
    )


@pytest.mark.parametrize(
    (
        "function_name",
        "path",
        "target_id",
        "extra_ids",
        "older_payload",
        "newer_payload",
        "older_evidence",
        "newer_evidence",
    ),
    (
        (
            "refreshAccount",
            "/account",
            "account-status",
            (
                "account-equity",
                "account-buying-power",
                "account-cash",
                "account-exposure",
                "proof-data",
            ),
            (
                '{equity: "100", buying_power: "200", cash: "90", '
                'gross_exposure: "10", positions: [{ticker: "OLD", '
                'qty: "1", avg_entry_price: "10", current_price: "10", '
                'market_value: "10"}], '
                'observed_at: new Date().toISOString()}'
            ),
            (
                '{equity: "200", buying_power: "300", cash: "160", '
                'gross_exposure: "40", positions: [{ticker: "NEW1", '
                'qty: "1", avg_entry_price: "20", current_price: "20", '
                'market_value: "20"}, {ticker: "NEW2", qty: "1", '
                'avg_entry_price: "20", current_price: "20", '
                'market_value: "20"}], '
                'observed_at: new Date().toISOString()}'
            ),
            "1 open broker position",
            "2 open broker positions",
        ),
        (
            "refreshPositions",
            "/positions",
            "positions",
            (),
            (
                '{positions: [{ticker: "OLD-POS", qty: "1", '
                'avg_entry_price: "10", current_price: "10", '
                'market_value: "10"}]}'
            ),
            (
                '{positions: [{ticker: "NEW-POS", qty: "2", '
                'avg_entry_price: "20", current_price: "21", '
                'market_value: "42"}]}'
            ),
            "OLD-POS",
            "NEW-POS",
        ),
        (
            "refreshHoldings",
            "/holdings",
            "holdings",
            ("external-stale",),
            (
                '{alpaca: [{ticker: "OLD-HOLD", source: "alpaca", '
                'read_only: false, qty: "1", market_value: "10"}], '
                'external: [], combined_by_ticker: {"OLD-HOLD": "10"}, '
                "external_stale: true, external_available: true}"
            ),
            (
                '{alpaca: [{ticker: "NEW-HOLD", source: "alpaca", '
                'read_only: false, qty: "2", market_value: "42"}], '
                'external: [], combined_by_ticker: {"NEW-HOLD": "42"}, '
                "external_stale: false, external_available: true}"
            ),
            "OLD-HOLD",
            "NEW-HOLD",
        ),
        (
            "refreshRiskLog",
            "/log",
            "risk-log",
            (),
            (
                '{risk_events: [{at: "2026-07-25T10:00:00Z", '
                'type: "old", reason: "OLD-RISK"}]}'
            ),
            (
                '{risk_events: [{at: "2026-07-25T10:01:00Z", '
                'type: "new", reason: "NEW-RISK"}]}'
            ),
            "OLD-RISK",
            "NEW-RISK",
        ),
    ),
)
@pytest.mark.parametrize(
    "completion_order",
    ("older_first", "newer_first"),
)
def test_operational_refreshes_bind_rendering_to_latest_generation(
    function_name,
    path,
    target_id,
    extra_ids,
    older_payload,
    newer_payload,
    older_evidence,
    newer_evidence,
    completion_order,
):
    ids = [target_id, *extra_ids]
    _run_page_module(
        _STATIC / "js" / "index.js",
        (function_name,),
        f"""
        const elements = installDom({ids!r});
        const older = deferred();
        const newer = deferred();
        const requests = [];
        let call = 0;
        globalThis.__api = (path, options = {{}}) => {{
          if (path !== {path!r}) {{
            throw new Error(`unexpected API path ${{path}}`);
          }}
          requests.push(options);
          call += 1;
          return call === 1 ? older.promise : newer.promise;
        }};
        const previousTruth = document.createElement("p");
        previousTruth.textContent = "previous broker truth";
        elements[{target_id!r}].appendChild(previousTruth);

        const manualRefresh = module[{function_name!r}]();
        const invalidatedAtStart = (
          elements[{target_id!r}].textContent.includes("Refreshing")
          && !elements[{target_id!r}].textContent.includes(
            "previous broker truth",
          )
        );
        const timerRefresh = module[{function_name!r}]();
        const firstWasAborted = Boolean(
          requests[0]
          && requests[0].signal
          && requests[0].signal.aborted === true
        );

        if ({completion_order!r} === "older_first") {{
          older.resolve({older_payload});
          await manualRefresh;
          newer.resolve({newer_payload});
          await timerRefresh;
        }} else {{
          newer.resolve({newer_payload});
          await timerRefresh;
          older.resolve({older_payload});
          await manualRefresh;
        }}

        const rendered = elements[{target_id!r}].textContent;
        const failures = [];
        if (!invalidatedAtStart) {{
          failures.push("refresh retained stale visible truth while loading");
        }}
        if (!firstWasAborted) {{
          failures.push("superseded request was not aborted");
        }}
        if (
          !rendered.includes({newer_evidence!r})
          || rendered.includes({older_evidence!r})
        ) {{
          failures.push(`rendered stale generation: ${{rendered}}`);
        }}
        if (failures.length) throw new Error(failures.join("; "));
        """,
    )


@pytest.mark.parametrize(
    (
        "function_name",
        "path",
        "target_id",
        "extra_ids",
        "newer_payload",
        "newer_evidence",
    ),
    (
        (
            "refreshAccount",
            "/account",
            "account-status",
            (
                "account-equity",
                "account-buying-power",
                "account-cash",
                "account-exposure",
                "proof-data",
            ),
            (
                '{equity: "200", buying_power: "300", cash: "160", '
                'gross_exposure: "40", positions: [{ticker: "NEW1", '
                'qty: "1", avg_entry_price: "20", current_price: "20", '
                'market_value: "20"}, {ticker: "NEW2", qty: "1", '
                'avg_entry_price: "20", current_price: "20", '
                'market_value: "20"}], '
                'observed_at: new Date().toISOString()}'
            ),
            "2 open broker positions",
        ),
        (
            "refreshPositions",
            "/positions",
            "positions",
            (),
            (
                '{positions: [{ticker: "CURRENT-POS", qty: "2", '
                'avg_entry_price: "20", current_price: "21", '
                'market_value: "42"}]}'
            ),
            "CURRENT-POS",
        ),
        (
            "refreshHoldings",
            "/holdings",
            "holdings",
            ("external-stale",),
            (
                '{alpaca: [{ticker: "CURRENT-HOLD", source: "alpaca", '
                'read_only: false, qty: "2", market_value: "42"}], '
                'external: [], combined_by_ticker: '
                '{"CURRENT-HOLD": "42"}, external_stale: false, '
                "external_available: true}"
            ),
            "CURRENT-HOLD",
        ),
        (
            "refreshRiskLog",
            "/log",
            "risk-log",
            (),
            (
                '{risk_events: [{at: "2026-07-25T10:01:00Z", '
                'type: "new", reason: "CURRENT-RISK"}]}'
            ),
            "CURRENT-RISK",
        ),
    ),
)
def test_operational_refreshes_ignore_late_error_after_newer_success(
    function_name,
    path,
    target_id,
    extra_ids,
    newer_payload,
    newer_evidence,
):
    ids = [target_id, *extra_ids]
    _run_page_module(
        _STATIC / "js" / "index.js",
        (function_name,),
        f"""
        const elements = installDom({ids!r});
        const older = deferred();
        const newer = deferred();
        let call = 0;
        globalThis.__api = (requestPath, _options = {{}}) => {{
          if (requestPath !== {path!r}) {{
            throw new Error(`unexpected API path ${{requestPath}}`);
          }}
          call += 1;
          return call === 1 ? older.promise : newer.promise;
        }};

        const oldRefresh = module[{function_name!r}]();
        const newRefresh = module[{function_name!r}]();
        newer.resolve({newer_payload});
        await newRefresh;
        older.reject(new Error("superseded request failed late"));
        const oldResult = await oldRefresh.then(
          () => "ignored",
          () => "rejected",
        );

        if (oldResult === "rejected") {{
          throw new Error("superseded error escaped to the current refresh");
        }}
        const rendered = elements[{target_id!r}].textContent;
        if (!rendered.includes({newer_evidence!r})) {{
          throw new Error(`late error erased newer truth: ${{rendered}}`);
        }}
        """,
    )


@pytest.mark.parametrize(
    (
        "script",
        "function_name",
        "path",
        "target_id",
        "older_payload",
        "newer_payload",
        "older_evidence",
        "newer_evidence",
    ),
    (
        (
            "plans.js",
            "refreshPlans",
            "/plans",
            "plans-list",
            (
                '{plans: [{plan_id: 1, symbol: "OLD-PLAN", '
                'action: "buy", status: "proposed"}]}'
            ),
            (
                '{plans: [{plan_id: 2, symbol: "NEW-PLAN", '
                'action: "sell", status: "approved"}]}'
            ),
            "OLD-PLAN",
            "NEW-PLAN",
        ),
        (
            "backtests.js",
            "refreshRuns",
            "/backtests",
            "backtest-runs",
            (
                '{backtests: [{run_id: 1, label: "OLD-RUN", '
                'created_at: "2026-07-25T10:00:00Z"}]}'
            ),
            (
                '{backtests: [{run_id: 2, label: "NEW-RUN", '
                'created_at: "2026-07-25T10:01:00Z"}]}'
            ),
            "OLD-RUN",
            "NEW-RUN",
        ),
    ),
)
def test_saved_resource_refresh_ignores_superseded_success(
    script,
    function_name,
    path,
    target_id,
    older_payload,
    newer_payload,
    older_evidence,
    newer_evidence,
):
    _run_page_module(
        _STATIC / "js" / script,
        (function_name,),
        f"""
        const elements = installDom([{target_id!r}]);
        const older = deferred();
        const newer = deferred();
        const requests = [];
        let call = 0;
        globalThis.__api = (requestPath, options = {{}}) => {{
          if (requestPath !== {path!r}) {{
            throw new Error(`unexpected API path ${{requestPath}}`);
          }}
          requests.push(options);
          call += 1;
          return call === 1 ? older.promise : newer.promise;
        }};
        const stale = document.createElement("p");
        stale.textContent = "previous saved resource truth";
        elements[{target_id!r}].appendChild(stale);

        const manualRefresh = module[{function_name!r}]();
        const invalidatedAtStart = (
          elements[{target_id!r}].textContent.includes("Refreshing")
          && !elements[{target_id!r}].textContent.includes("previous")
        );
        const actionRefresh = module[{function_name!r}]();
        const firstWasAborted = Boolean(
          requests[0].signal
          && requests[0].signal.aborted === true
        );
        newer.resolve({newer_payload});
        await actionRefresh;
        older.resolve({older_payload});
        await manualRefresh;

        const rendered = elements[{target_id!r}].textContent;
        const failures = [];
        if (!invalidatedAtStart) {{
          failures.push("saved resource was not invalidated while loading");
        }}
        if (!firstWasAborted) {{
          failures.push("superseded saved-resource request was not aborted");
        }}
        if (
          !rendered.includes({newer_evidence!r})
          || rendered.includes({older_evidence!r})
        ) {{
          failures.push(`stale saved resource rendered: ${{rendered}}`);
        }}
        if (failures.length) throw new Error(failures.join("; "));
        """,
    )


@pytest.mark.parametrize(
    (
        "script",
        "function_name",
        "path",
        "target_id",
        "newer_payload",
        "newer_evidence",
    ),
    (
        (
            "plans.js",
            "refreshPlans",
            "/plans",
            "plans-list",
            (
                '{plans: [{plan_id: 2, symbol: "CURRENT-PLAN", '
                'action: "sell", status: "approved"}]}'
            ),
            "CURRENT-PLAN",
        ),
        (
            "backtests.js",
            "refreshRuns",
            "/backtests",
            "backtest-runs",
            (
                '{backtests: [{run_id: 2, label: "CURRENT-RUN", '
                'created_at: "2026-07-25T10:01:00Z"}]}'
            ),
            "CURRENT-RUN",
        ),
    ),
)
def test_saved_resource_refresh_ignores_superseded_error(
    script,
    function_name,
    path,
    target_id,
    newer_payload,
    newer_evidence,
):
    _run_page_module(
        _STATIC / "js" / script,
        (function_name,),
        f"""
        const elements = installDom([{target_id!r}]);
        const older = deferred();
        const newer = deferred();
        let call = 0;
        globalThis.__api = (requestPath, _options = {{}}) => {{
          if (requestPath !== {path!r}) {{
            throw new Error(`unexpected API path ${{requestPath}}`);
          }}
          call += 1;
          return call === 1 ? older.promise : newer.promise;
        }};

        const oldRefresh = module[{function_name!r}]();
        const newRefresh = module[{function_name!r}]();
        newer.resolve({newer_payload});
        await newRefresh;
        older.reject(new Error("late saved resource failure"));
        const oldResult = await oldRefresh.then(
          () => "ignored",
          () => "rejected",
        );

        if (oldResult === "rejected") {{
          throw new Error("superseded saved-resource error escaped");
        }}
        if (!elements[{target_id!r}].textContent.includes(
          {newer_evidence!r},
        )) {{
          throw new Error("late error erased current saved resource");
        }}
        """,
    )


def test_pending_refresh_ignores_superseded_error_after_newer_truth():
    _run_page_module(
        _STATIC / "js" / "index.js",
        ("refreshPending",),
        r"""
        const elements = installDom([
          "pending-list",
          "approval-reason",
          "approval-confirm-button",
        ]);
        const older = deferred();
        const newer = deferred();
        const requests = [];
        let call = 0;
        globalThis.__api = (path, options = {}) => {
          if (path !== "/pending") {
            throw new Error(`unexpected API path ${path}`);
          }
          requests.push(options);
          call += 1;
          return call === 1 ? older.promise : newer.promise;
        };

        const oldRefresh = module.refreshPending();
        const newRefresh = module.refreshPending();
        const firstWasAborted = Boolean(
          requests[0].signal
          && requests[0].signal.aborted === true
        );
        newer.resolve({pending: []});
        await newRefresh;
        older.reject(new Error("late pending failure"));
        const oldResult = await oldRefresh.then(
          () => "ignored",
          () => "rejected",
        );

        if (!firstWasAborted) {
          throw new Error("superseded pending request was not aborted");
        }
        if (oldResult === "rejected") {
          throw new Error("superseded pending error escaped");
        }
        if (
          !elements["pending-list"].textContent.includes(
            "No verified pending proposals",
          )
        ) {
          throw new Error("late pending error erased current truth");
        }
        """,
    )


_BACKTEST_REPORT_DOM_SETUP = r"""
    const elements = installDom([
      "backtest-report",
      "report-title",
      "status-region",
    ]);
    const report = (runId, label, symbol, evidence) => ({
      run_id: runId,
      label,
      disclaimer: evidence,
      rows: [{
        symbol,
        strategy: `${label}-strategy`,
        window: "walk-forward",
        metrics: {
          total_return_pct: 1,
          sharpe: 1,
          max_drawdown_pct: -1,
          num_trades: 1,
          exposure_pct: 50,
          pnl_by_regime: {},
        },
        benchmark_buy_and_hold: {total_return_pct: 0},
        beat_buy_and_hold: true,
      }],
    });
"""


@pytest.mark.parametrize(
    "completion_order",
    ("older_first", "newer_first"),
)
def test_backtest_report_is_bound_to_latest_selected_run(
    completion_order,
):
    _run_page_module(
        _STATIC / "js" / "backtests.js",
        ("showReport",),
        _BACKTEST_REPORT_DOM_SETUP
        + f"""
        const older = deferred();
        const newer = deferred();
        const requests = [];
        globalThis.__api = (path, options = {{}}) => {{
          requests.push({{path, options}});
          if (path === "/backtests/1/report") return older.promise;
          if (path === "/backtests/2/report") return newer.promise;
          throw new Error(`unexpected API path ${{path}}`);
        }};
        elements["report-title"].textContent = "Report #99 · stale";

        const oldSelection = module.showReport(1);
        const newSelection = module.showReport(2);
        const invalidatedWhileLoading = (
          elements["report-title"].textContent.includes("#2")
          && elements["backtest-report"].textContent.includes("Loading")
        );
        const firstWasAborted = Boolean(
          requests[0].options.signal
          && requests[0].options.signal.aborted === true
        );

        if ({completion_order!r} === "older_first") {{
          older.resolve(report(1, "Alpha", "AAPL", "ALPHA-EVIDENCE"));
          await oldSelection;
          newer.resolve(report(2, "Beta", "MSFT", "BETA-EVIDENCE"));
          await newSelection;
        }} else {{
          newer.resolve(report(2, "Beta", "MSFT", "BETA-EVIDENCE"));
          await newSelection;
          older.resolve(report(1, "Alpha", "AAPL", "ALPHA-EVIDENCE"));
          await oldSelection;
        }}

        const title = elements["report-title"].textContent;
        const body = elements["backtest-report"].textContent;
        const failures = [];
        if (!invalidatedWhileLoading) {{
          failures.push("selected report title was not invalidated");
        }}
        if (!firstWasAborted) {{
          failures.push("superseded report request was not aborted");
        }}
        if (!title.includes("#2") || !title.includes("Beta")) {{
          failures.push(`title does not match run 2: ${{title}}`);
        }}
        if (
          !body.includes("BETA-EVIDENCE")
          || !body.includes("MSFT")
          || body.includes("ALPHA-EVIDENCE")
          || body.includes("AAPL")
        ) {{
          failures.push(`evidence does not match run 2: ${{body}}`);
        }}
        if (failures.length) throw new Error(failures.join("; "));
        """,
    )


def test_backtest_report_ignores_late_error_after_newer_selection():
    _run_page_module(
        _STATIC / "js" / "backtests.js",
        ("showReport",),
        _BACKTEST_REPORT_DOM_SETUP
        + r"""
        const older = deferred();
        const newer = deferred();
        globalThis.__api = (path, _options = {}) => {
          if (path === "/backtests/1/report") return older.promise;
          if (path === "/backtests/2/report") return newer.promise;
          throw new Error(`unexpected API path ${path}`);
        };

        const oldSelection = module.showReport(1);
        const newSelection = module.showReport(2);
        newer.resolve(report(2, "Beta", "MSFT", "BETA-EVIDENCE"));
        await newSelection;
        older.reject(new Error("late Alpha failure"));
        const oldResult = await oldSelection.then(
          () => "ignored",
          () => "rejected",
        );

        if (oldResult === "rejected") {
          throw new Error("superseded report error escaped");
        }
        if (
          !elements["report-title"].textContent.includes("#2")
          || !elements["backtest-report"].textContent.includes(
            "BETA-EVIDENCE",
          )
        ) {
          throw new Error("late report error erased run 2");
        }
        """,
    )


def test_backtest_report_repeated_same_run_uses_request_generation():
    _run_page_module(
        _STATIC / "js" / "backtests.js",
        ("showReport",),
        _BACKTEST_REPORT_DOM_SETUP
        + r"""
        const older = deferred();
        const newer = deferred();
        let call = 0;
        globalThis.__api = (path, _options = {}) => {
          if (path !== "/backtests/7/report") {
            throw new Error(`unexpected API path ${path}`);
          }
          call += 1;
          return call === 1 ? older.promise : newer.promise;
        };

        const oldSelection = module.showReport(7);
        const newSelection = module.showReport(7);
        newer.resolve(report(7, "Fresh", "MSFT", "FRESH-EVIDENCE"));
        await newSelection;
        older.resolve(report(7, "Stale", "AAPL", "STALE-EVIDENCE"));
        await oldSelection;

        const title = elements["report-title"].textContent;
        const body = elements["backtest-report"].textContent;
        if (
          !title.includes("Fresh")
          || !body.includes("FRESH-EVIDENCE")
          || body.includes("STALE-EVIDENCE")
        ) {
          throw new Error("same-run stale request replaced fresh evidence");
        }
        """,
    )


def test_backtest_report_response_identity_mismatch_fails_closed():
    _run_page_module(
        _STATIC / "js" / "backtests.js",
        ("showReport",),
        _BACKTEST_REPORT_DOM_SETUP
        + r"""
        globalThis.__api = (path) => {
          if (path !== "/backtests/2/report") {
            throw new Error(`unexpected API path ${path}`);
          }
          return Promise.resolve(
            report(1, "Alpha", "AAPL", "ALPHA-EVIDENCE"),
          );
        };

        await module.showReport(2);

        const title = elements["report-title"].textContent;
        const body = elements["backtest-report"].textContent;
        if (
          !title.includes("#2")
          || !body.toLowerCase().includes("identity")
          || body.includes("ALPHA-EVIDENCE")
          || body.includes("AAPL")
        ) {
          throw new Error(
            `mismatched report was rendered: ${title} / ${body}`,
          );
        }
        """,
    )


def test_starting_backtest_invalidates_prior_report_request():
    _run_page_module(
        _STATIC / "js" / "backtests.js",
        ("showReport", "submitBacktest"),
        _BACKTEST_REPORT_DOM_SETUP
        + r"""
        elements["backtest-reason"] = document.createElement("textarea");
        elements["backtest-reason"].value = "new simulation evidence";
        elements["backtest-runs"] = document.createElement("div");
        const priorReport = deferred();
        const runRequest = deferred();
        globalThis.__api = (path, _options = {}) => {
          if (path === "/backtests/1/report") return priorReport.promise;
          if (path === "/backtests/run") return runRequest.promise;
          if (path === "/backtests") {
            return Promise.resolve({backtests: []});
          }
          if (path === "/backtests/2/report") {
            return Promise.resolve(
              report(2, "Beta", "MSFT", "BETA-EVIDENCE"),
            );
          }
          throw new Error(`unexpected API path ${path}`);
        };
        document.getElementById = (id) => elements[id] || null;

        const oldSelection = module.showReport(1);
        const run = module.submitBacktest({preventDefault() {}});
        priorReport.resolve(
          report(1, "Alpha", "AAPL", "ALPHA-EVIDENCE"),
        );
        await oldSelection;
        if (
          !elements["backtest-report"].textContent.includes("Running")
          || elements["backtest-report"].textContent.includes(
            "ALPHA-EVIDENCE",
          )
        ) {
          throw new Error("prior report replaced active backtest state");
        }

        runRequest.resolve({run_id: 2});
        await run;
        if (
          !elements["report-title"].textContent.includes("#2")
          || !elements["backtest-report"].textContent.includes(
            "BETA-EVIDENCE",
          )
        ) {
          throw new Error("completed run did not bind report 2");
        }
        """,
    )


def test_newer_report_selection_wins_over_older_backtest_completion():
    _run_page_module(
        _STATIC / "js" / "backtests.js",
        ("showReport", "submitBacktest"),
        _BACKTEST_REPORT_DOM_SETUP
        + r"""
        elements["backtest-reason"] = document.createElement("textarea");
        elements["backtest-reason"].value = "new simulation evidence";
        elements["backtest-runs"] = document.createElement("div");
        const runRequest = deferred();
        globalThis.__api = (path, _options = {}) => {
          if (path === "/backtests/run") return runRequest.promise;
          if (path === "/backtests/3/report") {
            return Promise.resolve(
              report(3, "Gamma", "NVDA", "GAMMA-EVIDENCE"),
            );
          }
          if (path === "/backtests") {
            return Promise.resolve({backtests: []});
          }
          if (path === "/backtests/2/report") {
            return Promise.resolve(
              report(2, "Beta", "MSFT", "BETA-EVIDENCE"),
            );
          }
          throw new Error(`unexpected API path ${path}`);
        };
        document.getElementById = (id) => elements[id] || null;

        const run = module.submitBacktest({preventDefault() {}});
        await module.showReport(3);
        runRequest.resolve({run_id: 2});
        await run;

        const title = elements["report-title"].textContent;
        const body = elements["backtest-report"].textContent;
        if (
          !title.includes("#3")
          || !body.includes("GAMMA-EVIDENCE")
          || body.includes("BETA-EVIDENCE")
        ) {
          throw new Error(
            "older backtest completion replaced newer explicit selection",
          );
        }
        """,
    )


_PLAN_DOM_SETUP = r"""
    const elements = installDom([
      "plan-detail",
      "plan-detail-title",
      "plan-approval-dialog",
      "plan-approval-reason",
      "plan-approval-submit",
      "plan-approval-target-id",
      "plan-approval-target-symbol",
      "plan-approval-target-action",
      "plan-cancel-dialog",
      "plan-cancel-reason",
      "status-region",
    ]);
    const planDetail = (planId, symbol) => ({
      plan_id: planId,
      authority_digest: `digest-${planId}-${symbol}`,
      authority_version: 1,
      review_token: `plan:${planId}:authority:v1:digest-${planId}-${symbol}`,
      symbol,
      status: "proposed",
      paper_only: true,
      plan: {
        action: "buy",
        confidence: 0.8,
        regime_note: "test regime",
        thesis: `${symbol} test thesis`,
        scenarios: [],
        exit_plan: {
          targets: [],
          stop: "90.000000",
          trailing_stop_pct: null,
          time_stop_days: 30,
        },
      },
      sized: {
        total_shares: 2,
        risk_budget: "20.000000",
        tranches: [],
      },
    });
"""


def test_latest_analysis_submission_owns_plan_detail_workspace():
    _run_page_module(
        _STATIC / "js" / "plans.js",
        ("submitAnalysis",),
        _PLAN_DOM_SETUP
        + r"""
        elements["analysis-symbol"] = document.createElement("input");
        elements["analysis-reason"] = document.createElement("textarea");
        elements["plans-list"] = document.createElement("div");
        document.getElementById = (id) => elements[id] || null;
        const older = deferred();
        const newer = deferred();
        let analysisCall = 0;
        globalThis.__api = (path, _options = {}) => {
          if (path === "/analyze") {
            analysisCall += 1;
            return analysisCall === 1 ? older.promise : newer.promise;
          }
          if (path === "/plans") return Promise.resolve({plans: []});
          if (path === "/plans/1") {
            return Promise.resolve(planDetail(1, "AAPL"));
          }
          if (path === "/plans/2") {
            return Promise.resolve(planDetail(2, "MSFT"));
          }
          throw new Error(`unexpected API path ${path}`);
        };

        elements["analysis-symbol"].value = "AAPL";
        elements["analysis-reason"].value = "older analysis";
        const oldSubmission = module.submitAnalysis({
          preventDefault() {},
        });
        elements["analysis-symbol"].value = "MSFT";
        elements["analysis-reason"].value = "newer analysis";
        const newSubmission = module.submitAnalysis({
          preventDefault() {},
        });

        newer.resolve({plan_id: 2});
        await newSubmission;
        older.resolve({plan_id: 1});
        await oldSubmission;

        if (
          !elements["plan-detail-title"].textContent.startsWith(
            "Plan #2 · MSFT",
          )
          || elements["plan-detail-title"].textContent.includes("AAPL")
        ) {
          throw new Error("older analysis replaced newer plan detail");
        }
        """,
    )


def test_latest_proposal_submission_owns_plan_detail_workspace():
    _run_page_module(
        _STATIC / "js" / "plans.js",
        ("submitProposals",),
        _PLAN_DOM_SETUP
        + r"""
        elements["proposal-reason"] = document.createElement("textarea");
        elements["plans-list"] = document.createElement("div");
        document.getElementById = (id) => elements[id] || null;
        const older = deferred();
        const newer = deferred();
        let proposalCall = 0;
        globalThis.__api = (path, _options = {}) => {
          if (path === "/propose") {
            proposalCall += 1;
            return proposalCall === 1 ? older.promise : newer.promise;
          }
          if (path === "/plans") return Promise.resolve({plans: []});
          throw new Error(`unexpected API path ${path}`);
        };

        elements["proposal-reason"].value = "older proposal scan";
        const oldSubmission = module.submitProposals({
          preventDefault() {},
        });
        elements["proposal-reason"].value = "newer proposal scan";
        const newSubmission = module.submitProposals({
          preventDefault() {},
        });

        newer.resolve({
          note: "NEW-PROPOSAL-EVIDENCE",
          proposed: [],
        });
        await newSubmission;
        older.resolve({
          note: "OLD-PROPOSAL-EVIDENCE",
          proposed: [],
        });
        await oldSubmission;

        const rendered = elements["plan-detail"].textContent;
        if (
          !rendered.includes("NEW-PROPOSAL-EVIDENCE")
          || rendered.includes("OLD-PROPOSAL-EVIDENCE")
        ) {
          throw new Error("older proposal scan replaced newer evidence");
        }
        """,
    )


def test_latest_screen_request_owns_candidate_results():
    _run_page_module(
        _STATIC / "js" / "plans.js",
        ("runScreen",),
        r"""
        const elements = installDom([
          "screen-results",
          "analysis-symbol",
          "analysis-reason",
        ]);
        const older = deferred();
        const newer = deferred();
        const requests = [];
        let call = 0;
        globalThis.__api = (path, options = {}) => {
          if (path !== "/screen") {
            throw new Error(`unexpected API path ${path}`);
          }
          requests.push(options);
          call += 1;
          return call === 1 ? older.promise : newer.promise;
        };

        const oldScreen = module.runScreen();
        const newScreen = module.runScreen();
        const firstWasAborted = Boolean(
          requests[0].signal
          && requests[0].signal.aborted === true
        );
        newer.resolve({
          candidates: [{
            symbol: "NEW-SCREEN",
            score: "2",
            regime: "new",
          }],
        });
        await newScreen;
        older.resolve({
          candidates: [{
            symbol: "OLD-SCREEN",
            score: "1",
            regime: "old",
          }],
        });
        await oldScreen;

        const rendered = elements["screen-results"].textContent;
        if (!firstWasAborted) {
          throw new Error("superseded screen request was not aborted");
        }
        if (
          !rendered.includes("NEW-SCREEN")
          || rendered.includes("OLD-SCREEN")
        ) {
          throw new Error("older screen response replaced newer candidates");
        }
        """,
    )


def test_screen_ignores_late_error_after_newer_success():
    _run_page_module(
        _STATIC / "js" / "plans.js",
        ("runScreen",),
        r"""
        const elements = installDom([
          "screen-results",
          "analysis-symbol",
          "analysis-reason",
        ]);
        const older = deferred();
        const newer = deferred();
        let call = 0;
        globalThis.__api = (path) => {
          if (path !== "/screen") {
            throw new Error(`unexpected API path ${path}`);
          }
          call += 1;
          return call === 1 ? older.promise : newer.promise;
        };

        const oldScreen = module.runScreen();
        const newScreen = module.runScreen();
        newer.resolve({
          candidates: [{
            symbol: "CURRENT-SCREEN",
            score: "2",
            regime: "new",
          }],
        });
        await newScreen;
        older.reject(new Error("late screen failure"));
        const oldResult = await oldScreen.then(
          () => "ignored",
          () => "rejected",
        );

        if (oldResult === "rejected") {
          throw new Error("superseded screen error escaped");
        }
        if (
          !elements["screen-results"].textContent.includes(
            "CURRENT-SCREEN",
          )
        ) {
          throw new Error("late screen error erased current candidates");
        }
        """,
    )


def test_plan_detail_and_approval_ignore_out_of_order_other_plan():
    _run_page_module(
        _STATIC / "js" / "plans.js",
        (
            "showPlan",
            "submitPlanApproval",
        ),
        _PLAN_DOM_SETUP
        + r"""
        const a = deferred();
        const b = deferred();
        const approval = deferred();
        const calls = [];
        globalThis.__api = (path, options = {}) => {
          calls.push({path, options});
          if (path === "/plans/1") return a.promise;
          if (path === "/plans/2") return b.promise;
          if (path === "/plans/2/approve") return approval.promise;
          throw new Error(`unexpected API path ${path}`);
        };

        module.showPlan(1);
        module.showPlan(2);
        b.resolve(planDetail(2, "MSFT"));
        await flush();
        a.resolve(planDetail(1, "AAPL"));
        await flush();

        const approve = findButton(
          elements["plan-detail"],
          "Review plan approval",
        );
        if (!approve) throw new Error("current plan has no approval action");
        approve.click();
        elements["plan-approval-reason"].value = "reviewed MSFT plan";
        module.submitPlanApproval({preventDefault() {}});
        await flush();

        const submitted = calls.filter((call) => call.path.endsWith("/approve"));
        const failures = [];
        if (!elements["plan-detail-title"].textContent.startsWith("Plan #2 · MSFT")) {
          failures.push(`detail ${elements["plan-detail-title"].textContent}`);
        }
        if (elements["plan-approval-target-id"].textContent !== "2") {
          failures.push(
            `dialog plan ${elements["plan-approval-target-id"].textContent}`,
          );
        }
        if (elements["plan-approval-target-symbol"].textContent !== "MSFT") {
          failures.push("dialog symbol did not remain MSFT");
        }
        if (submitted.length !== 1 || submitted[0].path !== "/plans/2/approve") {
          failures.push(`submitted ${submitted.map((call) => call.path)}`);
        }
        const submittedBody = submitted.length
          ? JSON.parse(submitted[0].options.body)
          : {};
        if (
          submittedBody.review_token
          !== "plan:2:authority:v1:digest-2-MSFT"
        ) {
          failures.push(`review token ${submittedBody.review_token}`);
        }
        if (failures.length) throw new Error(failures.join("; "));
        """,
    )


def test_plan_approval_target_is_separate_from_newly_viewed_detail():
    _run_page_module(
        _STATIC / "js" / "plans.js",
        (
            "showPlan",
            "submitPlanApproval",
        ),
        _PLAN_DOM_SETUP
        + r"""
        const calls = [];
        globalThis.__api = (path, options = {}) => {
          calls.push({path, options});
          if (path === "/plans/1") {
            return Promise.resolve(planDetail(1, "AAPL"));
          }
          if (path === "/plans/2") {
            return Promise.resolve(planDetail(2, "MSFT"));
          }
          if (path.endsWith("/approve")) return Promise.resolve({});
          throw new Error(`unexpected API path ${path}`);
        };

        await module.showPlan(1);
        findButton(elements["plan-detail"], "Review plan approval").click();
        await module.showPlan(2);
        elements["plan-approval-reason"].value = "stale A dialog";
        module.submitPlanApproval({preventDefault() {}});
        await flush();

        if (calls.some((call) => call.path.endsWith("/approve"))) {
          throw new Error("A dialog submitted after detail switched to B");
        }
        """,
    )


def test_plan_approval_close_poisons_target_before_reopen():
    _run_page_module(
        _STATIC / "js" / "plans.js",
        (
            "showPlan",
            "submitPlanApproval",
            "closeDialog",
        ),
        _PLAN_DOM_SETUP
        + r"""
        const calls = [];
        globalThis.__api = (path, options = {}) => {
          calls.push({path, options});
          if (path === "/plans/1") {
            return Promise.resolve(planDetail(1, "AAPL"));
          }
          if (path === "/plans/2") {
            return Promise.resolve(planDetail(2, "MSFT"));
          }
          if (path.endsWith("/approve")) return Promise.resolve({});
          throw new Error(`unexpected API path ${path}`);
        };

        await module.showPlan(1);
        findButton(elements["plan-detail"], "Review plan approval").click();
        module.closeDialog(elements["plan-approval-dialog"]);
        elements["plan-approval-reason"].value = "closed dialog";
        module.submitPlanApproval({preventDefault() {}});
        await flush();
        if (calls.some((call) => call.path.endsWith("/approve"))) {
          throw new Error("closed approval dialog retained an actionable target");
        }

        await module.showPlan(2);
        findButton(elements["plan-detail"], "Review plan approval").click();
        if (elements["plan-approval-target-id"].textContent !== "2") {
          throw new Error("reopened dialog did not bind immutable plan 2");
        }
        """,
    )


def test_plan_approval_blocks_double_submit():
    _run_page_module(
        _STATIC / "js" / "plans.js",
        (
            "showPlan",
            "submitPlanApproval",
        ),
        _PLAN_DOM_SETUP
        + r"""
        const approval = deferred();
        const calls = [];
        globalThis.__api = (path, options = {}) => {
          calls.push({path, options});
          if (path === "/plans/1") {
            return Promise.resolve(planDetail(1, "AAPL"));
          }
          if (path === "/plans/1/approve") return approval.promise;
          throw new Error(`unexpected API path ${path}`);
        };

        await module.showPlan(1);
        findButton(elements["plan-detail"], "Review plan approval").click();
        elements["plan-approval-reason"].value = "single plan approval";
        module.submitPlanApproval({preventDefault() {}});
        module.submitPlanApproval({preventDefault() {}});
        await flush();

        const approvals = calls.filter(
          (call) => call.path === "/plans/1/approve",
        );
        if (approvals.length !== 1) {
          throw new Error(`plan approval submitted ${approvals.length} times`);
        }
        if (elements["plan-approval-reason"].value !== "") {
          throw new Error("plan approval state was not cleared after submit");
        }
        """,
    )


def test_plan_detail_response_id_mismatch_fails_closed():
    _run_page_module(
        _STATIC / "js" / "plans.js",
        ("showPlan",),
        _PLAN_DOM_SETUP
        + r"""
        globalThis.__api = (path) => {
          if (path === "/plans/2") {
            return Promise.resolve(planDetail(1, "AAPL"));
          }
          throw new Error(`unexpected API path ${path}`);
        };

        await module.showPlan(2);
        if (findButton(elements["plan-detail"], "Review plan approval")) {
          throw new Error("mismatched detail response exposed approval");
        }
        if (!elements["plan-detail"].textContent.toLowerCase().includes("mismatch")) {
          throw new Error("mismatched plan identity was not explained");
        }
        """,
    )


_HEALTH_DOM_SETUP = r"""
    const elements = installDom([
      "truth-broker",
      "truth-mode",
      "truth-database",
      "truth-daemon",
      "truth-safety",
      "truth-equity-breaker",
      "truth-crypto-breaker",
      "breaker-scope",
      "breaker-generation",
      "breaker-health",
      "breaker-reset-reason",
      "breaker-reset-button",
      "critical-banner",
      "critical-banner-title",
      "critical-banner-message",
      "status-region",
      "receipt-panel",
      "proof-broker",
      "proof-daemon",
      "proof-reconciliation",
      "proof-safety",
    ]);
    elements["breaker-scope"].value = "";
    const emptyUnsafeLocalState = () => ({
      live_or_unknown_order_ids: [],
      latched_order_ids: [],
      unsafe_fill_ids: [],
      active_rule_ids: [],
      unsafe_rule_group_ids: [],
      unknown_categories: [],
    });
    const safetyTruth = ({
      observedAt = new Date().toISOString(),
      state = "unsafe",
      complete = true,
      globalTripped = false,
      globalGeneration = 0,
      activeBreakers = [{
        scope: "loss:equity",
        kind: "loss",
        target: "equity",
        generation: 7,
      }],
      unsafeLocalState = emptyUnsafeLocalState(),
      unknownCategories = [],
    } = {}) => ({
      observed_at: observedAt,
      state,
      complete,
      local_enumeration: unsafeLocalState.unknown_categories.length
        ? "unknown"
        : "confirmed",
      remote_broker_open_orders: "unverified",
      operator_global_breaker: {
        tripped: globalTripped,
        generation: globalGeneration,
      },
      active_breakers: activeBreakers,
      unsafe_local_state: unsafeLocalState,
      unknown_categories: unknownCategories,
    });
    const validHealth = (generation = 7) => {
      const observedAt = new Date().toISOString();
      const activeBreakers = [{
        scope: "loss:equity",
        kind: "loss",
        target: "equity",
        generation,
      }];
      return {
        broker: "Alpaca",
        mode: "paper",
        observed_at: observedAt,
        db_ok: true,
        heartbeat_age_seconds: 1.5,
        daemon_alive: true,
        killswitch: {
          equity: true,
          crypto: false,
        },
        killswitch_generation: {
          equity: generation,
          crypto: 3,
        },
        active_breakers: activeBreakers,
        broker_contact_evidence_valid: true,
        reconciliation_age_seconds: 1.5,
        reconciliation_max_age_seconds: 300,
        safety: safetyTruth({
          observedAt,
          activeBreakers,
        }),
      };
    };
    const assertUnknownAndDisabled = () => {
      for (const id of [
        "truth-database",
        "truth-daemon",
        "truth-safety",
        "truth-equity-breaker",
        "truth-crypto-breaker",
      ]) {
        if (!elements[id].textContent.includes("Unknown")) {
          throw new Error(`${id} retained ${elements[id].textContent}`);
        }
        if (elements[id].className.includes("verified")) {
          throw new Error(`${id} remained verified`);
        }
      }
      if (!elements["breaker-reset-button"].disabled) {
        throw new Error("breaker reset remained enabled");
      }
      if (elements["critical-banner"].hidden) {
        throw new Error("unverified safety banner was hidden");
      }
      if (
        elements["critical-banner-title"].textContent
        !== "Safety state unverified"
      ) {
        throw new Error("unverified safety title was not restored");
      }
    };
"""


def test_reconciliation_proof_marks_aged_contact_stale():
    _run_page_module(
        _STATIC / "js" / "index.js",
        ("refreshHealth",),
        _HEALTH_DOM_SETUP
        + r"""
        globalThis.__api = (path) => {
          if (path !== "/health") throw new Error(`unexpected API path ${path}`);
          return Promise.resolve({
            ...validHealth(),
            broker_contact_evidence_valid: false,
            reconciliation_age_seconds: 301,
            reconciliation_max_age_seconds: 300,
          });
        };

        await module.refreshHealth();

        const proof = elements["proof-reconciliation"];
        if (!proof.textContent.includes("Stale")) {
          throw new Error(`aged evidence was not labeled stale: ${proof.textContent}`);
        }
        if (!proof.className.includes("caution")) {
          throw new Error("aged evidence retained a verified proof class");
        }
        """,
    )


@pytest.mark.parametrize(
    "invalid_health",
    (
        (
            '{broker: "Alpaca", mode: "paper", db_ok: false, '
            'error: "database_unavailable"}'
        ),
        (
            '{broker: "Alpaca", mode: "paper", db_ok: true, '
            "daemon_alive: true, heartbeat_age_seconds: 1.5}"
        ),
        (
            "({...validHealth(), observed_at: "
            "new Date(Date.now() - 120000).toISOString()})"
        ),
        (
            "(() => { const health = validHealth(); "
            "health.safety.observed_at = "
            "new Date(Date.now() - 1000).toISOString(); "
            "return health; })()"
        ),
        (
            "({...validHealth(), safety: {...validHealth().safety, "
            "active_breakers: [{scope: \"loss:equity\", "
            "kind: \"operator_global\", target: \"\", generation: 7}]}})"
        ),
    ),
)
def test_breaker_health_invalid_or_incomplete_response_is_unknown(
    invalid_health,
):
    _run_page_module(
        _STATIC / "js" / "index.js",
        (
            "refreshHealth",
            "updateBreakerReset",
        ),
        _HEALTH_DOM_SETUP
        + f"""
        globalThis.__api = (path) => {{
          if (path === "/health") return Promise.resolve({invalid_health});
          throw new Error(`unexpected API path ${{path}}`);
        }};
        elements["breaker-reset-reason"].value = "must remain disabled";
        await module.refreshHealth();
        module.updateBreakerReset();
        assertUnknownAndDisabled();
        """,
    )


def test_breaker_health_refresh_invalidates_before_network_failure():
    _run_page_module(
        _STATIC / "js" / "index.js",
        (
            "refreshHealth",
            "updateBreakerReset",
        ),
        _HEALTH_DOM_SETUP
        + r"""
        const failure = deferred();
        let call = 0;
        globalThis.__api = (path) => {
          if (path !== "/health") throw new Error(`unexpected API path ${path}`);
          call += 1;
          return call === 1
            ? Promise.resolve(validHealth())
            : failure.promise;
        };

        await module.refreshHealth();
        elements["breaker-reset-reason"].value = "observed healthy";
        module.updateBreakerReset();
        if (elements["breaker-reset-button"].disabled) {
          throw new Error("valid current health did not enable scoped reset");
        }

        const refresh = module.refreshHealth();
        assertUnknownAndDisabled();
        failure.reject(new Error("network unavailable"));
        await refresh.catch(() => {});
        assertUnknownAndDisabled();
        """,
    )


def test_breaker_health_ignores_older_success_after_newer_failure():
    _run_page_module(
        _STATIC / "js" / "index.js",
        (
            "refreshHealth",
            "updateBreakerReset",
        ),
        _HEALTH_DOM_SETUP
        + r"""
        const older = deferred();
        const newer = deferred();
        let call = 0;
        globalThis.__api = (path) => {
          if (path !== "/health") throw new Error(`unexpected API path ${path}`);
          call += 1;
          return call === 1 ? older.promise : newer.promise;
        };

        const oldRefresh = module.refreshHealth();
        const newRefresh = module.refreshHealth();
        newer.reject(new Error("new health failed"));
        await newRefresh.catch(() => {});
        older.resolve(validHealth(9));
        await oldRefresh;
        elements["breaker-reset-reason"].value = "stale result";
        module.updateBreakerReset();
        assertUnknownAndDisabled();
        """,
    )


def test_breaker_health_ignores_older_failure_after_newer_success():
    _run_page_module(
        _STATIC / "js" / "index.js",
        (
            "refreshHealth",
            "updateBreakerReset",
        ),
        _HEALTH_DOM_SETUP
        + r"""
        const older = deferred();
        const newer = deferred();
        let call = 0;
        globalThis.__api = (path) => {
          if (path !== "/health") throw new Error(`unexpected API path ${path}`);
          call += 1;
          return call === 1 ? older.promise : newer.promise;
        };

        const oldRefresh = module.refreshHealth();
        const newRefresh = module.refreshHealth();
        newer.resolve(validHealth(11));
        await newRefresh;
        older.reject(new Error("old health failed"));
        const staleResult = await oldRefresh.then(
          (result) => result,
          () => "rejected",
        );
        if (staleResult === "rejected") {
          throw new Error("older health failure was not ignored");
        }
        elements["breaker-reset-reason"].value = "new health remains current";
        module.updateBreakerReset();

        if (
          elements["truth-equity-breaker"].textContent !== "Tripped · gen 11"
        ) {
          throw new Error("older failure erased newer valid health");
        }
        if (elements["breaker-reset-button"].disabled) {
          throw new Error("newer valid observation was not retained");
        }
        """,
    )


@pytest.mark.parametrize(
    ("safety_expression", "expected_fragment"),
    (
        (
            """safetyTruth({
              globalTripped: true,
              globalGeneration: 4,
              activeBreakers: [{
                scope: "operator_global",
                kind: "operator_global",
                target: "",
                generation: 4,
              }],
            })""",
            "operator_global",
        ),
        (
            """safetyTruth({
              activeBreakers: [],
              unsafeLocalState: {
                ...emptyUnsafeLocalState(),
                latched_order_ids: [41],
              },
            })""",
            "latched orders",
        ),
        (
            """safetyTruth({
              activeBreakers: [],
              unsafeLocalState: {
                ...emptyUnsafeLocalState(),
                unsafe_fill_ids: [77],
              },
            })""",
            "unsafe fills",
        ),
    ),
)
def test_safety_banner_rehydrates_persisted_unsafe_truth_in_new_document(
    safety_expression,
    expected_fragment,
):
    _run_page_module(
        _STATIC / "js" / "index.js",
        ("refreshHealth",),
        _HEALTH_DOM_SETUP
        + f"""
        const health = validHealth();
        health.killswitch = {{equity: false, crypto: false}};
        health.killswitch_generation = {{equity: 0, crypto: 0}};
        health.safety = {safety_expression};
        health.safety.observed_at = health.observed_at;
        health.active_breakers = health.safety.active_breakers;
        globalThis.__api = (path) => {{
          if (path === "/health") return Promise.resolve(health);
          throw new Error(`unexpected API path ${{path}}`);
        }};

        await module.refreshHealth();
        if (elements["critical-banner"].hidden) {{
          throw new Error("persisted unsafe safety banner was hidden");
        }}
        if (
          !elements["critical-banner-message"].textContent
            .toLowerCase()
            .includes({expected_fragment!r})
        ) {{
          throw new Error(
            `missing durable evidence: ${{
              elements["critical-banner-message"].textContent
            }}`,
          );
        }}
        if (!elements["truth-safety"].textContent.includes("Unsafe")) {{
          throw new Error("truth rail did not report unsafe persisted state");
        }}
        """,
    )


def test_safety_banner_hides_only_for_complete_locally_clear_truth():
    _run_page_module(
        _STATIC / "js" / "index.js",
        ("refreshHealth",),
        _HEALTH_DOM_SETUP
        + r"""
        const health = validHealth();
        health.killswitch = {equity: false, crypto: false};
        health.killswitch_generation = {equity: 0, crypto: 0};
        health.active_breakers = [];
        health.safety = safetyTruth({
          observedAt: health.observed_at,
          state: "locally_clear",
          activeBreakers: [],
        });
        globalThis.__api = (path) => {
          if (path === "/health") return Promise.resolve(health);
          throw new Error(`unexpected API path ${path}`);
        };

        await module.refreshHealth();
        if (!elements["critical-banner"].hidden) {
          throw new Error("complete locally clear truth retained critical banner");
        }
        if (
          elements["truth-safety"].textContent
          !== "Locally clear · broker open orders unverified"
        ) {
          throw new Error(
            `locally clear truth overclaimed: ${
              elements["truth-safety"].textContent
            }`,
          );
        }
        """,
    )


def test_breaker_reset_invalidates_health_before_refresh_result():
    _run_page_module(
        _STATIC / "js" / "index.js",
        (
            "refreshHealth",
            "updateBreakerReset",
            "submitBreakerReset",
        ),
        _HEALTH_DOM_SETUP
        + r"""
        const afterReset = deferred();
        let healthCall = 0;
        globalThis.__api = (path) => {
          if (path === "/health") {
            healthCall += 1;
            return healthCall === 1
              ? Promise.resolve(validHealth(13))
              : afterReset.promise;
          }
          if (path === "/killswitch/reset") {
            return Promise.resolve({
              scope: "loss:equity",
              generation: 13,
              tripped: false,
            });
          }
          throw new Error(`unexpected API path ${path}`);
        };

        await module.refreshHealth();
        elements["breaker-reset-reason"].value = "reviewed breaker state";
        module.updateBreakerReset();
        module.submitBreakerReset({preventDefault() {}});
        await flush();
        assertUnknownAndDisabled();
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
    assert 'const assetClasses = ["equity", "crypto"]' in text
    assert "expected_generation" in text
    assert "scope: proof.scope" in text
    assert "health.active_breakers" in text
    assert "breakerScopeIsCanonical" in text
    assert '"/killswitch/reset"' in text


def test_pages_include_accessible_session_actions_dialogs_and_live_regions():
    index = (_STATIC / "index.html").read_text(encoding="utf-8")

    assert 'href="#main-content"' in index
    assert "<nav" in index
    assert 'aria-label="Primary"' in index
    assert 'id="critical-banner"' in index
    assert 'role="alert"' in index
    banner_start = index.index('id="critical-banner"')
    banner_tag_end = index.index(">", banner_start)
    assert "hidden" not in index[banner_start:banner_tag_end]
    assert "Safety state unverified" in index
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


def test_dynamic_text_surfaces_have_intrinsic_width_containment():
    css = (_STATIC / "css" / "console.css").read_text(encoding="utf-8")

    child_containment = css[
        css.index(":where(.app-header,")
        : css.index(
            "}",
            css.index(":where(.app-header,"),
        )
    ]
    assert "min-width: 0;" in child_containment
    assert "min-inline-size: 0;" in child_containment
    assert "max-width: 100%;" in child_containment

    dynamic_containment = css[
        css.index(":where(.dynamic-text,")
        : css.index(
            "}",
            css.index(":where(.dynamic-text,"),
        )
    ]
    assert "min-width: 0;" in dynamic_containment
    assert "min-inline-size: 0;" in dynamic_containment
    assert "max-width: 100%;" in dynamic_containment
    assert "overflow-wrap: anywhere;" in dynamic_containment
    assert "word-break: break-word;" in dynamic_containment

    table_start = css.index(".table-wrap {")
    table_rule = css[table_start:css.index("}", table_start)]
    assert "min-width: 0;" in table_rule
    assert "max-width: 100%;" in table_rule
    assert "overflow-x: auto;" in table_rule

    cells_start = css.index("th,\ntd {")
    cells_rule = css[cells_start:css.index("}", cells_start)]
    assert "white-space: normal;" in cells_rule
    assert "overflow-wrap: anywhere;" in cells_rule
    assert "word-break: break-word;" in cells_rule

    controls_start = css.index(".section-heading > button,")
    controls_rule = css[
        controls_start:css.index("}", controls_start)
    ]
    assert "flex-shrink: 0;" in controls_rule
    assert "max-width: 100%;" in controls_rule


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


def test_registered_runtime_secret_is_redacted_from_captured_logs(caplog):
    import logging

    from trading_assistant.config import Secrets
    from trading_assistant.logging import RedactionFilter, register_all_secrets

    marker = "injected-runtime-redaction-marker"
    redaction_filter = RedactionFilter()
    caplog.handler.addFilter(redaction_filter)
    try:
        register_all_secrets(Secrets(app_api_token=marker))

        logging.getLogger("trading_assistant.test").warning("opaque=%s", marker)

        assert marker not in caplog.text
        assert "***REDACTED***" in caplog.text
    finally:
        caplog.handler.removeFilter(redaction_filter)


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
