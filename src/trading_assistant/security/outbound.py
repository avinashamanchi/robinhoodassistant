"""Fail-closed outbound transport boundaries for committed provider origins.

Origins are configuration-time triples.  Request paths and query parameters may
extend an already-approved origin, but never select a destination themselves.
"""

from __future__ import annotations

import ipaddress
import json
import ssl
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import SplitResult, urlsplit

import requests


DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 30.0
DEFAULT_MAX_RESPONSE_BYTES = 1_000_000
_SUPPORTED_SCHEMES = frozenset({"https", "wss"})
_DEFAULT_PORTS = {"https": 443, "wss": 443}


class OutboundError(Exception):
    """Base class for URL-free outbound boundary failures."""


class OutboundOriginDenied(OutboundError, ValueError):
    """A URL is not a committed outbound origin.

    The message is intentionally URL-free: callers can safely surface it without
    logging provider paths, query values, or credentials.
    """

    def __init__(self) -> None:
        super().__init__("outbound origin denied")


class OutboundRedirectDenied(OutboundError, RuntimeError):
    """An outbound provider attempted to redirect a request."""

    def __init__(self) -> None:
        super().__init__("outbound redirect rejected")


class OutboundResponseTooLarge(OutboundError, RuntimeError):
    """An outbound response exceeded its configured byte cap."""

    def __init__(self) -> None:
        super().__init__("outbound response too large")


class OutboundResponseInvalid(OutboundError, RuntimeError):
    """A bounded outbound response could not be parsed safely."""

    def __init__(self) -> None:
        super().__init__("invalid outbound response")


class OutboundRequestFailed(OutboundError, RuntimeError):
    """A provider returned an unsuccessful status without disclosing URL details."""

    def __init__(self) -> None:
        super().__init__("outbound request failed")


class OutboundConnectionFailed(OutboundError, RuntimeError):
    """A WebSocket connector failed without exposing transport details."""

    def __init__(self) -> None:
        super().__init__("outbound connection failed")


def _deny() -> None:
    raise OutboundOriginDenied()


def _normalised_triple(
    parts: SplitResult,
    *,
    allowed_schemes: Iterable[str],
    allow_non_default_port: bool,
    allow_request_components: bool,
) -> tuple[str, str, int]:
    """Parse a URL into a security-relevant origin triple without echoing input."""
    try:
        scheme = parts.scheme.lower()
        allowed = frozenset(item.lower() for item in allowed_schemes)
        if scheme not in _SUPPORTED_SCHEMES or scheme not in allowed:
            _deny()
        if not parts.netloc or "@" in parts.netloc:
            _deny()
        if parts.username is not None or parts.password is not None:
            _deny()
        if not allow_request_components and (
            parts.path not in ("", "/") or parts.query or parts.fragment
        ):
            _deny()
        if allow_request_components and parts.fragment:
            _deny()
        hostname = parts.hostname
        if not hostname:
            _deny()
        hostname = hostname.encode("idna").decode("ascii").lower()
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            if ":" in hostname:
                _deny()
        else:
            _deny()
        parsed_port = parts.port
        port = _DEFAULT_PORTS[scheme] if parsed_port is None else parsed_port
        if port < 1 or port > 65535:
            _deny()
        if not allow_non_default_port and port != _DEFAULT_PORTS[scheme]:
            _deny()
        return scheme, hostname, port
    except (UnicodeError, ValueError):
        _deny()


@dataclass(frozen=True)
class OutboundOrigin:
    """Normalized, exact HTTPS or WSS origin; paths and query strings are absent."""

    scheme: str
    hostname: str
    port: int

    @classmethod
    def parse(
        cls,
        url: str,
        *,
        allowed_schemes: Iterable[str] = _SUPPORTED_SCHEMES,
        allow_non_default_port: bool = False,
    ) -> "OutboundOrigin":
        try:
            parts = urlsplit(str(url))
            scheme, hostname, port = _normalised_triple(
                parts,
                allowed_schemes=allowed_schemes,
                allow_non_default_port=allow_non_default_port,
                allow_request_components=False,
            )
            return cls(scheme, hostname, port)
        except (AttributeError, TypeError, ValueError):
            _deny()


class OutboundPolicy:
    """Allow only exact, pre-committed normalized origin triples."""

    def __init__(self, *origins: str | OutboundOrigin) -> None:
        if not origins:
            _deny()
        parsed: set[OutboundOrigin] = set()
        for origin in origins:
            if isinstance(origin, OutboundOrigin):
                parsed.add(origin)
            else:
                parsed.add(
                    OutboundOrigin.parse(
                        origin,
                        allow_non_default_port=True,
                    )
                )
        self._origins = frozenset(parsed)
        self._schemes = frozenset(origin.scheme for origin in self._origins)

    @property
    def origins(self) -> frozenset[OutboundOrigin]:
        return self._origins

    def assert_url(self, url: str) -> None:
        """Reject a request URL before transport I/O unless its triple is pinned."""
        try:
            parts = urlsplit(str(url))
            scheme, hostname, port = _normalised_triple(
                parts,
                allowed_schemes=self._schemes,
                allow_non_default_port=True,
                allow_request_components=True,
            )
            if OutboundOrigin(scheme, hostname, port) not in self._origins:
                _deny()
        except (AttributeError, TypeError, ValueError):
            _deny()

    def assert_response(self, response: Any) -> None:
        """Validate the final request URL and reject every redirect response."""
        request = getattr(response, "request", None)
        request_url = getattr(request, "url", None)
        if request_url is None:
            _deny()
        self.assert_url(str(request_url))
        status = getattr(response, "status_code", None)
        if isinstance(status, int) and 300 <= status < 400:
            raise OutboundRedirectDenied()


