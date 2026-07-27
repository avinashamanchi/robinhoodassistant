"""FastAPI session dependencies, request identity, and response hardening."""

from __future__ import annotations

import asyncio
import re
import logging
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import (
    RecentAuthenticationRequired,
    SessionAuth,
    SessionPrincipal,
)
from .errors import ApiError

_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}\Z")
_IDEMPOTENCY_KEY = re.compile(r"[\x21-\x7e]{1,64}\Z")
_LOG = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=()"
    ),
}


def session_auth(request: Request) -> SessionAuth:
    return request.app.state.session_auth


def _session_token(request: Request, auth: SessionAuth) -> str:
    return request.cookies.get(auth.cookie_name(), "")


def current_principal(
    request: Request,
    auth: SessionAuth = Depends(session_auth),
) -> SessionPrincipal:
    principal = auth.authenticate(_session_token(request, auth))
    request.state.principal = principal
    return principal


def csrf_protected(
    request: Request,
    auth: SessionAuth = Depends(session_auth),
) -> SessionPrincipal:
    principal = auth.require_csrf(
        _session_token(request, auth),
        request.headers.get("X-CSRF-Token", ""),
    )
    request.state.principal = principal
    return principal


def recent_principal(
    auth: SessionAuth = Depends(session_auth),
    principal: SessionPrincipal = Depends(csrf_protected),
) -> SessionPrincipal:
    age = auth.now() - principal.authenticated_at
    if age.total_seconds() < 0 or age > auth.reauthentication_window:
        raise RecentAuthenticationRequired
    return principal


def rate_limit_key(request: Request, principal: SessionPrincipal) -> str:
    return f"session:{principal.session_id}:{principal.actor}"


def _error_response(request: Request, error: ApiError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or uuid4().hex
    request.state.request_id = request_id
    content = {
        "error": {
            "code": error.code,
            "message": error.message,
            "request_id": request_id,
        }
    }
    if error.receipt is not None:
        content["receipt"] = error.receipt
    response = JSONResponse(
        status_code=error.status_code,
        content=content,
    )
    return _harden_response(request, response)


def _harden_response(request: Request, response):
    request_id = getattr(request.state, "request_id", None) or uuid4().hex
    request.state.request_id = request_id
    for key, value in SECURITY_HEADERS.items():
        response.headers[key] = value
    response.headers["X-Request-ID"] = request_id
    if request.url.path != "/health/live":
        response.headers["Cache-Control"] = "no-store"
    elif "Cache-Control" in response.headers:
        del response.headers["Cache-Control"]
    return response


def install_security(app: FastAPI) -> None:
    from .policy import install_route_policy

    install_route_policy(app)

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return _error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ):
        return _error_response(
            request,
            ApiError("invalid_request", 422, "Request validation failed"),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request, exc: StarletteHTTPException
    ):
        code = {
            404: "not_found",
            405: "method_not_allowed",
        }.get(exc.status_code, "http_error")
        message = {
            404: "Route not found",
            405: "Method not allowed",
        }.get(exc.status_code, "Request failed")
        return _error_response(request, ApiError(code, exc.status_code, message))

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        return _error_response(
            request,
            ApiError("internal_error", 500, "Internal server error"),
        )

    @app.middleware("http")
    async def secure_response(request: Request, call_next):
        incoming_request_id = request.headers.get("X-Request-ID", "")
        request.state.request_id = (
            incoming_request_id
            if _REQUEST_ID.fullmatch(incoming_request_id)
            else uuid4().hex
        )
        idempotency_key = request.headers.get("Idempotency-Key", "")
        if idempotency_key and not _IDEMPOTENCY_KEY.fullmatch(
            idempotency_key
        ):
            return _error_response(
                request,
                ApiError(
                    "invalid_idempotency_key",
                    422,
                    "Idempotency-Key is invalid",
                ),
            )
        request.state.idempotency_key = idempotency_key
        response = await call_next(request)
        receipt = getattr(request.state, "mutation_receipt", None)
        if receipt is not None:
            context, action, target_type, target_id = receipt
            try:
                await asyncio.to_thread(
                    request.app.state.audit.record,
                    context,
                    action,
                    target_type,
                    target_id,
                    f"http_{response.status_code}",
                )
            except Exception:
                _LOG.disabled = False
                _LOG.error(
                    "boundary_audit_unavailable action=%s request_id=%s",
                    action,
                    context.request_id,
                )
        if (
            request.method == "OPTIONS"
            and request.headers.get("Origin")
            and request.headers.get("Access-Control-Request-Method")
            and response.status_code >= 400
        ):
            response = _error_response(
                request,
                ApiError(
                    "cors_rejected",
                    403,
                    "CORS preflight was rejected",
                ),
            )
        return _harden_response(request, response)
