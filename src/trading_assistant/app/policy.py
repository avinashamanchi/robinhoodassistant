"""Complete, centrally validated policy inventory for HTTP routes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
import hashlib
import json
import math
from pathlib import PurePosixPath
import time
from typing import Literal
from urllib.parse import unquote
from uuid import uuid4

from fastapi import FastAPI, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import JSONResponse, Response
from starlette.routing import Match, Mount

from trading_assistant.db.models import PanicReceipt, utcnow

from .auth import RecentAuthenticationRequired, SessionPrincipal
from .errors import ApiError
from .limits import (
    ConcurrencyLeaseService,
    DurableRateLimiter,
    LeaseDecision,
    LimitDecision,
    LimitSpec,
    LimitStoreUnavailable,
)


class AuthLevel(str, Enum):
    PUBLIC = "public"
    SESSION = "session"
    CSRF = "csrf"
    RECENT = "recent"


@dataclass(frozen=True)
class RoutePolicy:
    method: str
    path: str
    auth: AuthLevel
    limit_name: str
    body_limit_name: str = "default"
    requires_idempotency: bool = False
    audit_mutation: bool = False
    broker_read: bool = False
    provider_category: str | None = None
    concurrency_scope: str = "principal"
    concurrency_behavior: Literal["reject", "coalesce_panic"] = "reject"
    target_param: str | None = None


@dataclass(frozen=True)
class ResolvedRoute:
    policy: RoutePolicy
    path_params: dict[str, str]


@dataclass(frozen=True)
class _PanicSnapshot:
    state: str
    response: dict[str, object] | None


ROUTE_POLICIES = (
    RoutePolicy("GET", "/health/live", AuthLevel.PUBLIC, "session_read"),
    RoutePolicy("GET", "/login", AuthLevel.PUBLIC, "session_read"),
    RoutePolicy("POST", "/auth/login", AuthLevel.PUBLIC, "login"),
    RoutePolicy("GET", "/auth/session", AuthLevel.SESSION, "session_read"),
    RoutePolicy("POST", "/auth/reauth", AuthLevel.CSRF, "privileged"),
    RoutePolicy("POST", "/auth/logout", AuthLevel.CSRF, "mutation"),
    RoutePolicy("GET", "/", AuthLevel.SESSION, "session_read"),
    RoutePolicy(
        "POST",
        "/chat",
        AuthLevel.CSRF,
        "chat",
        "chat",
        audit_mutation=True,
        provider_category="chat",
    ),
    RoutePolicy("GET", "/health", AuthLevel.SESSION, "session_read"),
    RoutePolicy("GET", "/pending", AuthLevel.SESSION, "session_read"),
    RoutePolicy(
        "GET",
        "/pending/{order_id}/confirmation",
        AuthLevel.SESSION,
        "session_read",
    ),
    RoutePolicy(
        "POST",
        "/approve/{order_id}",
        AuthLevel.RECENT,
        "approval",
        requires_idempotency=True,
        audit_mutation=True,
        broker_read=True,
        concurrency_scope="target",
        target_param="order_id",
    ),
    RoutePolicy(
        "POST",
        "/reject/{order_id}",
        AuthLevel.CSRF,
        "mutation",
        requires_idempotency=True,
        audit_mutation=True,
        concurrency_scope="target",
        target_param="order_id",
    ),
    RoutePolicy(
        "GET",
        "/positions",
        AuthLevel.SESSION,
        "broker_read",
        broker_read=True,
    ),
    RoutePolicy(
        "GET",
        "/account",
        AuthLevel.SESSION,
        "broker_read",
        broker_read=True,
    ),
    RoutePolicy("GET", "/log", AuthLevel.SESSION, "session_read"),
    RoutePolicy(
        "POST",
        "/killswitch/reset",
        AuthLevel.RECENT,
        "privileged",
        requires_idempotency=True,
        audit_mutation=True,
        concurrency_scope="account",
    ),
    RoutePolicy(
        "POST",
        "/orders/{order_id}/cancel",
        AuthLevel.CSRF,
        "mutation",
        requires_idempotency=True,
        audit_mutation=True,
        concurrency_scope="target",
        target_param="order_id",
    ),
    RoutePolicy(
        "POST",
        "/reconcile",
        AuthLevel.RECENT,
        "privileged",
        requires_idempotency=True,
        audit_mutation=True,
        broker_read=True,
    ),
    RoutePolicy(
        "POST",
        "/sync",
        AuthLevel.CSRF,
        "mutation",
        requires_idempotency=True,
        audit_mutation=True,
        broker_read=True,
    ),
    RoutePolicy(
        "POST",
        "/panic",
        AuthLevel.RECENT,
        "panic",
        requires_idempotency=True,
        audit_mutation=True,
        broker_read=True,
        concurrency_scope="account",
        concurrency_behavior="coalesce_panic",
    ),
    RoutePolicy(
        "GET",
        "/analyst/scorecard",
        AuthLevel.SESSION,
        "session_read",
    ),
    RoutePolicy(
        "POST",
        "/analyze",
        AuthLevel.CSRF,
        "analysis",
        requires_idempotency=True,
        audit_mutation=True,
        broker_read=True,
        provider_category="analysis",
    ),
    RoutePolicy("GET", "/plans", AuthLevel.SESSION, "session_read"),
    RoutePolicy("GET", "/plans/ui", AuthLevel.SESSION, "session_read"),
    RoutePolicy(
        "GET",
        "/plans/{plan_id}",
        AuthLevel.SESSION,
        "session_read",
    ),
    RoutePolicy(
        "POST",
        "/plans/{plan_id}/approve",
        AuthLevel.RECENT,
        "approval",
        requires_idempotency=True,
        audit_mutation=True,
        broker_read=True,
        concurrency_scope="target",
        target_param="plan_id",
    ),
    RoutePolicy(
        "POST",
        "/plans/{plan_id}/cancel",
        AuthLevel.CSRF,
        "mutation",
        requires_idempotency=True,
        audit_mutation=True,
        concurrency_scope="target",
        target_param="plan_id",
    ),
    RoutePolicy(
        "POST",
        "/screen",
        AuthLevel.CSRF,
        "analysis",
        provider_category="analysis",
        broker_read=True,
    ),
    RoutePolicy(
        "POST",
        "/propose",
        AuthLevel.CSRF,
        "analysis",
        requires_idempotency=True,
        audit_mutation=True,
        provider_category="analysis",
        broker_read=True,
    ),
    RoutePolicy(
        "GET",
        "/holdings",
        AuthLevel.SESSION,
        "broker_read",
        broker_read=True,
    ),
    RoutePolicy(
        "GET",
        "/external/positions",
        AuthLevel.SESSION,
        "session_read",
    ),
    RoutePolicy(
        "GET",
        "/external/summary",
        AuthLevel.SESSION,
        "session_read",
    ),
    RoutePolicy("GET", "/backtests", AuthLevel.SESSION, "session_read"),
    RoutePolicy(
        "POST",
        "/backtests/run",
        AuthLevel.CSRF,
        "backtest",
        requires_idempotency=True,
        audit_mutation=True,
        provider_category="backtest",
    ),
    RoutePolicy(
        "GET",
        "/backtests/{run_id}/report",
        AuthLevel.SESSION,
        "session_read",
    ),
    RoutePolicy(
        "GET",
        "/backtests/ui",
        AuthLevel.SESSION,
        "session_read",
    ),
    RoutePolicy(
        "GET",
        "/static/{path:path}",
        AuthLevel.PUBLIC,
        "session_read",
    ),
)


class RoutePolicyRegistry:
    def __init__(
        self,
        policies: tuple[RoutePolicy, ...] = ROUTE_POLICIES,
    ) -> None:
        self._policies = policies
        self._by_key = {
            (policy.method.upper(), policy.path): policy
            for policy in policies
        }

    def get(self, method: str, path: str) -> RoutePolicy:
        return self._by_key[(method.upper(), path)]

    def resolve(
        self,
        app: FastAPI,
        method: str,
        path: str,
    ) -> ResolvedRoute | None:
        scope = {
            "type": "http",
            "path": path,
            "root_path": "",
            "method": method.upper(),
        }
        for route in self._routes(app):
            match, child_scope = route.matches(scope)
            if match is not Match.FULL:
                continue
            template = (
                "/static/{path:path}"
                if isinstance(route, Mount) and route.path == "/static"
                else getattr(route, "path", "")
            )
            policy_method = (
                "GET"
                if (
                    isinstance(route, Mount)
                    and route.path == "/static"
                    and method.upper() == "HEAD"
                )
                else method.upper()
            )
            policy = self._by_key.get((policy_method, template))
            if policy is None:
                return None
            return ResolvedRoute(
                policy=policy,
                path_params={
                    key: str(value)
                    for key, value in child_scope.get(
                        "path_params",
                        {},
                    ).items()
                },
            )
        return None

    @staticmethod
    def _routes(app: FastAPI):
        pending = list(app.routes)
        while pending:
            route = pending.pop(0)
            included = getattr(route, "original_router", None)
            if included is not None:
                pending[0:0] = list(included.routes)
                continue
            yield route

    def duplicates(self) -> list[tuple[str, str]]:
        seen: set[tuple[str, str]] = set()
        duplicate_keys: set[tuple[str, str]] = set()
        for policy in self._policies:
            key = (policy.method.upper(), policy.path)
            if key in seen:
                duplicate_keys.add(key)
            seen.add(key)
        return sorted(duplicate_keys)

    def unclassified(self, app: FastAPI) -> list[tuple[str, str]]:
        policy_keys = set(self._by_key)
        route_keys: set[tuple[str, str]] = set()
        for route in self._routes(app):
            if isinstance(route, Mount):
                if route.path == "/static":
                    route_keys.add(("GET", "/static/{path:path}"))
                continue
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if path is None or methods is None:
                continue
            route_keys.update((method.upper(), path) for method in methods)
        return sorted(route_keys - policy_keys)


def validate_route_inventory(app: FastAPI) -> None:
    registry: RoutePolicyRegistry = app.state.route_policy_registry
    duplicates = registry.duplicates()
    unclassified = registry.unclassified(app)
    if duplicates or unclassified:
        raise RuntimeError(
            "route policy inventory invalid: "
            f"duplicates={duplicates!r} unclassified={unclassified!r}"
        )


def _limit_spec(app: FastAPI, policy: RoutePolicy) -> LimitSpec:
    configured = getattr(
        app.state.trading_service.config.security.rate_limits,
        policy.limit_name,
    )
    return LimitSpec(
        name=policy.limit_name,
        principal_requests=configured.requests,
        global_requests=configured.global_requests,
        window_seconds=configured.window_seconds,
        principal_daily_requests=configured.daily_requests,
        global_daily_requests=configured.global_daily_requests,
    )


def _rate_headers(
    app: FastAPI,
    policy: RoutePolicy,
    decision: LimitDecision,
) -> dict[str, str]:
    configured = getattr(
        app.state.trading_service.config.security.rate_limits,
        policy.limit_name,
    )
    return {
        "X-RateLimit-Limit": str(configured.requests),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str(
            math.ceil(decision.reset_at.timestamp())
        ),
        "Retry-After": str(decision.retry_after_seconds),
    }


def _lease_keys(
    app: FastAPI,
    resolved: ResolvedRoute,
    *,
    limit_principal: str,
) -> list[str]:
    policy = resolved.policy
    if policy.concurrency_scope == "target":
        target = resolved.path_params.get(policy.target_param or "", "")
        material = f"target:{policy.target_param}:{target}"
    elif policy.concurrency_scope == "account":
        material = "account:alpaca-paper"
    else:
        material = f"principal:{policy.limit_name}:{limit_principal}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    configured = getattr(
        app.state.trading_service.config.security.rate_limits,
        policy.limit_name,
    )
    return [
        f"route:{digest}:{slot}"
        for slot in range(configured.concurrency)
    ]


def _policy_error_response(
    request: Request,
    error: ApiError,
    *,
    headers: dict[str, str] | None = None,
):
    from .security import _error_response

    response = _error_response(request, error)
    for name, value in (headers or {}).items():
        response.headers[name] = value
    return response


def _authenticate(
    request: Request,
    policy: RoutePolicy,
) -> SessionPrincipal | None:
    if policy.auth is AuthLevel.PUBLIC:
        return None
    auth = request.app.state.session_auth
    token = request.cookies.get(auth.cookie_name(), "")
    if policy.auth is AuthLevel.SESSION:
        principal = auth.authenticate(token)
    else:
        principal = auth.require_csrf(
            token,
            request.headers.get("X-CSRF-Token", ""),
        )
    if policy.auth is AuthLevel.RECENT:
        age = auth.now() - principal.authenticated_at
        if (
            age.total_seconds() < 0
            or age > auth.reauthentication_window
        ):
            raise RecentAuthenticationRequired
    request.state.principal = principal
    return principal


def _is_safe_static_asset(request: Request) -> bool:
    raw_path = request.scope.get("raw_path", b"")
    encoded = raw_path.decode("ascii", errors="ignore")
    candidates = (
        encoded,
        unquote(encoded),
        unquote(unquote(encoded)),
        request.url.path,
    )
    for candidate in candidates:
        if not candidate.startswith("/static/"):
            return False
        relative = candidate.removeprefix("/static/")
        segments = relative.split("/")
        if (
            not relative
            or relative.endswith("/")
            or "\\" in relative
            or any(segment in {".", ".."} for segment in segments)
        ):
            return False
    return (
        PurePosixPath(request.url.path).suffix.lower()
        in {".css", ".js", ".svg"}
    )


def _panic_snapshot(app: FastAPI) -> _PanicSnapshot | None:
    try:
        with app.state.session_auth.session_factory() as session:
            row = session.get(PanicReceipt, "alpaca-paper")
            if row is None or row.expires_at <= utcnow():
                return None
            payload = (
                json.loads(row.response_json)
                if row.response_json is not None
                else None
            )
            return _PanicSnapshot(row.state, payload)
    except (SQLAlchemyError, OSError, ValueError, TypeError) as exc:
        raise LimitStoreUnavailable(
            "durable panic receipt store unavailable"
        ) from exc


def _start_panic_receipt(app: FastAPI, request_id: str) -> bool:
    now = utcnow()
    try:
        with app.state.session_auth.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(PanicReceipt, "alpaca-paper")
            if row is not None and row.expires_at > now:
                session.rollback()
                return False
            if row is None:
                row = PanicReceipt(account_scope="alpaca-paper")
                session.add(row)
            row.request_id = request_id
            row.state = "started"
            row.response_json = None
            row.started_at = now
            row.completed_at = None
            row.expires_at = now + timedelta(seconds=90)
            session.commit()
            return True
    except (SQLAlchemyError, OSError) as exc:
        raise LimitStoreUnavailable(
            "durable panic receipt store unavailable"
        ) from exc


def _finish_panic_receipt(
    app: FastAPI,
    request_id: str,
    *,
    response: dict[str, object] | None,
) -> None:
    now = utcnow()
    try:
        with app.state.session_auth.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(PanicReceipt, "alpaca-paper")
            if row is None or row.request_id != request_id:
                raise LimitStoreUnavailable(
                    "durable panic receipt ownership changed"
                )
            row.state = "completed" if response is not None else "failed"
            row.response_json = (
                json.dumps(
                    response,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if response is not None
                else None
            )
            row.completed_at = now
            session.commit()
    except LimitStoreUnavailable:
        raise
    except (SQLAlchemyError, OSError) as exc:
        raise LimitStoreUnavailable(
            "durable panic receipt store unavailable"
        ) from exc


def _discard_panic_receipt(app: FastAPI, request_id: str) -> None:
    try:
        with app.state.session_auth.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(PanicReceipt, "alpaca-paper")
            if row is not None and row.request_id == request_id:
                session.delete(row)
            session.commit()
    except (SQLAlchemyError, OSError) as exc:
        raise LimitStoreUnavailable(
            "durable panic receipt store unavailable"
        ) from exc


def _panic_response(request: Request, snapshot: _PanicSnapshot):
    if snapshot.state == "completed" and snapshot.response is not None:
        if snapshot.response.get("safe") is True:
            return JSONResponse(snapshot.response)
        return _policy_error_response(
            request,
            ApiError(
                "panic_incomplete",
                503,
                "Panic could not confirm a safe state",
                receipt=snapshot.response,
            ),
        )
    return _policy_error_response(
        request,
        ApiError(
            "panic_incomplete",
            503,
            "Panic could not confirm a safe state",
        ),
    )


async def _wait_for_panic_receipt(
    app: FastAPI,
    request: Request,
):
    deadline = time.monotonic() + min(
        90.0,
        app.state.trading_service.config.llm.request_timeout_seconds,
    )
    while True:
        try:
            snapshot = _panic_snapshot(app)
        except LimitStoreUnavailable:
            return _policy_error_response(
                request,
                ApiError(
                    "policy_store_unavailable",
                    503,
                    "Request policy is temporarily unavailable",
                ),
            )
        if snapshot is not None and snapshot.state != "started":
            return _panic_response(request, snapshot)
        if time.monotonic() >= deadline:
            return _policy_error_response(
                request,
                ApiError(
                    "panic_incomplete",
                    503,
                    "Panic could not confirm a safe state",
                ),
            )
        await asyncio.sleep(0.01)


async def _materialize_response(response) -> tuple[Response, bytes]:
    if hasattr(response, "body"):
        body = response.body
    else:
        body = b"".join(
            [chunk async for chunk in response.body_iterator]
        )
    headers = dict(response.headers)
    headers.pop("content-length", None)
    return (
        Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            background=response.background,
        ),
        body,
    )


def install_route_policy(app: FastAPI) -> RoutePolicyRegistry:
    registry = RoutePolicyRegistry()
    app.state.route_policy_registry = registry
    if getattr(app.state, "rate_limiter", None) is None:
        app.state.rate_limiter = DurableRateLimiter(
            app.state.session_auth.session_factory
        )
    if getattr(app.state, "leases", None) is None:
        app.state.leases = ConcurrencyLeaseService(
            app.state.session_auth.session_factory
        )

    @app.middleware("http")
    async def enforce_route_policy(request: Request, call_next):
        resolved = registry.resolve(
            app,
            request.method,
            request.url.path,
        )
        if resolved is None:
            return await call_next(request)
        policy = resolved.policy
        request.state.route_policy = policy
        if (
            policy.path == "/static/{path:path}"
            and not _is_safe_static_asset(request)
        ):
            return _policy_error_response(
                request,
                ApiError("not_found", 404, "Route not found"),
            )
        if policy.path == "/health/live":
            return await call_next(request)
        try:
            principal = _authenticate(request, policy)
        except ApiError as exc:
            return _policy_error_response(request, exc)
        if (
            policy.requires_idempotency
            and not getattr(
                request.state,
                "idempotency_key",
                request.headers.get("Idempotency-Key", ""),
            )
        ):
            return _policy_error_response(
                request,
                ApiError(
                    "idempotency_key_required",
                    422,
                    "Idempotency-Key is required",
                ),
            )
        source = request.client.host if request.client else "unknown"
        limit_principal = (
            f"session:{principal.session_id}:{principal.actor}"
            if principal is not None
            else f"source:{source}"
        )
        try:
            decision = app.state.rate_limiter.consume_pair(
                _limit_spec(app, policy),
                principal=limit_principal,
            )
        except LimitStoreUnavailable:
            return _policy_error_response(
                request,
                ApiError(
                    "policy_store_unavailable",
                    503,
                    "Request policy is temporarily unavailable",
                ),
            )
        if not decision.allowed:
            return _policy_error_response(
                request,
                ApiError(
                    "rate_limit_exceeded",
                    429,
                    "Request rate limit exceeded",
                ),
                headers=_rate_headers(app, policy, decision),
            )
        if policy.concurrency_behavior == "coalesce_panic":
            try:
                existing = _panic_snapshot(app)
            except LimitStoreUnavailable:
                return _policy_error_response(
                    request,
                    ApiError(
                        "policy_store_unavailable",
                        503,
                        "Request policy is temporarily unavailable",
                    ),
                )
            if existing is not None and existing.state != "started":
                return _panic_response(request, existing)
        configured = getattr(
            app.state.trading_service.config.security.rate_limits,
            policy.limit_name,
        )
        owner = uuid4().hex
        acquired_key = None
        contentions: list[LeaseDecision] = []
        try:
            for lease_key in _lease_keys(
                app,
                resolved,
                limit_principal=limit_principal,
            ):
                lease = app.state.leases.acquire(
                    lease_key,
                    owner=owner,
                    ttl_seconds=configured.window_seconds,
                )
                if lease.acquired:
                    acquired_key = lease_key
                    break
                contentions.append(lease)
        except LimitStoreUnavailable:
            return _policy_error_response(
                request,
                ApiError(
                    "policy_store_unavailable",
                    503,
                    "Request policy is temporarily unavailable",
                ),
            )
        if acquired_key is None:
            if policy.concurrency_behavior == "coalesce_panic":
                return await _wait_for_panic_receipt(app, request)
            retry_after = min(
                (
                    lease.retry_after_seconds
                    for lease in contentions
                    if lease.retry_after_seconds > 0
                ),
                default=1,
            )
            headers = _rate_headers(app, policy, decision)
            headers["Retry-After"] = str(retry_after)
            return _policy_error_response(
                request,
                ApiError(
                    "route_busy",
                    409,
                    "A matching request is already in progress",
                ),
                headers=headers,
            )
        if policy.concurrency_behavior == "coalesce_panic":
            try:
                started = _start_panic_receipt(
                    app,
                    request.state.request_id,
                )
            except LimitStoreUnavailable:
                app.state.leases.release(
                    acquired_key,
                    owner=owner,
                )
                return _policy_error_response(
                    request,
                    ApiError(
                        "policy_store_unavailable",
                        503,
                        "Request policy is temporarily unavailable",
                    ),
                )
            if not started:
                app.state.leases.release(
                    acquired_key,
                    owner=owner,
                )
                return await _wait_for_panic_receipt(app, request)
        release_error = False
        receipt_error = False
        try:
            response = await call_next(request)
            if policy.concurrency_behavior == "coalesce_panic":
                response, body = await _materialize_response(response)
                try:
                    payload = json.loads(body)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    payload = None
                durable_response = None
                if isinstance(payload, dict):
                    candidate = payload.get("receipt")
                    if isinstance(candidate, dict) and "safe" in candidate:
                        durable_response = candidate
                    elif "safe" in payload:
                        durable_response = payload
                try:
                    if (
                        durable_response is None
                        and response.status_code < 500
                    ):
                        _discard_panic_receipt(
                            app,
                            request.state.request_id,
                        )
                    else:
                        _finish_panic_receipt(
                            app,
                            request.state.request_id,
                            response=durable_response,
                        )
                except LimitStoreUnavailable:
                    receipt_error = True
        except BaseException:
            if policy.concurrency_behavior == "coalesce_panic":
                try:
                    _finish_panic_receipt(
                        app,
                        request.state.request_id,
                        response=None,
                    )
                except LimitStoreUnavailable:
                    pass
            raise
        finally:
            try:
                app.state.leases.release(
                    acquired_key,
                    owner=owner,
                )
            except LimitStoreUnavailable:
                release_error = True
        if release_error or receipt_error:
            return _policy_error_response(
                request,
                ApiError(
                    "policy_store_unavailable",
                    503,
                    "Request policy is temporarily unavailable",
                ),
            )
        return response

    app.router.add_event_handler(
        "startup",
        lambda: validate_route_inventory(app),
    )
    return registry
