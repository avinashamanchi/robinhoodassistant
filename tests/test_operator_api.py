"""Adversarial contract tests for the terminal's loopback API client."""

from __future__ import annotations

import io
import json
from pathlib import Path
import ssl
from urllib.error import HTTPError, URLError

import pytest

from trading_assistant.config import load_config


TEST_CA = Path("/etc/ssl/cert.pem").read_text(encoding="ascii")


class _Response:
    def __init__(self, payload, *, headers=None):
        self._body = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload).encode("utf-8")
        )
        self.headers = headers or {}
        self.status = 200

    def read(self, amount=-1):
        return self._body if amount < 0 else self._body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _RecordingOpener:
    def __init__(self, seen, *, response=None, failure=None):
        self.seen = seen
        self.response = _Response({} if response is None else response)
        self.failure = failure
        self.mutation_attempts = 0

    def open(self, request, *, timeout):
        self.seen.append(request)
        if request.get_method() == "POST":
            self.mutation_attempts += 1
        if self.failure is not None:
            raise self.failure
        return self.response


def _config_loader(path):
    return load_config(path)


def _project(tmp_path, *, ca=TEST_CA):
    ca_path = tmp_path / ".local/tls/rootCA.pem"
    ca_path.parent.mkdir(parents=True)
    ca_path.write_text(ca, encoding="ascii")
    source = Path(__file__).resolve().parent.parent / "config.yaml"
    config = source.read_text(encoding="utf-8")
    (tmp_path / "config.yaml").write_text(config, encoding="utf-8")
    return tmp_path


def client_for(tmp_path, *, opener=None, **kwargs):
    from trading_assistant.ops.operator_api import OperatorApiClient

    return OperatorApiClient(
        _project(tmp_path),
        opener=opener or _RecordingOpener([], response={"ok": True}),
        config_loader=_config_loader,
        **kwargs,
    )


def test_client_uses_only_loopback_https_and_local_ca(tmp_path):
    from trading_assistant.ops.operator_api import OperatorApiClient

    project = _project(tmp_path)
    seen = []
    client = OperatorApiClient(
        project,
        opener=_RecordingOpener(seen, response={"alive": True}),
        config_loader=_config_loader,
    )
    assert client.get("/health/live", authenticated=False) == {"alive": True}
    assert seen[0].full_url == "https://localhost:8020/health/live"
    assert seen[0].get_header("Proxy-authorization") is None


def test_default_client_refuses_redirects_to_another_origin(tmp_path, monkeypatch):
    from trading_assistant.ops import operator_api

    handlers = []
    monkeypatch.setattr(
        operator_api,
        "build_opener",
        lambda *provided: handlers.extend(provided) or _RecordingOpener([]),
    )
    client = operator_api.OperatorApiClient(
        _project(tmp_path), config_loader=_config_loader
    )
    redirect_handler = next(
        handler for handler in handlers if hasattr(handler, "redirect_request")
    )
    request = __import__("urllib.request", fromlist=["Request"]).Request(
        "https://localhost:8020/health/live"
    )
    assert redirect_handler.redirect_request(
        request, None, 302, "Found", {}, "https://evil.test/"
    ) is None
    assert client._opener is not None


@pytest.mark.parametrize(
    "path",
    ["//evil.test", "https://evil.test/x", "/../x", "/x?secret=value"],
)
def test_client_rejects_noncanonical_paths(tmp_path, path):
    with pytest.raises(ValueError, match="operator_path_invalid"):
        client_for(tmp_path).get(path)


@pytest.mark.parametrize("kind", ["missing", "symlink", "directory"])
def test_client_rejects_non_regular_local_ca_before_request(tmp_path, kind):
    from trading_assistant.ops.operator_api import OperatorApiClient

    project = _project(tmp_path)
    ca = project / ".local/tls/rootCA.pem"
    if kind == "missing":
        ca.unlink()
    elif kind == "symlink":
        target = project / "real-ca.pem"
        target.write_text(TEST_CA, encoding="ascii")
        ca.unlink()
        ca.symlink_to(target)
    else:
        ca.unlink()
        ca.mkdir()
    seen = []
    with pytest.raises(ValueError, match="operator_ca_invalid"):
        OperatorApiClient(
            project,
            opener=_RecordingOpener(seen),
            config_loader=_config_loader,
        )
    assert seen == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("origin", "https://127.0.0.1:8020"), ("bind_host", "::1")],
)
def test_client_rejects_drifted_loopback_config_before_request(
    tmp_path, field, value
):
    from trading_assistant.ops.operator_api import OperatorApiClient

    project = _project(tmp_path)
    config = load_config(project / "config.yaml")
    server = config.server.model_copy(update={field: value})
    seen = []
    with pytest.raises(ValueError, match="operator_origin_invalid"):
        OperatorApiClient(
            project,
            opener=_RecordingOpener(seen),
            config_loader=lambda _path: config.model_copy(update={"server": server}),
        )
    assert seen == []


