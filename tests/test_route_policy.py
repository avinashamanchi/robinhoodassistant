from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import threading
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from starlette.responses import JSONResponse
from starlette.routing import request_response

from trading_assistant.app.main import create_app
import trading_assistant.app.policy as policy_module
from trading_assistant.app.policy import (
    AuthLevel,
    RoutePolicy,
    RoutePolicyRegistry,
    validate_route_inventory,
)
from trading_assistant.app.limits import (
    LeaseDecision,
    LimitDecision,
    LimitStoreUnavailable,
)
from trading_assistant.db.models import (
    AuthSession,
    ConcurrencyLease,
    PanicReceipt,
    RateWindow,
    utcnow,
)


class _StubAgent:
    def chat(self, message, **context):
        return {"reply": "ok", "tool_calls": []}


def _with_limit(
    service,
    name: str,
    *,
    requests: int,
    global_requests: int,
    window_seconds: int = 60,
    concurrency: int = 1,
):
    limits = service.config.security.rate_limits
    configured = getattr(limits, name).model_copy(
        update={
            "requests": requests,
            "global_requests": global_requests,
            "window_seconds": window_seconds,
            "concurrency": concurrency,
        }
    )
    limits = limits.model_copy(update={name: configured})
    security = service.config.security.model_copy(
        update={"rate_limits": limits}
    )
    service.config = service.config.model_copy(
        update={"security": security}
    )
    return service


def test_every_api_route_has_exact_policy(make_service):
    app = create_app(
        service=make_service(),
        agent=_StubAgent(),
        api_token="route-policy-secret",
        planning=None,
    )

    registry = app.state.route_policy_registry

    assert registry.unclassified(app) == []
    assert registry.duplicates() == []
    assert (
        registry.get("POST", "/approve/{order_id}").auth
        is AuthLevel.RECENT
    )
    assert registry.get("POST", "/chat").limit_name == "chat"
    assert registry.get("POST", "/backtests/run").limit_name == "backtest"
    positions = registry.get("GET", "/positions")
    assert positions.limit_name == "broker_read"
    assert positions.broker_read is True
    assert (
        registry.get("POST", "/analyze").provider_category
        == "analysis"
    )
    assert (
        registry.get("GET", "/health/live").auth
        is AuthLevel.PUBLIC
    )


def test_duplicate_and_unclassified_routes_fail_inventory_validation():
    duplicate = RoutePolicy(
        "GET",
        "/only",
        AuthLevel.PUBLIC,
        "session_read",
    )
    duplicate_registry = RoutePolicyRegistry((duplicate, duplicate))
    assert duplicate_registry.duplicates() == [("GET", "/only")]

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/only")
    def only():
        return {"ok": True}

    @app.get("/missing")
    def missing():
        return {"ok": True}

    app.state.route_policy_registry = duplicate_registry

    assert duplicate_registry.unclassified(app) == [
        ("GET", "/missing"),
    ]
    with pytest.raises(
        RuntimeError,
        match=r"duplicates=.*GET.*only.*unclassified=.*GET.*missing",
    ):
        validate_route_inventory(app)


def test_route_resolution_uses_exact_fastapi_match(make_service):
    app = create_app(
        service=make_service(),
        agent=_StubAgent(),
        api_token="route-match-secret",
        planning=None,
    )
    registry = app.state.route_policy_registry

    literal = registry.resolve(app, "GET", "/plans/ui")
    parameterized = registry.resolve(app, "GET", "/plans/17")
    static_head = registry.resolve(
        app,
        "HEAD",
        "/static/js/login.js",
    )

    assert literal is not None
    assert literal.policy.path == "/plans/ui"
    assert literal.path_params == {}
    assert parameterized is not None
    assert parameterized.policy.path == "/plans/{plan_id}"
    assert parameterized.path_params == {"plan_id": "17"}
    assert static_head is not None
    assert static_head.policy.path == "/static/{path:path}"
    assert registry.resolve(app, "POST", "/plans/ui") is None
    assert registry.resolve(app, "GET", "/plans/ui/extra") is None


