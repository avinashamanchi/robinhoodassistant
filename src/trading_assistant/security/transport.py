"""Loopback-only HTTPS request boundary.

This module is intentionally an ASGI boundary instead of route dependencies:
rejected requests must not reach authentication, domain services, brokers, or
providers.  The policy has a narrowly-scoped test transport for in-process
tests; normal application construction always uses the production policy.
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send


_DEFAULT_BODY_BYTES = 16 * 1024
_CHAT_BODY_BYTES = 32 * 1024
_MAX_HEADER_COUNT = 64
_MAX_HEADER_BYTES = 16 * 1024
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
_FORWARDED_HEADERS = frozenset(
    {"forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"}
)


@dataclass(frozen=True)
class TransportPolicy:
    """Fixed request-perimeter configuration for one ASGI application."""

    production_mode: bool
    origin: str
    allowed_hosts: frozenset[str]
    require_https: bool
    reject_proxy_headers: bool
    default_body_bytes: int = _DEFAULT_BODY_BYTES
    chat_body_bytes: int = _CHAT_BODY_BYTES
    max_header_count: int = _MAX_HEADER_COUNT
    max_header_bytes: int = _MAX_HEADER_BYTES

    @classmethod
    def production(cls, server, *, request_bounds=None) -> "TransportPolicy":
        origin = str(server.origin).rstrip("/")
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.port != server.port
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeError("server.origin must be one HTTPS loopback origin")
        allowed_hosts = frozenset(host.lower() for host in server.allowed_hosts)
        if parsed.hostname.lower() not in allowed_hosts:
            raise RuntimeError("server.origin host must be an allowed loopback host")
        if not allowed_hosts or any(
            not _is_loopback_host(host) for host in allowed_hosts
        ):
            raise RuntimeError("server.allowed_hosts must contain only loopback hosts")
        return cls(
            production_mode=True,
            origin=origin,
            allowed_hosts=allowed_hosts,
            require_https=True,
            reject_proxy_headers=True,
            default_body_bytes=(
                request_bounds.default_body_bytes
                if request_bounds is not None
                else _DEFAULT_BODY_BYTES
            ),
            chat_body_bytes=(
                request_bounds.chat_body_bytes
                if request_bounds is not None
                else _CHAT_BODY_BYTES
            ),
            max_header_count=(
                request_bounds.max_header_count
                if request_bounds is not None
                else _MAX_HEADER_COUNT
            ),
            max_header_bytes=(
                request_bounds.max_header_bytes
                if request_bounds is not None
                else _MAX_HEADER_BYTES
            ),
        )

    @classmethod
    def test(cls) -> "TransportPolicy":
        return cls(
            production_mode=False,
            origin="http://testserver",
            allowed_hosts=frozenset({"testserver"}),
            require_https=False,
            reject_proxy_headers=True,
        )

    def body_limit(self, path: str) -> int:
        return self.chat_body_bytes if path == "/chat" else self.default_body_bytes

    @property
    def port(self) -> int:
        parsed = urlsplit(self.origin)
        if parsed.port is not None:
            return parsed.port
        return 443 if parsed.scheme == "https" else 80

    @property
    def canonical_host(self) -> str:
        host = urlsplit(self.origin).hostname
        if host is None:  # defensive; factories validate this invariant
            raise RuntimeError("transport origin has no host")
        return host.lower()


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _parse_host(value: bytes, *, expected_port: int) -> str | None:
    """Return a normalized host only for one valid RFC bracket/port form."""
    try:
        raw = value.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not raw or raw != raw.strip() or any(ord(char) < 33 for char in raw):
        return None

    host: str
    port_text: str
    if raw.startswith("["):
        close = raw.find("]")
        if close <= 1 or raw[close + 1 : close + 2] != ":":
            return None
        if raw.count("[") != 1 or raw.count("]") != 1:
            return None
        host = raw[1:close]
        port_text = raw[close + 2 :]
        try:
            if not ipaddress.IPv6Address(host).is_loopback:
                return None
        except ipaddress.AddressValueError:
            return None
    else:
        if ":" not in raw:
            if (
                expected_port not in {80, 443}
                or raw.lower() != "localhost" and raw.lower() != "testserver"
            ):
                return None
            return raw.lower()
        if raw.count(":") != 1 or "[" in raw or "]" in raw:
            return None
        host, port_text = raw.rsplit(":", 1)
        if not host or host.lower() != "localhost":
            try:
                if not ipaddress.IPv4Address(host).is_loopback:
                    return None
            except ipaddress.AddressValueError:
                return None
    if not port_text.isascii() or not port_text.isdecimal():
        return None
    port = int(port_text)
    if not 1 <= port <= 65535 or port != expected_port:
        return None
    return host.lower()


def _error_payload(code: str, message: str) -> bytes:
    return json.dumps(
        {
            "error": {
                "code": code,
                "message": message,
                "request_id": uuid4().hex,
            }
        },
        separators=(",", ":"),
    ).encode("utf-8")


async def _send_error(
    send: Send,
    *,
    status: int,
    code: str,
    message: str,
) -> None:
    payload = _error_payload(code, message)
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class TransportBoundaryMiddleware:
    """Reject malformed, cross-origin, proxy, HTTP, and oversized requests."""

    def __init__(self, app: ASGIApp, *, policy: TransportPolicy) -> None:
        self.app = app
        self.policy = policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = list(scope.get("headers", ()))
        if (
            len(headers) > self.policy.max_header_count
            or sum(len(name) + len(value) for name, value in headers)
            > self.policy.max_header_bytes
        ):
            await _send_error(
                send,
                status=431,
                code="headers_too_large",
                message="Request headers exceed the local perimeter limit",
            )
            return

        decoded: dict[str, bytes] = {}
        for name, value in headers:
            try:
                normalized = name.decode("ascii").lower()
            except UnicodeDecodeError:
                await _send_error(
                    send,
                    status=431,
                    code="headers_too_large",
                    message="Request headers are invalid",
                )
                return
            if normalized in decoded:
                await _send_error(
                    send,
                    status=400,
                    code="untrusted_host" if normalized == "host" else "headers_too_large",
                    message="Duplicate request header is not accepted",
                )
                return
            decoded[normalized] = value

        if self.policy.reject_proxy_headers and any(
            header in decoded for header in _FORWARDED_HEADERS
        ):
            await _send_error(
                send,
                status=400,
                code="proxy_headers_forbidden",
                message="Forwarded request headers are forbidden",
            )
            return

        normalized_host = _parse_host(
            decoded.get("host", b""),
            expected_port=self.policy.port,
        )
        if normalized_host not in self.policy.allowed_hosts:
            await _send_error(
                send,
                status=400,
                code="untrusted_host",
                message="Host is not an exact configured loopback host",
            )
            return

        origin = decoded.get("origin")
        if origin is not None:
            try:
                supplied_origin = origin.decode("ascii").rstrip("/")
            except UnicodeDecodeError:
                supplied_origin = ""
            if supplied_origin != self.policy.origin:
                await _send_error(
                    send,
                    status=403,
                    code="origin_mismatch",
                    message="Origin does not match the local operator origin",
                )
                return

        is_liveness = scope.get("path") == "/health/live"
        if self.policy.require_https and scope.get("scheme") != "https" and not is_liveness:
            await _send_error(
                send,
                status=426,
                code="https_required",
                message="HTTPS is required for this request",
            )
            return

        method = scope.get("method", "").upper()
        content_length = decoded.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                declared_length = self.policy.body_limit(scope.get("path", "")) + 1
            if declared_length < 0 or declared_length > self.policy.body_limit(
                scope.get("path", "")
            ):
                await _send_error(
                    send,
                    status=413,
                    code="body_too_large",
                    message="Request body exceeds the route limit",
                )
                return
        else:
            declared_length = 0

        if method in _BODY_METHODS and declared_length and not _is_json(
            decoded.get("content-type")
        ):
            await _send_error(
                send,
                status=415,
                code="unsupported_media_type",
                message="This route accepts JSON only",
            )
            return

        body, disconnected = await _read_bounded_body(
            receive,
            max_bytes=self.policy.body_limit(scope.get("path", "")),
        )
        if disconnected:
            return
        if body is None:
            await _send_error(
                send,
                status=413,
                code="body_too_large",
                message="Request body exceeds the route limit",
            )
            return
        if method in _BODY_METHODS and body and not _is_json(
            decoded.get("content-type")
        ):
            await _send_error(
                send,
                status=415,
                code="unsupported_media_type",
                message="This route accepts JSON only",
            )
            return

        # Starlette's TrustedHostMiddleware cannot parse bracketed IPv6 itself.
        # The boundary has already validated the original Host with RFC-aware
        # parsing, so hand TrustedHost one exact canonical configured hostname.
        canonical_header = _canonical_host_header(self.policy)
        normalized_headers = [
            (name, canonical_header if name.lower() == b"host" else value)
            for name, value in headers
        ]
        normalized_scope = dict(scope)
        normalized_scope["headers"] = normalized_headers

        async def replay_receive() -> Message:
            nonlocal body
            if body is None:
                return {"type": "http.disconnect"}
            replay = body
            body = None
            return {"type": "http.request", "body": replay, "more_body": False}

        async def guarded_send(message: Message) -> None:
            if (
                message["type"] == "http.response.start"
                and not self.policy.production_mode
            ):
                headers = list(message.get("headers", ()))
                headers.append((b"x-transport-degraded", b"test_transport"))
                message = {**message, "headers": headers}
            elif (
                message["type"] == "http.response.start"
                and self.policy.require_https
                and scope.get("scheme") != "https"
                and is_liveness
            ):
                headers = list(message.get("headers", ()))
                headers.append((b"x-transport-degraded", b"https_required"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(normalized_scope, replay_receive, guarded_send)


async def _read_bounded_body(
    receive: Receive,
    *,
    max_bytes: int,
) -> tuple[bytes | None, bool]:
    """Buffer the finite JSON body before app code can observe any request data."""
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None, True
        if message["type"] != "http.request":
            return None, False
        chunk = message.get("body", b"")
        if not isinstance(chunk, bytes):
            return None, False
        total += len(chunk)
        if total > max_bytes:
            return None, False
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks), False


def _is_json(content_type: bytes | None) -> bool:
    if content_type is None:
        return False
    try:
        media_type = content_type.decode("ascii").split(";", 1)[0].strip().lower()
    except UnicodeDecodeError:
        return False
    return media_type == "application/json"


def _canonical_host_header(policy: TransportPolicy) -> bytes:
    host = policy.canonical_host
    rendered_host = f"[{host}]" if ":" in host else host
    return f"{rendered_host}:{policy.port}".encode("ascii")