def _content_length(headers: Any) -> int | None:
    value = getattr(headers, "get", lambda _key, _default=None: None)(
        "Content-Length", None
    )
    if value is None:
        return None
    try:
        length = int(value)
    except (TypeError, ValueError):
        raise OutboundResponseTooLarge() from None
    if length < 0:
        raise OutboundResponseTooLarge()
    return length


def _bounded_bytes(chunks: Iterable[bytes], max_response_bytes: int) -> bytes:
    if max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be positive")
    body = bytearray()
    for chunk in chunks:
        if not chunk:
            continue
        body.extend(chunk)
        if len(body) > max_response_bytes:
            raise OutboundResponseTooLarge()
    return bytes(body)


def read_bounded_json(response: Any, *, max_response_bytes: int) -> Any:
    """Read a direct HTTP response once, enforcing a cap before JSON parsing."""
    length = _content_length(getattr(response, "headers", {}))
    if length is not None and length > max_response_bytes:
        raise OutboundResponseTooLarge()
    try:
        body = _bounded_bytes(response.iter_bytes(), max_response_bytes)
        return json.loads(body)
    except OutboundResponseTooLarge:
        raise
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise OutboundResponseInvalid() from None


class NoRedirectSession(requests.Session):
    """Requests session that pins its policy, TLS, timeouts, redirects, and body cap."""

    def __init__(
        self,
        policy: OutboundPolicy,
        *,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if read_timeout <= 0:
            raise ValueError("read_timeout must be positive")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        super().__init__()
        self._policy = policy
        self._default_timeout = float(read_timeout)
        self._max_response_bytes = max_response_bytes

    def request(self, method: str, url: str, **kwargs: Any):
        self._policy.assert_url(url)
        kwargs["allow_redirects"] = False
        kwargs["timeout"] = (DEFAULT_CONNECT_TIMEOUT, self._default_timeout)
        kwargs["verify"] = True
        kwargs["stream"] = True
        response = super().request(method, url, **kwargs)
        try:
            self._policy.assert_response(response)
            length = _content_length(response.headers)
            if length is not None and length > self._max_response_bytes:
                raise OutboundResponseTooLarge()
            response._content = _bounded_bytes(
                response.iter_content(chunk_size=8192), self._max_response_bytes
            )
            response._content_consumed = True
            return response
        except Exception:
            response.close()
            raise


def install_pinned_session(
    client: Any,
    policy: OutboundPolicy,
    *,
    read_timeout: float,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> None:
    """Install the requests guard on an SDK client after checking its base URL."""
    base_url = getattr(client, "_base_url", None)
    base_url = getattr(base_url, "value", base_url)
    policy.assert_url(str(base_url))
    client._session = NoRedirectSession(
        policy,
        read_timeout=read_timeout,
        max_response_bytes=max_response_bytes,
    )


def new_httpx_client(
    policy: OutboundPolicy,
    *,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    transport: Any = None,
):
    """Create an httpx client that validates every send and never follows redirects."""
    if read_timeout <= 0:
        raise ValueError("read_timeout must be positive")
    import httpx

    class _PinnedHTTPXClient(httpx.Client):
        def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
            policy.assert_url(str(request.url))
            kwargs["follow_redirects"] = False
            response = super().send(request, **kwargs)
            try:
                policy.assert_response(response)
                return response
            except Exception:
                response.close()
                raise

    options: dict[str, Any] = {
        "follow_redirects": False,
        "verify": True,
        "timeout": httpx.Timeout(
            connect=DEFAULT_CONNECT_TIMEOUT,
            read=read_timeout,
            write=read_timeout,
            pool=DEFAULT_CONNECT_TIMEOUT,
        ),
    }
    if transport is not None:
        options["transport"] = transport
    return _PinnedHTTPXClient(**options)


class PinnedWebSocket:
    """A connector adapter for the optional Alpaca stream, with no redirect path."""

    def __init__(
        self,
        policy: OutboundPolicy,
        connector: Any,
        *,
        open_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        ping_timeout: float = DEFAULT_READ_TIMEOUT,
        close_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        if min(open_timeout, ping_timeout, close_timeout) <= 0:
            raise ValueError("WebSocket timeouts must be positive")
        self._policy = policy
        self._connector = connector
        self._open_timeout = open_timeout
        self._ping_timeout = ping_timeout
        self._close_timeout = close_timeout

    def connect(self, url: str) -> Any:
        self._policy.assert_url(url)
        tls_context = ssl.create_default_context()
        try:
            handshake = self._connector(
                url,
                ssl_context=tls_context,
                open_timeout=self._open_timeout,
                ping_timeout=self._ping_timeout,
                close_timeout=self._close_timeout,
            )
        except (OutboundOriginDenied, OutboundRedirectDenied):
            raise
        except Exception:
            raise OutboundConnectionFailed() from None
        status = getattr(handshake, "status_code", 101)
        if isinstance(status, int) and 300 <= status < 400:
            raise OutboundRedirectDenied()
        return handshake