def test_durable_rate_denial_is_stable_and_precedes_login(
    make_service,
):
    service = _with_limit(
        make_service(),
        "login",
        requests=1,
        global_requests=20,
    )
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="route-rate-secret",
        planning=None,
    )
    client = TestClient(app)

    assert client.post(
        "/auth/login",
        json={"secret": "wrong"},
    ).status_code == 401
    denied = client.post(
        "/auth/login",
        json={"secret": "route-rate-secret"},
    )

    assert denied.status_code == 429
    assert denied.json()["error"]["code"] == "rate_limit_exceeded"
    assert denied.json()["error"]["message"] == "Request rate limit exceeded"
    assert denied.headers["X-RateLimit-Limit"] == "1"
    assert denied.headers["X-RateLimit-Remaining"] == "0"
    assert int(denied.headers["X-RateLimit-Reset"]) > 0
    assert int(denied.headers["Retry-After"]) > 0
    with service.session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(AuthSession)
        ) == 0


def test_authenticated_rate_principal_is_the_persisted_session(
    make_service,
):
    service = _with_limit(
        make_service(),
        "session_read",
        requests=1,
        global_requests=10,
    )
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="route-session-secret",
        planning=None,
    )
    first = TestClient(app)
    second = TestClient(app)
    assert first.post(
        "/auth/login",
        json={"secret": "route-session-secret"},
    ).status_code == 200
    assert second.post(
        "/auth/login",
        json={"secret": "route-session-secret"},
    ).status_code == 200

    assert first.get("/pending").status_code == 200
    assert second.get("/pending").status_code == 200
    denied = first.get("/pending")

    assert denied.status_code == 429
    assert denied.json()["error"]["code"] == "rate_limit_exceeded"


@pytest.mark.parametrize(
    "path",
    [
        "/approve/123",
        "/reject/123",
        "/killswitch/reset",
        "/orders/123/cancel",
        "/reconcile",
        "/sync",
        "/panic",
        "/analyze",
        "/plans/123/approve",
        "/plans/123/cancel",
        "/propose",
        "/backtests/run",
    ],
)
@pytest.mark.parametrize(
    ("idempotency_key", "expected_code"),
    [
        (None, "idempotency_key_required"),
        ("contains space", "invalid_idempotency_key"),
    ],
)
def test_every_protected_policy_rejects_missing_or_invalid_idempotency(
    make_service,
    path,
    idempotency_key,
    expected_code,
):
    service = make_service()
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="route-idempotency-secret",
        planning=None,
    )
    client = TestClient(app)
    login = client.post(
        "/auth/login",
        json={"secret": "route-idempotency-secret"},
    )
    headers = {"X-CSRF-Token": login.json()["csrf_token"]}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key

    denied = client.post(
        path,
        headers=headers,
        json={},
    )

    assert denied.status_code == 422
    assert denied.json()["error"]["code"] == expected_code
    assert (
        app.state.route_policy_registry.resolve(app, "POST", path)
        .policy.requires_idempotency
        is True
    )


