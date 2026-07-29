"""Outbound-provider trust boundary tests; all transports are in-process fakes."""

from __future__ import annotations

import asyncio
import io
import ssl

import pytest
import requests
from requests.adapters import BaseAdapter


@pytest.fixture
def outbound():
    from trading_assistant.security import outbound as module

    return module


@pytest.fixture
def alpaca_policy(outbound):
    return outbound.OutboundPolicy("https://paper-api.alpaca.markets")


class _ResponseAdapter(BaseAdapter):
    """A requests adapter that cannot reach a network and records every send."""

    def __init__(self, *, status: int = 200, body: bytes = b"{}", headers=None) -> None:
        self.calls: list[tuple[object, dict]] = []
        self.status = status
        self.body = body
        self.headers = headers or {}

    def send(self, request, **kwargs):
        self.calls.append((request, kwargs))
        response = requests.Response()
        response.status_code = self.status
        response.headers.update(self.headers)
        response.request = request
        response.url = request.url
        response.raw = io.BytesIO(self.body)
        response._content = False
        return response

    def close(self):
        pass


def test_outbound_manifest_is_exact_and_role_feature_scoped(
    outbound,
    app_config,
):
    keys = tuple(rule.key for rule in outbound.OUTBOUND_ORIGIN_MANIFEST)

    assert keys == (
        "alpaca_trading",
        "alpaca_data",
        "alpaca_stream",
        "anthropic",
        "gemini",
        "groq",
        "telegram",
        "coingecko",
    )
    assert "composio" not in keys
    assert outbound.configured_origins_match_manifest(
        app_config.provider_origins
    )
    selected_origin = {
        "anthropic": "https://api.anthropic.com",
        "gemini": "https://generativelanguage.googleapis.com",
        "groq": "https://api.groq.com",
    }[app_config.llm.provider]
    assert outbound.origins_for_role(app_config, "preflight") == frozenset(
        {
            "https://paper-api.alpaca.markets",
            "https://data.alpaca.markets",
            selected_origin,
        }
    )

    anthropic = app_config.model_copy(
        update={
            "llm": app_config.llm.model_copy(
                update={"provider": "anthropic"}
            )
        }
    )
    assert "https://api.anthropic.com" in outbound.origins_for_role(
        anthropic,
        "preflight",
    )
    assert (
        "https://generativelanguage.googleapis.com"
        not in outbound.origins_for_role(anthropic, "preflight")
    )

    notifications = app_config.model_copy(
        update={
            "features": app_config.features.model_copy(
                update={"telegram_notifications": True}
            )
        }
    )
    assert "https://api.telegram.org" in outbound.origins_for_role(
        notifications,
        "preflight",
    )
    assert "https://api.telegram.org" not in outbound.origins_for_role(
        notifications,
        "validate-analyst",
    )


def test_origin_normalizes_idna_case_and_default_port(outbound):
    """Changing host casing, Unicode spelling, or :443 must not create a new origin."""
    origin = outbound.OutboundOrigin.parse("HTTPS://B\u00dcCHER.example:443/")

    assert (origin.scheme, origin.hostname, origin.port) == (
        "https",
        "xn--bcher-kva.example",
        443,
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://paper-api.alpaca.markets/v2/account",
        "https://paper-api.alpaca.markets.evil.test/v2/account",
        "https://127.0.0.1/v2/account",
        "https://[::1]/v2/account",
        "file:///etc/passwd",
        "https://paper-api.alpaca.markets:bad/v2/account",
        "wss://paper-api.alpaca.markets/v2/account",
    ],
)
def test_outbound_policy_rejects_non_exact_origin(url, alpaca_policy, outbound):
    """A different scheme, host, IP literal, or malformed port cannot reach an adapter."""
    with pytest.raises(outbound.OutboundOriginDenied):
        alpaca_policy.assert_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@api.example.test",
        "https://api.example.test?next=https://evil.test",
        "https://api.example.test/#fragment",
        "https://api.example.test/v1",
        "https://api.example.test:444",
    ],
)
def test_origin_rejects_non_origin_configuration(url, outbound):
    """Configured origins are triples, never credentials, paths, or unapproved ports."""
    with pytest.raises(outbound.OutboundOriginDenied):
        outbound.OutboundOrigin.parse(url)


