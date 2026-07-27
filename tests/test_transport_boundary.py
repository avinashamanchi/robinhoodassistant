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
from fastapi.testclient import TestClient

from trading_assistant.app.main import create_app
from trading_assistant.security.transport import (
    TransportBoundaryMiddleware,
    TransportPolicy,
)


class _StubAgent:
    def chat(self, message, **context):
        return {"reply": message, "context": context}


def _app(make_service, *, policy=None):
    return create_app(
        service=make_service(),
        agent=_StubAgent(),
        api_token="transport-boundary-test-secret",
        planning=None,
        transport_policy=policy,
    )


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


def _write_tls_pair(tmp_path: Path) -> SimpleNamespace:
    tls_directory = tmp_path / ".local" / "tls"
    tls_directory.mkdir(parents=True)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "localhost")]))
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    x509.IPAddress(ipaddress.ip_address("::1")),
                ]
            ),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    certificate_path = tls_directory / "localhost.pem"
    key_path = tls_directory / "localhost-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(tls_directory, 0o700)
    os.chmod(certificate_path, 0o644)
    os.chmod(key_path, 0o600)
    return SimpleNamespace(
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
    os.chmod(tmp_path / ".local/tls/localhost-key.pem", 0o644)
    with pytest.raises(TLSMaterialError, match="tls_private_key_permissions_invalid"):
        validate_tls_material(server)


def test_production_encryption_inspector_remains_blocked_until_task_five():
    """Assuming encryption completion before its DB proof exists would fail open."""
    from trading_assistant.ops.serve import EncryptionInspectorUnavailable

    assert EncryptionInspectorUnavailable().inspect().code == (
        "encryption_inspector_unavailable"
    )


def test_startup_guard_never_constructs_app_when_encryption_is_unavailable(
    app_config,
    monkeypatch,
):
    """Bypassing the unavailable inspector would violate the Task 5 sequencing gate."""
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

    with pytest.raises(serve.StartupGuardBlocked, match="encryption_inspector_unavailable"):
        serve.run_startup_guard(config=config, secrets=secrets)


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
    monkeypatch.setattr(serve.uvicorn, "run", lambda *_args, **_kwargs: called.append(True))

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
    called: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(serve, "load_config", lambda: SimpleNamespace(server=server))
    monkeypatch.setattr(serve, "run_startup_guard", lambda **_kwargs: ())
    monkeypatch.setattr(
        serve.uvicorn,
        "run",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )

    assert serve.main() == 0
    assert called == [
        (
            ("trading_assistant.app.main:create_app",),
            {
                "factory": True,
                "host": "127.0.0.1",
                "port": 8020,
                "ssl_certfile": ".local/tls/localhost.pem",
                "ssl_keyfile": ".local/tls/localhost-key.pem",
                "proxy_headers": False,
                "forwarded_allow_ips": "",
                "access_log": False,
            },
        )
    ]
