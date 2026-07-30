"""Strict, memory-only HTTPS client for the local operator application."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import secrets
import ssl
import stat
from http.cookiejar import CookieJar
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import (
    HTTPSHandler,
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from ..config import load_config


_ORIGIN = "https://localhost:8020"
_BIND_HOST = "127.0.0.1"
_CA_RELATIVE_PATH = Path(".local/tls/rootCA.pem")
_GENERIC_HTTP_MESSAGE = "Operator API request failed"
_GENERIC_REQUEST_MESSAGE = "Operator API transport failed"


@dataclass(frozen=True)
class OperatorSession:
    actor: str
    csrf_token: str
    expires_at: str | None


class OperatorApiError(RuntimeError):
    def __init__(
        self,
        *,
        status: int | None,
        code: str,
        message: str,
        request_id: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.request_id = request_id
        self.retry_after = retry_after
        super().__init__(message)


class _NoRedirect(HTTPRedirectHandler):
    """Treat every redirect as a failed local request, never a new destination."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        return None


def _require_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise OperatorApiError(
            status=200,
            code="operator_response_invalid",
            message="Operator API response is invalid",
        )
    return value


def _optional_text(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise OperatorApiError(
            status=200,
            code="operator_response_invalid",
            message="Operator API response is invalid",
        )
    return value


def _positive_finite(value: float, *, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError(name)
    return float(value)


class OperatorApiClient:
    """Reach only the configured local HTTPS operator application."""

    def __init__(
        self,
        project_root: Path,
        *,
        opener: Any = None,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 1_048_576,
        config_loader: Callable[[Path], Any] = load_config,
        ssl_context_factory: Callable[..., ssl.SSLContext] = ssl.create_default_context,
    ) -> None:
        self._project_root = self._resolve_project_root(project_root)
        self._timeout_seconds = _positive_finite(
            timeout_seconds, name="operator_timeout_invalid"
        )
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes <= 0
        ):
            raise ValueError("operator_response_bound_invalid")
        self._max_response_bytes = max_response_bytes

        config = config_loader(self._project_root / "config.yaml")
        self._validate_config(config)
        ca_path = self._validated_ca_path(config)
        context = ssl_context_factory(cafile=str(ca_path))
        if (
            getattr(context, "check_hostname", None) is not True
            or getattr(context, "verify_mode", None) != ssl.CERT_REQUIRED
        ):
            raise ValueError("operator_tls_invalid")

        self._cookies = CookieJar()
        self._csrf_token: str | None = None
        self._opener = opener or build_opener(
            ProxyHandler({}),
            HTTPCookieProcessor(self._cookies),
            HTTPSHandler(context=context),
            _NoRedirect(),
        )

    @staticmethod
    def _resolve_project_root(project_root: Path) -> Path:
        root = Path(project_root)
        if root.is_symlink():
            raise ValueError("operator_project_root_invalid")
        try:
            resolved = root.resolve(strict=True)
        except OSError:
            raise ValueError("operator_project_root_invalid") from None
        if not resolved.is_dir():
            raise ValueError("operator_project_root_invalid")
        return resolved

    @staticmethod
    def _validate_config(config: Any) -> None:
        server = getattr(config, "server", None)
        if (
            server is None
            or str(getattr(server, "origin", "")) != _ORIGIN
            or getattr(server, "bind_host", None) != _BIND_HOST
            or Path(getattr(server, "tls_ca_path", "")) != _CA_RELATIVE_PATH
        ):
            raise ValueError("operator_origin_invalid")

    def _validated_ca_path(self, config: Any) -> Path:
        relative = Path(config.server.tls_ca_path)
        candidate = self._project_root
        for part in relative.parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise ValueError("operator_ca_invalid")
        try:
            mode = candidate.stat().st_mode
        except OSError:
            raise ValueError("operator_ca_invalid") from None
        if not stat.S_ISREG(mode):
            raise ValueError("operator_ca_invalid")
        return candidate

    @staticmethod
    def _path(path: str) -> str:
        if not isinstance(path, str):
            raise ValueError("operator_path_invalid")
        parsed = urlsplit(path)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or not path.startswith("/")
            or path.startswith("//")
            or quote(path, safe="/-._~") != path
        ):
            raise ValueError("operator_path_invalid")
        segments = path.split("/")[1:]
        if any(segment in {"", ".", ".."} for segment in segments):
            raise ValueError("operator_path_invalid")
        return path

    def _read_payload(self, stream: Any, *, status: int | None) -> dict[str, object]:
        try:
            body = stream.read(self._max_response_bytes + 1)
        except Exception:
            raise OperatorApiError(
                status=status,
                code="operator_response_invalid",
                message="Operator API response is invalid",
            ) from None
        if not isinstance(body, bytes):
            raise OperatorApiError(
                status=status,
                code="operator_response_invalid",
                message="Operator API response is invalid",
            )
        if len(body) > self._max_response_bytes:
            raise OperatorApiError(
                status=status,
                code="operator_response_too_large",
                message="Operator API response is too large",
            )
        try:
            payload = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
            raise OperatorApiError(
                status=status,
                code="operator_response_invalid",
                message="Operator API response is invalid",
            ) from None
        if not isinstance(payload, dict):
            raise OperatorApiError(
                status=status,
                code="operator_response_invalid",
                message="Operator API response is invalid",
            )
        return payload

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        headers: dict[str, str] | None = None,
        authenticated: bool = True,
    ) -> dict[str, object]:
        del authenticated  # Cookies remain managed solely by the in-memory jar.
        canonical_path = self._path(path)
        body: bytes | None = None
        request_headers = {"Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        if payload is not None:
            try:
                body = json.dumps(
                    payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False
                ).encode("utf-8")
            except (TypeError, ValueError, UnicodeEncodeError):
                raise ValueError("operator_payload_invalid") from None
            request_headers["Content-Type"] = "application/json"
        request = Request(
            _ORIGIN + canonical_path,
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                status = getattr(response, "status", 200)
                return self._read_payload(response, status=status)
        except HTTPError as error:
            failure = self._http_failure(error)
        except ssl.SSLError:
            failure = OperatorApiError(
                status=None,
                code="operator_tls_failed",
                message=_GENERIC_REQUEST_MESSAGE,
            )
        except (URLError, TimeoutError, OSError):
            failure = OperatorApiError(
                status=None,
                code="operator_request_failed",
                message=_GENERIC_REQUEST_MESSAGE,
            )
        if failure.status == 401:
            self._clear_auth_state()
        raise failure

    def _http_failure(self, error: HTTPError) -> OperatorApiError:
        status = getattr(error, "code", None)
        retry_after = _retry_after(getattr(error, "headers", None))
        try:
            payload = self._read_payload(error, status=status)
        except OperatorApiError as invalid:
            return OperatorApiError(
                status=status,
                code=invalid.code,
                message=str(invalid),
                retry_after=retry_after,
            )
        envelope = payload.get("error")
        if not isinstance(envelope, dict):
            return OperatorApiError(
                status=status,
                code="operator_http_error",
                message=_GENERIC_HTTP_MESSAGE,
                retry_after=retry_after,
            )
        code = envelope.get("code")
        message = envelope.get("message")
        request_id = envelope.get("request_id")
        if (
            not isinstance(code, str)
            or not code
            or not isinstance(message, str)
            or not message
            or (request_id is not None and not isinstance(request_id, str))
        ):
            return OperatorApiError(
                status=status,
                code="operator_http_error",
                message=_GENERIC_HTTP_MESSAGE,
                retry_after=retry_after,
            )
        return OperatorApiError(
            status=status,
            code=code,
            message=message,
            request_id=request_id,
            retry_after=retry_after,
        )

    def login(self, secret: str) -> OperatorSession:
        payload = self._request(
            "POST", "/auth/login", {"secret": secret}, authenticated=False
        )
        self._csrf_token = _require_text(payload, "csrf_token")
        return OperatorSession(
            actor=_require_text(payload, "actor"),
            csrf_token=self._csrf_token,
            expires_at=_optional_text(payload, "expires_at"),
        )

    def reauthenticate(self, secret: str) -> OperatorSession:
        csrf_token = self._require_csrf()
        payload = self._request(
            "POST",
            "/auth/reauth",
            {"secret": secret},
            headers={"X-CSRF-Token": csrf_token},
        )
        return OperatorSession(
            actor=_require_text(payload, "actor"),
            csrf_token=csrf_token,
            expires_at=_optional_text(payload, "expires_at"),
        )

    def logout(self) -> None:
        try:
            self._request(
                "POST",
                "/auth/logout",
                {},
                headers={"X-CSRF-Token": self._require_csrf()},
            )
        finally:
            self._clear_auth_state()

    def get(self, path: str, *, authenticated: bool = True) -> dict[str, object]:
        return self._request("GET", path, authenticated=authenticated)

    def mutate(
        self,
        path: str,
        payload: dict[str, object],
        *,
        idempotent: bool,
    ) -> dict[str, object]:
        headers = {"X-CSRF-Token": self._require_csrf()}
        if idempotent:
            headers["Idempotency-Key"] = secrets.token_urlsafe(32)
        return self._request("POST", path, payload, headers=headers)

    def _require_csrf(self) -> str:
        if not self._csrf_token:
            raise OperatorApiError(
                status=None,
                code="operator_csrf_missing",
                message="Operator session is not authenticated",
            )
        return self._csrf_token

    def _clear_auth_state(self) -> None:
        self._csrf_token = None
        self._cookies.clear()


def _reject_constant(_value: str) -> None:
    raise ValueError("invalid JSON constant")


def _retry_after(headers: Any) -> int | None:
    if headers is None:
        return None
    value = headers.get("Retry-After")
    if not isinstance(value, str) or not value.strip().isdigit():
        return None
    return min(max(int(value.strip()), 0), 3600)
