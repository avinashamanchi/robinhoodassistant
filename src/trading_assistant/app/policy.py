"""Complete, centrally validated policy inventory for HTTP routes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse, Response
from starlette.routing import Match, Mount, WebSocketRoute

from trading_assistant.db.models import (
    ConcurrencyLease,
    PanicReceipt,
    utcnow,
)
from trading_assistant.security.sensitive_fields import sensitive_store

from .auth import RecentAuthenticationRequired, SessionPrincipal
from .errors import ApiError
from .limits import (
    ConcurrencyLeaseService,
    DurableRateLimiter,
    InterlockDecision,
    LeaseDecision,
    LimitDecision,
    LimitSpec,
    LimitStoreUnavailable,
    MutationInterlockService,
)

_DEFAULT_LEASE_TTL_SECONDS = 30
_LEASE_TTL_SECONDS = _DEFAULT_LEASE_TTL_SECONDS
_PANIC_LEASE_TTL_SECONDS = 90
_BACKTEST_LEASE_TTL_SECONDS = 1_500
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
    mutation_operation: str | None = None


@dataclass(frozen=True)
class ResolvedRoute:
    policy: RoutePolicy
    path_params: dict[str, str]


class _RoutePolicyMissing(RuntimeError):
    """A concrete application route matched without a registered policy."""


@dataclass(frozen=True)
class _PanicSnapshot:
    request_id: str
    lease_generation: int
    state: str
    response: dict[str, object] | None
    expires_at: datetime


@dataclass(frozen=True)
class _PanicClaim:
    claimed: bool
    snapshot: _PanicSnapshot


@dataclass(frozen=True)
class _PanicFenceObservation:
    authoritative: bool
    snapshot: _PanicSnapshot | None
    wait_expires_at: datetime


@dataclass
class _LeaseHold:
    resource_key: str
    owner: str
    generation: int
    expires_at: datetime
    ttl_seconds: int = _DEFAULT_LEASE_TTL_SECONDS
    interlock_key: str | None = None


def _lease_ttl_seconds(policy: RoutePolicy) -> int:
    # Existing concurrency tests deliberately lower the shared TTL. Preserve
    # that deterministic override while using task-specific production TTLs.
    if _LEASE_TTL_SECONDS != _DEFAULT_LEASE_TTL_SECONDS:
        return _LEASE_TTL_SECONDS
    if policy.path == "/panic":
        return _PANIC_LEASE_TTL_SECONDS
    if policy.path == "/backtests/run":
        return _BACKTEST_LEASE_TTL_SECONDS
    return _DEFAULT_LEASE_TTL_SECONDS


def _panic_lease_ttl_seconds() -> int:
    if _LEASE_TTL_SECONDS != _DEFAULT_LEASE_TTL_SECONDS:
        return _LEASE_TTL_SECONDS
    return _PANIC_LEASE_TTL_SECONDS


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
        mutation_operation="order_approve",
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
        mutation_operation="order_reject",
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
        mutation_operation="breaker_reset",
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
        mutation_operation="order_cancel",
    ),
    RoutePolicy(
        "POST",
        "/reconcile",
        AuthLevel.RECENT,
        "privileged",
        requires_idempotency=True,
        audit_mutation=True,
        broker_read=True,
        mutation_operation="portfolio_reconcile",
    ),
    RoutePolicy(
        "POST",
        "/sync",
        AuthLevel.CSRF,
        "mutation",
        requires_idempotency=True,
        audit_mutation=True,
        broker_read=True,
        mutation_operation="order_sync",
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
        mutation_operation="panic",
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
        mutation_operation="analysis",
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
        mutation_operation="plan_approve",
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
        mutation_operation="plan_cancel",
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
        mutation_operation="proposal_batch",
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
        mutation_operation="backtest",
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
                raise _RoutePolicyMissing(
                    f"{method.upper()} {template or path}"
                )
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
                else:
                    route_keys.add(("MOUNT", route.path))
                continue
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if path is None:
                continue
            if isinstance(route, WebSocketRoute):
                route_keys.add(("WEBSOCKET", path))
                continue
            if methods is None:
                route_keys.add(("ROUTE", path))
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
    if policy.path == "/backtests/run":
        return ["backtest:global"]
    material = _resource_material(
        resolved,
        limit_principal=limit_principal,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    configured = getattr(
        app.state.trading_service.config.security.rate_limits,
        policy.limit_name,
    )
    concurrency = (
        1
        if policy.requires_idempotency
        else configured.concurrency
    )
    return [
        f"route:{digest}:{slot}"
        for slot in range(concurrency)
    ]


def _resource_material(
    resolved: ResolvedRoute,
    *,
    limit_principal: str,
) -> str:
    policy = resolved.policy
    path = policy.path
    if path in {
        "/approve/{order_id}",
        "/reject/{order_id}",
        "/orders/{order_id}/cancel",
    }:
        return f"order:{resolved.path_params.get('order_id', '')}"
    if path in {
        "/plans/{plan_id}/approve",
        "/plans/{plan_id}/cancel",
    }:
        return f"plan:{resolved.path_params.get('plan_id', '')}"
    if path in {"/killswitch/reset", "/panic"}:
        return "paper-account:alpaca-paper"
    if path == "/reconcile":
        return "portfolio:alpaca-paper"
    if path == "/sync":
        return "broker-orders:alpaca-paper"
    if path in {"/analyze", "/propose"}:
        return f"analysis:{limit_principal}"
    if path == "/backtests/run":
        return f"backtest:{limit_principal}"
    if policy.concurrency_scope == "target":
        target = resolved.path_params.get(policy.target_param or "", "")
        return f"target:{policy.target_param}:{target}"
    if policy.concurrency_scope == "account":
        return "account:alpaca-paper"
    return f"principal:{policy.limit_name}:{limit_principal}"


def _operation_interlock_key(
    resolved: ResolvedRoute,
    *,
    lease_key: str,
    limit_principal: str,
    operation: str | None = None,
) -> str:
    policy = resolved.policy
    selected_operation = operation or policy.mutation_operation
    if (
        policy.path not in {"/killswitch/reset", "/panic"}
        or selected_operation not in {"breaker_reset", "panic"}
    ):
        return lease_key
    material = (
        f"{_resource_material(resolved, limit_principal=limit_principal)}"
        f"\0mutation:{selected_operation}"
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"route:{digest}:0"


def _hold_interlock_key(hold: _LeaseHold) -> str:
    return hold.interlock_key or hold.resource_key


def _reconciliation_control_key() -> str:
    digest = hashlib.sha256(
        b"reconciliation-control:alpaca-paper"
    ).hexdigest()
    return f"route:{digest}:0"


def _interlock_denial_response(
    request: Request,
    policy: RoutePolicy,
):
    if policy.concurrency_behavior == "coalesce_panic":
        return _panic_incomplete_response(request)
    return _policy_error_response(
        request,
        ApiError(
            "mutation_reconciliation_required",
            409,
            "A previous mutation requires explicit reconciliation",
        ),
    )


def _backtest_busy_response(request: Request):
    return _policy_error_response(
        request,
        ApiError(
            "backtest_busy",
            409,
            "A backtest is already in progress",
        ),
    )


async def _backtest_bounds_response(
    app: FastAPI,
    request: Request,
):
    try:
        payload = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    limits = app.state.trading_service.config.security.backtest_limits
    symbols = payload.get("symbols", [])
    exceeded = (
        isinstance(symbols, list)
        and len(symbols) > limits.max_symbols
    )
    start_value = payload.get("start_date")
    end_value = payload.get("end_date")
    if (
        not exceeded
        and isinstance(start_value, str)
        and isinstance(end_value, str)
    ):
        try:
            start_date = date.fromisoformat(start_value)
            end_date = date.fromisoformat(end_value)
        except ValueError:
            pass
        else:
            inclusive_days = (end_date - start_date).days + 1
            exceeded = inclusive_days > limits.max_calendar_days
    if not exceeded:
        return None
    return _policy_error_response(
        request,
        ApiError(
            "backtest_bounds_exceeded",
            422,
            "Backtest request exceeds configured bounds",
        ),
    )


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


def _snapshot_from_row(row: PanicReceipt, session) -> _PanicSnapshot:
    payload = (
        json.loads(sensitive_store(session).read(row, "response_json"))
        if row.response_json is not None
        else None
    )
    return _PanicSnapshot(
        request_id=row.request_id,
        lease_generation=row.lease_generation,
        state=row.state,
        response=payload,
        expires_at=row.expires_at,
    )


def _start_panic_receipt(
    app: FastAPI,
    request_id: str,
    lease_generation: int,
    expires_at: datetime,
) -> _PanicClaim:
    now = utcnow()
    try:
        with app.state.session_auth.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(PanicReceipt, "alpaca-paper")
            if (
                row is not None
                and row.expires_at > now
                and row.state != "completed"
            ):
                session.rollback()
                return _PanicClaim(
                    claimed=False,
                    snapshot=_snapshot_from_row(row, session),
                )
            if row is None:
                row = PanicReceipt(
                    account_scope="alpaca-paper",
                    request_id=request_id,
                    lease_generation=lease_generation,
                    state="started",
                    started_at=now,
                    completed_at=None,
                    expires_at=expires_at,
                )
                sensitive_store(session).write_many(
                    row,
                    {"response_json": "{}"},
                )
            row.request_id = request_id
            row.lease_generation = lease_generation
            row.state = "started"
            if row.response_json is not None:
                sensitive_store(session).clear(row, {"response_json"})
            row.started_at = now
            row.completed_at = None
            row.expires_at = expires_at
            session.commit()
            return _PanicClaim(
                claimed=True,
                snapshot=_snapshot_from_row(row, session),
            )
    except (SQLAlchemyError, OSError, ValueError, TypeError) as exc:
        raise LimitStoreUnavailable(
            "durable panic receipt store unavailable"
        ) from exc


def _renew_panic_receipt(
    app: FastAPI,
    lease_key: str,
    request_id: str,
    lease_generation: int,
    expires_at: datetime,
) -> bool:
    try:
        with app.state.session_auth.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            first_proof_at = utcnow()
            if not _panic_lease_is_authoritative(
                session,
                lease_key=lease_key,
                owner=request_id,
                generation=lease_generation,
                expires_at=expires_at,
                now=first_proof_at,
            ):
                session.rollback()
                return False
            row = session.get(PanicReceipt, "alpaca-paper")
            if (
                row is None
                or row.request_id != request_id
                or row.lease_generation != lease_generation
                or row.state != "started"
            ):
                session.rollback()
                return False
            row.expires_at = expires_at
            session.flush()
            second_proof_at = utcnow()
            if (
                second_proof_at < first_proof_at
                or not _panic_lease_is_authoritative(
                    session,
                    lease_key=lease_key,
                    owner=request_id,
                    generation=lease_generation,
                    expires_at=expires_at,
                    now=second_proof_at,
                )
            ):
                session.rollback()
                return False
            session.commit()
            return True
    except (SQLAlchemyError, OSError, ValueError, TypeError) as exc:
        raise LimitStoreUnavailable(
            "durable panic receipt store unavailable"
        ) from exc


def _panic_lease_is_authoritative(
    session: Session,
    *,
    lease_key: str,
    owner: str,
    generation: int,
    expires_at: datetime,
    now: datetime,
) -> bool:
    lease = session.execute(
        select(
            ConcurrencyLease.owner,
            ConcurrencyLease.generation,
            ConcurrencyLease.expires_at,
        ).where(ConcurrencyLease.resource_key == lease_key)
    ).one_or_none()
    return bool(
        lease is not None
        and lease.owner == owner
        and lease.generation == generation
        and lease.expires_at == expires_at
        and lease.expires_at > now
    )


def _finish_panic_receipt(
    app: FastAPI,
    lease_key: str,
    request_id: str,
    *,
    lease_generation: int,
    response: dict[str, object] | None,
    expires_at: datetime,
) -> None:
    try:
        with app.state.session_auth.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            now = utcnow()
            if not _panic_lease_is_authoritative(
                session,
                lease_key=lease_key,
                owner=request_id,
                generation=lease_generation,
                expires_at=expires_at,
                now=now,
            ):
                session.rollback()
                raise LimitStoreUnavailable(
                    "durable panic receipt ownership changed"
                )
            row = session.get(PanicReceipt, "alpaca-paper")
            if (
                row is None
                or row.request_id != request_id
                or row.lease_generation != lease_generation
                or row.state != "started"
            ):
                session.rollback()
                raise LimitStoreUnavailable(
                    "durable panic receipt ownership changed"
                )
            row.state = "completed" if response is not None else "failed"
            store = sensitive_store(session)
            if response is not None:
                store.write_many(
                    row,
                    {
                        "response_json": json.dumps(
                            response,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    },
                )
            elif row.response_json is not None:
                store.clear(row, {"response_json"})
            row.completed_at = now
            row.expires_at = expires_at
            session.flush()
            second_proof_at = utcnow()
            if (
                second_proof_at < now
                or not _panic_lease_is_authoritative(
                    session,
                    lease_key=lease_key,
                    owner=request_id,
                    generation=lease_generation,
                    expires_at=expires_at,
                    now=second_proof_at,
                )
            ):
                session.rollback()
                raise LimitStoreUnavailable(
                    "durable panic receipt ownership changed"
                )
            session.commit()
    except LimitStoreUnavailable:
        raise
    except (SQLAlchemyError, OSError, ValueError, TypeError) as exc:
        raise LimitStoreUnavailable(
            "durable panic receipt store unavailable"
        ) from exc


def _observe_panic_fence(
    app: FastAPI,
    lease_key: str,
    owner: str,
    generation: int,
) -> _PanicFenceObservation:
    read_started_at = utcnow()
    try:
        with app.state.session_auth.session_factory() as session:
            session.execute(text("BEGIN"))
            receipt = session.get(PanicReceipt, "alpaca-paper")
            lease = session.execute(
                select(
                    ConcurrencyLease.owner,
                    ConcurrencyLease.generation,
                    ConcurrencyLease.expires_at,
                ).where(ConcurrencyLease.resource_key == lease_key)
            ).one_or_none()
            snapshot = (
                None
                if receipt is None
                else _snapshot_from_row(receipt, session)
            )
            now = utcnow()
            session.rollback()
    except (SQLAlchemyError, OSError, ValueError, TypeError) as exc:
        raise LimitStoreUnavailable(
            "durable panic receipt store unavailable"
        ) from exc

    if now < read_started_at:
        return _PanicFenceObservation(False, snapshot, now)
    if lease is None:
        return _PanicFenceObservation(False, snapshot, now)
    if snapshot is not None and (
        snapshot.request_id != owner
        or snapshot.lease_generation != generation
    ):
        return _PanicFenceObservation(False, snapshot, now)

    active = (
        lease.owner == owner
        and lease.generation == generation
        and lease.expires_at > now
    )
    released_after_completion = bool(
        snapshot is not None
        and snapshot.state != "started"
        and snapshot.expires_at > now
        and lease.owner == ""
        and lease.generation == generation + 1
        and lease.expires_at <= now
    )
    if not active and not released_after_completion:
        return _PanicFenceObservation(False, snapshot, lease.expires_at)
    if (
        snapshot is not None
        and snapshot.state != "started"
        and active
        and snapshot.expires_at != lease.expires_at
    ):
        return _PanicFenceObservation(False, snapshot, lease.expires_at)
    return _PanicFenceObservation(
        True,
        snapshot,
        (
            snapshot.expires_at
            if released_after_completion and snapshot is not None
            else lease.expires_at
        ),
    )


def _mark_panic_uncertain(
    app: FastAPI,
    request_id: str,
    *,
    lease_generation: int,
    response: dict[str, object] | None,
    expires_at: datetime,
) -> bool:
    now = utcnow()
    block_until = max(
        expires_at,
        now + timedelta(seconds=_panic_lease_ttl_seconds()),
    )
    try:
        with app.state.session_auth.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(PanicReceipt, "alpaca-paper")
            if (
                row is None
                or row.request_id != request_id
                or row.lease_generation != lease_generation
            ):
                session.rollback()
                return False
            row.state = "failed"
            store = sensitive_store(session)
            if response is not None:
                store.write_many(
                    row,
                    {
                        "response_json": json.dumps(
                            response,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    },
                )
            elif row.response_json is not None:
                store.clear(row, {"response_json"})
            row.completed_at = now
            row.expires_at = block_until
            session.commit()
            return True
    except (SQLAlchemyError, OSError, TypeError) as exc:
        raise LimitStoreUnavailable(
            "durable panic receipt store unavailable"
        ) from exc


def _discard_panic_receipt(
    app: FastAPI,
    request_id: str,
    lease_generation: int,
) -> bool:
    try:
        with app.state.session_auth.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(PanicReceipt, "alpaca-paper")
            if (
                row is None
                or row.request_id != request_id
                or row.lease_generation != lease_generation
            ):
                session.rollback()
                return False
            sensitive_store(session).delete(row)
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
    loop = asyncio.get_running_loop()
    wait_deadline = (
        loop.time()
        + app.state.trading_service.config.trading.request_timeout_seconds
    )
    while True:
        request_remaining = wait_deadline - loop.time()
        if request_remaining <= 0:
            return _panic_incomplete_response(request)
        try:
            observation = await _offload(
                _observe_panic_fence,
                app,
                lease_key,
                observed.owner,
                observed.generation,
            )
        except LimitStoreUnavailable:
            return _policy_store_error_response(request)
        if not observation.authoritative:
            return _panic_incomplete_response(request)
        snapshot = observation.snapshot
        if snapshot is not None and snapshot.state != "started":
            return _panic_response(request, snapshot)
        remaining = (
            observation.wait_expires_at - utcnow()
        ).total_seconds()
        if remaining <= 0:
            return _panic_incomplete_response(request)
        await asyncio.sleep(
            min(delay, remaining, request_remaining)
        )
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
        max(0.01, hold.ttl_seconds / 3),
    )
    retry_delay = min(0.025, interval)
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return None
        except TimeoutError:
            pass
        while True:
            remaining = (hold.expires_at - utcnow()).total_seconds()
            if remaining <= 0:
                return "store"
            try:
                renewed = await _offload(
                    app.state.leases.renew,
                    hold.resource_key,
                    owner=hold.owner,
                    generation=hold.generation,
                    ttl_seconds=hold.ttl_seconds,
                )
            except LimitStoreUnavailable:
                await asyncio.sleep(min(retry_delay, remaining))
                retry_delay = min(
                    retry_delay * 2,
                    _PANIC_POLL_MAX_SECONDS,
                )
                continue
            if (
                not renewed.acquired
                or renewed.owner != hold.owner
                or renewed.generation != hold.generation
            ):
                return "lost"
            if panic_request_id is not None:
                coherent_horizon = renewed.expires_at
                while True:
                    remaining = (
                        coherent_horizon - utcnow()
                    ).total_seconds()
                    if remaining <= 0:
                        return "store"
                    try:
                        receipt_renewed = await _offload(
                            _renew_panic_receipt,
                            app,
                            hold.resource_key,
                            panic_request_id,
                            hold.generation,
                            renewed.expires_at,
                        )
                    except LimitStoreUnavailable:
                        try:
                            await asyncio.wait_for(
                                stop.wait(),
                                timeout=min(retry_delay, remaining),
                            )
                        except TimeoutError:
                            retry_delay = min(
                                retry_delay * 2,
                                _PANIC_POLL_MAX_SECONDS,
                            )
                            continue
                        return "store"
                    if not receipt_renewed:
                        return "lost"
                    break
            hold.expires_at = renewed.expires_at
            retry_delay = min(0.025, interval)
            break


async def _await_task_shielded(task: asyncio.Task):
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


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
    renewal_result: Literal["lost", "store"] | None = None
    try:
        done, _pending = await asyncio.wait(
            {handler, renewal},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if renewal in done:
            renewal_result = renewal.result()
        response = await asyncio.shield(handler)
    except asyncio.CancelledError:
        try:
            await _await_task_shielded(handler)
        except BaseException:
            pass
        stop.set()
        try:
            await _await_task_shielded(renewal)
        except BaseException:
            pass
        raise
    except BaseException:
        stop.set()
        try:
            await _await_task_shielded(renewal)
        except BaseException:
            pass
        raise
    stop.set()
    if renewal_result is None:
        renewal_result = await renewal
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
            lease_generation=hold.generation,
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


async def _mark_interlock_uncertain(
    app: FastAPI,
    hold: _LeaseHold,
    *,
    outcome_code: str,
    worker_finished: bool,
) -> bool:
    try:
        return await _offload(
            app.state.mutation_interlocks.mark_uncertain,
            _hold_interlock_key(hold),
            owner=hold.owner,
            generation=hold.generation,
            outcome_code=outcome_code,
            worker_finished=worker_finished,
        )
    except LimitStoreUnavailable:
        return False


async def _settle_and_release_interlock(
    app: FastAPI,
    hold: _LeaseHold,
) -> Literal["settle", "release"] | None:
    try:
        settled = await _offload(
            app.state.mutation_interlocks.settle,
            _hold_interlock_key(hold),
            owner=hold.owner,
            generation=hold.generation,
            outcome_code="handler_completed",
        )
    except LimitStoreUnavailable:
        return "settle"
    if not settled:
        return "settle"
    try:
        released = await _offload(
            app.state.mutation_interlocks.release_settled,
            _hold_interlock_key(hold),
            lease_resource_key=hold.resource_key,
            owner=hold.owner,
            generation=hold.generation,
        )
    except LimitStoreUnavailable:
        return "release"
    return None if released else "release"


async def _inspect_interlock(
    app: FastAPI,
    resource_key: str,
) -> InterlockDecision | None:
    return await _offload(
        app.state.mutation_interlocks.inspect,
        resource_key,
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
    if getattr(app.state, "mutation_interlocks", None) is None:
        app.state.mutation_interlocks = MutationInterlockService(
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
        try:
            resolved = registry.resolve(
                app,
                request.method,
                request.url.path,
            )
        except _RoutePolicyMissing:
            return _policy_error_response(
                request,
                ApiError(
                    "route_policy_missing",
                    503,
                    "Request route policy is unavailable",
                ),
            )
        if resolved is None:
            return await call_next(request)
        policy = resolved.policy
        request.state.route_policy = policy
        if policy.path == "/health/live":
            return await call_next(request)
        runtime_tenure_guard = getattr(
            app.state,
            "runtime_tenure_guard",
            None,
        )
        if (
            request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
            and runtime_tenure_guard is not None
        ):
            try:
                runtime_tenure_guard.ensure_owned()
            except Exception:
                return _policy_error_response(
                    request,
                    ApiError(
                        "runtime_tenure_lost",
                        503,
                        "Runtime mutation authority is unavailable",
                    ),
                )
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
        if policy.path == "/backtests/run":
            bounds_response = await _backtest_bounds_response(
                app,
                request,
            )
            if bounds_response is not None:
                return bounds_response
        lease_keys = _lease_keys(
            app,
            resolved,
            limit_principal=limit_principal,
        )
        interlock_key = _operation_interlock_key(
            resolved,
            lease_key=lease_keys[0],
            limit_principal=limit_principal,
        )
        reconciliation_target: InterlockDecision | None = None
        if policy.requires_idempotency:
            try:
                if policy.path == "/reconcile":
                    control = await _inspect_interlock(
                        app,
                        _reconciliation_control_key(),
                    )
                    if control is not None:
                        return _interlock_denial_response(
                            request,
                            policy,
                        )
                existing_interlock = await _inspect_interlock(
                    app,
                    interlock_key,
                )
                if policy.path in {"/killswitch/reset", "/panic"}:
                    companion_operation = (
                        "panic"
                        if policy.path == "/killswitch/reset"
                        else "breaker_reset"
                    )
                    companion_key = _operation_interlock_key(
                        resolved,
                        lease_key=lease_keys[0],
                        limit_principal=limit_principal,
                        operation=companion_operation,
                    )
                    companion_interlock = await _inspect_interlock(
                        app,
                        companion_key,
                    )
                    legacy_interlock = await _inspect_interlock(
                        app,
                        lease_keys[0],
                    )
                else:
                    companion_interlock = None
                    legacy_interlock = None
            except LimitStoreUnavailable:
                return _policy_store_error_response(request)
            if policy.path == "/killswitch/reset":
                if companion_interlock is not None:
                    return _interlock_denial_response(
                        request,
                        policy,
                    )
                if legacy_interlock is not None:
                    if (
                        legacy_interlock.operation == "breaker_reset"
                        and existing_interlock is None
                    ):
                        existing_interlock = legacy_interlock
                    elif legacy_interlock is not existing_interlock:
                        return _interlock_denial_response(
                            request,
                            policy,
                        )
            elif policy.path == "/panic":
                reset_blockers = [
                    candidate
                    for candidate in (
                        companion_interlock,
                        (
                            legacy_interlock
                            if legacy_interlock is not None
                            and legacy_interlock.operation
                            == "breaker_reset"
                            else None
                        ),
                    )
                    if candidate is not None
                ]
                if any(
                    blocker.worker_finished_at is None
                    for blocker in reset_blockers
                ):
                    return _panic_incomplete_response(request)
                if legacy_interlock is not None:
                    if (
                        legacy_interlock.operation == "panic"
                        and existing_interlock is None
                    ):
                        existing_interlock = legacy_interlock
                    elif legacy_interlock.operation not in {
                        "breaker_reset",
                        "panic",
                    }:
                        return _panic_incomplete_response(request)
            if existing_interlock is not None:
                if (
                    policy.path == "/backtests/run"
                    and existing_interlock.state == "active"
                ):
                    try:
                        observed = await _offload(
                            app.state.leases.inspect,
                            lease_keys[0],
                        )
                    except LimitStoreUnavailable:
                        return _policy_store_error_response(request)
                    if (
                        observed.acquired
                        and observed.owner
                        == existing_interlock.owner
                        and observed.generation
                        == existing_interlock.generation
                    ):
                        return _backtest_busy_response(request)
                    if observed.acquired:
                        return _interlock_denial_response(
                            request,
                            policy,
                        )
                    try:
                        reclaimed = await _offload(
                            app.state.mutation_interlocks.release_expired_backtest,
                            lease_keys[0],
                            owner=existing_interlock.owner,
                            generation=existing_interlock.generation,
                        )
                    except LimitStoreUnavailable:
                        return _policy_store_error_response(request)
                    if not reclaimed:
                        return _interlock_denial_response(
                            request,
                            policy,
                        )
                    existing_interlock = None
                if (
                    existing_interlock is not None
                    and policy.concurrency_behavior == "coalesce_panic"
                    and existing_interlock.state == "active"
                ):
                    try:
                        observed = await _offload(
                            app.state.leases.inspect,
                            lease_keys[0],
                        )
                    except LimitStoreUnavailable:
                        return _policy_store_error_response(request)
                    if (
                        observed.acquired
                        and observed.owner
                        == existing_interlock.owner
                        and observed.generation
                        == existing_interlock.generation
                    ):
                        return await _wait_for_panic_receipt(
                            app,
                            request,
                            lease_key=lease_keys[0],
                            observed=observed,
                        )
                    return _panic_incomplete_response(request)
                if existing_interlock is None:
                    pass
                elif (
                    policy.path == "/reconcile"
                    and existing_interlock.operation
                    == "portfolio_reconcile"
                    and existing_interlock.worker_finished_at
                    is not None
                ):
                    reconciliation_target = existing_interlock
                    lease_keys = [_reconciliation_control_key()]
                    interlock_key = lease_keys[0]
                else:
                    return _interlock_denial_response(
                        request,
                        policy,
                    )
        owner = uuid4().hex
        hold: _LeaseHold | None = None
        contentions: list[tuple[str, LeaseDecision]] = []
        ttl_seconds = _lease_ttl_seconds(policy)
        try:
            for lease_key in lease_keys:
                lease = await _offload(
                    app.state.leases.acquire,
                    lease_key,
                    owner=owner,
                    ttl_seconds=ttl_seconds,
                )
                if lease.acquired:
                    hold = _LeaseHold(
                        resource_key=lease_key,
                        owner=lease.owner,
                        generation=lease.generation,
                        expires_at=lease.expires_at,
                        ttl_seconds=ttl_seconds,
                        interlock_key=interlock_key,
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
            if policy.path == "/backtests/run":
                return _backtest_busy_response(request)
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
        if policy.requires_idempotency:
            try:
                interlock = await _offload(
                    app.state.mutation_interlocks.claim,
                    _hold_interlock_key(hold),
                    owner=hold.owner,
                    generation=hold.generation,
                    operation=policy.mutation_operation or "",
                )
            except (LimitStoreUnavailable, ValueError):
                await _release_lease(app, hold)
                return _policy_store_error_response(request)
            if not interlock.acquired:
                await _release_lease(app, hold)
                return _interlock_denial_response(request, policy)
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
                    hold.generation,
                    hold.expires_at,
                )
            except LimitStoreUnavailable:
                await _mark_interlock_uncertain(
                    app,
                    hold,
                    outcome_code="panic_settlement_unproven",
                    worker_finished=True,
                )
                return _policy_store_error_response(request)
            if not claim.claimed:
                await _mark_interlock_uncertain(
                    app,
                    hold,
                    outcome_code="panic_settlement_unproven",
                    worker_finished=True,
                )
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
        except BaseException as exc:
            if panic_request_id is not None:
                await _mark_panic_uncertain_quietly(
                    app,
                    hold,
                    panic_request_id,
                    None,
                )
            if policy.requires_idempotency:
                await _mark_interlock_uncertain(
                    app,
                    hold,
                    outcome_code=(
                        "request_cancelled"
                        if isinstance(exc, asyncio.CancelledError)
                        else "handler_failed"
                    ),
                    worker_finished=True,
                )
            else:
                await _release_lease(app, hold)
            raise

        if renewal_failure is not None:
            if panic_request_id is not None:
                await _mark_panic_uncertain_quietly(
                    app,
                    hold,
                    panic_request_id,
                    None,
                )
            if policy.requires_idempotency:
                await _mark_interlock_uncertain(
                    app,
                    hold,
                    outcome_code=(
                        "lease_renewal_unproven"
                        if renewal_failure == "store"
                        else "lease_ownership_lost"
                    ),
                    worker_finished=True,
                )
            _LOG.error(
                "route_lease_renewal_uncertain request_id=%s",
                request.state.request_id,
            )
            return response

        durable_response = None
        settlement_succeeded = True
        if panic_request_id is not None:
            response, body = await _materialize_response(response)
            durable_response = _panic_payload(body)
            receipt_response = (
                None
                if getattr(
                    request.state,
                    "panic_owner_failed",
                    False,
                )
                else durable_response
            )
            try:
                if (
                    durable_response is None
                    and response.status_code < 500
                ):
                    settlement_succeeded = await _offload(
                        _discard_panic_receipt,
                        app,
                        panic_request_id,
                        hold.generation,
                    )
                else:
                    await _offload(
                        _finish_panic_receipt,
                        app,
                        hold.resource_key,
                        panic_request_id,
                        lease_generation=hold.generation,
                        response=receipt_response,
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
                await _mark_interlock_uncertain(
                    app,
                    hold,
                    outcome_code="panic_settlement_unproven",
                    worker_finished=True,
                )
                return response

        if reconciliation_target is not None:
            proof = getattr(
                request.state,
                "mutation_reconciliation_proof",
                None,
            )
            if proof == "portfolio_truth_reconciled":
                try:
                    await _offload(
                        app.state.mutation_interlocks.reconcile_clear,
                        reconciliation_target.resource_key,
                        owner=reconciliation_target.owner,
                        generation=reconciliation_target.generation,
                        actor=principal.actor,
                        request_id=request.state.request_id,
                        evidence_code=proof,
                        worker_termination_proven=False,
                    )
                except LimitStoreUnavailable:
                    _LOG.error(
                        "mutation_reconciliation_store_unavailable "
                        "request_id=%s",
                        request.state.request_id,
                    )

        if policy.requires_idempotency:
            cleanup_failure = await _settle_and_release_interlock(
                app,
                hold,
            )
            if cleanup_failure is not None:
                await _mark_interlock_uncertain(
                    app,
                    hold,
                    outcome_code=(
                        "interlock_settlement_unproven"
                        if cleanup_failure == "settle"
                        else "lease_release_unproven"
                    ),
                    worker_finished=True,
                )
                if panic_request_id is not None:
                    await _mark_panic_uncertain_quietly(
                        app,
                        hold,
                        panic_request_id,
                        durable_response,
                    )
                _LOG.error(
                    "route_interlock_cleanup_uncertain request_id=%s",
                    request.state.request_id,
                )
        else:
            released = await _release_lease(app, hold)
            if not released:
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
