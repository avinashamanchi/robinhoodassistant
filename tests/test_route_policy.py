from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

import trading_assistant.app.limits as limits_module
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
    AuditEvent,
    AuthSession,
    ConcurrencyLease,
    MutationInterlock,
    PanicReceipt,
    RateWindow,
    utcnow,
)
from trading_assistant.risk.breakers import BreakerScope
from trading_assistant.security.sensitive_fields import persist_sensitive
from tests.conftest import decrypt_test_sensitive


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


def _persist_panic_fence(
    service,
    *,
    lease_key: str,
    lease_owner: str,
    lease_generation: int,
    lease_expires_at: datetime,
    receipt_owner: str,
    receipt_generation: int,
    receipt_expires_at: datetime,
    receipt_state: str = "started",
    response: dict[str, object] | None = None,
) -> None:
    with service.session_factory() as session:
        session.add(
            ConcurrencyLease(
                resource_key=lease_key,
                owner=lease_owner,
                generation=lease_generation,
                expires_at=lease_expires_at,
            )
        )
        persist_sensitive(
            session,
            PanicReceipt(
                account_scope="alpaca-paper",
                request_id=receipt_owner,
                lease_generation=receipt_generation,
                state=receipt_state,
                started_at=utcnow(),
                completed_at=(
                    utcnow() if receipt_state != "started" else None
                ),
                expires_at=receipt_expires_at,
            ),
            {
                "response_json": json.dumps(
                    response or {},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            },
            session_factory=service.session_factory,
        )
        session.commit()


def _panic_request(app: FastAPI) -> Request:
    request = Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "scheme": "https",
            "path": "/panic",
            "raw_path": b"/panic",
            "query_string": b"",
            "headers": [],
            "server": ("localhost", 8020),
            "client": ("127.0.0.1", 50000),
            "root_path": "",
        }
    )
    request.state.request_id = "panic-follower-test"
    return request


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


def test_route_inventory_rejects_mounts_websockets_and_imperative_routes():
    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    child = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.mount("/plugin", child)
    app.add_api_route(
        "/imperative",
        lambda: {"unsafe": True},
        methods=["GET"],
    )

    @app.websocket("/socket")
    async def socket(_websocket):
        return None

    registry = RoutePolicyRegistry(())
    app.state.route_policy_registry = registry

    assert registry.unclassified(app) == [
        ("GET", "/imperative"),
        ("MOUNT", "/plugin"),
        ("WEBSOCKET", "/socket"),
    ]
    with pytest.raises(
        RuntimeError,
        match=r"MOUNT.*plugin.*WEBSOCKET.*socket",
    ):
        validate_route_inventory(app)


def test_matched_route_added_without_policy_fails_closed(make_service):
    app = create_app(
        service=make_service(),
        agent=_StubAgent(),
        api_token="late-route-policy-secret",
        planning=None,
    )
    calls = 0

    def late_route():
        nonlocal calls
        calls += 1
        return {"unsafe": True}

    app.add_api_route(
        "/late-unclassified",
        late_route,
        methods=["GET"],
    )
    response = TestClient(app).get("/late-unclassified")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "route_policy_missing"
    assert calls == 0


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


@pytest.mark.parametrize(
    ("path", "expected_status", "expected_code"),
    [
        ("/approve/123", 409, "mutation_reconciliation_required"),
        ("/reject/123", 409, "mutation_reconciliation_required"),
        ("/killswitch/reset", 409, "mutation_reconciliation_required"),
        ("/orders/123/cancel", 409, "mutation_reconciliation_required"),
        ("/reconcile", 409, "mutation_reconciliation_required"),
        ("/sync", 409, "mutation_reconciliation_required"),
        ("/panic", 503, "panic_incomplete"),
        ("/analyze", 409, "mutation_reconciliation_required"),
        ("/plans/123/approve", 409, "mutation_reconciliation_required"),
        ("/plans/123/cancel", 409, "mutation_reconciliation_required"),
        ("/propose", 409, "mutation_reconciliation_required"),
        ("/backtests/run", 409, "mutation_reconciliation_required"),
    ],
)
def test_every_protected_policy_honors_a_nonexpiring_interlock(
    make_service,
    path,
    expected_status,
    expected_code,
):
    class ExistingInterlock:
        def inspect(self, _resource_key):
            return SimpleNamespace(
                acquired=False,
                owner="internal-owner",
                generation=9,
                operation="POST protected",
                state="uncertain",
                outcome_code="reconciliation_required",
                worker_finished_at=utcnow(),
            )

    app = create_app(
        service=make_service(),
        agent=_StubAgent(),
        api_token="route-interlock-matrix-secret",
        planning=None,
    )
    app.state.mutation_interlocks = ExistingInterlock()
    client = TestClient(app)
    login = client.post(
        "/auth/login",
        json={"secret": "route-interlock-matrix-secret"},
    )

    denied = client.post(
        path,
        headers={
            "X-CSRF-Token": login.json()["csrf_token"],
            "Idempotency-Key": (
                f"latched-{path.strip('/').replace('/', '-')}"
            ),
        },
        json={},
    )

    assert denied.status_code == expected_status
    assert denied.json()["error"]["code"] == expected_code
    assert "Retry-After" not in denied.headers


