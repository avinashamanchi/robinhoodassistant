"""Deterministic edge coverage for candidate and outbound security boundaries."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
import ssl
from types import SimpleNamespace

from pydantic import ValidationError
import pytest
import requests

from trading_assistant.broker.models import Quote
from trading_assistant.security import candidates, outbound


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
BASE64URL_32_ZERO_BYTES = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def _order_payload(
    *,
    quote_as_of: datetime = NOW - timedelta(seconds=1),
) -> candidates.OrderCandidate:
    return candidates.OrderCandidate(
        ticker="AAPL",
        side="buy",
        notional="25",
        order_type="market",
        reference_price="100",
        quote_as_of=quote_as_of,
        thesis="Deterministic candidate fixture.",
    )


def _rule_payload(
    *,
    quote_as_of: datetime = NOW - timedelta(seconds=1),
) -> candidates.RuleCandidate:
    return candidates.RuleCandidate(
        ticker="AAPL",
        condition={
            "comparator": "price_below",
            "trigger_price": "95",
        },
        action={
            "side": "buy",
            "notional": "25",
            "order_type": "market",
        },
        reference_price="100",
        quote_as_of=quote_as_of,
        thesis="Deterministic standing-rule fixture.",
    )


def _signed_input(
    payload: candidates.CandidatePayload,
    *,
    kind: str,
    issued_at: datetime = NOW,
    expires_at: datetime = NOW + timedelta(minutes=1),
    actor: str = "operator",
) -> dict[str, object]:
    return {
        "version": 1,
        "kind": kind,
        "actor": actor,
        "session_binding": BASE64URL_32_ZERO_BYTES,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "nonce": BASE64URL_32_ZERO_BYTES,
        "payload": payload,
        "signature": BASE64URL_32_ZERO_BYTES,
    }


@pytest.mark.parametrize(
    "value",
    [
        True,
        Decimal("NaN"),
        Decimal("0"),
        1,
        "01",
        "1.0",
        "1e2",
        "0",
    ],
)
def test_candidate_decimal_rejects_ambiguous_or_nonpositive_encodings(value):
    """Relaxing canonical decimal checks would make signatures representation-dependent."""

    with pytest.raises(ValueError):
        candidates._canonical_decimal(value)


def test_candidate_decimal_normalizes_trusted_decimal_before_signing():
    """Decimal objects remain accepted only after conversion to canonical fixed point."""

    assert candidates._canonical_decimal(Decimal("1.2300")) == Decimal("1.23")


class _IndeterminateTimezone(tzinfo):
    def utcoffset(self, _value):
        return None

    def dst(self, _value):
        return None


@pytest.mark.parametrize(
    "value",
    [
        object(),
        datetime(2026, 7, 29, 12, 0),
        datetime(2026, 7, 29, 12, 0, tzinfo=_IndeterminateTimezone()),
        datetime(
            2026,
            7,
            29,
            12,
            0,
            tzinfo=timezone(timedelta(hours=1)),
        ),
    ],
)
def test_candidate_timestamps_reject_non_utc_or_indeterminate_time(value):
    """Candidate freshness must not depend on naive or non-UTC clock interpretation."""

    with pytest.raises(ValueError):
        candidates._aware_utc(value)


def test_candidate_timestamp_accepts_and_preserves_utc():
    assert candidates._aware_utc(NOW) == NOW


@pytest.mark.parametrize(
    "value",
    [
        b"not-text",
        "short",
        f"{BASE64URL_32_ZERO_BYTES}=",
        f"{BASE64URL_32_ZERO_BYTES[:-1]}B",
    ],
)
def test_candidate_base64url_rejects_wrong_type_padding_length_or_pad_bits(value):
    """Noncanonical nonce material must not admit multiple signed spellings."""

    with pytest.raises(ValueError):
        candidates._decode_unpadded(value)


def test_candidate_base64url_round_trip_and_exact_length_gate():
    assert candidates._decode_unpadded(BASE64URL_32_ZERO_BYTES) == bytes(32)
    with pytest.raises(ValueError):
        candidates._encode_unpadded(bytes(31))


@pytest.mark.parametrize(
    ("updates", "expected_message"),
    [
        ({}, "exactly one of quantity or notional is required"),
        (
            {"quantity": "1", "notional": "25"},
            "exactly one of quantity or notional is required",
        ),
        (
            {
                "notional": "25",
                "order_type": "market",
                "limit_price": "99",
            },
            "limit_price must be present only for limit orders",
        ),
        (
            {"notional": "25", "order_type": "limit"},
            "limit_price must be present only for limit orders",
        ),
    ],
)
def test_rule_action_rejects_ambiguous_size_and_limit_shapes(
    updates,
    expected_message,
):
    """A standing rule cannot carry two sizes or an order-type/price mismatch."""

    raw = {
        "side": "buy",
        "quantity": None,
        "notional": None,
        "order_type": "market",
        "limit_price": None,
    }
    raw.update(updates)

    with pytest.raises(ValidationError, match=expected_message):
        candidates.RuleActionCandidate.model_validate(raw)


def test_signed_candidate_rejects_noncanonical_actor():
    with pytest.raises(ValidationError):
        candidates.SignedCandidate.model_validate(
            _signed_input(
                _order_payload(),
                kind="order",
                actor=" operator",
            )
        )


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("order", _rule_payload()),
        ("rule", _order_payload()),
    ],
)
def test_signed_candidate_kind_must_match_payload_schema(kind, payload):
    """Changing only the envelope kind must not reinterpret the signed action."""

    with pytest.raises(ValidationError):
        candidates.SignedCandidate.model_validate(
            _signed_input(payload, kind=kind)
        )


@pytest.mark.parametrize(
    "expires_at",
    [
        NOW,
        NOW + timedelta(minutes=5, microseconds=1),
    ],
)
def test_signed_candidate_ttl_is_positive_and_bounded(expires_at):
    """Candidate replay authority cannot be zero-length or outlive five minutes."""

    with pytest.raises(ValidationError):
        candidates.SignedCandidate.model_validate(
            _signed_input(
                _order_payload(),
                kind="order",
                expires_at=expires_at,
            )
        )


def test_signed_candidate_quote_cannot_postdate_issuance():
    with pytest.raises(ValidationError):
        candidates.SignedCandidate.model_validate(
            _signed_input(
                _order_payload(quote_as_of=NOW + timedelta(microseconds=1)),
                kind="order",
            )
        )


@pytest.mark.parametrize(
    "value",
    [
        Decimal("NaN"),
        Decimal("0"),
        Decimal("1E+14"),
        object(),
    ],
)
def test_canonical_json_value_rejects_unsafe_numeric_or_unknown_types(value):
    """Signing input cannot silently stringify unsupported or oversized values."""

    with pytest.raises((TypeError, ValueError)):
        candidates._json_value(value)


def test_canonical_json_value_recurses_through_tuples():
    assert candidates._json_value((Decimal("1.5"), "fixed")) == [
        "1.5",
        "fixed",
    ]


def test_candidate_signer_rejects_non_32_byte_root_keys():
    with pytest.raises(ValueError):
        candidates.CandidateSigner(b"short", now=lambda: NOW)


def test_runtime_secret_buffer_is_zeroed_after_signer_derivation(monkeypatch):
    """The mutable decoded root key must be erased after subkeys are derived."""

    decoded = bytearray(b"k" * 32)

    def fake_validate(name, value):
        assert name == "candidate_signing_key"
        assert value == "fixture-only"
        return decoded

    monkeypatch.setattr(candidates, "validate_base64_key", fake_validate)

    signer = candidates.CandidateSigner.from_runtime_secrets(
        SimpleNamespace(candidate_signing_key="fixture-only"),
        now=lambda: NOW,
    )

    assert isinstance(signer, candidates.CandidateSigner)
    assert decoded == bytearray(32)


@pytest.mark.parametrize(
    ("actor", "session_id"),
    [
        (" ", 1),
        ("operator", 0),
        ("operator", 1.0),
    ],
)
def test_session_binding_rejects_empty_or_nonpositive_session_identity(
    actor,
    session_id,
):
    signer = candidates.CandidateSigner(b"k" * 32, now=lambda: NOW)

    with pytest.raises(ValueError):
        signer.session_binding(
            actor=actor,
            session_id=session_id,
            authenticated_at=NOW,
        )


@pytest.mark.parametrize(
    ("purpose", "value"),
    [
        ("", "value"),
        ("purpose", object()),
        ("purpose", ""),
    ],
)
def test_metadata_hash_rejects_missing_or_nontext_inputs(purpose, value):
    signer = candidates.CandidateSigner(b"k" * 32, now=lambda: NOW)

    with pytest.raises(ValueError):
        signer.metadata_hash(purpose, value)


@pytest.mark.parametrize(
    "updates",
    [
        {},
        {"quantity": "1", "notional": "25"},
        {"notional": "25", "limit_price": "99"},
        {"notional": "25", "order_type": "limit"},
    ],
)
def test_draft_order_input_fails_closed_on_size_and_limit_shape(updates):
    """Malformed LLM tool input is rejected before quote or broker access."""

    raw = {
        "ticker": "AAPL",
        "side": "buy",
        "quantity": None,
        "notional": None,
        "order_type": "market",
        "limit_price": None,
        "thesis": "Fixture input.",
    }
    raw.update(updates)

    with pytest.raises(ValidationError):
        candidates._DraftOrderInput.model_validate(raw)


class _QuoteBroker:
    def __init__(self, result):
        self.result = result

    def get_quote(self, _ticker):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _quote(
    *,
    ticker: str = "AAPL",
    at: datetime = NOW,
    bid: Decimal = Decimal("99"),
    ask: Decimal = Decimal("101"),
) -> Quote:
    return Quote(
        ticker=ticker,
        bid=bid,
        ask=ask,
        last=Decimal("100"),
        as_of=at,
        book_as_of=at,
        trade_as_of=at,
    )


def _draft_service(result, *, now: datetime = NOW):
    risk = SimpleNamespace(
        ticker_allowlist=["AAPL"],
        max_quote_age_seconds=30.0,
    )
    service = SimpleNamespace(
        broker=_QuoteBroker(result),
        config=SimpleNamespace(risk=risk, crypto_risk=None),
    )
    return (
        candidates.CandidateDraftService(
            service,
            candidates.CandidateSigner(b"k" * 32, now=lambda: now),
            now=lambda: now,
        ),
        risk,
    )


def test_draft_symbol_validation_rejects_malformed_ticker_before_quote_access():
    drafts, _risk = _draft_service(
        AssertionError("quote access must not occur"),
    )

    with pytest.raises(candidates.CandidateError) as raised:
        drafts._ticker_and_config("AA PL")

    assert (raised.value.code, raised.value.status_code) == (
        "candidate_symbol_invalid",
        422,
    )


@pytest.mark.parametrize(
    "result",
    [
        object(),
        _quote(ticker="MSFT"),
        _quote(bid=Decimal("102"), ask=Decimal("101")),
    ],
)
def test_draft_quote_rejects_wrong_type_symbol_or_invalid_market_values(result):
    drafts, risk = _draft_service(result)

    with pytest.raises(candidates.CandidateError) as raised:
        drafts._quote("AAPL", risk_config=risk)

    assert (raised.value.code, raised.value.status_code) == (
        "candidate_quote_invalid",
        503,
    )


def test_draft_quote_scrubs_provider_exception():
    marker = "fixture-provider-secret"
    drafts, risk = _draft_service(RuntimeError(marker))

    with pytest.raises(candidates.CandidateError) as raised:
        drafts._quote("AAPL", risk_config=risk)

    assert raised.value.code == "candidate_dependency_unavailable"
    assert marker not in str(raised.value)


def test_draft_quote_rejects_naive_component_timestamp():
    drafts, risk = _draft_service(
        _quote(at=datetime(2026, 7, 29, 12, 0)),
    )

    with pytest.raises(candidates.CandidateError) as raised:
        drafts._quote("AAPL", risk_config=risk)

    assert raised.value.code == "candidate_quote_invalid"


@pytest.mark.parametrize(
    "quote_at",
    [
        NOW + timedelta(microseconds=1),
        NOW - timedelta(seconds=31),
    ],
)
def test_draft_quote_rejects_future_or_over_budget_age(quote_at):
    drafts, risk = _draft_service(_quote(at=quote_at))

    with pytest.raises(candidates.CandidateError) as raised:
        drafts._quote("AAPL", risk_config=risk)

    assert (raised.value.code, raised.value.status_code) == (
        "candidate_quote_stale",
        503,
    )


@pytest.mark.parametrize(
    ("role", "adapter", "url"),
    [
        ("", "alpaca.trading", "https://paper-api.alpaca.markets"),
        ("app", "", "https://paper-api.alpaca.markets"),
        ("app", "alpaca.trading", None),
    ],
)
def test_manifest_origin_requires_nonempty_text_identity(role, adapter, url):
    with pytest.raises(outbound.OutboundOriginDenied):
        outbound.require_origin(role, adapter, url)


def test_invalid_provider_config_fails_closed_without_field_introspection():
    assert outbound.configured_origins_match_manifest(object()) is False
    with pytest.raises(outbound.OutboundOriginDenied):
        outbound.require_configured_role_origins(
            SimpleNamespace(provider_origins=object()),
            "app",
        )


def test_disabled_role_features_remove_websocket_and_crypto_origins():
    config = SimpleNamespace(
        daemon=SimpleNamespace(use_websocket=False),
        llm=SimpleNamespace(provider="none"),
        features=SimpleNamespace(telegram_notifications=False),
        crypto_risk=None,
    )

    enabled = outbound.origins_for_role(config, "daemon")

    assert "wss://stream.data.alpaca.markets" not in enabled
    assert "https://api.coingecko.com" not in enabled


class _LocalResponse:
    def __init__(
        self,
        *,
        body: bytes = b"{}",
        headers=None,
        close_error: Exception | None = None,
    ):
        self.url = outbound.LOCAL_LIVENESS_URL
        self.status_code = 200
        self.history = ()
        self.headers = headers if headers is not None else {}
        self.body = body
        self.close_error = close_error
        self.closed = 0

    def iter_bytes(self):
        yield self.body

    def close(self):
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


class _LocalClient:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def get(self, _url, **_kwargs):
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def test_local_liveness_rejects_unpinned_requested_url_before_client_call():
    client = _LocalClient(AssertionError("client must not be called"))

    with pytest.raises(outbound.OutboundRequestFailed):
        outbound.LocalLivenessTransport(client).fetch(
            "https://localhost:8020/health/other",
            timeout_seconds=1,
        )

    assert client.calls == 0


def test_local_liveness_declared_size_cap_survives_close_failure():
    """Cleanup failure cannot replace the response-budget boundary error."""

    response = _LocalResponse(
        headers={
            "Content-Length": str(
                outbound.LOCAL_LIVENESS_MAX_RESPONSE_BYTES + 1
            )
        },
        close_error=RuntimeError("fixture-close-secret"),
    )
    client = _LocalClient(response)

    with pytest.raises(outbound.OutboundResponseTooLarge):
        outbound.LocalLivenessTransport(client).fetch(
            outbound.LOCAL_LIVENESS_URL,
            timeout_seconds=1,
        )

    assert response.closed == 1


def test_local_liveness_scrubs_unexpected_client_failure():
    marker = "fixture-client-secret"
    client = _LocalClient(RuntimeError(marker))

    with pytest.raises(outbound.OutboundRequestFailed) as raised:
        outbound.LocalLivenessTransport(client).fetch(
            outbound.LOCAL_LIVENESS_URL,
            timeout_seconds=1,
        )

    assert marker not in str(raised.value)


def test_default_local_liveness_factory_stays_proxy_free(monkeypatch):
    import httpx

    seen = {}
    fake_client = object()
    fake_context = object()

    def client_factory(**kwargs):
        seen.update(kwargs)
        return fake_client

    monkeypatch.setattr(httpx, "Client", client_factory)

    transport = outbound.build_local_liveness_transport(
        ".local/tls/rootCA.pem",
        ssl_context_factory=lambda *, cafile: (
            seen.update(cafile=cafile) or fake_context
        ),
    )

    assert transport._client is fake_client
    assert seen == {
        "cafile": ".local/tls/rootCA.pem",
        "follow_redirects": False,
        "trust_env": False,
        "proxy": None,
        "verify": fake_context,
    }


@pytest.mark.parametrize(
    "value",
    [
        True,
        "1",
        float("nan"),
        float("inf"),
        0,
        -1,
    ],
)
def test_timeout_validation_rejects_bool_nonnumber_nonfinite_or_nonpositive(value):
    with pytest.raises(ValueError):
        outbound._positive_finite(value, name="fixture_timeout")


def test_timeout_validation_returns_plain_finite_float():
    assert outbound._positive_finite(1, name="timeout") == 1.0


def test_verified_requests_adapter_discards_caller_ca_and_client_cert():
    """Direct adapter use cannot reintroduce caller-selected TLS material."""

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    adapter = outbound._VerifiedHTTPAdapter(context)
    request = requests.Request("GET", "https://api.example.test/v1").prepare()
    try:
        _host, pool = adapter.build_connection_pool_key_attributes(
            request,
            verify=False,
            cert=("caller-cert.pem", "caller-key.pem"),
        )
    finally:
        adapter.close()

    assert pool["ssl_context"] is context
    assert pool["cert_reqs"] == "CERT_REQUIRED"
    assert {
        "ca_certs",
        "ca_cert_dir",
        "cert_file",
        "key_file",
    }.isdisjoint(pool)


@pytest.mark.parametrize(
    "url",
    [
        "https:///missing-host",
        "https://api.example.test:0",
    ],
)
def test_origin_parser_rejects_missing_host_and_zero_port(url):
    with pytest.raises(outbound.OutboundOriginDenied):
        outbound.OutboundOrigin.parse(
            url,
            allow_non_default_port=True,
        )


def test_request_policy_rejects_fragments_on_otherwise_pinned_origin():
    policy = outbound.OutboundPolicy("https://api.example.test")

    with pytest.raises(outbound.OutboundOriginDenied):
        policy.assert_url("https://api.example.test/v1#not-sent-to-server")


def test_policy_requires_origins_and_single_origin_property():
    with pytest.raises(outbound.OutboundOriginDenied):
        outbound.OutboundPolicy()

    parsed = outbound.OutboundOrigin("https", "api.example.test", 443)
    assert outbound.OutboundPolicy(parsed).origin == "https://api.example.test"

    multiple = outbound.OutboundPolicy(
        "https://api.example.test",
        "https://other.example.test",
    )
    with pytest.raises(outbound.OutboundOriginDenied):
        _ = multiple.origin


def test_response_policy_requires_request_url():
    response = SimpleNamespace(
        request=SimpleNamespace(url=None),
        status_code=200,
    )

    with pytest.raises(outbound.OutboundOriginDenied):
        outbound.OutboundPolicy(
            "https://api.example.test"
        ).assert_response(response)


@pytest.mark.parametrize("value", ["not-an-int", -1])
def test_content_length_rejects_malformed_or_negative_values(value):
    with pytest.raises(outbound.OutboundResponseTooLarge):
        outbound._content_length({"Content-Length": value})


def test_bounded_bytes_skips_empty_chunks_and_enforces_positive_cap():
    assert outbound._bounded_bytes([b"", b"ab", b""], 2) == b"ab"
    with pytest.raises(ValueError):
        outbound._bounded_bytes([b"unused"], 0)
    with pytest.raises(outbound.OutboundResponseTooLarge):
        outbound._bounded_bytes([b"ab", b"c"], 2)


@pytest.mark.parametrize(
    "params",
    [
        object(),
        ["not-a-pair"],
        [(1, "value")],
        [("API_KEY", "fixture-secret")],
    ],
)
def test_query_parameter_validation_rejects_unsupported_shapes_and_credentials(
    params,
):
    with pytest.raises(outbound.OutboundOriginDenied):
        outbound._validated_query_params(params)


def test_query_parameter_validation_preserves_safe_pair_sequence():
    params = [("page", 1), ("symbol", "AAPL")]
    validated = outbound._validated_query_params(params)

    assert validated == [("page", 1), ("symbol", "AAPL")]
    assert [key for key, _value in validated] == ["page", "symbol"]


class _BoundedResponse:
    def __init__(self, chunks, *, headers=None):
        self.headers = headers if headers is not None else {}
        self.chunks = chunks
        self._content = None

    def iter_bytes(self):
        yield from self.chunks


def test_bounded_json_parses_valid_body_with_empty_chunks():
    response = _BoundedResponse(
        [b"", b'{"safe":true}'],
        headers={"Content-Length": "13"},
    )

    assert outbound.read_bounded_json(
        response,
        max_response_bytes=13,
    ) == {"safe": True}


def test_bounded_json_rejects_declared_streamed_and_invalid_bodies():
    with pytest.raises(outbound.OutboundResponseTooLarge):
        outbound.read_bounded_json(
            _BoundedResponse(
                [b"{}"],
                headers={"Content-Length": "3"},
            ),
            max_response_bytes=2,
        )
    with pytest.raises(outbound.OutboundResponseTooLarge):
        outbound.read_bounded_json(
            _BoundedResponse([b"ab", b"c"]),
            max_response_bytes=2,
        )
    with pytest.raises(outbound.OutboundResponseInvalid):
        outbound.read_bounded_json(
            _BoundedResponse([b"\xff"]),
            max_response_bytes=2,
        )


def test_sync_response_binding_rejects_declared_oversize_before_buffering():
    response = _BoundedResponse(
        [b"must-not-be-read"],
        headers={"Content-Length": "4"},
    )

    with pytest.raises(outbound.OutboundResponseTooLarge):
        outbound._bound_httpx_response(response, 3)

    assert response._content is None


class _AsyncBoundedResponse:
    def __init__(self, chunks, *, headers=None):
        self.headers = headers if headers is not None else {}
        self.chunks = chunks
        self._content = None

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk


def test_async_response_binding_rejects_declared_and_streamed_oversize():
    async def exercise():
        with pytest.raises(outbound.OutboundResponseTooLarge):
            await outbound._bound_async_httpx_response(
                _AsyncBoundedResponse(
                    [b"unused"],
                    headers={"Content-Length": "4"},
                ),
                3,
            )
        with pytest.raises(outbound.OutboundResponseTooLarge):
            await outbound._bound_async_httpx_response(
                _AsyncBoundedResponse([b"", b"ab", b"c"]),
                2,
            )

    asyncio.run(exercise())


@pytest.mark.parametrize("factory", ["requests", "httpx", "async-httpx"])
def test_response_budget_must_be_positive_before_transport_construction(factory):
    policy = outbound.OutboundPolicy("https://api.example.test")

    with pytest.raises(ValueError):
        if factory == "requests":
            outbound.NoRedirectSession(policy, max_response_bytes=0)
        elif factory == "httpx":
            outbound.new_httpx_client(policy, max_response_bytes=0)
        else:
            outbound.new_async_httpx_client(
                policy,
                max_response_bytes=0,
            )


def test_nonredirect_websocket_exception_is_returned_to_connector_loop():
    marker = RuntimeError("fixture-handshake")

    assert (
        outbound._NoRedirectWebSocketConnect.process_redirect(
            object(),
            marker,
        )
        is marker
    )


@pytest.mark.parametrize(
    ("connector_error", "expected_error"),
    [
        (
            outbound.OutboundOriginDenied(),
            outbound.OutboundOriginDenied,
        ),
        (
            outbound.OutboundRedirectDenied(),
            outbound.OutboundRedirectDenied,
        ),
        (
            RuntimeError("fixture-connector-secret"),
            outbound.OutboundConnectionFailed,
        ),
    ],
)
def test_pinned_websocket_preserves_policy_errors_and_scrubs_connector_failures(
    monkeypatch,
    connector_error,
    expected_error,
):
    class FailingAttempt:
        def __init__(self, *_args, **_kwargs):
            pass

        def __await__(self):
            async def fail():
                raise connector_error

            return fail().__await__()

    monkeypatch.setattr(
        outbound,
        "_NoRedirectWebSocketConnect",
        FailingAttempt,
    )
    monkeypatch.setattr(
        outbound.ssl,
        "create_default_context",
        lambda: object(),
    )
    stream = outbound.PinnedWebSocket(
        outbound.OutboundPolicy("wss://stream.example.test"),
    )

    with pytest.raises(expected_error) as raised:
        asyncio.run(stream.connect("wss://stream.example.test/v1"))

    assert "fixture-connector-secret" not in str(raised.value)