def test_static_assets_reject_directories_and_traversal(make_service):
    app = create_app(
        service=make_service(),
        agent=_StubAgent(),
        api_token="route-static-secret",
        planning=None,
    )
    client = TestClient(app)

    assert client.get("/static/js/login.js").status_code == 200
    for path in (
        "/static",
        "/static/",
        "/static//js/login.js",
        "/static/js//login.js",
        "/static/js/%2flogin.js",
        "/static/js",
        "/static/js/",
        "/static/css/%2e%2e/js/login.js",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "not_found"


def test_concurrency_contention_is_stable_and_releases_lease(
    make_service,
):
    class BlockingAgent:
        def __init__(self):
            self.calls = 0
            self.first_started = threading.Event()
            self.release_first = threading.Event()
            self.lock = threading.Lock()

        def chat(self, message, **context):
            with self.lock:
                self.calls += 1
                call_number = self.calls
            if call_number == 1:
                self.first_started.set()
                assert self.release_first.wait(timeout=5)
            return {"reply": message, "tool_calls": []}

    agent = BlockingAgent()
    app = create_app(
        service=make_service(),
        agent=agent,
        api_token="route-concurrency-secret",
        planning=None,
    )
    owner_client = TestClient(app)
    follower_client = TestClient(app)
    login = owner_client.post(
        "/auth/login",
        json={"secret": "route-concurrency-secret"},
    )
    csrf = login.json()["csrf_token"]
    cookie_name = app.state.session_auth.cookie_name()
    follower_client.cookies.set(
        cookie_name,
        owner_client.cookies.get(cookie_name),
    )
    headers = {"X-CSRF-Token": csrf}

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(
            owner_client.post,
            "/chat",
            json={"message": "owner"},
            headers=headers,
        )
        assert agent.first_started.wait(timeout=5)
        try:
            follower = follower_client.post(
                "/chat",
                json={"message": "follower"},
                headers=headers,
            )
        finally:
            agent.release_first.set()
        owner_response = owner.result(timeout=5)

    assert owner_response.status_code == 200
    assert follower.status_code == 409
    assert follower.json()["error"]["code"] == "route_busy"
    assert int(follower.headers["Retry-After"]) > 0
    assert follower.headers["X-RateLimit-Limit"] == "10"
    assert int(follower.headers["X-RateLimit-Remaining"]) >= 0
    assert int(follower.headers["X-RateLimit-Reset"]) > 0
    assert owner_client.post(
        "/chat",
        json={"message": "after release"},
        headers=headers,
    ).status_code == 200
    assert agent.calls == 2


def test_durable_store_error_fails_closed_except_exact_liveness(
    make_service,
):
    class UnavailableLimiter:
        def consume_pair(self, *args, **kwargs):
            raise LimitStoreUnavailable("private sqlite failure")

    service = make_service()
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="route-store-secret",
        planning=None,
    )
    app.state.rate_limiter = UnavailableLimiter()
    client = TestClient(app)

    denied = client.post(
        "/auth/login",
        json={"secret": "route-store-secret"},
    )

    assert denied.status_code == 503
    assert denied.json()["error"]["code"] == "policy_store_unavailable"
    assert "private sqlite failure" not in denied.text
    assert denied.headers["Content-Security-Policy"]
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/live/extra").status_code == 404
    with service.session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(AuthSession)
        ) == 0


def test_lease_store_error_fails_closed_before_handler(make_service):
    class UnavailableLeases:
        def acquire(self, *args, **kwargs):
            raise LimitStoreUnavailable("private lease failure")

    service = make_service()
    calls = []

    def get_pending():
        calls.append(True)
        return []

    service.get_pending = get_pending
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="route-lease-store-secret",
        planning=None,
    )
    client = TestClient(app)
    assert client.post(
        "/auth/login",
        json={"secret": "route-lease-store-secret"},
    ).status_code == 200
    app.state.leases = UnavailableLeases()

    denied = client.get("/pending")

    assert denied.status_code == 503
    assert denied.json()["error"]["code"] == "policy_store_unavailable"
    assert "private lease failure" not in denied.text
    assert calls == []


def test_durable_keys_exclude_cookie_and_request_body(make_service):
    service = make_service()
    secret = "route-raw-secret-material"
    message = "body-material-must-never-be-a-key"
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token=secret,
        planning=None,
    )
    client = TestClient(app)
    login = client.post("/auth/login", json={"secret": secret})
    csrf = login.json()["csrf_token"]
    cookie = client.cookies.get(app.state.session_auth.cookie_name())

    assert client.post(
        "/chat",
        headers={"X-CSRF-Token": csrf},
        json={"message": message},
    ).status_code == 200

    with service.session_factory() as session:
        bucket_keys = list(session.scalars(select(RateWindow.bucket_key)))
        lease_keys = list(
            session.scalars(select(ConcurrencyLease.resource_key))
        )
    assert bucket_keys
    assert lease_keys
    assert all(
        len(key) == 64
        and all(character in "0123456789abcdef" for character in key)
        for key in bucket_keys
    )
    persisted = "\n".join([*bucket_keys, *lease_keys])
    assert secret not in persisted
    assert cookie not in persisted
    assert message not in persisted