@pytest.mark.parametrize(
    ("paths", "resource_material", "expected_codes"),
    [
        (
            ("/approve/7", "/reject/7", "/orders/7/cancel"),
            "order:7",
            (
                "mutation_reconciliation_required",
                "mutation_reconciliation_required",
                "mutation_reconciliation_required",
            ),
        ),
        (
            ("/plans/7/approve", "/plans/7/cancel"),
            "plan:7",
            (
                "mutation_reconciliation_required",
                "mutation_reconciliation_required",
            ),
        ),
    ],
)
def test_cross_route_mutations_share_one_domain_interlock(
    make_service,
    paths,
    resource_material,
    expected_codes,
):
    expected_key = (
        "route:"
        + hashlib.sha256(resource_material.encode("utf-8")).hexdigest()
        + ":0"
    )

    class ExactDomainInterlock:
        def inspect(self, resource_key):
            if resource_key != expected_key:
                return None
            return SimpleNamespace(
                acquired=False,
                resource_key=resource_key,
                owner="internal-cross-route-owner",
                generation=17,
                operation="order_approve",
                state="uncertain",
                outcome_code="lease_renewal_unproven",
                worker_finished_at=utcnow(),
            )

    app = create_app(
        service=make_service(),
        agent=_StubAgent(),
        api_token="route-cross-scope-secret",
        planning=None,
    )
    app.state.mutation_interlocks = ExactDomainInterlock()
    client = TestClient(app)
    login = client.post(
        "/auth/login",
        json={"secret": "route-cross-scope-secret"},
    )

    for index, (path, expected_code) in enumerate(
        zip(paths, expected_codes, strict=True)
    ):
        denied = client.post(
            path,
            headers={
                "X-CSRF-Token": login.json()["csrf_token"],
                "Idempotency-Key": f"cross-route-{index}",
            },
            json={},
        )
        assert denied.json()["error"]["code"] == expected_code
        assert "Retry-After" not in denied.headers


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
    original_inspect = app.state.leases.inspect
    acquisition_count = 0
    acquisition_lock = threading.Lock()
    follower_attempted = threading.Event()

    def observed_acquire(*args, **kwargs):
        nonlocal acquisition_count
        result = original_acquire(*args, **kwargs)
        with acquisition_lock:
            acquisition_count += 1
        return result

    def observed_inspect(*args, **kwargs):
        result = original_inspect(*args, **kwargs)
        if result.acquired:
            follower_attempted.set()
        return result

    app.state.leases.acquire = observed_acquire
    app.state.leases.inspect = observed_inspect
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
    assert acquisition_count == 1
    with service.session_factory() as session:
        durable = session.get(PanicReceipt, "alpaca-paper")
        assert durable is not None
        assert durable.state == "completed"
        assert json.loads(
            decrypt_test_sensitive(durable, "response_json")
        ) == receipt

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
    assert calls == 2


def test_successful_breaker_reset_invalidates_prior_panic_tenure(
    make_service,
):
    service = make_service()
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="panic-reset-tenure-secret",
        planning=None,
    )
    client = TestClient(app)
    login = client.post(
        "/auth/login",
        json={"secret": "panic-reset-tenure-secret"},
    )
    csrf = login.json()["csrf_token"]
    scope = BreakerScope.operator_global()
    pending_orders: list[str] = []
    panic_generations: list[int] = []

    def fake_panic(context):
        tripped = service.breakers.trip(
            scope,
            "local fake panic",
            context.actor,
            request_id=context.request_id,
        )
        panic_generations.append(tripped.generation)
        canceled = list(pending_orders)
        pending_orders.clear()
        return {
            "safe": True,
            "confirmed_canceled": canceled,
            "unconfirmed_order_ids": [],
        }

    def fake_reset(
        requested_scope,
        *,
        expected_generation,
        context,
    ):
        reset = service.breakers.reset(
            BreakerScope.parse(requested_scope),
            context.actor,
            context.reason,
            {"local_state": "confirmed"},
            expected_generation=expected_generation,
            request_id=context.request_id,
        )
        return {
            "killswitch": "reset",
            "scope": requested_scope,
            "tripped": reset.tripped,
            "generation": reset.generation,
        }

    app.state.operations.panic = fake_panic
    app.state.operations.reset_breaker = fake_reset

    first = client.post(
        "/panic",
        json={"reason": "first local fake panic"},
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "panic-before-reset",
        },
    )
    reset = client.post(
        "/killswitch/reset",
        json={
            "scope": scope.key,
            "reason": "verified local fake reset",
            "expected_generation": panic_generations[-1],
        },
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "reset-after-panic",
        },
    )
    with service.session_factory() as session:
        assert session.get(PanicReceipt, "alpaca-paper") is None
    pending_orders.append("paper-order-after-reset")
    second = client.post(
        "/panic",
        json={"reason": "second local fake panic"},
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "panic-after-reset",
        },
    )

    assert first.status_code == 200
    assert reset.status_code == 200
    assert second.status_code == 200
    assert second.json()["confirmed_canceled"] == [
        "paper-order-after-reset"
    ]
    assert pending_orders == []
    assert panic_generations == [1, 3]
    assert service.broker.submit_calls == 0


def test_reset_cleanup_uncertainty_does_not_disable_panic_after_lease_expiry(
    make_service,
    monkeypatch,
):
    monkeypatch.setattr(policy_module, "_LEASE_TTL_SECONDS", 1)
    service = make_service()
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="reset-cleanup-panic-secret",
        planning=None,
    )
    client = TestClient(app)
    login = client.post(
        "/auth/login",
        json={"secret": "reset-cleanup-panic-secret"},
    )
    csrf = login.json()["csrf_token"]
    original_release = (
        app.state.mutation_interlocks.release_settled
    )
    release_calls = 0
    panic_calls = 0

    def fail_first_cleanup(*args, **kwargs):
        nonlocal release_calls
        release_calls += 1
        if release_calls == 1:
            return False
        return original_release(*args, **kwargs)

    def fake_reset(_scope, *, expected_generation, context):
        return {
            "killswitch": "reset",
            "scope": "operator_global",
            "tripped": False,
            "generation": expected_generation + 1,
        }

    def fake_panic(_context):
        nonlocal panic_calls
        panic_calls += 1
        return {
            "safe": True,
            "confirmed_canceled": [],
            "unconfirmed_order_ids": [],
        }

    app.state.mutation_interlocks.release_settled = fail_first_cleanup
    app.state.operations.reset_breaker = fake_reset
    app.state.operations.panic = fake_panic

    reset = client.post(
        "/killswitch/reset",
        json={
            "scope": "operator_global",
            "reason": "successful reset with uncertain cleanup",
            "expected_generation": 1,
        },
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "reset-cleanup-owner",
        },
    )
    account_lease_key = (
        "route:"
        + hashlib.sha256(
            b"paper-account:alpaca-paper"
        ).hexdigest()
        + ":0"
    )
    expiry_deadline = time.monotonic() + 2
    while (
        app.state.leases.inspect(account_lease_key).acquired
        and time.monotonic() < expiry_deadline
    ):
        time.sleep(0.02)

    with service.session_factory() as session:
        reset_latch = session.scalar(
            select(MutationInterlock).where(
                MutationInterlock.operation == "breaker_reset"
            )
        )
    assert reset.status_code == 200
    assert reset_latch is not None
    assert reset_latch.state == "uncertain"
    assert reset_latch.worker_finished_at is not None
    assert (
        app.state.leases.inspect(account_lease_key).acquired
        is False
    )

    panic = client.post(
        "/panic",
        json={"reason": "safety increase after reset cleanup loss"},
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "panic-after-reset-cleanup",
        },
    )

    assert panic.status_code == 200
    assert panic.json()["safe"] is True
    assert panic_calls == 1
    assert service.broker.submit_calls == 0
    with service.session_factory() as session:
        remaining_reset = session.scalar(
            select(MutationInterlock).where(
                MutationInterlock.operation == "breaker_reset"
            )
        )
    assert remaining_reset is not None
    assert remaining_reset.state == "uncertain"


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