def test_policy_accepts_request_paths_and_queries_only_after_exact_origin_match(
    alpaca_policy,
):
    """Request data may extend a pinned origin, but can never choose one."""
    assert alpaca_policy.assert_url(
        "https://PAPER-API.ALPACA.MARKETS:443/v2/orders?status=open"
    ) is None


def test_policy_allows_explicitly_configured_non_default_port(outbound):
    """A non-default port only works when it is part of the committed origin."""
    policy = outbound.OutboundPolicy("https://api.example.test:8443")

    policy.assert_url("https://api.example.test:8443/v1/data")
    with pytest.raises(outbound.OutboundOriginDenied):
        policy.assert_url("https://api.example.test/v1/data")


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_requests_session_rejects_redirect_before_second_request(
    status, alpaca_policy, outbound
):
    """Removing the redirect rejection would permit a second adapter call to an attacker."""
    adapter = _ResponseAdapter(
        status=status,
        headers={"Location": "https://evil.test/provider-secret"},
    )
    session = outbound.NoRedirectSession(
        alpaca_policy,
        read_timeout=7.5,
        max_response_bytes=128,
    )
    session.mount("https://", adapter)

    with pytest.raises(outbound.OutboundRedirectDenied) as raised:
        session.get("https://paper-api.alpaca.markets/v2/account")

    assert len(adapter.calls) == 1
    assert str(raised.value) == "outbound redirect rejected"
    assert "provider-secret" not in str(raised.value)


def test_requests_session_forces_tls_verification_and_finite_timeouts(
    alpaca_policy, outbound
):
    """Caller-supplied redirect, TLS, and timeout options cannot weaken the transport."""
    adapter = _ResponseAdapter()
    session = outbound.NoRedirectSession(
        alpaca_policy,
        read_timeout=7.5,
        max_response_bytes=128,
    )
    session.mount("https://", adapter)

    session.get(
        "https://paper-api.alpaca.markets/v2/account",
        allow_redirects=True,
        verify=False,
        timeout=None,
    )

    _, sent = adapter.calls[0]
    assert sent["verify"] is True
    assert sent["timeout"] == (5.0, 7.5)


def test_requests_session_rejects_oversized_response_without_body_leak(
    alpaca_policy, outbound
):
    """A large provider response is rejected before application parsing or error logging."""
    secret = b"provider-secret-body"
    adapter = _ResponseAdapter(
        body=secret,
        headers={"Content-Length": str(len(secret))},
    )
    session = outbound.NoRedirectSession(
        alpaca_policy,
        read_timeout=7.5,
        max_response_bytes=4,
    )
    session.mount("https://", adapter)

    with pytest.raises(outbound.OutboundResponseTooLarge) as raised:
        session.get("https://paper-api.alpaca.markets/v2/account")

    assert str(raised.value) == "outbound response too large"
    assert secret.decode() not in str(raised.value)


def test_requests_session_rejects_unannounced_oversized_stream_without_body_leak(
    alpaca_policy, outbound
):
    """Chunked responses cannot bypass the response cap by omitting Content-Length."""
    secret = b"provider-secret-body"
    adapter = _ResponseAdapter(body=secret)
    session = outbound.NoRedirectSession(
        alpaca_policy,
        read_timeout=7.5,
        max_response_bytes=4,
    )
    session.mount("https://", adapter)

    with pytest.raises(outbound.OutboundResponseTooLarge) as raised:
        session.get("https://paper-api.alpaca.markets/v2/account")

    assert str(raised.value) == "outbound response too large"
    assert secret.decode() not in str(raised.value)


