"""Fail-closed outbound transport boundaries for committed provider origins.

Origins are configuration-time triples.  Request paths and query parameters may
extend an already-approved origin, but never select a destination themselves.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import numbers
import ssl
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable
from urllib.parse import SplitResult, parse_qsl, urlsplit

import certifi
import requests
from requests.adapters import HTTPAdapter
from websockets.asyncio.client import connect as _WebSocketConnect


DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 30.0
DEFAULT_MAX_RESPONSE_BYTES = 1_000_000
LOCAL_LIVENESS_URL = "https://localhost:8020/health/live"
LOCAL_LIVENESS_MAX_RESPONSE_BYTES = 1024
_SUPPORTED_SCHEMES = frozenset({"https", "wss"})
_DEFAULT_PORTS = {"https": 443, "wss": 443}
_QUERY_CREDENTIAL_NAMES = frozenset(
    {"access_key", "api_key", "apikey", "credential", "secret", "token"}
)
_UNSET = object()


@dataclass(frozen=True, slots=True)
class OutboundOriginRule:
    """One exact adapter destination and the runtime roles that may use it."""

    key: str
    adapter: str
    origin: str
    roles: frozenset[str]
    feature_gate: str | None = None


OUTBOUND_ORIGIN_MANIFEST = (
    OutboundOriginRule(
        "alpaca_trading",
        "alpaca.trading",
        "https://paper-api.alpaca.markets",
        frozenset(
            {
                "app",
                "daemon",
                "mcp",
                "paper-drill",
                "preflight",
                "safety-drill",
                "watchdog",
            }
        ),
    ),
    OutboundOriginRule(
        "alpaca_data",
        "alpaca.historical",
        "https://data.alpaca.markets",
        frozenset(
            {
                "app",
                "daemon",
                "mcp",
                "paper-drill",
                "preflight",
                "safety-drill",
                "validate-analyst",
            }
        ),
    ),
    OutboundOriginRule(
        "alpaca_stream",
        "alpaca.stream",
        "wss://stream.data.alpaca.markets",
        frozenset({"daemon"}),
        "daemon.use_websocket",
    ),
    OutboundOriginRule(
        "anthropic",
        "llm.anthropic",
        "https://api.anthropic.com",
        frozenset({"app", "daemon", "validate-analyst"}),
        "llm.provider=anthropic",
    ),
    OutboundOriginRule(
        "gemini",
        "llm.gemini",
        "https://generativelanguage.googleapis.com",
        frozenset({"app", "daemon", "validate-analyst"}),
        "llm.provider=gemini",
    ),
    OutboundOriginRule(
        "groq",
        "llm.groq",
        "https://api.groq.com",
        frozenset({"app", "daemon", "validate-analyst"}),
        "llm.provider=groq",
    ),
    OutboundOriginRule(
        "telegram",
        "notifier.telegram",
        "https://api.telegram.org",
        frozenset({"app", "daemon", "preflight", "watchdog"}),
        "features.telegram_notifications",
    ),
    OutboundOriginRule(
        "coingecko",
        "marketdata.coingecko",
        "https://api.coingecko.com",
        frozenset({"app", "daemon"}),
        "crypto_risk",
    ),
)
OUTBOUND_ORIGINS_BY_KEY = MappingProxyType(
    {rule.key: rule.origin for rule in OUTBOUND_ORIGIN_MANIFEST}
)


def require_origin(role: str, adapter: str, url: str) -> str:
    """Return an exact manifest origin or reject without disclosing the input."""

    if not all(
        isinstance(value, str) and value
        for value in (role, adapter, url)
    ):
        raise OutboundOriginDenied()
    matches = [
        rule
        for rule in OUTBOUND_ORIGIN_MANIFEST
        if rule.adapter == adapter
        and role in rule.roles
        and rule.origin == url
    ]
    if len(matches) != 1:
        raise OutboundOriginDenied()
    OutboundOrigin.parse(url, allow_non_default_port=True)
    return matches[0].origin


def configured_origins_match_manifest(configured: Any) -> bool:
    """Return only whether config has the complete, exact committed manifest."""

    try:
        observed = {
            key: str(getattr(configured, key))
            for key in type(configured).model_fields
        }
    except (AttributeError, TypeError, ValueError):
        return False
    return observed == dict(OUTBOUND_ORIGINS_BY_KEY)


def origins_for_role(config: Any, role: str) -> frozenset[str]:
    """Resolve destinations enabled for one role without constructing clients."""

    selected: set[str] = set()
    for rule in OUTBOUND_ORIGIN_MANIFEST:
        if role not in rule.roles:
            continue
        gate = rule.feature_gate
        if gate == "daemon.use_websocket" and not config.daemon.use_websocket:
            continue
        if gate and gate.startswith("llm.provider="):
            if config.llm.provider != gate.partition("=")[2]:
                continue
        if (
            gate == "features.telegram_notifications"
            and not config.features.telegram_notifications
        ):
            continue
        if gate == "crypto_risk" and config.crypto_risk is None:
            continue
        selected.add(rule.origin)
    return frozenset(selected)


def require_configured_role_origins(
    config: Any,
    role: str,
) -> frozenset[str]:
    """Prove each enabled role/adapter destination against the manifest."""

    if not configured_origins_match_manifest(config.provider_origins):
        raise OutboundOriginDenied()
    enabled = origins_for_role(config, role)
    for rule in OUTBOUND_ORIGIN_MANIFEST:
        if role not in rule.roles or rule.origin not in enabled:
            continue
        configured = str(getattr(config.provider_origins, rule.key))
        require_origin(role, rule.adapter, configured)
    return enabled


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


class LocalLivenessTransport:
    """One proxy-free client pinned to the local HTTPS liveness endpoint."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def fetch(
        self,
        url: str,
        *,
        timeout_seconds: float,
    ) -> bytes:
        if url != LOCAL_LIVENESS_URL:
            raise OutboundRequestFailed()
        timeout = _positive_finite(
            timeout_seconds,
            name="timeout_seconds",
        )
        response = None
        try:
            response = self._client.get(
                LOCAL_LIVENESS_URL,
                follow_redirects=False,
                timeout=timeout,
            )
            if (
                str(getattr(response, "url", "")) != LOCAL_LIVENESS_URL
                or getattr(response, "status_code", None) != 200
                or bool(getattr(response, "history", ()))
            ):
                raise OutboundRequestFailed()
            length = _content_length(getattr(response, "headers", {}))
            if (
                length is not None
                and length > LOCAL_LIVENESS_MAX_RESPONSE_BYTES
            ):
                raise OutboundResponseTooLarge()
            return _bounded_bytes(
                response.iter_bytes(),
                LOCAL_LIVENESS_MAX_RESPONSE_BYTES,
            )
        except OutboundError:
            raise
        except Exception:
            raise OutboundRequestFailed() from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass


def build_local_liveness_transport(
    ca_certificate_path: Any,
    *,
    client_factory: Any = None,
    ssl_context_factory: Any = ssl.create_default_context,
) -> LocalLivenessTransport:
    """Build a local-only HTTPX transport without proxy/environment trust."""

    if str(ca_certificate_path) != ".local/tls/rootCA.pem":
        raise OutboundOriginDenied()
    if client_factory is None:
        import httpx

        client_factory = httpx.Client
    context = ssl_context_factory(cafile=str(ca_certificate_path))
    client = client_factory(
        follow_redirects=False,
        trust_env=False,
        proxy=None,
        verify=context,
    )
    return LocalLivenessTransport(client)


def _positive_finite(value: Any, *, name: str) -> float:
    """Return a numeric timeout only when it is strictly positive and finite."""
    if (
        isinstance(value, bool)
        or not isinstance(value, numbers.Real)
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError(f"{name} must be a positive finite number")
    return float(value)


def _verified_ssl_context() -> ssl.SSLContext:
    """Build a verified TLS context without reading SSL_CERT_FILE or proxy env."""
    return ssl.create_default_context(cafile=certifi.where())


class _VerifiedHTTPAdapter(HTTPAdapter):
    """Requests adapter fixed to an explicit verified TLS context."""

    def __init__(self, ssl_context: ssl.SSLContext) -> None:
        self._ssl_context = ssl_context
        super().__init__(max_retries=0)

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        kwargs["ssl_context"] = self._ssl_context
        super().init_poolmanager(*args, **kwargs)

    def build_connection_pool_key_attributes(
        self,
        request: Any,
        verify: Any,
        cert: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(
            request,
            True,
            None,
        )
        pool_kwargs["ssl_context"] = self._ssl_context
        pool_kwargs["cert_reqs"] = "CERT_REQUIRED"
        for key in ("ca_certs", "ca_cert_dir", "cert_file", "key_file"):
            pool_kwargs.pop(key, None)
        return host_params, pool_kwargs


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

    @property
    def origin(self) -> str:
        """Expose one canonical manifest origin for structural inspection only."""

        if len(self._origins) != 1:
            raise OutboundOriginDenied()
        origin = next(iter(self._origins))
        default_port = _DEFAULT_PORTS[origin.scheme]
        suffix = "" if origin.port == default_port else f":{origin.port}"
        return f"{origin.scheme}://{origin.hostname}{suffix}"

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
            if any(
                name.lower() in _QUERY_CREDENTIAL_NAMES
                for name, _value in parse_qsl(
                    parts.query,
                    keep_blank_values=True,
                )
            ):
                _deny()
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


def _validated_query_params(params: Any) -> Any:
    """Reject unsupported/query-credential mappings before request creation."""

    if params is None:
        return None
    if hasattr(params, "items"):
        pairs = list(params.items())
    elif isinstance(params, (list, tuple)):
        pairs = list(params)
    else:
        raise OutboundOriginDenied()
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise OutboundOriginDenied()
        key = pair[0]
        if not isinstance(key, str):
            raise OutboundOriginDenied()
        if key.lower() in _QUERY_CREDENTIAL_NAMES:
            raise OutboundOriginDenied()
    return params


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


def _bound_httpx_response(response: Any, max_response_bytes: int) -> Any:
    """Consume an HTTPX response once, retaining only a bounded in-memory body."""
    length = _content_length(response.headers)
    if length is not None and length > max_response_bytes:
        raise OutboundResponseTooLarge()
    response._content = _bounded_bytes(response.iter_bytes(), max_response_bytes)
    return response


async def _bound_async_httpx_response(response: Any, max_response_bytes: int) -> Any:
    """Async equivalent of the HTTPX response cap without eager unbounded reads."""
    length = _content_length(response.headers)
    if length is not None and length > max_response_bytes:
        raise OutboundResponseTooLarge()
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        body.extend(chunk)
        if len(body) > max_response_bytes:
            raise OutboundResponseTooLarge()
    response._content = bytes(body)
    return response


async def _aclose_async_response_shielded(response: Any) -> None:
    """Finish exactly one async response close despite repeated task cancellation."""
    close_task = asyncio.ensure_future(response.aclose())
    while not close_task.done():
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            # The caller's cancellation must still propagate after the response
            # is closed; shield keeps this separate cleanup task running.
            continue
    try:
        close_task.result()
    except BaseException:
        # A cleanup failure must not replace the original boundary error or
        # cancellation that entered the caller's except BaseException branch.
        pass


class NoRedirectSession(requests.Session):
    """Requests session that pins its policy, TLS, timeouts, redirects, and body cap."""

    def __init__(
        self,
        policy: OutboundPolicy,
        *,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        read_timeout = _positive_finite(read_timeout, name="read_timeout")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        super().__init__()
        self._policy = policy
        self._default_timeout = read_timeout
        self._max_response_bytes = max_response_bytes
        self.trust_env = False
        self.proxies = {}
        self.verify = True
        self._ssl_context = _verified_ssl_context()
        self.adapters["https://"] = _VerifiedHTTPAdapter(self._ssl_context)

    def request(
        self,
        method: str,
        url: str,
        params: Any = None,
        data: Any = None,
        headers: Any = None,
        cookies: Any = None,
        files: Any = None,
        auth: Any = None,
        timeout: Any = None,
        allow_redirects: bool = False,
        proxies: Any = None,
        hooks: Any = None,
        stream: Any = None,
        verify: Any = None,
        cert: Any = None,
        json: Any = None,
    ):
        self._policy.assert_url(url)
        return super().request(
            method,
            url,
            params=_validated_query_params(params),
            data=data,
            headers=headers,
            cookies=cookies,
            files=files,
            auth=auth,
            timeout=timeout,
            allow_redirects=False,
            proxies={},
            hooks=hooks,
            stream=stream,
            verify=True,
            cert=None,
            json=json,
        )

    def send(
        self,
        request: requests.PreparedRequest,
        *,
        stream: Any = None,
        timeout: Any = None,
        verify: Any = None,
        cert: Any = None,
        proxies: Any = None,
        allow_redirects: bool = False,
    ):
        """Enforce policy for direct PreparedRequest sends as well as request()."""
        self._policy.assert_url(str(request.url))
        response = super().send(
            request,
            allow_redirects=False,
            timeout=(DEFAULT_CONNECT_TIMEOUT, self._default_timeout),
            verify=True,
            stream=True,
            cert=None,
            proxies={},
        )
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
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    write_timeout: float | object = _UNSET,
    pool_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    transport: Any = None,
):
    """Create an HTTPX client with immutable transport policy on every send."""
    import httpx

    connect_timeout = _positive_finite(connect_timeout, name="connect_timeout")
    read_timeout = _positive_finite(read_timeout, name="read_timeout")
    if write_timeout is _UNSET:
        write_timeout = read_timeout
    write_timeout = _positive_finite(write_timeout, name="write_timeout")
    pool_timeout = _positive_finite(pool_timeout, name="pool_timeout")
    if max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be positive")
    timeout = httpx.Timeout(
        connect=connect_timeout,
        read=read_timeout,
        write=write_timeout,
        pool=pool_timeout,
    )

    class _PinnedHTTPXClient(httpx.Client):
        def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
            policy.assert_url(str(request.url))
            kwargs["follow_redirects"] = False
            kwargs["stream"] = True
            request.extensions["timeout"] = timeout.as_dict()
            response = super().send(request, **kwargs)
            try:
                policy.assert_response(response)
                return _bound_httpx_response(response, max_response_bytes)
            except Exception:
                response.close()
                raise

    options: dict[str, Any] = {
        "follow_redirects": False,
        "verify": _verified_ssl_context(),
        "trust_env": False,
        "proxy": None,
        "timeout": timeout,
    }
    if transport is not None:
        options["transport"] = transport
    return _PinnedHTTPXClient(**options)


def new_async_httpx_client(
    policy: OutboundPolicy,
    *,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    write_timeout: float | object = _UNSET,
    pool_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    transport: Any = None,
):
    """Create the async counterpart with the same immutable send boundary."""
    import httpx

    connect_timeout = _positive_finite(connect_timeout, name="connect_timeout")
    read_timeout = _positive_finite(read_timeout, name="read_timeout")
    if write_timeout is _UNSET:
        write_timeout = read_timeout
    write_timeout = _positive_finite(write_timeout, name="write_timeout")
    pool_timeout = _positive_finite(pool_timeout, name="pool_timeout")
    if max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be positive")
    timeout = httpx.Timeout(
        connect=connect_timeout,
        read=read_timeout,
        write=write_timeout,
        pool=pool_timeout,
    )

    class _PinnedAsyncHTTPXClient(httpx.AsyncClient):
        async def send(
            self,
            request: httpx.Request,
            **kwargs: Any,
        ) -> httpx.Response:
            policy.assert_url(str(request.url))
            kwargs["follow_redirects"] = False
            kwargs["stream"] = True
            request.extensions["timeout"] = timeout.as_dict()
            response = await super().send(request, **kwargs)
            try:
                policy.assert_response(response)
                return await _bound_async_httpx_response(
                    response,
                    max_response_bytes,
                )
            except BaseException:
                await _aclose_async_response_shielded(response)
                raise

    options: dict[str, Any] = {
        "follow_redirects": False,
        "verify": _verified_ssl_context(),
        "trust_env": False,
        "proxy": None,
        "timeout": timeout,
    }
    if transport is not None:
        options["transport"] = transport
    return _PinnedAsyncHTTPXClient(**options)


class _NoRedirectWebSocketConnect(_WebSocketConnect):
    """Installed websockets redirect hook that refuses every handshake 3xx."""

    def process_redirect(self, exc: Exception) -> Exception | str:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int) and 300 <= status < 400:
            raise OutboundRedirectDenied() from None
        return exc


class PinnedWebSocket:
    """Concrete websocket boundary using the installed no-redirect connector."""

    def __init__(
        self,
        policy: OutboundPolicy,
        *,
        open_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        ping_timeout: float = DEFAULT_READ_TIMEOUT,
        close_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        self._policy = policy
        self._open_timeout = _positive_finite(open_timeout, name="open_timeout")
        self._ping_timeout = _positive_finite(ping_timeout, name="ping_timeout")
        self._close_timeout = _positive_finite(close_timeout, name="close_timeout")

    async def connect(self, url: str) -> Any:
        self._policy.assert_url(url)
        tls_context = ssl.create_default_context()
        try:
            return await _NoRedirectWebSocketConnect(
                url,
                proxy=None,
                ssl=tls_context,
                open_timeout=self._open_timeout,
                ping_interval=self._ping_timeout,
                ping_timeout=self._ping_timeout,
                close_timeout=self._close_timeout,
            )
        except (OutboundOriginDenied, OutboundRedirectDenied):
            raise
        except Exception:
            raise OutboundConnectionFailed() from None