def test_concurrent_panic_requests_coalesce_to_durable_receipt(
    make_service,
):
    service = make_service()
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="route-panic-secret",
        planning=None,
    )
    owner_client = TestClient(app)
    follower_client = TestClient(app)
    login = owner_client.post(
        "/auth/login",
        json={"secret": "route-panic-secret"},
    )
    csrf = login.json()["csrf_token"]
    cookie_name = app.state.session_auth.cookie_name()
    follower_client.cookies.set(
        cookie_name,
        owner_client.cookies.get(cookie_name),
    )
    receipt = {
        "safe": True,
        "confirmed_canceled": ["paper-order-1"],
        "unconfirmed_order_ids": [],
    }
    calls = 0
    calls_lock = threading.Lock()
    owner_started = threading.Event()
    release_owner = threading.Event()

    def blocking_panic(context):
        nonlocal calls
        with calls_lock:
            calls += 1
        owner_started.set()
        assert release_owner.wait(timeout=5)
        return receipt

    app.state.operations.panic = blocking_panic
    original_acquire = app.state.leases.acquire
    acquisition_count = 0
    acquisition_lock = threading.Lock()
    follower_attempted = threading.Event()

    def observed_acquire(*args, **kwargs):
        nonlocal acquisition_count
        result = original_acquire(*args, **kwargs)
        with acquisition_lock:
            acquisition_count += 1
            if acquisition_count == 2:
                follower_attempted.set()
        return result

    app.state.leases.acquire = observed_acquire
    owner_headers = {
        "X-CSRF-Token": csrf,
        "Idempotency-Key": "panic-owner",
    }
    follower_headers = {
        "X-CSRF-Token": csrf,
        "Idempotency-Key": "panic-follower",
    }

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(
            owner_client.post,
            "/panic",
            json={"reason": "coalesced panic owner"},
            headers=owner_headers,
        )
        assert owner_started.wait(timeout=5)
        follower = pool.submit(
            follower_client.post,
            "/panic",
            json={"reason": "coalesced panic follower"},
            headers=follower_headers,
        )
        assert follower_attempted.wait(timeout=5)
        release_owner.set()
        owner_response = owner.result(timeout=5)
        follower_response = follower.result(timeout=5)

    assert owner_response.status_code == 200
    assert follower_response.status_code == 200
    assert owner_response.json() == follower_response.json() == receipt
    assert calls == 1
    with service.session_factory() as session:
        durable = session.get(PanicReceipt, "alpaca-paper")
        assert durable is not None
        assert durable.state == "completed"
        assert json.loads(durable.response_json) == receipt

    repeated = owner_client.post(
        "/panic",
        json={"reason": "reuse durable panic receipt"},
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "panic-repeated",
        },
    )
    assert repeated.status_code == 200
    assert repeated.json() == receipt
    assert calls == 1


def test_handler_longer_than_initial_lease_is_renewed_without_overlap(
    make_service,
    monkeypatch,
):
    monkeypatch.setattr(
        policy_module,
        "_LEASE_TTL_SECONDS",
        1,
        raising=False,
    )
    monkeypatch.setattr(
        policy_module,
        "_LEASE_RENEW_INTERVAL_SECONDS",
        0.1,
        raising=False,
    )

    class SlowAgent:
        def __init__(self):
            self.calls = 0
            self.active = 0
            self.max_active = 0
            self.started = threading.Event()
            self.release = threading.Event()
            self.lock = threading.Lock()

        def chat(self, message, **context):
            with self.lock:
                self.calls += 1
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                call_number = self.calls
            try:
                if call_number == 1:
                    self.started.set()
                    assert self.release.wait(timeout=5)
                return {"reply": message, "tool_calls": []}
            finally:
                with self.lock:
                    self.active -= 1

    service = _with_limit(
        make_service(),
        "chat",
        requests=20,
        global_requests=40,
        window_seconds=1,
        concurrency=1,
    )
    agent = SlowAgent()
    app = create_app(
        service=service,
        agent=agent,
        api_token="route-long-lease-secret",
        planning=None,
    )
    owner_client = TestClient(app)
    follower_client = TestClient(app)
    login = owner_client.post(
        "/auth/login",
        json={"secret": "route-long-lease-secret"},
    )
    cookie_name = app.state.session_auth.cookie_name()
    follower_client.cookies.set(
        cookie_name,
        owner_client.cookies.get(cookie_name),
    )
    headers = {"X-CSRF-Token": login.json()["csrf_token"]}

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(
            owner_client.post,
            "/chat",
            json={"message": "slow owner"},
            headers=headers,
        )
        assert agent.started.wait(timeout=5)
        time.sleep(1.2)
        try:
            follower = follower_client.post(
                "/chat",
                json={"message": "must not overlap"},
                headers=headers,
            )
        finally:
            agent.release.set()
        owner_response = owner.result(timeout=5)

    assert owner_response.status_code == 200
    assert follower.status_code == 409
    assert follower.json()["error"]["code"] == "route_busy"
    assert agent.calls == 1
    assert agent.max_active == 1