def test_real_sync_mutation_interlock_survives_renewal_failure_and_lease_expiry(
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
        0.05,
        raising=False,
    )
    service = _with_limit(
        make_service(),
        "approval",
        requests=20,
        global_requests=40,
        window_seconds=1,
        concurrency=1,
    )
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="route-sync-interlock-secret",
        planning=None,
    )
    owner_client = TestClient(app)
    follower_client = TestClient(app)
    login = owner_client.post(
        "/auth/login",
        json={"secret": "route-sync-interlock-secret"},
    )
    cookie_name = app.state.session_auth.cookie_name()
    follower_client.cookies.set(
        cookie_name,
        owner_client.cookies.get(cookie_name),
    )
    csrf = login.json()["csrf_token"]
    started = threading.Event()
    release_worker = threading.Event()
    renewal_attempted = threading.Event()
    lock = threading.Lock()
    calls = 0
    active = 0
    max_active = 0
    side_effects = 0

    def blocking_approve(*_args, **_kwargs):
        nonlocal calls, active, max_active, side_effects
        with lock:
            calls += 1
            call_number = calls
            active += 1
            max_active = max(max_active, active)
        try:
            if call_number == 1:
                started.set()
                assert release_worker.wait(timeout=5)
            with lock:
                side_effects += 1
            return {"status": "submitted", "executed": True}
        finally:
            with lock:
                active -= 1

    service.approve_order = blocking_approve
    delegate = app.state.leases
    release_calls = 0

    class RenewalUnavailableLeases:
        def acquire(self, *args, **kwargs):
            return delegate.acquire(*args, **kwargs)

        def renew(self, *_args, **_kwargs):
            renewal_attempted.set()
            raise LimitStoreUnavailable("renewal store unavailable")

        def release(self, *args, **kwargs):
            nonlocal release_calls
            release_calls += 1
            return delegate.release(*args, **kwargs)

        def inspect(self, *args, **kwargs):
            return delegate.inspect(*args, **kwargs)

    app.state.leases = RenewalUnavailableLeases()
    headers = {
        "X-CSRF-Token": csrf,
        "Idempotency-Key": "sync-interlock-owner",
    }

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(
            owner_client.post,
            "/approve/7",
            json={"reason": "real synchronous owner"},
            headers=headers,
        )
        assert started.wait(timeout=5)
        assert renewal_attempted.wait(timeout=5)
        time.sleep(1.1)
        try:
            follower = follower_client.post(
                "/approve/7",
                json={"reason": "must remain interlocked"},
                headers={
                    "X-CSRF-Token": csrf,
                    "Idempotency-Key": "sync-interlock-follower",
                },
            )
        finally:
            release_worker.set()
        owner_response = owner.result(timeout=5)

    assert owner_response.status_code == 200
    assert follower.status_code == 409
    assert (
        follower.json()["error"]["code"]
        == "mutation_reconciliation_required"
    )
    assert "Retry-After" not in follower.headers
    assert max_active == 1
    assert calls == side_effects == 1
    assert release_calls == 0

    still_latched = owner_client.post(
        "/approve/7",
        json={"reason": "cannot retry an uncertain outcome"},
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "sync-interlock-after-finish",
        },
    )
    assert still_latched.status_code == 409
    assert (
        still_latched.json()["error"]["code"]
        == "mutation_reconciliation_required"
    )


def test_cancelled_request_awaits_real_sync_worker_and_keeps_interlock(
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
        0.05,
        raising=False,
    )
    service = _with_limit(
        make_service(),
        "approval",
        requests=20,
        global_requests=40,
        window_seconds=1,
        concurrency=1,
    )
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="route-cancel-interlock-secret",
        planning=None,
    )
    started = threading.Event()
    release_worker = threading.Event()
    renewal_attempted = threading.Event()
    lock = threading.Lock()
    calls = 0
    active = 0
    max_active = 0
    side_effects = 0

    def blocking_approve(*_args, **_kwargs):
        nonlocal calls, active, max_active, side_effects
        with lock:
            calls += 1
            call_number = calls
            active += 1
            max_active = max(max_active, active)
        try:
            if call_number == 1:
                started.set()
                assert release_worker.wait(timeout=5)
            with lock:
                side_effects += 1
            return {"status": "submitted", "executed": True}
        finally:
            with lock:
                active -= 1

    service.approve_order = blocking_approve
    delegate = app.state.leases

    class RenewalUnavailableLeases:
        def acquire(self, *args, **kwargs):
            return delegate.acquire(*args, **kwargs)

        def renew(self, *_args, **_kwargs):
            renewal_attempted.set()
            raise LimitStoreUnavailable("renewal store unavailable")

        def release(self, *args, **kwargs):
            return delegate.release(*args, **kwargs)

        def inspect(self, *args, **kwargs):
            return delegate.inspect(*args, **kwargs)

    app.state.leases = RenewalUnavailableLeases()

    async def exercise():
        leaked: list[dict[str, object]] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(
            lambda _loop, context: leaked.append(context)
        )
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://localhost:8020",
            ) as client:
                login = await client.post(
                    "/auth/login",
                    json={"secret": "route-cancel-interlock-secret"},
                )
                csrf = login.json()["csrf_token"]
                owner = asyncio.create_task(
                    client.post(
                        "/approve/7",
                        json={"reason": "cancel transport, not worker"},
                        headers={
                            "X-CSRF-Token": csrf,
                            "Idempotency-Key": "cancel-interlock-owner",
                        },
                    )
                )
                assert await asyncio.to_thread(started.wait, 5)
                owner.cancel()
                assert await asyncio.to_thread(
                    renewal_attempted.wait,
                    5,
                )
                await asyncio.sleep(0)
                assert owner.done() is False
                await asyncio.sleep(1.1)
                follower = await client.post(
                    "/approve/7",
                    json={"reason": "cannot replace canceled owner"},
                    headers={
                        "X-CSRF-Token": csrf,
                        "Idempotency-Key": "cancel-interlock-follower",
                    },
                )
                release_worker.set()
                with pytest.raises(asyncio.CancelledError):
                    await owner
                await asyncio.sleep(0)
                still_latched = await client.post(
                    "/approve/7",
                    json={"reason": "requires reconciliation"},
                    headers={
                        "X-CSRF-Token": csrf,
                        "Idempotency-Key": "cancel-interlock-retry",
                    },
                )
                return follower, still_latched, leaked
        finally:
            release_worker.set()
            loop.set_exception_handler(previous_handler)

    follower, still_latched, leaked = asyncio.run(exercise())

    for response in (follower, still_latched):
        assert response.status_code == 409
        assert (
            response.json()["error"]["code"]
            == "mutation_reconciliation_required"
        )
        assert "Retry-After" not in response.headers
    assert max_active == 1
    assert calls == side_effects == 1
    assert leaked == []