def test_client_rejects_tls_verification_disabled_before_request(tmp_path):
    from trading_assistant.ops.operator_api import OperatorApiClient

    class InsecureContext:
        check_hostname = True
        verify_mode = 0

    seen = []
    with pytest.raises(ValueError, match="operator_tls_invalid"):
        OperatorApiClient(
            _project(tmp_path),
            opener=_RecordingOpener(seen),
            config_loader=_config_loader,
            ssl_context_factory=lambda **_kwargs: InsecureContext(),
        )
    assert seen == []


def test_login_keeps_cookie_and_csrf_in_memory_and_mutation_is_single_shot(tmp_path):
    from trading_assistant.ops.operator_api import OperatorApiClient

    secret = "operator-secret-never-log"
    csrf = "csrf-never-log"
    cookie = "session=never-persist"
    seen = []
    opener = _RecordingOpener(
        seen,
        response={"actor": "operator:local", "csrf_token": csrf, "expires_at": None},
    )
    client = OperatorApiClient(
        _project(tmp_path), opener=opener, config_loader=_config_loader
    )
    session = client.login(secret)
    assert session.actor == "operator:local"
    assert session.csrf_token == csrf
    opener.mutation_attempts = 0
    opener.response = _Response({"accepted": True})
    client.mutate("/rules", {"name": "safe"}, idempotent=True)
    client.mutate("/rules", {"name": "safe"}, idempotent=True)
    keys = [request.get_header("Idempotency-key") for request in seen[1:]]
    assert all(request.get_header("X-csrf-token") == csrf for request in seen[1:])
    assert keys[0] and keys[0] != keys[1]
    assert opener.mutation_attempts == 2
    assert secret.encode() in seen[0].data
    assert cookie not in repr(session)


def test_mutation_transport_failure_is_not_retried(tmp_path):
    from trading_assistant.ops.operator_api import OperatorApiClient, OperatorApiError

    opener = _RecordingOpener(
        [], response={"actor": "operator:local", "csrf_token": "csrf"}
    )
    client = OperatorApiClient(
        _project(tmp_path), opener=opener, config_loader=_config_loader
    )
    client.login("secret")
    opener.mutation_attempts = 0
    opener.failure = URLError("offline")
    with pytest.raises(OperatorApiError) as caught:
        client.mutate("/rules", {"name": "safe"}, idempotent=True)
    assert caught.value.code == "operator_request_failed"
    assert opener.mutation_attempts == 1


def test_reauthenticate_uses_current_csrf_and_logout_clears_state_on_failure(tmp_path):
    from trading_assistant.ops.operator_api import OperatorApiClient, OperatorApiError

    csrf = "csrf-current"
    seen = []
    opener = _RecordingOpener(
        seen, response={"actor": "operator:local", "csrf_token": csrf}
    )
    client = OperatorApiClient(
        _project(tmp_path), opener=opener, config_loader=_config_loader
    )
    client.login("secret")
    opener.response = _Response({"actor": "operator:local"})
    assert client.reauthenticate("new-secret").csrf_token == csrf
    assert seen[-1].get_header("X-csrf-token") == csrf
    opener.failure = _http_error(503, {"error": {"code": "busy", "message": "busy"}})
    with pytest.raises(OperatorApiError):
        client.logout()
    with pytest.raises(OperatorApiError) as caught:
        client.mutate("/rules", {}, idempotent=False)
    assert caught.value.code == "operator_csrf_missing"


def _http_error(status, body, *, headers=None):
    return HTTPError(
        "https://localhost:8020/redacted",
        status,
        "provider secret message",
        headers or {},
        io.BytesIO(json.dumps(body).encode("utf-8")),
    )