def test_requests_session_denies_before_any_io(alpaca_policy, outbound):
    """A model- or request-derived host must be rejected before the adapter runs."""
    adapter = _ResponseAdapter()
    session = outbound.NoRedirectSession(alpaca_policy)
    session.mount("https://", adapter)

    with pytest.raises(outbound.OutboundOriginDenied):
        session.get("https://paper-api.alpaca.markets.evil.test/v2/account")

    assert adapter.calls == []


def test_httpx_transport_validates_response_url_and_rejects_redirect(outbound):
    """A redirect response is an error even when httpx is configured not to follow it."""
    import httpx

    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(
            302,
            headers={"Location": "https://evil.test/provider-secret"},
            request=request,
        )

    policy = outbound.OutboundPolicy("https://api.example.test")
    client = outbound.new_httpx_client(
        policy,
        read_timeout=7.5,
        transport=httpx.MockTransport(transport),
    )

    with pytest.raises(outbound.OutboundRedirectDenied) as raised:
        client.get("https://api.example.test/v1/data")

    assert len(calls) == 1
    assert client.follow_redirects is False
    assert client.timeout.connect == 5.0
    assert client.timeout.read == 7.5
    secure_client = outbound.new_httpx_client(policy, read_timeout=7.5)
    assert secure_client._transport._pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
    secure_client.close()
    assert str(raised.value) == "outbound redirect rejected"
    assert "provider-secret" not in str(raised.value)


def test_websocket_connector_uses_concrete_no_redirect_adapter(
    monkeypatch, outbound
):
    """The production stream constructs the installed no-redirect adapter directly."""
    seen = {}

    class Attempt:
        def __init__(self, url, **kwargs):
            seen["url"] = url
            seen.update(kwargs)

        def __await__(self):
            async def complete():
                return object()

            return complete().__await__()

    monkeypatch.setattr(outbound, "_NoRedirectWebSocketConnect", Attempt)
    stream = outbound.PinnedWebSocket(
        outbound.OutboundPolicy("wss://stream.data.alpaca.markets"),
        open_timeout=4.0,
        ping_timeout=5.0,
        close_timeout=6.0,
    )

    asyncio.run(stream.connect("wss://stream.data.alpaca.markets/v2/sip"))

    assert seen["url"] == "wss://stream.data.alpaca.markets/v2/sip"
    assert seen["proxy"] is None
    assert seen["open_timeout"] == 4.0
    assert seen["ping_interval"] == 5.0
    assert seen["ping_timeout"] == 5.0
    assert seen["close_timeout"] == 6.0
    assert seen["ssl"].check_hostname is True
    assert seen["ssl"].verify_mode == ssl.CERT_REQUIRED
    with pytest.raises(outbound.OutboundOriginDenied):
        asyncio.run(stream.connect("https://stream.data.alpaca.markets/v2/sip"))


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_requests_prepared_send_rejects_redirect_before_a_second_request(
    status, alpaca_policy, outbound
):
    """Calling Session.send directly cannot bypass the redirect boundary."""
    adapter = _ResponseAdapter(
        status=status,
        headers={"Location": "https://evil.test/provider-secret"},
    )
    session = outbound.NoRedirectSession(
        alpaca_policy,
        read_timeout=7.5,
        max_response_bytes=128,
    )
    session.mount("https://", adapter)
    request = session.prepare_request(
        requests.Request("GET", "https://paper-api.alpaca.markets/v2/account")
    )

    with pytest.raises(outbound.OutboundRedirectDenied) as raised:
        session.send(
            request,
            timeout=None,
            verify=False,
            proxies={"https": "http://caller-proxy.invalid"},
        )

    assert len(adapter.calls) == 1
    _, sent = adapter.calls[0]
    assert sent["timeout"] == (5.0, 7.5)
    assert sent["verify"] is True
    assert sent["proxies"] == {}
    assert str(raised.value) == "outbound redirect rejected"
    assert "provider-secret" not in str(raised.value)