def test_cancelled_request_consumes_real_sync_worker_failure(
    make_service,
):
    service = _with_limit(
        make_service(),
        "approval",
        requests=20,
        global_requests=40,
        concurrency=1,
    )
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="route-cancel-worker-error-secret",
        planning=None,
    )
    started = threading.Event()
    release_worker = threading.Event()

    def failing_approve(*_args, **_kwargs):
        started.set()
        assert release_worker.wait(timeout=5)
        raise RuntimeError("real sync worker failed after cancellation")

    service.approve_order = failing_approve
    resource_key = (
        "route:"
        + hashlib.sha256(b"order:7").hexdigest()
        + ":0"
    )

    async def exercise():
        leaked: list[dict[str, object]] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(
            lambda _loop, context: leaked.append(context)
        )
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://localhost:8020",
            ) as client:
                login = await client.post(
                    "/auth/login",
                    json={"secret": "route-cancel-worker-error-secret"},
                )
                owner = asyncio.create_task(
                    client.post(
                        "/approve/7",
                        json={"reason": "worker fails after cancellation"},
                        headers={
                            "X-CSRF-Token": login.json()["csrf_token"],
                            "Idempotency-Key": (
                                "cancel-worker-error-owner"
                            ),
                        },
                    )
                )
                assert await asyncio.to_thread(started.wait, 5)
                owner.cancel()
                await asyncio.sleep(0)
                assert owner.done() is False
                release_worker.set()
                with pytest.raises(asyncio.CancelledError):
                    await owner
                await asyncio.sleep(0)
                return leaked
        finally:
            release_worker.set()
            loop.set_exception_handler(previous_handler)

    leaked = asyncio.run(exercise())
    latch = app.state.mutation_interlocks.inspect(resource_key)

    assert latch is not None
    assert latch.state == "uncertain"
    assert latch.outcome_code == "request_cancelled"
    assert latch.worker_finished_at is not None
    assert leaked == []


def test_panic_receipt_renewal_rejects_successor_lease_owner(
    make_service,
):
    service = make_service()
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="panic-renew-successor-secret",
        planning=None,
    )
    lease_key = "route:" + "d" * 64 + ":0"
    original_expiry = utcnow() + timedelta(seconds=30)
    successor_expiry = utcnow() + timedelta(seconds=60)
    _persist_panic_fence(
        service,
        lease_key=lease_key,
        lease_owner="owner-b",
        lease_generation=2,
        lease_expires_at=successor_expiry,
        receipt_owner="owner-a",
        receipt_generation=1,
        receipt_expires_at=original_expiry,
    )

    try:
        renewed = policy_module._renew_panic_receipt(
            app,
            lease_key,
            "owner-a",
            1,
            successor_expiry,
        )
    except TypeError as exc:
        pytest.fail(f"renewal does not accept the lease fence: {exc}")

    assert renewed is False
    with service.session_factory() as session:
        receipt = session.get(PanicReceipt, "alpaca-paper")
        assert receipt.state == "started"
        assert receipt.expires_at == original_expiry


def test_panic_receipt_finish_rejects_successor_lease_owner(
    make_service,
):
    service = make_service()
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="panic-finish-successor-secret",
        planning=None,
    )
    lease_key = "route:" + "e" * 64 + ":0"
    original_expiry = utcnow() + timedelta(seconds=30)
    successor_expiry = utcnow() + timedelta(seconds=60)
    _persist_panic_fence(
        service,
        lease_key=lease_key,
        lease_owner="owner-b",
        lease_generation=2,
        lease_expires_at=successor_expiry,
        receipt_owner="owner-a",
        receipt_generation=1,
        receipt_expires_at=original_expiry,
    )

    try:
        with pytest.raises(LimitStoreUnavailable):
            policy_module._finish_panic_receipt(
                app,
                lease_key,
                "owner-a",
                lease_generation=1,
                response={"safe": True, "owner": "owner-a"},
                expires_at=successor_expiry,
            )
    except TypeError as exc:
        pytest.fail(f"finish does not accept the lease fence: {exc}")

    with service.session_factory() as session:
        receipt = session.get(PanicReceipt, "alpaca-paper")
        assert receipt.state == "started"
        assert receipt.completed_at is None
        assert json.loads(
            decrypt_test_sensitive(receipt, "response_json")
        ) == {}


@pytest.mark.parametrize("operation", ["renew", "finish"])
@pytest.mark.parametrize("clock_change", ["past_horizon", "backward"])
def test_panic_receipt_write_rolls_back_if_post_flush_clock_is_unsafe(
    make_service,
    monkeypatch,
    operation,
    clock_change,
):
    service = make_service()
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token=f"panic-delayed-{operation}-secret",
        planning=None,
    )
    lease_key = "route:" + ("f" if operation == "renew" else "a") * 64 + ":0"
    before = datetime(2026, 7, 28, 16, 0, tzinfo=timezone.utc)
    horizon = before + timedelta(seconds=1)
    original_expiry = before + timedelta(milliseconds=500)
    _persist_panic_fence(
        service,
        lease_key=lease_key,
        lease_owner="owner-a",
        lease_generation=7,
        lease_expires_at=horizon,
        receipt_owner="owner-a",
        receipt_generation=7,
        receipt_expires_at=original_expiry,
    )
    clock_calls = 0

    def crossing_clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls == 1:
            return before
        if clock_change == "backward":
            return before - timedelta(microseconds=1)
        return horizon + timedelta(microseconds=1)

    monkeypatch.setattr(policy_module, "utcnow", crossing_clock)

    try:
        if operation == "renew":
            result = policy_module._renew_panic_receipt(
                app,
                lease_key,
                "owner-a",
                7,
                horizon,
            )
            assert result is False
        else:
            with pytest.raises(LimitStoreUnavailable):
                policy_module._finish_panic_receipt(
                    app,
                    lease_key,
                    "owner-a",
                    lease_generation=7,
                    response={"safe": True},
                    expires_at=horizon,
                )
    except TypeError as exc:
        pytest.fail(f"receipt write does not accept lease fence: {exc}")

    assert clock_calls >= 2
    with service.session_factory() as session:
        receipt = session.get(PanicReceipt, "alpaca-paper")
        assert receipt.state == "started"
        assert receipt.completed_at is None
        assert receipt.expires_at == original_expiry
        assert json.loads(
            decrypt_test_sensitive(receipt, "response_json")
        ) == {}