def test_lease_ownership_loss_is_reported_instead_of_handler_success(
    make_service,
    monkeypatch,
):
    monkeypatch.setattr(
        policy_module,
        "_LEASE_TTL_SECONDS",
        1,
        raising=False,
    )
    monkeypatch.setattr(
        policy_module,
        "_LEASE_RENEW_INTERVAL_SECONDS",
        0.01,
        raising=False,
    )
    app = create_app(
        service=make_service(),
        agent=_StubAgent(),
        api_token="route-lost-lease-secret",
        planning=None,
    )
    client = TestClient(app)
    login = client.post(
        "/auth/login",
        json={"secret": "route-lost-lease-secret"},
    )
    started = threading.Event()

    async def delayed_chat(_request):
        started.set()
        await asyncio.sleep(0.2)
        return JSONResponse({"reply": "unsafe overlap"})

    chat_route = next(
        route
        for route in app.routes
        if getattr(route, "path", None) == "/chat"
    )
    chat_route.app = request_response(delayed_chat)

    class LosingLeases:
        def acquire(self, _resource_key, *, owner, ttl_seconds):
            return LeaseDecision(
                acquired=True,
                owner=owner,
                expires_at=utcnow() + timedelta(seconds=ttl_seconds),
                generation=7,
                retry_after_seconds=0,
            )

        def renew(
            self,
            _resource_key,
            *,
            owner,
            generation,
            ttl_seconds,
        ):
            return LeaseDecision(
                acquired=False,
                owner="replacement-request",
                expires_at=utcnow() + timedelta(seconds=ttl_seconds),
                generation=generation + 1,
                retry_after_seconds=ttl_seconds,
            )

        def release(self, *_args, **_kwargs):
            return False

    app.state.leases = LosingLeases()
    denied = client.post(
        "/chat",
        json={"message": "must be cancelled"},
        headers={"X-CSRF-Token": login.json()["csrf_token"]},
    )

    assert started.wait(timeout=1)
    assert denied.status_code == 503
    assert denied.json()["error"]["code"] == "route_lease_lost"


def test_panic_follower_never_accepts_replacement_owner_receipt(
    make_service,
):
    service = make_service()
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="route-panic-owner-secret",
        planning=None,
    )
    client = TestClient(app)
    login = client.post(
        "/auth/login",
        json={"secret": "route-panic-owner-secret"},
    )
    panic_calls = 0

    def should_not_execute(_context):
        nonlocal panic_calls
        panic_calls += 1
        return {"safe": True}

    app.state.operations.panic = should_not_execute
    expires_at = utcnow() + timedelta(seconds=60)
    with service.session_factory() as session:
        session.add(
            PanicReceipt(
                account_scope="alpaca-paper",
                request_id="owner-request-a",
                state="started",
                response_json=None,
                started_at=utcnow(),
                expires_at=expires_at,
            )
        )
        session.commit()

    attempted = threading.Event()
    replaced = threading.Event()

    class ReplacedLease:
        def acquire(self, *_args, **_kwargs):
            attempted.set()
            return LeaseDecision(
                acquired=False,
                owner="owner-request-a",
                expires_at=expires_at,
                generation=41,
                retry_after_seconds=60,
            )

        def inspect(self, *_args, **_kwargs):
            if replaced.is_set():
                return LeaseDecision(
                    acquired=True,
                    owner="owner-request-b",
                    expires_at=utcnow() + timedelta(seconds=60),
                    generation=42,
                    retry_after_seconds=60,
                )
            return LeaseDecision(
                acquired=True,
                owner="owner-request-a",
                expires_at=expires_at,
                generation=41,
                retry_after_seconds=60,
            )

    app.state.leases = ReplacedLease()
    headers = {
        "X-CSRF-Token": login.json()["csrf_token"],
        "Idempotency-Key": "panic-pinned-follower",
    }
    with ThreadPoolExecutor(max_workers=1) as pool:
        follower = pool.submit(
            client.post,
            "/panic",
            json={"reason": "follow exact owner"},
            headers=headers,
        )
        assert attempted.wait(timeout=5)
        with service.session_factory() as session:
            row = session.get(PanicReceipt, "alpaca-paper")
            row.request_id = "owner-request-b"
            row.state = "completed"
            row.response_json = json.dumps(
                {"safe": True, "owner": "request-b"}
            )
            row.completed_at = utcnow()
            row.expires_at = utcnow() + timedelta(seconds=60)
            session.commit()
        replaced.set()
        response = follower.result(timeout=5)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "panic_incomplete"
    assert response.json().get("owner") is None
    assert panic_calls == 0


