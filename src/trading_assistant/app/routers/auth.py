"""Operator login, session inspection, reauthentication, and logout routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from ..auth import SessionAuth, SessionPrincipal
from ..errors import ApiError
from ..security import csrf_protected, current_principal, session_auth

router = APIRouter()


class LoginIn(BaseModel):
    secret: str


class ReauthenticateIn(BaseModel):
    secret: str


def _token(request: Request, auth: SessionAuth) -> str:
    return request.cookies.get(auth.cookie_name(), "")


@router.post("/auth/login")
def login(
    body: LoginIn,
    request: Request,
    response: Response,
    auth: SessionAuth = Depends(session_auth),
):
    source = request.client.host if request.client else "unknown"
    if not request.app.state.login_rate.allow(f"login:{source}"):
        raise ApiError(
            "rate_limit_exceeded",
            429,
            "Login rate limit exceeded",
        )
    issued = auth.login(body.secret, request.app.state.operator_secret)
    response.set_cookie(
        key=auth.cookie_name(),
        value=issued.token,
        max_age=int(auth.ttl.total_seconds()),
        httponly=True,
        secure=auth.cookie_secure,
        samesite="strict",
        path="/",
    )
    return {
        "actor": "operator:local",
        "csrf_token": issued.csrf,
        "expires_at": issued.expires_at.isoformat(),
    }


@router.get("/auth/session")
def get_session(
    request: Request,
    auth: SessionAuth = Depends(session_auth),
    principal: SessionPrincipal = Depends(current_principal),
):
    return {
        "actor": principal.actor,
        "session_id": principal.session_id,
        "authenticated_at": principal.authenticated_at.isoformat(),
        "csrf_token": auth.csrf_token(_token(request, auth)),
    }


@router.post("/auth/reauth")
def reauthenticate(
    body: ReauthenticateIn,
    request: Request,
    auth: SessionAuth = Depends(session_auth),
    principal: SessionPrincipal = Depends(csrf_protected),
):
    refreshed = auth.reauthenticate(
        _token(request, auth),
        body.secret,
        request.app.state.operator_secret,
    )
    return {
        "actor": refreshed.actor,
        "authenticated_at": refreshed.authenticated_at.isoformat(),
    }


@router.post("/auth/logout")
def logout(
    request: Request,
    response: Response,
    auth: SessionAuth = Depends(session_auth),
    principal: SessionPrincipal = Depends(csrf_protected),
):
    auth.logout(_token(request, auth))
    response.delete_cookie(
        key=auth.cookie_name(),
        path="/",
        secure=auth.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return {"logged_out": True}