def test_requests_prepared_send_applies_response_cap(alpaca_policy, outbound):
    """PreparedRequest sends cannot bypass bounded direct response reads."""
    secret = b"provider-secret-body"
    adapter = _ResponseAdapter(body=secret)
    session = outbound.NoRedirectSession(
        alpaca_policy,
        read_timeout=7.5,
        max_response_bytes=4,
    )
    session.mount("https://", adapter)
    request = session.prepare_request(
        requests.Request("GET", "https://paper-api.alpaca.markets/v2/account")
    )

    with pytest.raises(outbound.OutboundResponseTooLarge) as raised:
        session.send(request, stream=False)

    assert len(adapter.calls) == 1
    assert str(raised.value) == "outbound response too large"
    assert secret.decode() not in str(raised.value)


def test_requests_policy_ignores_proxy_and_ca_environment_markers(
    monkeypatch, alpaca_policy, outbound
):
    """Environment proxy and CA settings cannot select this session's transport."""
    marker = "/tmp/env-ca-marker.pem"
    monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy.invalid")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", marker)
    monkeypatch.setenv("CURL_CA_BUNDLE", marker)
    session = outbound.NoRedirectSession(alpaca_policy)

    settings = session.merge_environment_settings(
        "https://paper-api.alpaca.markets/v2/account",
        {},
        None,
        True,
        None,
    )
    adapter = session.get_adapter("https://paper-api.alpaca.markets")

    assert session.trust_env is False
    assert settings["proxies"] == {}
    assert settings["verify"] is True
    assert adapter._ssl_context.check_hostname is True
    assert adapter._ssl_context.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize("timeout", [None, float("nan"), float("inf"), float("-inf"), 0, -1])
def test_httpx_rejects_non_finite_or_non_positive_timeouts(outbound, timeout):
    """A disabled, NaN, or infinite timeout cannot be installed on a provider client."""
    with pytest.raises(ValueError):
        outbound.new_httpx_client(
            outbound.OutboundPolicy("https://api.example.test"),
            read_timeout=timeout,
        )


@pytest.mark.parametrize(
    "timeout_name",
    ["connect_timeout", "write_timeout", "pool_timeout"],
)
def test_httpx_rejects_non_finite_configured_transport_timeouts(
    outbound, timeout_name
):
    """Each named connect/write/pool timeout is independently fail-closed."""
    policy = outbound.OutboundPolicy("https://api.example.test")
    with pytest.raises(ValueError):
        outbound.new_httpx_client(policy, **{timeout_name: float("inf")})
    with pytest.raises(ValueError):
        outbound.new_async_httpx_client(policy, **{timeout_name: float("nan")})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("read_timeout", float("inf")),
        ("open_timeout", float("inf")),
        ("ping_timeout", float("nan")),
        ("close_timeout", 0),
    ],
)
def test_requests_and_websocket_reject_non_finite_timeouts(outbound, name, value):
    """No inbound caller can disable a Requests or WebSocket timeout with a float."""
    if name == "read_timeout":
        with pytest.raises(ValueError):
            outbound.NoRedirectSession(
                outbound.OutboundPolicy("https://api.example.test"),
                **{name: value},
            )
    else:
        with pytest.raises(ValueError):
            outbound.PinnedWebSocket(
                outbound.OutboundPolicy("wss://stream.example.test"),
                **{name: value},
            )