def test_long_panic_keeps_one_lease_owner_and_executes_once(
    make_service,
    monkeypatch,
):
    monkeypatch.setattr(
        policy_module,
        "_LEASE_TTL_SECONDS",
        1,
        raising=False,
    )
    monkeypatch.setattr(
        policy_module,
        "_LEASE_RENEW_INTERVAL_SECONDS",
        0.1,
        raising=False,
    )
    service = _with_limit(
        make_service(),
        "panic",
        requests=20,
        global_requests=40,
        window_seconds=1,
        concurrency=1,
    )
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="route-long-panic-secret",
        planning=None,
    )
    owner_client = TestClient(app)
    follower_client = TestClient(app)
    login = owner_client.post(
        "/auth/login",
        json={"secret": "route-long-panic-secret"},
    )
    cookie_name = app.state.session_auth.cookie_name()
    follower_client.cookies.set(
        cookie_name,
        owner_client.cookies.get(cookie_name),
    )
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def slow_panic(_context):
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=5)
        return {"safe": True, "owner": "original"}

    app.state.operations.panic = slow_panic
    original_acquire = app.state.leases.acquire
    acquired_owners: list[str] = []

    def record_acquire(*args, **kwargs):
        decision = original_acquire(*args, **kwargs)
        if decision.acquired:
            acquired_owners.append(decision.owner)
        return decision

    app.state.leases.acquire = record_acquire
    owner_headers = {
        "X-CSRF-Token": login.json()["csrf_token"],
        "Idempotency-Key": "panic-long-owner",
        "X-Request-ID": "shared-external-panic-id",
    }
    follower_headers = {
        "X-CSRF-Token": login.json()["csrf_token"],
        "Idempotency-Key": "panic-long-follower",
        "X-Request-ID": "shared-external-panic-id",
    }

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(
            owner_client.post,
            "/panic",
            json={"reason": "long owner"},
            headers=owner_headers,
        )
        assert started.wait(timeout=5)
        time.sleep(1.2)
        follower = pool.submit(
            follower_client.post,
            "/panic",
            json={"reason": "must follow original"},
            headers=follower_headers,
        )
        time.sleep(0.1)
        release.set()
        owner_response = owner.result(timeout=5)
        follower_response = follower.result(timeout=5)

    assert owner_response.status_code == 200
    assert follower_response.status_code == 200
    assert owner_response.json() == follower_response.json()
    assert calls == 1
    assert len(acquired_owners) == 1