def test_panic_receipt_finish_fails_closed_when_sqlite_writer_is_busy(
    make_service,
    engine,
):
    service = make_service()
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="panic-busy-finish-secret",
        planning=None,
    )
    lease_key = "route:" + "b" * 64 + ":0"
    horizon = utcnow() + timedelta(seconds=60)
    _persist_panic_fence(
        service,
        lease_key=lease_key,
        lease_owner="owner-a",
        lease_generation=9,
        lease_expires_at=horizon,
        receipt_owner="owner-a",
        receipt_generation=9,
        receipt_expires_at=horizon,
    )
    holder = engine.connect()
    contender = engine.connect()
    holder.exec_driver_sql("BEGIN IMMEDIATE")
    contender.exec_driver_sql("PRAGMA busy_timeout=1")
    app.state.session_auth.session_factory = sessionmaker(
        bind=contender,
        expire_on_commit=False,
        future=True,
    )

    try:
        try:
            with pytest.raises(LimitStoreUnavailable):
                policy_module._finish_panic_receipt(
                    app,
                    lease_key,
                    "owner-a",
                    lease_generation=9,
                    response={"safe": True},
                    expires_at=horizon,
                )
        except TypeError as exc:
            pytest.fail(f"finish does not accept the lease fence: {exc}")
    finally:
        holder.rollback()
        contender.close()
        holder.close()

    with service.session_factory() as session:
        receipt = session.get(PanicReceipt, "alpaca-paper")
        assert receipt.state == "started"


def test_panic_follower_rejects_completed_a1_after_b2_takeover(
    make_service,
):
    service = make_service()
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="panic-stale-follower-secret",
        planning=None,
    )
    lease_key = "route:" + "c" * 64 + ":0"
    expired = utcnow() - timedelta(seconds=1)
    _persist_panic_fence(
        service,
        lease_key=lease_key,
        lease_owner="owner-b",
        lease_generation=2,
        lease_expires_at=utcnow() + timedelta(seconds=60),
        receipt_owner="owner-a",
        receipt_generation=1,
        receipt_expires_at=expired,
        receipt_state="completed",
        response={"safe": True, "owner": "owner-a"},
    )
    observed = LeaseDecision(
        acquired=True,
        owner="owner-a",
        generation=1,
        expires_at=expired,
        retry_after_seconds=0,
    )

    response = asyncio.run(
        policy_module._wait_for_panic_receipt(
            app,
            _panic_request(app),
            lease_key=lease_key,
            observed=observed,
        )
    )

    assert response.status_code == 503
    assert json.loads(response.body)["error"]["code"] == "panic_incomplete"


def test_panic_follower_replays_completed_a1_after_exact_release(
    make_service,
):
    service = make_service()
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="panic-released-follower-secret",
        planning=None,
    )
    lease_key = "route:" + "8" * 64 + ":0"
    receipt_horizon = utcnow() + timedelta(seconds=60)
    _persist_panic_fence(
        service,
        lease_key=lease_key,
        lease_owner="",
        lease_generation=2,
        lease_expires_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
        receipt_owner="owner-a",
        receipt_generation=1,
        receipt_expires_at=receipt_horizon,
        receipt_state="completed",
        response={"safe": True, "owner": "owner-a"},
    )
    observed = LeaseDecision(
        acquired=True,
        owner="owner-a",
        generation=1,
        expires_at=receipt_horizon,
        retry_after_seconds=60,
    )

    response = asyncio.run(
        policy_module._wait_for_panic_receipt(
            app,
            _panic_request(app),
            lease_key=lease_key,
            observed=observed,
        )
    )

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "safe": True,
        "owner": "owner-a",
    }


def test_panic_follower_rejects_receipt_if_horizon_expires_during_read(
    make_service,
    monkeypatch,
):
    service = make_service()
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="panic-expiring-follower-secret",
        planning=None,
    )
    lease_key = "route:" + "7" * 64 + ":0"
    before = datetime(2026, 7, 28, 17, 0, tzinfo=timezone.utc)
    horizon = before + timedelta(seconds=1)
    _persist_panic_fence(
        service,
        lease_key=lease_key,
        lease_owner="owner-a",
        lease_generation=1,
        lease_expires_at=horizon,
        receipt_owner="owner-a",
        receipt_generation=1,
        receipt_expires_at=horizon,
        receipt_state="completed",
        response={"safe": True, "owner": "owner-a"},
    )
    clock_calls = 0

    def crossing_clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return before if clock_calls == 1 else horizon + timedelta(microseconds=1)

    monkeypatch.setattr(policy_module, "utcnow", crossing_clock)
    observed = LeaseDecision(
        acquired=True,
        owner="owner-a",
        generation=1,
        expires_at=horizon,
        retry_after_seconds=1,
    )

    response = asyncio.run(
        policy_module._wait_for_panic_receipt(
            app,
            _panic_request(app),
            lease_key=lease_key,
            observed=observed,
        )
    )

    assert clock_calls >= 2
    assert response.status_code == 503
    assert json.loads(response.body)["error"]["code"] == "panic_incomplete"