def test_httpx_direct_send_overwrites_prebuilt_timeout_and_redirect_flags(outbound):
    """A prebuilt HTTPX request cannot retain a caller-controlled timeout policy."""
    import httpx

    seen = []

    def transport(request):
        seen.append(dict(request.extensions["timeout"]))
        return httpx.Response(
            302,
            headers={"Location": "https://evil.test/provider-secret"},
            request=request,
        )

    client = outbound.new_httpx_client(
        outbound.OutboundPolicy("https://api.example.test"),
        read_timeout=7.5,
        transport=httpx.MockTransport(transport),
    )
    request = client.build_request(
        "GET", "https://api.example.test/v1/data", timeout=None
    )
    request.extensions["timeout"] = {
        "connect": float("inf"),
        "read": float("inf"),
        "write": float("inf"),
        "pool": float("inf"),
    }

    try:
        with pytest.raises(outbound.OutboundRedirectDenied):
            client.send(request, follow_redirects=True)
    finally:
        client.close()

    assert seen == [
        {"connect": 5.0, "read": 7.5, "write": 7.5, "pool": 5.0}
    ]


def test_httpx_clients_ignore_proxy_and_ca_environment_markers(monkeypatch, outbound):
    """Both HTTPX client types disable environment-derived proxy and trust roots."""
    marker = "/tmp/env-ca-marker.pem"
    monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy.invalid")
    monkeypatch.setenv("SSL_CERT_FILE", marker)
    policy = outbound.OutboundPolicy("https://api.example.test")
    sync_client = outbound.new_httpx_client(policy, read_timeout=7.5)
    async_client = outbound.new_async_httpx_client(policy, read_timeout=7.5)

    try:
        assert sync_client._trust_env is False
        assert async_client._trust_env is False
        assert sync_client._transport._pool._ssl_context.check_hostname is True
        assert sync_client._transport._pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
        assert async_client._transport._pool._ssl_context.check_hostname is True
        assert async_client._transport._pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
    finally:
        sync_client.close()
        asyncio.run(async_client.aclose())


def test_async_httpx_direct_send_overwrites_prebuilt_timeout(outbound):
    """An async prebuilt request cannot retain a disabled or infinite timeout."""
    import httpx

    seen = []

    def transport(request):
        seen.append(dict(request.extensions["timeout"]))
        return httpx.Response(
            302,
            headers={"Location": "https://evil.test/provider-secret"},
            request=request,
        )

    async def send_once():
        client = outbound.new_async_httpx_client(
            outbound.OutboundPolicy("https://api.example.test"),
            read_timeout=7.5,
            transport=httpx.MockTransport(transport),
        )
        request = client.build_request(
            "GET", "https://api.example.test/v1/data", timeout=None
        )
        request.extensions["timeout"] = {
            "connect": float("inf"),
            "read": float("inf"),
            "write": float("inf"),
            "pool": float("inf"),
        }
        try:
            with pytest.raises(outbound.OutboundRedirectDenied):
                await client.send(request, follow_redirects=True)
        finally:
            await client.aclose()

    asyncio.run(send_once())

    assert seen == [
        {"connect": 5.0, "read": 7.5, "write": 7.5, "pool": 5.0}
    ]


def test_async_httpx_cancellation_closes_acquired_streaming_response(outbound):
    """Cancelling during buffering must close the acquired response before re-raising."""
    import httpx

    buffering = asyncio.Event()
    release = asyncio.Event()
    stream_closed = asyncio.Event()
    close_calls = []
    responses = []

    class BlockingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            buffering.set()
            await release.wait()
            yield b"body"

        async def aclose(self):
            close_calls.append(1)
            stream_closed.set()

    def transport(request):
        response = httpx.Response(200, stream=BlockingStream(), request=request)
        responses.append(response)
        return response

    async def cancel_while_buffering():
        client = outbound.new_async_httpx_client(
            outbound.OutboundPolicy("https://api.example.test"),
            transport=httpx.MockTransport(transport),
        )
        task = asyncio.create_task(client.get("https://api.example.test/v1/data"))
        try:
            await buffering.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert responses[0].is_closed is True
            assert stream_closed.is_set()
            assert close_calls == [1]
        finally:
            release.set()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            await client.aclose()

    asyncio.run(cancel_while_buffering())


