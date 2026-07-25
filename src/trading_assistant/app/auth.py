"""Opaque, server-side operator sessions."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import select

from ..db.models import AuthSession, utcnow
from .errors import ApiError


class InvalidCredentials(ApiError):
    def __init__(self) -> None:
        super().__init__(
            code="invalid_credentials",
            status_code=401,
            message="Invalid operator credentials",
        )


class InvalidSession(ApiError):
    def __init__(self) -> None:
        super().__init__(
            code="invalid_session",
            status_code=401,
            message="A valid operator session is required",
        )


class SessionExpired(ApiError):
    def __init__(self) -> None:
        super().__init__(
            code="session_expired",
            status_code=401,
            message="The operator session has expired",
        )


class CsrfRejected(ApiError):
    def __init__(self) -> None:
        super().__init__(
            code="csrf_required",
            status_code=403,
            message="A valid CSRF token is required",
        )


class RecentAuthenticationRequired(ApiError):
    def __init__(self) -> None:
        super().__init__(
            code="recent_authentication_required",
            status_code=403,
            message="Recent operator reauthentication is required",
        )


@dataclass(frozen=True)
class SessionPrincipal:
    actor: str
    session_id: int
    authenticated_at: datetime


@dataclass(frozen=True)
class IssuedSession:
    token: str
    csrf: str
    expires_at: datetime


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _derive_key(application_secret: str, purpose: bytes) -> bytes:
    return hmac.new(
        application_secret.encode("utf-8"),
        b"trading-assistant/session-keys/v1\x00" + purpose,
        hashlib.sha256,
    ).digest()


def _keyed_hash(value: str, key: bytes) -> str:
    return hmac.new(
        key,
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class SessionAuth:
    def __init__(
        self,
        session_factory,
        *,
        application_secret: str,
        ttl: timedelta = timedelta(hours=8),
        reauthentication_window: timedelta = timedelta(minutes=5),
        cookie_secure: bool = False,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        if not application_secret or not application_secret.strip():
            raise RuntimeError("APP_API_TOKEN is required")
        self.session_factory = session_factory
        self._application_secret = application_secret
        self._session_key = _derive_key(
            application_secret,
            b"session-token-lookup",
        )
        self._csrf_key = _derive_key(application_secret, b"csrf-token")
        self.ttl = ttl
        self.reauthentication_window = reauthentication_window
        self.cookie_secure = cookie_secure
        self.now = now

    def cookie_name(self) -> str:
        return (
            "__Host-trading_session"
            if self.cookie_secure
            else "trading_session"
        )

    def login(
        self,
        supplied_secret: str,
        expected_secret: str,
        actor: str = "operator:local",
    ) -> IssuedSession:
        if not expected_secret or not expected_secret.strip():
            raise RuntimeError("APP_API_TOKEN is required")
        if not hmac.compare_digest(expected_secret, self._application_secret):
            raise RuntimeError("session authentication key mismatch")
        if not isinstance(supplied_secret, str) or not hmac.compare_digest(
            supplied_secret, expected_secret
        ):
            raise InvalidCredentials
        token = secrets.token_urlsafe(32)
        csrf = _keyed_hash(token, self._csrf_key)
        now = self.now()
        with self.session_factory() as session:
            row = AuthSession(
                token_hash=_keyed_hash(token, self._session_key),
                csrf_hash=_hash(csrf),
                actor=actor,
                created_at=now,
                authenticated_at=now,
                expires_at=now + self.ttl,
            )
            session.add(row)
            session.commit()
            expires_at = row.expires_at
        return IssuedSession(token=token, csrf=csrf, expires_at=expires_at)

    def authenticate(self, token: str) -> SessionPrincipal:
        if not token:
            raise InvalidSession
        token_hash = _keyed_hash(token, self._session_key)
        with self.session_factory() as session:
            row = session.execute(
                select(AuthSession).where(
                    AuthSession.token_hash == token_hash
                )
            ).scalar_one_or_none()
            if (
                row is None
                or row.revoked_at is not None
                or not hmac.compare_digest(row.token_hash, token_hash)
            ):
                raise InvalidSession
            if self.now() >= row.expires_at:
                raise SessionExpired
            return SessionPrincipal(
                actor=row.actor,
                session_id=row.id,
                authenticated_at=row.authenticated_at,
            )

    def require_csrf(self, token: str, supplied_csrf: str) -> SessionPrincipal:
        principal = self.authenticate(token)
        if not supplied_csrf:
            raise CsrfRejected
        with self.session_factory() as session:
            row = session.get(AuthSession, principal.session_id)
            if row is None or not hmac.compare_digest(
                row.csrf_hash, _hash(supplied_csrf)
            ):
                raise CsrfRejected
        return principal

    def csrf_token(self, token: str) -> str:
        principal = self.authenticate(token)
        csrf = _keyed_hash(token, self._csrf_key)
        with self.session_factory() as session:
            row = session.get(AuthSession, principal.session_id)
            if row is None or row.revoked_at is not None:
                raise InvalidSession
            if not hmac.compare_digest(row.csrf_hash, _hash(csrf)):
                raise CsrfRejected
        return csrf

    def reauthenticate(
        self,
        token: str,
        supplied_secret: str,
        expected_secret: str,
    ) -> SessionPrincipal:
        principal = self.authenticate(token)
        if not expected_secret or not expected_secret.strip():
            raise RuntimeError("APP_API_TOKEN is required")
        if not hmac.compare_digest(expected_secret, self._application_secret):
            raise RuntimeError("session authentication key mismatch")
        if not isinstance(supplied_secret, str) or not hmac.compare_digest(
            supplied_secret, expected_secret
        ):
            raise InvalidCredentials
        authenticated_at = self.now()
        with self.session_factory() as session:
            row = session.get(AuthSession, principal.session_id)
            if row is None or row.revoked_at is not None:
                raise InvalidSession
            row.authenticated_at = authenticated_at
            session.commit()
        return SessionPrincipal(
            actor=principal.actor,
            session_id=principal.session_id,
            authenticated_at=authenticated_at,
        )

    def logout(self, token: str) -> None:
        principal = self.authenticate(token)
        with self.session_factory() as session:
            row = session.get(AuthSession, principal.session_id)
            if row is None:
                raise InvalidSession
            row.revoked_at = self.now()
            session.commit()
