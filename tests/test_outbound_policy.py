"""Outbound-provider trust boundary tests; all transports are in-process fakes."""

from __future__ import annotations

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


def test_websocket_connector_pins_wss_origin_tls_and_finite_timeouts(outbound):
    """The optional stream cannot switch to HTTPS, an IP, or an unverified TLS context."""
    seen = {}

    def connector(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return type("Handshake", (), {"status_code": 101})()

    policy = outbound.OutboundPolicy("wss://stream.data.alpaca.markets")
    stream = outbound.PinnedWebSocket(
        policy,
        connector,
        open_timeout=4.0,
        ping_timeout=5.0,
        close_timeout=6.0,
    )

    stream.connect("wss://stream.data.alpaca.markets/v2/sip")

    assert seen["url"] == "wss://stream.data.alpaca.markets/v2/sip"
    assert seen["open_timeout"] == 4.0
    assert seen["ping_timeout"] == 5.0
    assert seen["close_timeout"] == 6.0
    assert seen["ssl_context"].check_hostname is True
    assert seen["ssl_context"].verify_mode == ssl.CERT_REQUIRED
    with pytest.raises(outbound.OutboundOriginDenied):
        stream.connect("https://stream.data.alpaca.markets/v2/sip")


def test_websocket_connector_rejects_redirect_handshake_without_leaking_location(outbound):
    """A WSS redirect is rejected rather than delegating a second connection to a library."""
    calls = []

    def connector(_url, **_kwargs):
        calls.append(1)
        return type(
            "Handshake",
            (), {"status_code": 307, "location": "wss://evil.test/provider-secret"},
        )()

    stream = outbound.PinnedWebSocket(
        outbound.OutboundPolicy("wss://stream.data.alpaca.markets"),
        connector,
    )

    with pytest.raises(outbound.OutboundRedirectDenied) as raised:
        stream.connect("wss://stream.data.alpaca.markets/v2/sip")

    assert calls == [1]
    assert str(raised.value) == "outbound redirect rejected"
    assert "provider-secret" not in str(raised.value)