def test_async_httpx_second_cancellation_waits_for_shielded_cleanup(outbound):
    """A second cancellation cannot interrupt the one required response close."""
    import httpx

    buffering = asyncio.Event()
    close_started = asyncio.Event()
    finish_close = asyncio.Event()
    close_calls = []

    class BlockingCloseStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            buffering.set()
            await asyncio.Event().wait()
            yield b"unreachable"

        async def aclose(self):
            close_calls.append(1)
            close_started.set()
            await finish_close.wait()

    def transport(request):
        return httpx.Response(200, stream=BlockingCloseStream(), request=request)

    async def cancel_twice():
        client = outbound.new_async_httpx_client(
            outbound.OutboundPolicy("https://api.example.test"),
            transport=httpx.MockTransport(transport),
        )
        task = asyncio.create_task(client.get("https://api.example.test/v1/data"))
        try:
            await buffering.wait()
            task.cancel()
            for _ in range(3):
                await asyncio.sleep(0)
            assert close_started.is_set()
            task.cancel()
            for _ in range(3):
                await asyncio.sleep(0)
            assert not task.done()
            finish_close.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert close_calls == [1]
        finally:
            finish_close.set()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            await client.aclose()

    asyncio.run(cancel_twice())


def test_async_httpx_buffer_exception_closes_once_and_success_stays_buffered(outbound):
    """Ordinary buffer errors close once while successful buffered responses remain usable."""
    import httpx

    failed_close_calls = []
    success_close_calls = []

    class FailingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise RuntimeError("buffer failure")
            yield b"unreachable"

        async def aclose(self):
            failed_close_calls.append(1)

    class SuccessfulStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"ready"

        async def aclose(self):
            success_close_calls.append(1)

    calls = []

    def transport(request):
        stream = FailingStream() if not calls else SuccessfulStream()
        calls.append(1)
        return httpx.Response(200, stream=stream, request=request)

    async def exercise_paths():
        client = outbound.new_async_httpx_client(
            outbound.OutboundPolicy("https://api.example.test"),
            transport=httpx.MockTransport(transport),
        )
        try:
            with pytest.raises(RuntimeError, match="buffer failure"):
                await client.get("https://api.example.test/v1/error")
            response = await client.get("https://api.example.test/v1/success")
            assert response.content == b"ready"
            await response.aclose()
        finally:
            await client.aclose()

    asyncio.run(exercise_paths())

    assert failed_close_calls == [1]
    assert success_close_calls == [1]


@pytest.mark.parametrize(
    ("status", "location"),
    [
        (301, "wss://stream.data.alpaca.markets/redirect-secret"),
        (302, "wss://evil.test/redirect-secret"),
        (307, "wss://stream.data.alpaca.markets/redirect-secret"),
        (308, "wss://evil.test/redirect-secret"),
    ],
)
def test_installed_websocket_redirect_loop_never_follows_a_handshake_redirect(
    outbound, status, location
):
    """The installed websocket loop raises at its redirect hook after one handshake."""
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    attempts = []

    class Transport:
        def abort(self):
            pass

    class Connection:
        transport = Transport()

        async def handshake(self, _headers, _user_agent):
            attempts.append(1)
            raise InvalidStatus(
                Response(
                    status,
                    "redirect",
                    Headers([("Location", location)]),
                )
            )

    connector = outbound._NoRedirectWebSocketConnect(
        "wss://stream.data.alpaca.markets/v2/sip",
        proxy=None,
        ssl=ssl.create_default_context(),
        open_timeout=4.0,
        ping_interval=5.0,
        ping_timeout=5.0,
        close_timeout=6.0,
    )

    async def create_connection():
        return Connection()

    connector.create_connection = create_connection

    async def run_connector():
        return await connector

    with pytest.raises(outbound.OutboundRedirectDenied) as raised:
        asyncio.run(run_connector())

    assert attempts == [1]
    assert str(raised.value) == "outbound redirect rejected"
    assert "redirect-secret" not in str(raised.value)