def test_completed_panic_response_survives_settlement_failure_and_blocks_retry(
    make_service,
    monkeypatch,
):
    service = make_service()
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="route-panic-settlement-secret",
        planning=None,
    )
    client = TestClient(app)
    login = client.post(
        "/auth/login",
        json={"secret": "route-panic-settlement-secret"},
    )
    calls = 0
    receipt = {"safe": True, "owner": "settlement-failure"}

    def panic(_context):
        nonlocal calls
        calls += 1
        return receipt

    def fail_settlement(*_args, **_kwargs):
        raise LimitStoreUnavailable("settlement failed")

    app.state.operations.panic = panic
    monkeypatch.setattr(
        policy_module,
        "_finish_panic_receipt",
        fail_settlement,
    )
    headers = {
        "X-CSRF-Token": login.json()["csrf_token"],
        "Idempotency-Key": "panic-settlement-owner",
    }

    completed = client.post(
        "/panic",
        json={"reason": "settlement fails after completion"},
        headers=headers,
    )

    assert completed.status_code == 200
    assert completed.json() == receipt
    with service.session_factory() as session:
        durable = session.get(PanicReceipt, "alpaca-paper")
        assert durable.state == "failed"
    retry = client.post(
        "/panic",
        json={"reason": "uncertain retry must not execute"},
        headers={
            "X-CSRF-Token": login.json()["csrf_token"],
            "Idempotency-Key": "panic-settlement-retry",
        },
    )
    assert retry.status_code == 503
    assert retry.json()["error"]["code"] == "panic_incomplete"
    assert calls == 1


@pytest.mark.parametrize("release_failure", ["false", "exception"])
def test_completed_panic_response_survives_release_failure_and_blocks_retry(
    make_service,
    release_failure,
):
    service = make_service()
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="route-panic-release-secret",
        planning=None,
    )
    client = TestClient(app)
    login = client.post(
        "/auth/login",
        json={"secret": "route-panic-release-secret"},
    )
    delegate = app.state.leases

    class FailingReleaseLeases:
        def acquire(self, *args, **kwargs):
            return delegate.acquire(*args, **kwargs)

        def renew(self, *args, **kwargs):
            return delegate.renew(*args, **kwargs)

        def inspect(self, *args, **kwargs):
            return delegate.inspect(*args, **kwargs)

        def release(self, *_args, **_kwargs):
            if release_failure == "exception":
                raise LimitStoreUnavailable("release failed")
            return False

    app.state.leases = FailingReleaseLeases()
    calls = 0
    receipt = {"safe": True, "owner": "release-failure"}

    def panic(_context):
        nonlocal calls
        calls += 1
        return receipt

    app.state.operations.panic = panic
    completed = client.post(
        "/panic",
        json={"reason": "release fails after completion"},
        headers={
            "X-CSRF-Token": login.json()["csrf_token"],
            "Idempotency-Key": f"panic-release-{release_failure}",
        },
    )

    assert completed.status_code == 200
    assert completed.json() == receipt
    with service.session_factory() as session:
        durable = session.get(PanicReceipt, "alpaca-paper")
        assert durable.state == "failed"
    retry = client.post(
        "/panic",
        json={"reason": "uncertain retry must not execute"},
        headers={
            "X-CSRF-Token": login.json()["csrf_token"],
            "Idempotency-Key": f"panic-release-{release_failure}-retry",
        },
    )
    assert retry.status_code == 503
    assert retry.json()["error"]["code"] == "panic_incomplete"
    assert calls == 1


def test_blocked_durable_limiter_does_not_delay_exact_liveness(
    make_service,
):
    app = create_app(
        service=make_service(),
        agent=_StubAgent(),
        api_token="route-responsive-secret",
        planning=None,
    )
    blocked = threading.Event()
    release = threading.Event()

    class BlockingLimiter:
        def consume_pair(self, *_args, **_kwargs):
            blocked.set()
            release.wait(timeout=0.5)
            now = utcnow()
            return LimitDecision(
                allowed=True,
                remaining=1,
                retry_after_seconds=0,
                reset_at=now + timedelta(seconds=60),
            )

    app.state.rate_limiter = BlockingLimiter()

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            started_at = time.monotonic()
            login = asyncio.create_task(
                client.post(
                    "/auth/login",
                    json={"secret": "route-responsive-secret"},
                )
            )
            assert await asyncio.to_thread(blocked.wait, 1)
            liveness = await asyncio.wait_for(
                client.get("/health/live"),
                timeout=0.2,
            )
            elapsed = time.monotonic() - started_at
            release.set()
            login_response = await login
            return liveness, login_response, elapsed

    liveness, login_response, elapsed = asyncio.run(exercise())

    assert liveness.status_code == 200
    assert login_response.status_code == 200
    assert elapsed < 0.25