@pytest.mark.parametrize("status", [401, 403, 409, 422, 429, 503])
def test_http_errors_use_only_stable_envelope_and_401_clears_auth(tmp_path, status):
    from trading_assistant.ops.operator_api import OperatorApiClient, OperatorApiError

    csrf = "csrf-redact"
    secret = "secret-redact"
    cookie = "cookie-redact"
    seen = []
    opener = _RecordingOpener(seen, response={"actor": "a", "csrf_token": csrf})
    client = OperatorApiClient(_project(tmp_path), opener=opener, config_loader=_config_loader)
    client.login(secret)
    opener.failure = _http_error(
        status,
        {"error": {"code": "stable_code", "message": "Stable message", "request_id": "r-1"}},
        headers={"Retry-After": "99999", "Set-Cookie": cookie},
    )
    with pytest.raises(OperatorApiError) as caught:
        client.get("/rules")
    error = caught.value
    assert (error.status, error.code, str(error), error.request_id) == (
        status, "stable_code", "Stable message", "r-1"
    )
    assert error.retry_after == 3600
    assert cookie not in repr(error)
    if status == 401:
        with pytest.raises(OperatorApiError) as caught:
            client.mutate("/rules", {}, idempotent=False)
        assert caught.value.code == "operator_csrf_missing"


@pytest.mark.parametrize("response", [b"\xff", []])
def test_client_rejects_non_object_or_invalid_json_responses(tmp_path, response):
    from trading_assistant.ops.operator_api import OperatorApiClient, OperatorApiError

    seen = []
    opener = _RecordingOpener(seen, response=response)
    client = OperatorApiClient(_project(tmp_path), opener=opener, config_loader=_config_loader)
    with pytest.raises(OperatorApiError) as caught:
        client.get("/health/live", authenticated=False)
    assert (caught.value.status, caught.value.code) == (200, "operator_response_invalid")


def test_client_rejects_malformed_error_envelope_without_provider_text(tmp_path):
    from trading_assistant.ops.operator_api import OperatorApiClient, OperatorApiError

    opener = _RecordingOpener(
        [],
        failure=_http_error(
            503,
            {"error": {"code": ["not-text"], "message": "provider secret"}},
        ),
    )
    client = OperatorApiClient(
        _project(tmp_path), opener=opener, config_loader=_config_loader
    )
    with pytest.raises(OperatorApiError) as caught:
        client.get("/health/live", authenticated=False)
    assert caught.value.code == "operator_http_error"
    assert "provider secret" not in repr(caught.value)


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (URLError("timeout secret-not-in-error"), "operator_request_failed"),
        (ssl.SSLError("tls secret-not-in-error"), "operator_tls_failed"),
        (OSError("network secret-not-in-error"), "operator_request_failed"),
    ],
)
def test_client_bounds_response_and_redacts_transport_failures(
    tmp_path, caplog, failure, code
):
    from trading_assistant.ops.operator_api import OperatorApiClient, OperatorApiError

    secret = "secret-not-in-error"
    seen = []
    client = OperatorApiClient(
        _project(tmp_path),
        opener=_RecordingOpener(seen, response=b"x" * 5),
        config_loader=_config_loader,
        max_response_bytes=4,
    )
    with pytest.raises(OperatorApiError) as caught:
        client.get("/health/live", authenticated=False)
    assert caught.value.code == "operator_response_too_large"
    client._opener.failure = failure
    with pytest.raises(OperatorApiError) as caught:
        client.get("/health/live", authenticated=False)
    assert caught.value.code == code
    assert secret not in caplog.text
    assert secret not in repr(caught.value)


def test_missing_csrf_is_local_error_without_a_mutation_attempt(tmp_path):
    from trading_assistant.ops.operator_api import OperatorApiClient, OperatorApiError

    seen = []
    opener = _RecordingOpener(seen, response={"accepted": True})
    client = OperatorApiClient(_project(tmp_path), opener=opener, config_loader=_config_loader)
    with pytest.raises(OperatorApiError) as caught:
        client.mutate("/rules", {}, idempotent=False)
    assert caught.value.code == "operator_csrf_missing"
    assert opener.mutation_attempts == 0
