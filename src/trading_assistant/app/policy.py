"""Complete, centrally validated policy inventory for HTTP routes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import hashlib
import json
import logging
import math
from pathlib import PurePosixPath
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

_LEASE_TTL_SECONDS = 30
_LEASE_RENEW_INTERVAL_SECONDS = 10.0
_PANIC_POLL_INITIAL_SECONDS = 0.025
_PANIC_POLL_MAX_SECONDS = 0.25
_LOG = logging.getLogger(__name__)


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
    request_id: str
    state: str
    response: dict[str, object] | None
    expires_at: datetime


@dataclass(frozen=True)
class _PanicClaim:
    claimed: bool
    snapshot: _PanicSnapshot


@dataclass
class _LeaseHold:
    resource_key: str
    owner: str
    generation: int
    expires_at: datetime


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
            or any(
                not segment or segment in {".", ".."}
                for segment in segments
            )
        ):
            return False
    return (
        PurePosixPath(request.url.path).suffix.lower()
        in {".css", ".js", ".svg"}
    )


def _snapshot_from_row(row: PanicReceipt) -> _PanicSnapshot:
    payload = (
        json.loads(row.response_json)
        if row.response_json is not None
        else None
    )
    return _PanicSnapshot(
        request_id=row.request_id,
        state=row.state,
        response=payload,
        expires_at=row.expires_at,
    )


def _panic_snapshot(app: FastAPI) -> _PanicSnapshot | None:
    try:
        with app.state.session_auth.session_factory() as session:
            row = session.get(PanicReceipt, "alpaca-paper")
            return None if row is None else _snapshot_from_row(row)
    except (SQLAlchemyError, OSError, ValueError, TypeError) as exc:
        raise LimitStoreUnavailable(
            "durable panic receipt store unavailable"
        ) from exc


def _start_panic_receipt(
    app: FastAPI,
    request_id: str,
    expires_at: datetime,
) -> _PanicClaim:
    now = utcnow()
    try:
        with app.state.session_auth.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(PanicReceipt, "alpaca-paper")
            if row is not None and row.expires_at > now:
                session.rollback()
                return _PanicClaim(
                    claimed=False,
                    snapshot=_snapshot_from_row(row),
                )
            if row is None:
                row = PanicReceipt(account_scope="alpaca-paper")
                session.add(row)
            row.request_id = request_id
            row.state = "started"
            row.response_json = None
            row.started_at = now
            row.completed_at = None
            row.expires_at = expires_at
            session.commit()
            return _PanicClaim(
                claimed=True,
                snapshot=_snapshot_from_row(row),
            )
    except (SQLAlchemyError, OSError, ValueError, TypeError) as exc:
        raise LimitStoreUnavailable(
            "durable panic receipt store unavailable"
        ) from exc


def _renew_panic_receipt(
    app: FastAPI,
    request_id: str,
    expires_at: datetime,
) -> bool:
    try:
        with app.state.session_auth.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(PanicReceipt, "alpaca-paper")
            if (
                row is None
                or row.request_id != request_id
                or row.state != "started"
            ):
                session.rollback()
                return False
            row.expires_at = expires_at
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
    expires_at: datetime,
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
            row.expires_at = expires_at
            session.commit()
    except LimitStoreUnavailable:
        raise
    except (SQLAlchemyError, OSError, TypeError) as exc:
        raise LimitStoreUnavailable(
            "durable panic receipt store unavailable"
        ) from exc


def _mark_panic_uncertain(
    app: FastAPI,
    request_id: str,
    *,
    response: dict[str, object] | None,
    expires_at: datetime,
) -> bool:
    now = utcnow()
    block_until = max(
        expires_at,
        now + timedelta(seconds=_LEASE_TTL_SECONDS),
    )
    try:
        with app.state.session_auth.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(PanicReceipt, "alpaca-paper")
            if row is None or row.request_id != request_id:
                session.rollback()
                return False
            row.state = "failed"
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
            row.expires_at = block_until
            session.commit()
            return True
    except (SQLAlchemyError, OSError, TypeError) as exc:
        raise LimitStoreUnavailable(
            "durable panic receipt store unavailable"
        ) from exc


def _discard_panic_receipt(app: FastAPI, request_id: str) -> bool:
    try:
        with app.state.session_auth.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(PanicReceipt, "alpaca-paper")
            if row is None or row.request_id != request_id:
                session.rollback()
                return False
            session.delete(row)
            session.commit()
            return True
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
            receipt=snapshot.response,
        ),
    )


def _panic_incomplete_response(request: Request):
    return _policy_error_response(
        request,
        ApiError(
            "panic_incomplete",
            503,
            "Panic could not confirm a safe state",
        ),
    )


def _policy_store_error_response(request: Request):
    return _policy_error_response(
        request,
        ApiError(
            "policy_store_unavailable",
            503,
            "Request policy is temporarily unavailable",
        ),
    )


async def _offload(function, /, *args, **kwargs):
    return await asyncio.to_thread(function, *args, **kwargs)


async def _wait_for_panic_receipt(
    app: FastAPI,
    request: Request,
    *,
    lease_key: str,
    observed: LeaseDecision,
):
    delay = _PANIC_POLL_INITIAL_SECONDS
    while True:
        try:
            snapshot = await _offload(_panic_snapshot, app)
        except LimitStoreUnavailable:
            return _policy_store_error_response(request)
        if (
            snapshot is not None
            and snapshot.request_id != observed.owner
        ):
            return _panic_incomplete_response(request)
        if snapshot is not None and snapshot.state != "started":
            return _panic_response(request, snapshot)
        try:
            current = await _offload(
                app.state.leases.inspect,
                lease_key,
            )
        except LimitStoreUnavailable:
            return _policy_store_error_response(request)
        if (
            not current.acquired
            or current.owner != observed.owner
            or current.generation != observed.generation
        ):
            return _panic_incomplete_response(request)
        remaining = (current.expires_at - utcnow()).total_seconds()
        if remaining <= 0:
            return _panic_incomplete_response(request)
        await asyncio.sleep(min(delay, remaining))
        delay = min(delay * 2, _PANIC_POLL_MAX_SECONDS)


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


async def _maintain_lease(
    app: FastAPI,
    hold: _LeaseHold,
    stop: asyncio.Event,
    *,
    panic_request_id: str | None,
) -> Literal["lost", "store"] | None:
    interval = min(
        _LEASE_RENEW_INTERVAL_SECONDS,
        max(0.01, _LEASE_TTL_SECONDS / 3),
    )
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return None
        except TimeoutError:
            pass
        try:
            renewed = await _offload(
                app.state.leases.renew,
                hold.resource_key,
                owner=hold.owner,
                generation=hold.generation,
                ttl_seconds=_LEASE_TTL_SECONDS,
            )
        except LimitStoreUnavailable:
            return "store"
        if (
            not renewed.acquired
            or renewed.owner != hold.owner
            or renewed.generation != hold.generation
        ):
            return "lost"
        hold.expires_at = renewed.expires_at
        if panic_request_id is not None:
            try:
                receipt_renewed = await _offload(
                    _renew_panic_receipt,
                    app,
                    panic_request_id,
                    renewed.expires_at,
                )
            except LimitStoreUnavailable:
                return "store"
            if not receipt_renewed:
                return "lost"


async def _call_with_lease_renewal(
    app: FastAPI,
    request: Request,
    call_next,
    hold: _LeaseHold,
    *,
    panic_request_id: str | None,
) -> tuple[Response | None, Literal["lost", "store"] | None]:
    stop = asyncio.Event()
    handler = asyncio.create_task(call_next(request))
    renewal = asyncio.create_task(
        _maintain_lease(
            app,
            hold,
            stop,
            panic_request_id=panic_request_id,
        )
    )
    done, _pending = await asyncio.wait(
        {handler, renewal},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if handler in done:
        stop.set()
        try:
            response = await handler
        finally:
            renewal_result = await renewal
        return response, renewal_result

    renewal_result = await renewal
    handler.cancel()
    try:
        response = await handler
    except asyncio.CancelledError:
        return None, renewal_result
    return response, renewal_result


def _panic_payload(body: bytes) -> dict[str, object] | None:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    candidate = payload.get("receipt")
    if isinstance(candidate, dict) and "safe" in candidate:
        return candidate
    if "safe" in payload:
        return payload
    return None


async def _mark_panic_uncertain_quietly(
    app: FastAPI,
    hold: _LeaseHold,
    request_id: str,
    response: dict[str, object] | None,
) -> None:
    try:
        marked = await _offload(
            _mark_panic_uncertain,
            app,
            request_id,
            response=response,
            expires_at=hold.expires_at,
        )
    except LimitStoreUnavailable:
        marked = False
    if not marked:
        _LOG.error(
            "panic_cleanup_uncertain request_id=%s",
            request_id,
        )


async def _release_lease(
    app: FastAPI,
    hold: _LeaseHold,
) -> bool:
    try:
        return await _offload(
            app.state.leases.release,
            hold.resource_key,
            owner=hold.owner,
            generation=hold.generation,
        )
    except LimitStoreUnavailable:
        return False


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
        if (
            request.url.path == "/static"
            or request.url.path.startswith("/static/")
        ) and not _is_safe_static_asset(request):
            return _policy_error_response(
                request,
                ApiError("not_found", 404, "Route not found"),
            )
        resolved = registry.resolve(
            app,
            request.method,
            request.url.path,
        )
        if resolved is None:
            return await call_next(request)
        policy = resolved.policy
        request.state.route_policy = policy
        if policy.path == "/health/live":
            return await call_next(request)
        try:
            principal = await _offload(
                _authenticate,
                request,
                policy,
            )
        except ApiError as exc:
            return _policy_error_response(request, exc)
        if principal is not None:
            request.state.principal = principal
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
            decision = await _offload(
                app.state.rate_limiter.consume_pair,
                _limit_spec(app, policy),
                principal=limit_principal,
            )
        except LimitStoreUnavailable:
            return _policy_store_error_response(request)
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
                existing = await _offload(_panic_snapshot, app)
            except LimitStoreUnavailable:
                return _policy_store_error_response(request)
            if (
                existing is not None
                and existing.expires_at > utcnow()
                and existing.state != "started"
            ):
                return _panic_response(request, existing)
        owner = uuid4().hex
        hold: _LeaseHold | None = None
        contentions: list[tuple[str, LeaseDecision]] = []
        try:
            for lease_key in _lease_keys(
                app,
                resolved,
                limit_principal=limit_principal,
            ):
                lease = await _offload(
                    app.state.leases.acquire,
                    lease_key,
                    owner=owner,
                    ttl_seconds=_LEASE_TTL_SECONDS,
                )
                if lease.acquired:
                    hold = _LeaseHold(
                        resource_key=lease_key,
                        owner=lease.owner,
                        generation=lease.generation,
                        expires_at=lease.expires_at,
                    )
                    break
                contentions.append((lease_key, lease))
        except LimitStoreUnavailable:
            return _policy_store_error_response(request)
        if hold is None:
            if policy.concurrency_behavior == "coalesce_panic":
                lease_key, observed = contentions[0]
                return await _wait_for_panic_receipt(
                    app,
                    request,
                    lease_key=lease_key,
                    observed=observed,
                )
            retry_after = min(
                (
                    lease.retry_after_seconds
                    for _lease_key, lease in contentions
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
        panic_request_id = (
            owner
            if policy.concurrency_behavior == "coalesce_panic"
            else None
        )
        if policy.concurrency_behavior == "coalesce_panic":
            try:
                claim = await _offload(
                    _start_panic_receipt,
                    app,
                    panic_request_id,
                    hold.expires_at,
                )
            except LimitStoreUnavailable:
                await _release_lease(app, hold)
                return _policy_store_error_response(request)
            if not claim.claimed:
                await _release_lease(app, hold)
                if claim.snapshot.state != "started":
                    return _panic_response(request, claim.snapshot)
                return _panic_incomplete_response(request)

        try:
            response, renewal_failure = await _call_with_lease_renewal(
                app,
                request,
                call_next,
                hold,
                panic_request_id=panic_request_id,
            )
        except BaseException:
            if panic_request_id is not None:
                await _mark_panic_uncertain_quietly(
                    app,
                    hold,
                    panic_request_id,
                    None,
                )
            await _release_lease(app, hold)
            raise

        if response is None:
            if panic_request_id is not None:
                await _mark_panic_uncertain_quietly(
                    app,
                    hold,
                    panic_request_id,
                    None,
                )
            await _release_lease(app, hold)
            if renewal_failure == "store":
                return _policy_store_error_response(request)
            return _policy_error_response(
                request,
                ApiError(
                    "route_lease_lost",
                    503,
                    "Request lease ownership was lost",
                ),
            )

        durable_response = None
        settlement_succeeded = True
        if panic_request_id is not None:
            response, body = await _materialize_response(response)
            durable_response = _panic_payload(body)
            try:
                if (
                    durable_response is None
                    and response.status_code < 500
                ):
                    settlement_succeeded = await _offload(
                        _discard_panic_receipt,
                        app,
                        panic_request_id,
                    )
                else:
                    await _offload(
                        _finish_panic_receipt,
                        app,
                        panic_request_id,
                        response=durable_response,
                        expires_at=hold.expires_at,
                    )
            except LimitStoreUnavailable:
                settlement_succeeded = False
            if not settlement_succeeded:
                await _mark_panic_uncertain_quietly(
                    app,
                    hold,
                    panic_request_id,
                    durable_response,
                )

        released = await _release_lease(app, hold)
        cleanup_uncertain = renewal_failure is not None or not released
        if cleanup_uncertain:
            if panic_request_id is not None:
                await _mark_panic_uncertain_quietly(
                    app,
                    hold,
                    panic_request_id,
                    durable_response,
                )
            _LOG.error(
                "route_lease_cleanup_uncertain request_id=%s",
                request.state.request_id,
            )
        return response

    app.router.add_event_handler(
        "startup",
        lambda: validate_route_inventory(app),
    )
    return registry
