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
      "truth-equity-breaker",
      "truth-crypto-breaker",
      "breaker-scope",
      "breaker-generation",
      "breaker-health",
      "breaker-reset-reason",
      "breaker-reset-button",
      "status-region",
      "receipt-panel",
    ]);
    elements["breaker-scope"].value = "equity";
    const validHealth = (generation = 7) => ({
      broker: "Alpaca",
      mode: "paper",
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
    });
    const assertUnknownAndDisabled = () => {
      for (const id of [
        "truth-database",
        "truth-daemon",
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
    };
"""


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
              asset_class: "equity",
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