def test_cancel_during_panic_finish_replays_fence_without_reexecution(
    make_service,
    monkeypatch,
):
    service = make_service()
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="panic-cancel-finish-secret",
        planning=None,
    )
    calls = 0
    finish_started = threading.Event()
    release_finish = threading.Event()
    original_finish = policy_module._finish_panic_receipt

    def blocking_finish(*args, **kwargs):
        finish_started.set()
        assert release_finish.wait(timeout=5)
        return original_finish(*args, **kwargs)

    def panic(_context):
        nonlocal calls
        calls += 1
        return {"safe": True, "owner": "cancelled-request"}

    monkeypatch.setattr(
        policy_module,
        "_finish_panic_receipt",
        blocking_finish,
    )
    app.state.operations.panic = panic

    async def exercise():
        leaked: list[dict[str, object]] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(
            lambda _loop, context: leaked.append(context)
        )
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://localhost:8020",
            ) as client:
                login = await client.post(
                    "/auth/login",
                    json={"secret": "panic-cancel-finish-secret"},
                )
                headers = {
                    "X-CSRF-Token": login.json()["csrf_token"],
                    "Idempotency-Key": "panic-cancel-finish-owner",
                }
                owner = asyncio.create_task(
                    client.post(
                        "/panic",
                        json={"reason": "cancel while receipt flushes"},
                        headers=headers,
                    )
                )
                assert await asyncio.to_thread(finish_started.wait, 5)
                owner.cancel()
                await asyncio.sleep(0)
                assert owner.done() is False
                release_finish.set()
                with pytest.raises(asyncio.CancelledError):
                    await owner
                retry = await client.post(
                    "/panic",
                    json={"reason": "must not execute twice"},
                    headers={
                        "X-CSRF-Token": login.json()["csrf_token"],
                        "Idempotency-Key": "panic-cancel-finish-retry",
                    },
                )
                await asyncio.sleep(0)
                return retry, leaked
        finally:
            release_finish.set()
            loop.set_exception_handler(previous_handler)

    retry, leaked = asyncio.run(exercise())

    assert retry.status_code == 200
    assert retry.json() == {
        "safe": True,
        "owner": "cancelled-request",
    }
    assert calls == 1
    assert leaked == []
    with service.session_factory() as session:
        receipt = session.get(PanicReceipt, "alpaca-paper")
        assert receipt.state == "completed"


def test_panic_follower_store_unknown_fails_closed(
    make_service,
    monkeypatch,
):
    service = make_service()
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="panic-follower-store-secret",
        planning=None,
    )

    def unavailable(*_args, **_kwargs):
        raise LimitStoreUnavailable("unknown sqlite result")

    monkeypatch.setattr(
        policy_module,
        "_observe_panic_fence",
        unavailable,
    )
    observed = LeaseDecision(
        acquired=True,
        owner="owner-a",
        generation=1,
        expires_at=utcnow() + timedelta(seconds=60),
        retry_after_seconds=60,
    )

    response = asyncio.run(
        policy_module._wait_for_panic_receipt(
            app,
            _panic_request(app),
            lease_key="route:" + "9" * 64 + ":0",
            observed=observed,
        )
    )

    assert response.status_code == 503
    assert json.loads(response.body)["error"]["code"] == (
        "policy_store_unavailable"
    )


def test_panic_receipt_renewal_store_loss_stops_without_renewing_again(
    make_service,
    monkeypatch,
):
    monkeypatch.setattr(
        policy_module,
        "_LEASE_TTL_SECONDS",
        1,
    )
    monkeypatch.setattr(
        policy_module,
        "_LEASE_RENEW_INTERVAL_SECONDS",
        0.01,
    )
    app = create_app(
        service=make_service(),
        agent=_StubAgent(),
        api_token="route-receipt-renewal-secret",
        planning=None,
    )
    renew_calls = 0
    receipt_calls = 0
    repeated_receipt_failure = threading.Event()
    hold = policy_module._LeaseHold(
        resource_key="route:" + "e" * 64 + ":0",
        owner="internal-panic-owner",
        generation=11,
        expires_at=utcnow() + timedelta(seconds=1),
    )

    class SuccessfulLeaseRenewal:
        def renew(self, *_args, **_kwargs):
            nonlocal renew_calls
            renew_calls += 1
            return LeaseDecision(
                acquired=True,
                owner=hold.owner,
                generation=hold.generation,
                expires_at=utcnow() + timedelta(seconds=5),
                retry_after_seconds=0,
            )

    def unavailable_receipt_store(*_args, **_kwargs):
        nonlocal receipt_calls
        receipt_calls += 1
        if receipt_calls >= 2:
            repeated_receipt_failure.set()
        raise LimitStoreUnavailable("receipt renewal unavailable")

    app.state.leases = SuccessfulLeaseRenewal()
    monkeypatch.setattr(
        policy_module,
        "_renew_panic_receipt",
        unavailable_receipt_store,
    )

    async def exercise():
        leaked: list[dict[str, object]] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(
            lambda _loop, context: leaked.append(context)
        )
        stop = asyncio.Event()
        task = asyncio.create_task(
            policy_module._maintain_lease(
                app,
                hold,
                stop,
                panic_request_id="internal-panic-owner",
            )
        )
        try:
            assert await asyncio.to_thread(
                repeated_receipt_failure.wait,
                2,
            )
            stop.set()
            result = await asyncio.wait_for(task, timeout=0.5)
            await asyncio.sleep(0)
            return result, leaked
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except BaseException:
                pass
            loop.set_exception_handler(previous_handler)

    result, leaked = asyncio.run(exercise())

    assert result == "store"
    assert renew_calls == 1
    assert receipt_calls >= 2
    assert leaked == []


@pytest.mark.parametrize("attempt", range(20))
def test_panic_stop_after_lease_renewal_completes_receipt_fence(
    make_service,
    monkeypatch,
    attempt,
):
    monkeypatch.setattr(
        policy_module,
        "_LEASE_RENEW_INTERVAL_SECONDS",
        0.01,
    )
    app = create_app(
        service=make_service(),
        agent=_StubAgent(),
        api_token="route-receipt-fence-secret",
        planning=None,
    )
    lease_renewed = threading.Event()
    allow_renewal_return = threading.Event()
    receipt_calls = 0
    hold = policy_module._LeaseHold(
        resource_key=f"route:{attempt:064x}:0",
        owner=f"fenced-panic-owner-{attempt}",
        generation=17 + attempt,
        expires_at=utcnow() + timedelta(seconds=1),
        ttl_seconds=1,
    )
    renewed_expires_at = utcnow() + timedelta(seconds=5)

    class BlockingSuccessfulLeaseRenewal:
        def renew(self, *_args, **_kwargs):
            lease_renewed.set()
            assert allow_renewal_return.wait(timeout=5)
            return LeaseDecision(
                acquired=True,
                owner=hold.owner,
                generation=hold.generation,
                expires_at=renewed_expires_at,
                retry_after_seconds=0,
            )

    def renew_receipt(
        _app,
        lease_key,
        request_id,
        generation,
        expires_at,
    ):
        nonlocal receipt_calls
        receipt_calls += 1
        assert lease_key == hold.resource_key
        assert request_id == hold.owner
        assert generation == hold.generation
        assert expires_at == renewed_expires_at
        return True

    app.state.leases = BlockingSuccessfulLeaseRenewal()
    monkeypatch.setattr(
        policy_module,
        "_renew_panic_receipt",
        renew_receipt,
    )

    async def exercise():
        stop = asyncio.Event()
        task = asyncio.create_task(
            policy_module._maintain_lease(
                app,
                hold,
                stop,
                panic_request_id=hold.owner,
            )
        )
        assert await asyncio.to_thread(lease_renewed.wait, 2)
        stop.set()
        allow_renewal_return.set()
        return await asyncio.wait_for(task, timeout=1)

    try:
        result = asyncio.run(exercise())
    finally:
        allow_renewal_return.set()

    assert result is None
    assert receipt_calls == 1
    assert hold.expires_at == renewed_expires_at


