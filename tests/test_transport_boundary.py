"""The loopback HTTPS request perimeter rejects unsafe traffic before routes."""

from __future__ import annotations

import asyncio
import ipaddress
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID
from cryptography.x509.verification import (
    DNSName,
    PolicyBuilder,
    Store,
    VerificationError,
)
from fastapi.testclient import TestClient

from trading_assistant.app.main import create_test_app as create_app
from trading_assistant.db.models import (
    AuditEvent,
    AuthSession,
    ConcurrencyLease,
    MutationInterlock,
    RateWindow,
)
from trading_assistant.security.transport import (
    TransportBoundaryMiddleware,
    TransportPolicy,
)


class _StubAgent:
    def chat(self, message, **context):
        return {"reply": message, "context": context}


class _CountingAgent:
    def __init__(self) -> None:
        self.calls = 0

    def chat(self, message, **context):
        self.calls += 1
        return {"reply": message, "context": context}


def _app(make_service, *, policy=None):
    return create_app(
        service=make_service(),
        agent=_StubAgent(),
        api_token="transport-boundary-test-secret",
        planning=None,
        transport_policy=policy,
    )


class _CountingSessionFactory:
    """Count session access while retaining the real test database factory."""

    def __init__(self, factory) -> None:
        self._factory = factory
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self._factory()


def _patch_fake_launcher_composition(monkeypatch, serve, app) -> None:
    """Keep launcher tests on one injected secrets/receipt/container chain."""

    from trading_assistant.operations import security_posture as posture

    secrets = object()
    provider = SimpleNamespace(
        last_successful_role_load_at=datetime.now(timezone.utc)
    )
    container = SimpleNamespace(secrets=secrets)
    monkeypatch.setattr(
        serve,
        "MacOSKeychainSecretProvider",
        lambda: provider,
    )
    monkeypatch.setattr(
        serve,
        "load_role_secrets",
        lambda *_args, **_kwargs: secrets,
    )

    def run_guard(*, config, secrets: object, secret_loaded_at, **_kwargs):
        return posture._issue_startup_guard_receipt(
            config=config,
            secrets=secrets,
            checks=(
                SimpleNamespace(
                    name="runtime_configuration",
                    passed=True,
                    code="ok",
                ),
                SimpleNamespace(
                    name="loopback_https",
                    passed=True,
                    code="ok",
                ),
                SimpleNamespace(name="tls", passed=True, code="ok"),
                SimpleNamespace(
                    name="database",
                    passed=True,
                    code="ok",
                ),
                SimpleNamespace(
                    name="encryption",
                    passed=True,
                    code="ok",
                ),
            ),
            observed_at=secret_loaded_at,
            secret_loaded_at=secret_loaded_at,
            runtime_role="app",
        )

    monkeypatch.setattr(serve, "run_startup_guard", run_guard)

    def build_container(
        config,
        loaded,
        *,
        runtime_role,
        startup_guard_receipt,
    ):
        assert loaded is secrets
        assert runtime_role == "app"
        context = posture._consume_startup_guard_receipt(
            startup_guard_receipt,
            config=config,
            secrets=loaded,
            runtime_role=runtime_role,
        )
        container.startup_evidence = (
            posture._validate_consumed_startup_guard(
                context,
                config=config,
                secrets=loaded,
                runtime_role=runtime_role,
            )
        )
        return container

    monkeypatch.setattr(
        serve,
        "_build_guarded_container",
        build_container,
    )

    def create_app(*, container: object):
        assert container is not None
        assert container.startup_evidence.secret_load_status == "pass"
        return app

    monkeypatch.setattr(serve, "_create_guarded_app", create_app)


def _durable_perimeter_state(
    service,
    *,
    session_factory=None,
) -> tuple[int, int, int, int, int]:
    with (session_factory or service.session_factory)() as session:
        return (
            session.query(AuthSession).count(),
            session.query(RateWindow).count(),
            session.query(AuditEvent).count(),
            session.query(ConcurrencyLease).count(),
            session.query(MutationInterlock).count(),
        )