def test_panic_receipt_fence_uses_successfully_renewed_horizon(
    make_service,
    monkeypatch,
):
    monkeypatch.setattr(
        policy_module,
        "_LEASE_RENEW_INTERVAL_SECONDS",
        0.01,
    )
    app = create_app(
        service=make_service(),
        agent=_StubAgent(),
        api_token="route-renewed-horizon-secret",
        planning=None,
    )
    initial_now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    original_expires_at = initial_now + timedelta(seconds=1)
    renewed_expires_at = initial_now + timedelta(seconds=5)
    observed_now = [initial_now]
    stop_holder = []
    loop_holder = []
    receipt_calls = 0
    hold = policy_module._LeaseHold(
        resource_key="route:" + "a" * 64 + ":0",
        owner="renewed-horizon-owner",
        generation=23,
        expires_at=original_expires_at,
        ttl_seconds=1,
    )

    monkeypatch.setattr(
        policy_module,
        "utcnow",
        lambda: observed_now[0],
    )

    class DelayedSuccessfulLeaseRenewal:
        def renew(self, *_args, **_kwargs):
            observed_now[0] = original_expires_at + timedelta(
                milliseconds=1
            )
            return LeaseDecision(
                acquired=True,
                owner=hold.owner,
                generation=hold.generation,
                expires_at=renewed_expires_at,
                retry_after_seconds=0,
            )

    def renew_receipt(
        _app,
        lease_key,
        request_id,
        generation,
        expires_at,
    ):
        nonlocal receipt_calls
        receipt_calls += 1
        assert lease_key == hold.resource_key
        assert request_id == hold.owner
        assert generation == hold.generation
        assert expires_at == renewed_expires_at
        loop_holder[0].call_soon_threadsafe(stop_holder[0].set)
        return True

    app.state.leases = DelayedSuccessfulLeaseRenewal()
    monkeypatch.setattr(
        policy_module,
        "_renew_panic_receipt",
        renew_receipt,
    )

    async def exercise():
        stop = asyncio.Event()
        stop_holder.append(stop)
        loop_holder.append(asyncio.get_running_loop())
        return await asyncio.wait_for(
            policy_module._maintain_lease(
                app,
                hold,
                stop,
                panic_request_id=hold.owner,
            ),
            timeout=1,
        )

    result = asyncio.run(exercise())

    assert result is None
    assert receipt_calls == 1
    assert hold.expires_at == renewed_expires_at


def test_panic_receipt_transient_store_busy_retries_with_same_fence(
    make_service,
    monkeypatch,
):
    monkeypatch.setattr(
        policy_module,
        "_LEASE_RENEW_INTERVAL_SECONDS",
        0.01,
    )
    app = create_app(
        service=make_service(),
        agent=_StubAgent(),
        api_token="route-transient-receipt-secret",
        planning=None,
    )
    hold = policy_module._LeaseHold(
        resource_key="route:" + "b" * 64 + ":0",
        owner="transient-receipt-owner",
        generation=29,
        expires_at=utcnow() + timedelta(seconds=1),
        ttl_seconds=1,
    )
    renewed_expires_at = utcnow() + timedelta(seconds=2)
    receipt_calls = 0
    stop_holder = []
    loop_holder = []

    class SuccessfulLeaseRenewal:
        def renew(self, *_args, **_kwargs):
            return LeaseDecision(
                acquired=True,
                owner=hold.owner,
                generation=hold.generation,
                expires_at=renewed_expires_at,
                retry_after_seconds=0,
            )

    def transient_receipt_store(
        _app,
        lease_key,
        request_id,
        generation,
        expires_at,
    ):
        nonlocal receipt_calls
        receipt_calls += 1
        assert lease_key == hold.resource_key
        assert request_id == hold.owner
        assert generation == hold.generation
        assert expires_at == renewed_expires_at
        if receipt_calls == 1:
            raise LimitStoreUnavailable("temporary sqlite busy")
        loop_holder[0].call_soon_threadsafe(stop_holder[0].set)
        return True

    app.state.leases = SuccessfulLeaseRenewal()
    monkeypatch.setattr(
        policy_module,
        "_renew_panic_receipt",
        transient_receipt_store,
    )

    async def exercise():
        stop = asyncio.Event()
        stop_holder.append(stop)
        loop_holder.append(asyncio.get_running_loop())
        return await asyncio.wait_for(
            policy_module._maintain_lease(
                app,
                hold,
                stop,
                panic_request_id=hold.owner,
            ),
            timeout=1,
        )

    result = asyncio.run(exercise())

    assert result is None
    assert receipt_calls == 2
    assert hold.expires_at == renewed_expires_at


def test_panic_renewal_fails_closed_on_genuine_fence_loss(
    make_service,
    monkeypatch,
):
    monkeypatch.setattr(
        policy_module,
        "_LEASE_RENEW_INTERVAL_SECONDS",
        0.01,
    )
    app = create_app(
        service=make_service(),
        agent=_StubAgent(),
        api_token="route-fence-loss-secret",
        planning=None,
    )
    receipt_calls = 0
    hold = policy_module._LeaseHold(
        resource_key="route:" + "c" * 64 + ":0",
        owner="predecessor-owner",
        generation=31,
        expires_at=utcnow() + timedelta(seconds=1),
        ttl_seconds=1,
    )

    class SuccessorOwnedLease:
        def renew(self, *_args, **_kwargs):
            return LeaseDecision(
                acquired=False,
                owner="successor-owner",
                generation=hold.generation + 1,
                expires_at=utcnow() + timedelta(seconds=2),
                retry_after_seconds=2,
            )

    def forbidden_receipt_renewal(*_args, **_kwargs):
        nonlocal receipt_calls
        receipt_calls += 1
        return True

    app.state.leases = SuccessorOwnedLease()
    monkeypatch.setattr(
        policy_module,
        "_renew_panic_receipt",
        forbidden_receipt_renewal,
    )

    result = asyncio.run(
        policy_module._maintain_lease(
            app,
            hold,
            asyncio.Event(),
            panic_request_id=hold.owner,
        )
    )

    assert result == "lost"
    assert receipt_calls == 0


@pytest.mark.parametrize("reconciled", [True, False])
def test_reconcile_clears_only_after_proven_truth_and_atomic_audit(
    make_service,
    reconciled,
):
    service_type = getattr(
        limits_module,
        "MutationInterlockService",
        None,
    )
    assert service_type is not None
    service = make_service()
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="route-reconcile-latch-secret",
        planning=None,
    )
    interlocks = service_type(service.session_factory)
    app.state.mutation_interlocks = interlocks
    resource_key = (
        "route:"
        + hashlib.sha256(
            b"portfolio:alpaca-paper"
        ).hexdigest()
        + ":0"
    )
    claimed = interlocks.claim(
        resource_key,
        owner="abandoned-reconcile-owner",
        generation=5,
        operation="portfolio_reconcile",
    )
    assert claimed.acquired is True
    assert interlocks.mark_uncertain(
        resource_key,
        owner=claimed.owner,
        generation=claimed.generation,
        outcome_code="request_cancelled",
        worker_finished=True,
    )

    if not reconciled:
        service.reconcile_positions = lambda **_kwargs: {
            "reconciled": False,
            "drift": {"AAPL": {"broker": "1", "local": "0"}},
        }

    client = TestClient(app)
    login = client.post(
        "/auth/login",
        json={"secret": "route-reconcile-latch-secret"},
    )
    response = client.post(
        "/reconcile",
        json={"reason": "prove broker and domain truth"},
        headers={
            "X-CSRF-Token": login.json()["csrf_token"],
            "Idempotency-Key": f"reconcile-proof-{reconciled}",
        },
    )

    assert response.status_code == 200
    assert response.json()["reconciled"] is reconciled
    remaining = interlocks.inspect(resource_key)
    with service.session_factory() as session:
        reconciliation_audits = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action
                    == "mutation_interlock.reconcile",
                    AuditEvent.request_id
                    == response.headers["X-Request-ID"],
                )
            )
        )
    if reconciled:
        assert remaining is None
        assert len(reconciliation_audits) == 1
        assert decrypt_test_sensitive(
            reconciliation_audits[0],
            "reason",
        ) == (
            "portfolio_truth_reconciled"
        )
        assert reconciliation_audits[0].result_code == "cleared"
    else:
        assert remaining is not None
        assert remaining.state == "uncertain"
        assert reconciliation_audits == []


@pytest.mark.parametrize(
    ("replacement_owner", "replacement_generation"),
    [
        ("owner-request-b", 42),
        ("owner-request-a", 42),
    ],
)
def test_panic_follower_never_accepts_replacement_owner_receipt(
    make_service,
    replacement_owner,
    replacement_generation,
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
        persist_sensitive(
            session,
            PanicReceipt(
                account_scope="alpaca-paper",
                request_id="owner-request-a",
                lease_generation=41,
                state="started",
                started_at=utcnow(),
                expires_at=expires_at,
            ),
            {"response_json": "{}"},
            session_factory=service.session_factory,
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
            row.request_id = replacement_owner
            row.lease_generation = replacement_generation
            row.state = "completed"
            persist_sensitive(
                session,
                row,
                {
                    "response_json": json.dumps(
                        {"safe": True, "owner": "request-b"}
                    )
                },
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
    original_renew = app.state.leases.renew
    original_wait_for_receipt = policy_module._wait_for_panic_receipt
    acquired_owners: list[str] = []
    lease_renewed = threading.Event()
    follower_waiting = threading.Event()

    def record_acquire(*args, **kwargs):
        decision = original_acquire(*args, **kwargs)
        if decision.acquired:
            acquired_owners.append(decision.owner)
        return decision

    def record_renew(*args, **kwargs):
        decision = original_renew(*args, **kwargs)
        if decision.acquired:
            lease_renewed.set()
        return decision

    async def record_wait_for_receipt(*args, **kwargs):
        follower_waiting.set()
        return await original_wait_for_receipt(*args, **kwargs)

    app.state.leases.acquire = record_acquire
    app.state.leases.renew = record_renew
    monkeypatch.setattr(
        policy_module,
        "_wait_for_panic_receipt",
        record_wait_for_receipt,
    )
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
        assert lease_renewed.wait(timeout=5)
        follower = pool.submit(
            follower_client.post,
            "/panic",
            json={"reason": "must follow original"},
            headers=follower_headers,
        )
        assert follower_waiting.wait(timeout=5)
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


def test_panic_common_mode_receipt_failure_remains_latched_without_expiry(
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
        0.05,
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
        api_token="route-panic-common-store-secret",
        planning=None,
    )
    client = TestClient(app)
    login = client.post(
        "/auth/login",
        json={"secret": "route-panic-common-store-secret"},
    )
    calls = 0
    receipt = {"safe": True, "owner": "common-store-failure"}

    def panic(_context):
        nonlocal calls
        calls += 1
        return receipt

    def fail_receipt_store(*_args, **_kwargs):
        raise LimitStoreUnavailable("coupled receipt store unavailable")

    app.state.operations.panic = panic
    monkeypatch.setattr(
        policy_module,
        "_finish_panic_receipt",
        fail_receipt_store,
    )
    monkeypatch.setattr(
        policy_module,
        "_mark_panic_uncertain",
        fail_receipt_store,
    )

    first = client.post(
        "/panic",
        json={"reason": "panic common-mode store failure"},
        headers={
            "X-CSRF-Token": login.json()["csrf_token"],
            "Idempotency-Key": "panic-common-store-owner",
        },
    )
    time.sleep(1.1)
    retry = client.post(
        "/panic",
        json={"reason": "must not duplicate broker panic"},
        headers={
            "X-CSRF-Token": login.json()["csrf_token"],
            "Idempotency-Key": "panic-common-store-retry",
        },
    )

    assert first.status_code == 200
    assert first.json() == receipt
    assert retry.status_code == 503
    assert retry.json()["error"]["code"] == "panic_incomplete"
    assert "Retry-After" not in retry.headers
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
    def fail_atomic_release(*_args, **_kwargs):
        if release_failure == "exception":
            raise LimitStoreUnavailable("release failed")
        return False

    app.state.mutation_interlocks.release_settled = fail_atomic_release
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
            base_url="https://localhost:8020",
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