class _FailFastInnerLayer:
    """A denied perimeter request must never reach this fake dependency."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    def _touched(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError(f"{self.name} must not run for transport denial")

    def __getattr__(self, _name):
        return self._touched


@pytest.mark.parametrize(
    ("base_url", "headers", "body_factory", "expected_status"),
    [
        (
            "https://localhost:8020",
            {"Host": "evil.example"},
            lambda: b'{"message":"denied"}',
            400,
        ),
        (
            "https://localhost:8020",
            {"Origin": "https://evil.example"},
            lambda: b'{"message":"denied"}',
            403,
        ),
        (
            "https://localhost:8020",
            {"Forwarded": "for=198.51.100.7;proto=https"},
            lambda: b'{"message":"denied"}',
            400,
        ),
        (
            "https://localhost:8020",
            {"X-Forwarded-For": "198.51.100.7"},
            lambda: b'{"message":"denied"}',
            400,
        ),
        (
            "http://localhost:8020",
            {},
            lambda: b'{"message":"denied"}',
            426,
        ),
        (
            "https://localhost:8020",
            {"X-Too-Large": "x" * 65_536},
            lambda: b'{"message":"denied"}',
            431,
        ),
        (
            "https://localhost:8020",
            {f"X-Count-{index}": "x" for index in range(65)},
            lambda: b'{"message":"denied"}',
            431,
        ),
        (
            "https://localhost:8020",
            {"Content-Length": "65536"},
            lambda: b'{"message":"denied"}',
            413,
        ),
        (
            "https://localhost:8020",
            {},
            lambda: iter((b'{"message":"', b"x" * 65_536, b'"}')),
            413,
        ),
        (
            "https://localhost:8020",
            {"Content-Type": "text/plain"},
            lambda: b'{"message":"denied"}',
            415,
        ),
    ],
    ids=(
        "host",
        "origin",
        "forwarded",
        "x_forwarded",
        "http",
        "header_size",
        "header_count",
        "content_length",
        "streamed_body",
        "content_type",
    ),
)
def test_transport_denials_have_zero_inner_layer_side_effects(
    make_service,
    monkeypatch,
    base_url,
    headers,
    body_factory,
    expected_status,
):
    """Every `/chat` denial must precede auth, durable limits, domain, and loaders."""
    service = make_service()
    domain = _FailFastInnerLayer("domain_handler")
    app = create_app(
        service=service,
        agent=domain,
        api_token="transport-boundary-test-secret",
        planning=None,
    )
    with TestClient(app, base_url="https://localhost:8020") as setup_client:
        login = setup_client.post(
            "/auth/login",
            json={"secret": "transport-boundary-test-secret"},
        )
        assert login.status_code == 200
        token = setup_client.cookies.get("__Host-trading_session")
        csrf = login.json()["csrf_token"]

    session_auth = _FailFastInnerLayer("session_auth")
    rate_limiter = _FailFastInnerLayer("persistent_rate_limiter")
    broker = _FailFastInnerLayer("broker_client")
    secret_loader = _FailFastInnerLayer("secret_provider_loader")
    app.state.session_auth = session_auth
    app.state.rate_limiter = rate_limiter
    service.broker = broker
    monkeypatch.setattr(
        "trading_assistant.security.secrets.load_role_secrets",
        secret_loader._touched,
    )
    real_session_factory = service.session_factory
    before = _durable_perimeter_state(
        service,
        session_factory=real_session_factory,
    )
    database_access = _CountingSessionFactory(real_session_factory)
    service.session_factory = database_access
    request_headers = {
        "Cookie": f"__Host-trading_session={token}",
        "X-CSRF-Token": csrf,
        "Content-Type": "application/json",
        **headers,
    }

    with TestClient(
        app,
        base_url=base_url,
        raise_server_exceptions=False,
    ) as client:
        response = client.post(
            "/chat",
            content=body_factory(),
            headers=request_headers,
        )

    assert response.status_code == expected_status
    assert session_auth.calls == 0
    assert rate_limiter.calls == 0
    assert domain.calls == 0
    assert broker.calls == 0
    assert secret_loader.calls == 0
    assert database_access.calls == 0
    assert (
        _durable_perimeter_state(
            service,
            session_factory=real_session_factory,
        )
        == before
    )


@pytest.mark.parametrize(
    ("base_url", "headers", "content"),
    [
        (
            "https://localhost:8020",
            {"Host": "evil.example"},
            b'{"message":"denied"}',
        ),
        (
            "https://localhost:8020",
            {"Origin": "https://evil.example"},
            b'{"message":"denied"}',
        ),
        (
            "https://localhost:8020",
            {"Forwarded": "for=198.51.100.7;proto=https"},
            b'{"message":"denied"}',
        ),
        (
            "http://localhost:8020",
            {},
            b'{"message":"denied"}',
        ),
        (
            "https://localhost:8020",
            {"X-Too-Large": "x" * 65_536},
            b'{"message":"denied"}',
        ),
        (
            "https://localhost:8020",
            {},
            b'{"message":"' + b"x" * 65_536 + b'"}',
        ),
        (
            "https://localhost:8020",
            {"Content-Type": "text/plain"},
            b'{"message":"denied"}',
        ),
    ],
    ids=(
        "host",
        "origin",
        "forwarded",
        "http",
        "headers",
        "body",
        "content_type",
    ),
)
def test_every_transport_denial_precedes_authenticated_chat_state(
    make_service,
    base_url,
    headers,
    content,
):
    """Denied `/chat` traffic must not read sessions or mutate durable limits."""
    service = make_service()
    agent = _CountingAgent()
    app = create_app(
        service=service,
        agent=agent,
        api_token="transport-boundary-test-secret",
        planning=None,
    )
    with TestClient(app, base_url="https://localhost:8020") as setup_client:
        login = setup_client.post(
            "/auth/login",
            json={"secret": "transport-boundary-test-secret"},
        )
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        token = setup_client.cookies.get("__Host-trading_session")

    session_factory = _CountingSessionFactory(
        app.state.session_auth.session_factory
    )
    app.state.session_auth.session_factory = session_factory
    before = _durable_perimeter_state(service)
    request_headers = {
        "Cookie": f"__Host-trading_session={token}",
        "X-CSRF-Token": csrf,
        "Content-Type": "application/json",
        **headers,
    }
    with TestClient(
        app,
        base_url=base_url,
        raise_server_exceptions=False,
    ) as client:
        response = client.post("/chat", content=content, headers=request_headers)

    assert response.status_code in {400, 403, 413, 415, 426, 431}
    assert session_factory.calls == 0
    assert _durable_perimeter_state(service) == before
    assert agent.calls == 0
    assert service.broker.submit_calls == 0


def test_untrusted_host_is_rejected_before_anonymous_liveness(make_service):
    """Removing host validation would expose the anonymous liveness route."""
    with TestClient(_app(make_service), base_url="https://localhost:8020") as client:
        response = client.get("/health/live", headers={"Host": "evil.example"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "untrusted_host"


def test_forwarded_headers_are_rejected_before_anonymous_liveness(make_service):
    """Trusting a proxy header would let a caller rewrite request authority."""
    with TestClient(_app(make_service), base_url="https://localhost:8020") as client:
        response = client.get(
            "/health/live",
            headers={"Forwarded": "for=198.51.100.7;proto=https"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "proxy_headers_forbidden"


@pytest.mark.parametrize(
    "host",
    ["localhost:8020", "127.0.0.1:8020", "[::1]:8020"],
)
def test_exact_configured_loopback_hosts_are_accepted(make_service, host):
    """Rejecting an allowed loopback form would break local operator access."""
    with TestClient(_app(make_service), base_url="https://localhost:8020") as client:
        response = client.get("/health/live", headers={"Host": host})

    assert response.status_code == 200


@pytest.mark.parametrize(
    "host",
    ["localhost", "localhost:bad", "[::1:8020", "[::1]8020", "::1:8020"],
)
def test_malformed_or_portless_host_is_rejected(make_service, host):
    """Relaxing RFC-aware host parsing could widen the local perimeter."""
    with TestClient(_app(make_service), base_url="https://localhost:8020") as client:
        response = client.get("/health/live", headers={"Host": host})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "untrusted_host"


def test_cross_origin_request_is_rejected_before_anonymous_liveness(make_service):
    """Accepting a foreign Origin would reintroduce a CORS-like trust path."""
    with TestClient(_app(make_service), base_url="https://localhost:8020") as client:
        response = client.get(
            "/health/live",
            headers={"Origin": "https://evil.example"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_mismatch"
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    "header",
    ["Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto"],
)
def test_each_forwarded_header_is_rejected_before_route_code(make_service, header):
    """Trusting any forwarding convention would permit authority spoofing."""
    with TestClient(_app(make_service), base_url="https://localhost:8020") as client:
        response = client.get(
            "/health/live",
            headers={header: "https://evil.example"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "proxy_headers_forbidden"


def test_http_state_changing_request_is_rejected_before_login(make_service):
    """Removing HTTPS enforcement would let credentials cross plaintext HTTP."""
    with TestClient(_app(make_service), base_url="http://localhost:8020") as client:
        response = client.post(
            "/auth/login",
            json={"secret": "transport-boundary-test-secret"},
        )

    assert response.status_code == 426
    assert response.json()["error"]["code"] == "https_required"


def test_production_http_liveness_is_rejected_without_liveness_data(make_service):
    """Production plaintext must not expose even anonymous application state."""
    with TestClient(_app(make_service), base_url="http://localhost:8020") as client:
        response = client.get("/health/live")

    assert response.status_code == 426
    assert response.json()["error"]["code"] == "https_required"
    assert "alive" not in response.text
    assert "database_reachable" not in response.text


def test_test_transport_keeps_liveness_available_with_explicit_degradation(
    make_service,
):
    """Tests need anonymous liveness without silently simulating production TLS."""
    with TestClient(
        _app(make_service, policy=TransportPolicy.test()),
        base_url="http://testserver",
    ) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {
        "alive": True,
        "database_reachable": True,
    }
    assert response.headers["X-Transport-Degraded"] == "test_transport"


def test_login_issues_exactly_one_host_only_secure_cookie(make_service):
    """A weaker cookie scope would expose a browser-readable session token."""
    with TestClient(_app(make_service), base_url="https://localhost:8020") as client:
        response = client.post(
            "/auth/login",
            json={"secret": "transport-boundary-test-secret"},
        )

    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 1
    cookie = cookies[0]
    assert cookie.startswith("__Host-trading_session=")
    assert "; HttpOnly" in cookie
    assert "; Path=/" in cookie
    assert "; SameSite=strict" in cookie
    assert "; Secure" in cookie
    assert "Domain=" not in cookie


def test_logout_expires_exactly_one_host_only_secure_cookie(make_service):
    """Logout must not create a second or weaker browser-readable cookie."""
    with TestClient(_app(make_service), base_url="https://localhost:8020") as client:
        login = client.post(
            "/auth/login",
            json={"secret": "transport-boundary-test-secret"},
        )
        assert login.status_code == 200
        response = client.post(
            "/auth/logout",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
        )

    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 1
    cookie = cookies[0]
    assert cookie.startswith("__Host-trading_session=")
    assert "; HttpOnly" in cookie
    assert "; Path=/" in cookie
    assert "; SameSite=strict" in cookie
    assert "; Secure" in cookie
    assert "Domain=" not in cookie


def test_production_rejects_insecure_cookie_configuration_at_app_construction(
    make_service,
):
    """A bypassed Pydantic literal must not weaken production cookies."""
    service = make_service()
    service.config = service.config.model_copy(
        update={
            "server": service.config.server.model_copy(
                update={"secure_cookies": False}
            )
        }
    )

    with pytest.raises(RuntimeError, match="secure_cookies"):
        create_app(
            service=service,
            agent=_StubAgent(),
            api_token="transport-boundary-test-secret",
            planning=None,
        )


def test_test_transport_cannot_downgrade_session_cookie_security(
    make_service,
):
    """Even an in-process transport cannot issue an insecure session cookie."""
    service = make_service()
    service.config = service.config.model_copy(
        update={
            "server": service.config.server.model_copy(
                update={"secure_cookies": False}
            )
        }
    )
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token="transport-boundary-test-secret",
        planning=None,
        transport_policy=TransportPolicy.test(),
    )

    with TestClient(app, base_url="http://testserver") as client:
        response = client.post(
            "/auth/login",
            json={"secret": "transport-boundary-test-secret"},
        )

    assert response.status_code == 200
    assert response.headers["set-cookie"].startswith("trading_session=")
    assert "; Secure" in response.headers["set-cookie"]
    assert "; HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]


def test_large_body_is_rejected_before_json_route_code(make_service):
    """Removing the body cap would permit route handlers to buffer unbounded data."""
    with TestClient(_app(make_service), base_url="https://localhost:8020") as client:
        response = client.post(
            "/auth/login",
            json={"secret": "x" * 131_072},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "body_too_large"


def test_large_headers_are_rejected_before_anonymous_liveness(make_service):
    """Removing the header cap would permit unbounded request metadata allocation."""
    with TestClient(_app(make_service), base_url="https://localhost:8020") as client:
        response = client.get(
            "/health/live",
            headers={"X-Long": "x" * 65_536},
        )

    assert response.status_code == 431
    assert response.json()["error"]["code"] == "headers_too_large"


def test_json_route_rejects_another_content_type(make_service):
    """Removing JSON media validation would widen parsing behavior on mutation routes."""
    with TestClient(_app(make_service), base_url="https://localhost:8020") as client:
        response = client.post(
            "/auth/login",
            content='{"secret":"transport-boundary-test-secret"}',
            headers={"Content-Type": "text/plain"},
        )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"


def test_denials_do_not_execute_liveness_or_domain_code(make_service):
    """Moving denials behind routes would permit denied traffic to touch domain state."""
    app = _app(make_service)
    calls: list[str] = []

    class _ExplodingOperations:
        def liveness(self):
            calls.append("liveness")
            raise AssertionError("route code must not run for a denied request")

    app.state.operations = _ExplodingOperations()
    with TestClient(app, base_url="https://localhost:8020") as client:
        response = client.get("/health/live", headers={"Host": "evil.example"})

    assert response.status_code == 400
    assert calls == []


def test_streamed_body_without_content_length_is_bounded_before_app_code():
    """Relying only on Content-Length would permit chunked bodies to bypass the cap."""
    called = False
    sent: list[dict] = []

    async def app(scope, receive, send):
        nonlocal called
        called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    messages = iter(
        [
            {"type": "http.request", "body": b"x" * 65_536, "more_body": True},
            {"type": "http.request", "body": b"x" * 65_536, "more_body": False},
        ]
    )

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/login",
        "scheme": "https",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
        ],
    }
    asyncio.run(
        TransportBoundaryMiddleware(app, policy=TransportPolicy.test())(
            scope,
            receive,
            send,
        )
    )

    assert called is False
    assert sent[0]["status"] == 413


def _write_tls_pair(
    tmp_path: Path,
    *,
    not_valid_before: datetime | None = None,
    not_valid_after: datetime | None = None,
    dns_names: tuple[str, ...] = ("localhost",),
    ip_names: tuple[str, ...] = ("127.0.0.1", "::1"),
    key_matches_certificate: bool = True,
    ca_key_cert_sign: bool = True,
    leaf_server_auth: bool | None = True,
) -> SimpleNamespace:
    tls_directory = tmp_path / ".local" / "tls"
    tls_directory.mkdir(parents=True)
    root_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    root_name = x509.Name(
        [x509.NameAttribute(x509.NameOID.COMMON_NAME, "fixture local CA")]
    )
    now = datetime.now(timezone.utc)
    root_certificate = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=ca_key_cert_sign,
                crl_sign=ca_key_cert_sign,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(
                root_key.public_key()
            ),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )
    certificate_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    private_key = (
        certificate_key
        if key_matches_certificate
        else rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    certificate_builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(root_name)
        .public_key(certificate_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_valid_before or now - timedelta(minutes=1))
        .not_valid_after(not_valid_after or now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [*(x509.DNSName(name) for name in dns_names)]
                + [
                    x509.IPAddress(ipaddress.ip_address(address))
                    for address in ip_names
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
    )
    if leaf_server_auth is not None:
        certificate_builder = certificate_builder.add_extension(
            x509.ExtendedKeyUsage(
                [
                    ExtendedKeyUsageOID.SERVER_AUTH
                    if leaf_server_auth
                    else ExtendedKeyUsageOID.CLIENT_AUTH
                ]
            ),
            critical=False,
        )
    certificate = (
        certificate_builder
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(
                certificate_key.public_key()
            ),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                root_key.public_key()
            ),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )
    ca_path = tls_directory / "rootCA.pem"
    certificate_path = tls_directory / "localhost.pem"
    key_path = tls_directory / "localhost-key.pem"
    ca_path.write_bytes(
        root_certificate.public_bytes(serialization.Encoding.PEM)
    )
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(tls_directory, 0o700)
    os.chmod(ca_path, 0o644)
    os.chmod(certificate_path, 0o644)
    os.chmod(key_path, 0o600)
    return SimpleNamespace(
        tls_ca_path=Path(".local/tls/rootCA.pem"),
        tls_cert_path=Path(".local/tls/localhost.pem"),
        tls_key_path=Path(".local/tls/localhost-key.pem"),
    )


def test_tls_inspection_requires_local_sans_permissions_and_matching_key(
    tmp_path,
    monkeypatch,
):
    """Weak key permissions would leak the TLS private key to another user."""
    from trading_assistant.ops.tls import TLSMaterialError, validate_tls_material

    server = _write_tls_pair(tmp_path)
    monkeypatch.chdir(tmp_path)

    status = validate_tls_material(server)

    assert status.sans == ("localhost", "127.0.0.1", "::1")
    assert status.ca_certificate_path.name == "rootCA.pem"
    os.chmod(tmp_path / ".local/tls/localhost-key.pem", 0o644)
    with pytest.raises(TLSMaterialError, match="tls_private_key_permissions_invalid"):
        validate_tls_material(server)


@pytest.mark.parametrize(
    ("target", "mode", "code"),
    [
        ("rootCA.pem", 0o600, "tls_ca_permissions_invalid"),
        ("localhost.pem", 0o600, "tls_certificate_permissions_invalid"),
        ("localhost-key.pem", 0o644, "tls_private_key_permissions_invalid"),
    ],
)
def test_tls_inspection_rejects_unsafe_certificate_and_key_modes(
    tmp_path,
    monkeypatch,
    target,
    mode,
    code,
):
    """TLS file permissions must remain independently fail-closed."""
    from trading_assistant.ops.tls import TLSMaterialError, validate_tls_material

    server = _write_tls_pair(tmp_path)
    monkeypatch.chdir(tmp_path)
    os.chmod(tmp_path / ".local/tls" / target, mode)

    with pytest.raises(TLSMaterialError) as exc_info:
        validate_tls_material(server)

    assert exc_info.value.code == code
    assert "PRIVATE KEY" not in str(exc_info.value)


def test_tls_inspection_requires_a_valid_ca_that_signed_the_local_leaf(
    tmp_path,
    monkeypatch,
):
    from trading_assistant.ops.tls import TLSMaterialError, validate_tls_material

    server = _write_tls_pair(tmp_path)
    monkeypatch.chdir(tmp_path)
    ca_path = tmp_path / ".local/tls/rootCA.pem"
    leaf_path = tmp_path / ".local/tls/localhost.pem"

    ca_path.write_bytes(leaf_path.read_bytes())
    with pytest.raises(TLSMaterialError) as exc_info:
        validate_tls_material(server)
    assert exc_info.value.code == "tls_ca_invalid"

    other = tmp_path / "other"
    _write_tls_pair(other)
    ca_path.write_bytes((other / ".local/tls/rootCA.pem").read_bytes())
    with pytest.raises(TLSMaterialError) as exc_info:
        validate_tls_material(server)
    assert exc_info.value.code == "tls_ca_chain_invalid"


def test_tls_inspection_rejects_paths_outside_and_symlink_escapes(
    tmp_path,
    monkeypatch,
):
    """Configured TLS paths cannot escape the owner-only local TLS directory."""
    from trading_assistant.ops.tls import TLSMaterialError, validate_tls_material

    server = _write_tls_pair(tmp_path)
    monkeypatch.chdir(tmp_path)
    outside = tmp_path / "outside.pem"
    outside.write_text("not tls", encoding="utf-8")
    os.chmod(outside, 0o644)
    escaped = SimpleNamespace(
        tls_ca_path=server.tls_ca_path,
        tls_cert_path=outside,
        tls_key_path=server.tls_key_path,
    )

    with pytest.raises(TLSMaterialError) as exc_info:
        validate_tls_material(escaped)
    assert exc_info.value.code == "tls_path_outside_local_directory"

    symlink = tmp_path / ".local/tls/escaped.pem"
    symlink.symlink_to(outside)
    escaped_symlink = SimpleNamespace(
        tls_ca_path=server.tls_ca_path,
        tls_cert_path=Path(".local/tls/escaped.pem"),
        tls_key_path=server.tls_key_path,
    )
    with pytest.raises(TLSMaterialError) as exc_info:
        validate_tls_material(escaped_symlink)
    assert exc_info.value.code == "tls_path_outside_local_directory"


@pytest.mark.parametrize("symlinked_component", (".local", ".local/tls"))
def test_tls_inspection_rejects_symlinked_root_components(
    tmp_path,
    monkeypatch,
    symlinked_component,
):
    """The accepted TLS root is anchored below the canonical repository, not resolved input."""
    from trading_assistant.ops.tls import TLSMaterialError, validate_tls_material

    repository = tmp_path / "repository"
    repository.mkdir()
    outside = tmp_path / "outside"
    server = _write_tls_pair(outside)
    if symlinked_component == ".local":
        (repository / ".local").symlink_to(outside / ".local")
    else:
        (repository / ".local").mkdir()
        (repository / ".local/tls").symlink_to(outside / ".local/tls")
    monkeypatch.chdir(repository)

    with pytest.raises(TLSMaterialError) as exc_info:
        validate_tls_material(server)

    assert exc_info.value.code == "tls_root_symlink_forbidden"


@pytest.mark.parametrize(
    ("not_valid_before", "not_valid_after"),
    [
        (
            datetime.now(timezone.utc) - timedelta(days=2),
            datetime.now(timezone.utc) - timedelta(days=1),
        ),
        (
            datetime.now(timezone.utc) + timedelta(days=1),
            datetime.now(timezone.utc) + timedelta(days=2),
        ),
    ],
    ids=("expired", "not_yet_valid"),
)
def test_tls_inspection_rejects_certificates_outside_the_validity_window(
    tmp_path,
    monkeypatch,
    not_valid_before,
    not_valid_after,
):
    """A well-formed but stale or premature certificate cannot start TLS."""
    from trading_assistant.ops.tls import TLSMaterialError, validate_tls_material

    server = _write_tls_pair(
        tmp_path,
        not_valid_before=not_valid_before,
        not_valid_after=not_valid_after,
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(TLSMaterialError) as exc_info:
        validate_tls_material(server)

    assert exc_info.value.code == "tls_certificate_not_current"


def test_tls_inspection_rejects_missing_loopback_sans_and_key_mismatch(
    tmp_path,
    monkeypatch,
):
    """The certificate must prove all three local names and the matching key."""
    from trading_assistant.ops.tls import TLSMaterialError, validate_tls_material

    missing_san = _write_tls_pair(tmp_path, ip_names=("127.0.0.1",))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TLSMaterialError) as exc_info:
        validate_tls_material(missing_san)
    assert exc_info.value.code == "tls_certificate_san_invalid"

    mismatch_root = tmp_path / "mismatch"
    mismatch = _write_tls_pair(
        mismatch_root,
        key_matches_certificate=False,
    )
    monkeypatch.chdir(mismatch_root)
    with pytest.raises(TLSMaterialError) as exc_info:
        validate_tls_material(mismatch)
    assert exc_info.value.code == "tls_certificate_key_mismatch"
    assert "PRIVATE KEY" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("fixture_options", "expected_code"),
    [
        ({"ca_key_cert_sign": False}, "tls_ca_invalid"),
        ({"leaf_server_auth": False}, "tls_ca_chain_invalid"),
    ],
    ids=("ca-without-key-cert-sign", "leaf-without-server-auth"),
)
def test_tls_inspection_matches_standards_verifier_constraints(
    tmp_path,
    monkeypatch,
    fixture_options,
    expected_code,
):
    """The local validator must reject every chain the standards verifier rejects."""
    from trading_assistant.ops.tls import TLSMaterialError, validate_tls_material

    server = _write_tls_pair(tmp_path, **fixture_options)
    ca = x509.load_pem_x509_certificate(
        (tmp_path / ".local/tls/rootCA.pem").read_bytes()
    )
    leaf = x509.load_pem_x509_certificate(
        (tmp_path / ".local/tls/localhost.pem").read_bytes()
    )
    verifier = (
        PolicyBuilder()
        .store(Store([ca]))
        .build_server_verifier(DNSName("localhost"))
    )
    with pytest.raises(VerificationError):
        verifier.verify(leaf, [])

    monkeypatch.chdir(tmp_path)
    with pytest.raises(TLSMaterialError) as exc_info:
        validate_tls_material(server)

    assert exc_info.value.code == expected_code


def test_tls_inspection_requires_explicit_server_auth_eku(
    tmp_path,
    monkeypatch,
):
    from trading_assistant.ops.tls import TLSMaterialError, validate_tls_material

    server = _write_tls_pair(tmp_path, leaf_server_auth=None)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(TLSMaterialError) as exc_info:
        validate_tls_material(server)

    assert exc_info.value.code == "tls_ca_chain_invalid"


def test_production_encryption_inspector_fails_closed_without_singleton(
    tmp_path,
):
    """Missing durable encryption proof must remain a structural blocker."""
    from trading_assistant.db.models import Base
    from trading_assistant.db.session import create_db_engine
    from trading_assistant.preflight import SensitiveEncryptionStateInspector

    engine = create_db_engine(f"sqlite:///{tmp_path}/missing-state.db")
    Base.metadata.create_all(engine)

    assert SensitiveEncryptionStateInspector(
        engine,
        schema_version=1,
        active_key_id="configured-key-2026",
    ).inspect().code == (
        "sensitive_migration_state_invalid"
    )


def test_startup_guard_never_constructs_app_when_encryption_state_is_invalid(
    app_config,
    monkeypatch,
):
    """Bypassing missing durable DB proof would fail open."""
    from trading_assistant.config import BrokerKind
    from trading_assistant.ops import serve
    from trading_assistant.preflight import StructuralCheck
    from trading_assistant.security.secrets import RuntimeSecrets

    config = app_config.model_copy(
        update={
            "trading": app_config.trading.model_copy(
                update={"broker": BrokerKind.ALPACA}
            )
        }
    )
    secrets = RuntimeSecrets(
        app_api_token="guard-secret-with-sufficient-entropy-0123456789",
        database_url="sqlite:///unused-guard.db",
    )
    monkeypatch.setattr(
        serve,
        "_transport_check",
        lambda _config: StructuralCheck("loopback_https", "passed", "ok"),
    )
    monkeypatch.setattr(
        serve,
        "_tls_check",
        lambda _config: StructuralCheck("tls", "passed", "ok"),
    )
    monkeypatch.setattr(
        serve,
        "_database_check",
        lambda _secrets: StructuralCheck("database", "passed", "ok"),
    )

    with pytest.raises(
        serve.StartupGuardBlocked,
        match="sensitive_migration_state_invalid",
    ):
        serve.run_startup_guard(
            config=config,
            secrets=secrets,
            secret_loaded_at=datetime.now(timezone.utc),
        )


def test_strict_launcher_stops_before_uvicorn_on_structural_failure(monkeypatch):
    """Calling Uvicorn after a failed local guard would construct an unsafe app."""
    from trading_assistant.ops import serve
    from trading_assistant.preflight import StructuralCheck

    blocked = serve.StartupGuardBlocked(
        (StructuralCheck("tls", "blocked", "tls_certificate_san_invalid"),)
    )
    called: list[object] = []
    monkeypatch.setattr(serve, "load_config", lambda: object())
    monkeypatch.setattr(
        serve,
        "run_startup_guard",
        lambda **_kwargs: (_ for _ in ()).throw(blocked),
    )
    monkeypatch.setattr(
        serve.uvicorn,
        "Server",
        lambda *_args, **_kwargs: called.append(True),
    )

    with pytest.raises(serve.StartupGuardBlocked):
        serve.main()

    assert called == []


def test_strict_launcher_uses_only_loopback_tls_and_disables_proxy_headers(
    monkeypatch,
):
    """Changing launcher arguments could re-enable trusted-proxy or HTTP serving."""
    from trading_assistant.ops import serve

    server = SimpleNamespace(
        bind_host="127.0.0.1",
        port=8020,
        tls_cert_path=Path(".local/tls/localhost.pem"),
        tls_key_path=Path(".local/tls/localhost-key.pem"),
    )
    configured: list[dict[str, object]] = []
    served: list[object] = []
    closed: list[bool] = []
    shutdown_callbacks: list[object] = []

    app = SimpleNamespace(
        state=SimpleNamespace(
            install_controlled_shutdown=shutdown_callbacks.append,
        )
    )

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False

        def run(self):
            served.append(self)
            shutdown_callbacks[0]()

    class FakeControl:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(serve, "load_config", lambda: SimpleNamespace(server=server))
    monkeypatch.setattr(serve, "run_startup_guard", lambda **_kwargs: ())
    monkeypatch.setattr(serve, "start_app_control", lambda _project: FakeControl())
    _patch_fake_launcher_composition(monkeypatch, serve, app)
    monkeypatch.setattr(
        serve.uvicorn,
        "Config",
        lambda _app, **kwargs: (
            configured.append({"app": _app, **kwargs})
            or SimpleNamespace(app=_app, kwargs=kwargs)
        ),
    )
    monkeypatch.setattr(serve.uvicorn, "Server", FakeServer)

    assert serve.main() == 0
    assert configured == [{
        "app": app,
        "host": "127.0.0.1",
        "port": 8020,
        "ssl_certfile": ".local/tls/localhost.pem",
        "ssl_keyfile": ".local/tls/localhost-key.pem",
        "proxy_headers": False,
        "forwarded_allow_ips": "",
        "access_log": False,
    }]
    assert len(served) == 1
    assert served[0].should_exit is True
    assert closed == [True]


def test_launcher_closes_running_tenure_if_server_construction_fails(
    monkeypatch,
):
    from trading_assistant.ops import serve

    server_config = SimpleNamespace(
        bind_host="127.0.0.1",
        port=8020,
        tls_cert_path=Path(".local/tls/localhost.pem"),
        tls_key_path=Path(".local/tls/localhost-key.pem"),
    )

    class Guard:
        closed = False

        def close(self):
            self.closed = True
            return True

    guard = Guard()
    app = SimpleNamespace(
        state=SimpleNamespace(
            runtime_tenure_guard=guard,
            install_controlled_shutdown=lambda _callback: None,
        )
    )
    control = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(
        serve,
        "load_config",
        lambda: SimpleNamespace(server=server_config),
    )
    monkeypatch.setattr(serve, "run_startup_guard", lambda **_kwargs: ())
    monkeypatch.setattr(serve, "start_app_control", lambda _project: control)
    _patch_fake_launcher_composition(monkeypatch, serve, app)
    monkeypatch.setattr(
        serve.uvicorn,
        "Config",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        serve.uvicorn,
        "Server",
        lambda _config: (_ for _ in ()).throw(RuntimeError("server-failed")),
    )

    with pytest.raises(RuntimeError, match="server-failed"):
        serve.main()

    assert guard.closed is True


def test_launcher_fails_nonzero_when_lifespan_already_saw_uncertain_release(
    monkeypatch,
):
    from trading_assistant.ops import serve
    from trading_assistant.ops.tenure import RuntimeTenureGuard

    server_config = SimpleNamespace(
        bind_host="127.0.0.1",
        port=8020,
        tls_cert_path=Path(".local/tls/localhost.pem"),
        tls_key_path=Path(".local/tls/localhost-key.pem"),
    )

    class Handle:
        role = "app"

        def renew(self, *, ttl_seconds):
            return None

        def release(self):
            return False

    guard = RuntimeTenureGuard(
        Handle(),
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    shutdown_callbacks: list[object] = []
    app = SimpleNamespace(
        state=SimpleNamespace(
            runtime_tenure_guard=guard,
            install_controlled_shutdown=shutdown_callbacks.append,
        )
    )

    class FakeServer:
        should_exit = False

        def __init__(self, _config):
            pass

        def run(self):
            # Simulate FastAPI's shutdown callback running before Uvicorn
            # returns control to the launcher.
            assert guard.close() is False

    monkeypatch.setattr(
        serve,
        "load_config",
        lambda: SimpleNamespace(server=server_config),
    )
    monkeypatch.setattr(serve, "run_startup_guard", lambda **_kwargs: ())
    monkeypatch.setattr(
        serve,
        "start_app_control",
        lambda _project: SimpleNamespace(close=lambda: None),
    )
    _patch_fake_launcher_composition(monkeypatch, serve, app)
    monkeypatch.setattr(
        serve.uvicorn,
        "Config",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(serve.uvicorn, "Server", FakeServer)

    with pytest.raises(
        RuntimeError,
        match="runtime_tenure_cleanup_uncertain",
    ):
        serve.main()
