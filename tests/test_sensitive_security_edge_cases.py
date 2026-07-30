"""Fail-closed edge cases for the sensitive security boundary.

These tests use only deterministic in-memory values, synthetic ASGI messages,
and pytest-managed temporary files.  They intentionally avoid every external
secret, process, broker, network, and normal-database boundary.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterator, Mapping
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from trading_assistant.db.models import AuditEvent, AuthSession
import trading_assistant.security.crypto as crypto_module
from trading_assistant.security.crypto import (
    SensitiveDataCipher,
    SensitiveDataInvalid,
    SensitiveFieldRef,
    build_sensitive_data_cipher,
)
import trading_assistant.security.secrets as secret_module
from trading_assistant.security.secrets import (
    EnvironmentSecretProvider,
    MacOSKeychainSecretProvider,
    RuntimeSecrets,
    SecretUnavailable,
    SecretValidationError,
)
import trading_assistant.security.sensitive_fields as field_module
from trading_assistant.security.sensitive_fields import (
    PlaintextSensitiveField,
    SensitiveFieldStore,
    bind_sensitive_cipher,
    install_sensitive_field_guards,
    sensitive_store,
)
from trading_assistant.security.sensitive_write_scan import (
    scan_sensitive_writes,
)
import trading_assistant.security.transport as transport_module
from trading_assistant.security.transport import (
    TransportBoundaryMiddleware,
    TransportPolicy,
)


KEY_ID = "edge-key-2026-07"
RETAINED_KEY_ID = "edge-key-2026-06"
KEY = hashlib.sha256(b"edge-active-key").digest()
RETAINED_KEY = hashlib.sha256(b"edge-retained-key").digest()
NONCE = bytes(range(12))


def _cipher() -> SensitiveDataCipher:
    return SensitiveDataCipher({KEY_ID: KEY}, active_key_id=KEY_ID)


def _encoded_key(label: str) -> str:
    return base64.b64encode(hashlib.sha256(label.encode("ascii")).digest()).decode(
        "ascii"
    )


def _encryption(*, retained: tuple[str, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(
        active_key_id=KEY_ID,
        retained_key_ids=list(retained),
    )


def _config(
    *,
    provider: str = "anthropic",
    telegram: bool = False,
    retained: tuple[str, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        encryption=_encryption(retained=retained),
        llm=SimpleNamespace(provider=provider),
        features=SimpleNamespace(telegram_notifications=telegram),
    )


@pytest.mark.parametrize(
    "values",
    [
        ("", "1", "reason", 1),
        ("audit_events", "", "reason", 1),
        ("audit_events", "1", "", 1),
        ("audit\x00events", "1", "reason", 1),
        ("audit_events", "1\x00", "reason", 1),
        ("audit_events", "1", "rea\x00son", 1),
        ("audit_events", "1", "reason", True),
        ("audit_events", "1", "reason", 0),
        ("audit_events", "1", "reason", "1"),
    ],
)
def test_sensitive_field_refs_reject_ambiguous_identity(values):
    with pytest.raises(
        SensitiveDataInvalid,
        match="^sensitive_data_invalid$",
    ):
        SensitiveFieldRef(*values)


class _ExplodingMapping(Mapping[str, bytes]):
    def __getitem__(self, key: str) -> bytes:
        raise AssertionError(key)

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError("mapping traversal marker")

    def __len__(self) -> int:
        return 1


def test_cipher_constructor_wraps_mapping_failure_and_exposes_no_marker():
    with pytest.raises(SensitiveDataInvalid) as captured:
        SensitiveDataCipher(_ExplodingMapping(), active_key_id=KEY_ID)

    assert str(captured.value) == f"sensitive_data_invalid key_id={KEY_ID}"
    assert "mapping traversal marker" not in str(captured.value)


def test_cipher_properties_and_wrong_runtime_types_fail_closed(monkeypatch):
    cipher = _cipher()
    ref = SensitiveFieldRef("audit_events", "17", "reason", 1)
    monkeypatch.setattr(crypto_module.os, "urandom", lambda length: NONCE[:length])

    assert cipher.key_ids == (KEY_ID,)
    with pytest.raises(
        SensitiveDataInvalid,
        match=f"^sensitive_data_invalid key_id={KEY_ID}$",
    ):
        cipher.encrypt("sensitive value", object())  # type: ignore[arg-type]
    with pytest.raises(SensitiveDataInvalid, match="^sensitive_data_invalid$"):
        cipher.decrypt(17, ref)  # type: ignore[arg-type]
    with pytest.raises(SensitiveDataInvalid, match="^sensitive_data_invalid$"):
        cipher.decrypt("enc:v1:anything", object())  # type: ignore[arg-type]


def test_cipher_rejects_short_authenticated_payload_and_empty_plaintext():
    cipher = _cipher()
    ref = SensitiveFieldRef("audit_events", "17", "reason", 1)
    short_payload = base64.urlsafe_b64encode(bytes(27)).rstrip(b"=").decode("ascii")
    empty_ciphertext = AESGCM(KEY).encrypt(
        NONCE,
        b"",
        ref.associated_data(),
    )
    empty_payload = (
        base64.urlsafe_b64encode(NONCE + empty_ciphertext)
        .rstrip(b"=")
        .decode("ascii")
    )

    for envelope in (
        f"enc:v1:{KEY_ID}:{short_payload}",
        f"enc:v1:{KEY_ID}:{empty_payload}",
    ):
        with pytest.raises(
            SensitiveDataInvalid,
            match=f"^sensitive_data_invalid key_id={KEY_ID}$",
        ):
            cipher.decrypt(envelope, ref)


def test_cipher_builder_rejects_duplicate_configured_key_ids_first():
    encryption = _encryption(retained=(KEY_ID,))
    secrets = RuntimeSecrets(
        field_encryption_keys={
            KEY_ID: SecretStr(_encoded_key("duplicate-configured-key"))
        }
    )

    with pytest.raises(
        SecretValidationError,
        match="duplicate_key_id",
    ):
        build_sensitive_data_cipher(encryption, secrets)  # type: ignore[arg-type]


def test_secret_key_id_and_duplicate_configuration_are_rejected():
    with pytest.raises(
        SecretValidationError,
        match="encryption key ID is invalid",
    ):
        secret_module.validate_key_id("short")
    with pytest.raises(
        SecretValidationError,
        match="field encryption key IDs must be distinct",
    ):
        secret_module._configured_key_ids(  # noqa: SLF001
            _encryption(retained=(KEY_ID,))
        )


@pytest.mark.parametrize(
    ("raw", "stable_code"),
    [
        ("{", "invalid_json"),
        ("[]", "invalid_json"),
        (f'{{"{KEY_ID}": 7}}', "invalid_json"),
        ("", "missing"),
    ],
)
def test_environment_field_key_parser_fails_closed(
    raw: str,
    stable_code: str,
):
    with pytest.raises((SecretUnavailable, SecretValidationError)) as captured:
        secret_module._parse_environment_field_keys(  # noqa: SLF001
            raw,
            encryption=_encryption(),
        )

    assert captured.value.stable_code == stable_code
    if raw:
        assert raw not in str(captured.value)


def test_environment_field_key_parser_can_explicitly_allow_missing_keys():
    assert (
        secret_module._parse_environment_field_keys(  # noqa: SLF001
            "",
            encryption=_encryption(),
            allow_missing=True,
        )
        == {}
    )


def test_environment_provider_requires_mapping_and_stable_configuration():
    with pytest.raises(TypeError, match="explicitly injected mapping"):
        EnvironmentSecretProvider(  # type: ignore[arg-type]
            environ=[],
            encryption=_encryption(),
        )

    provider = EnvironmentSecretProvider(
        environ={
            "FIELD_ENCRYPTION_KEYS_JSON": (
                f'{{"{KEY_ID}":"{_encoded_key("environment-field")}"}}'
            )
        },
        encryption=_encryption(),
    )
    with pytest.raises(
        SecretValidationError,
        match="configuration changed",
    ):
        provider.load(encryption=_encryption(retained=(RETAINED_KEY_ID,)))


class _PresenceBackend:
    def get_password(self, _service: str, account: str) -> str | None:
        if account == "telegram_chat_id":
            raise RuntimeError("synthetic read failure")
        return "present" if account == "database_url" else None

    def set_password(self, _service: str, _account: str, _value: str) -> None:
        raise AssertionError("write is forbidden")

    def delete_password(self, _service: str, _account: str) -> None:
        raise AssertionError("delete is forbidden")


def test_presence_probe_reports_unknown_without_touching_real_keychain():
    provider = MacOSKeychainSecretProvider(backend=_PresenceBackend())

    presence = provider.read_presence(encryption=_encryption())

    assert presence["database_url"] is True
    assert presence["telegram_chat_id"] is None
    assert presence["alpaca_api_key"] is False


def test_role_helpers_reject_unknown_provider_and_add_optional_authority():
    with pytest.raises(
        SecretValidationError,
        match="configured LLM provider is unsupported",
    ):
        secret_module._selected_llm_secret_field(  # noqa: SLF001
            _config(provider="unknown")
        )

    fields = secret_module._required_fields(  # noqa: SLF001
        "daemon",
        _config(telegram=True),
    )
    assert fields[-2:] == ("telegram_bot_token", "telegram_chat_id")
    assert "anthropic_api_key" in fields


def test_role_projection_drops_every_unowned_secret():
    loaded = RuntimeSecrets(
        app_api_token=SecretStr("operator-token-must-be-dropped"),
        database_url=SecretStr("sqlite:///edge-only.db"),
        anthropic_api_key=SecretStr("model-key-must-be-dropped"),
        field_encryption_keys={
            KEY_ID: SecretStr(_encoded_key("projection-field-key"))
        },
    )

    projected = secret_module._project_role_secrets(  # noqa: SLF001
        "watchdog",
        _config(),
        loaded,
    )

    assert projected.database_url.get_secret_value() == "sqlite:///edge-only.db"
    assert projected.app_api_token.get_secret_value() == ""
    assert projected.anthropic_api_key.get_secret_value() == ""
    assert projected.field_encryption_keys == {}


def test_key_material_validation_rejects_exact_id_mismatch():
    secrets = RuntimeSecrets(
        candidate_signing_key=SecretStr(_encoded_key("candidate-edge")),
        backup_encryption_key=SecretStr(_encoded_key("backup-edge")),
        field_encryption_keys={
            RETAINED_KEY_ID: SecretStr(_encoded_key("wrong-field-id"))
        },
    )

    with pytest.raises(SecretValidationError) as captured:
        secret_module._validate_key_material(_config(), secrets)  # noqa: SLF001

    assert captured.value.stable_code == "key_id_mismatch"


def test_role_loader_rejects_unknown_role_before_provider_selection():
    with pytest.raises(ValueError, match="runtime role is invalid"):
        secret_module.load_role_secrets(
            "not-a-role",
            config=_config(),
            runtime_secrets=RuntimeSecrets(),
        )


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        (object(), True),
        ("INSERT OR IGNORE INTO audit_events (actor) VALUES ('x')", True),
        ("REPLACE INTO proposals (reasoning) VALUES ('x')", True),
        ("DELETE FROM risk_events WHERE id = 1", True),
        ("UPDATE OR ABORT audit_events SET reason = 'x'", True),
        ("UPDATE audit_events SET actor = 'x'", False),
        ("UPDATE harmless SET reason = 'x'", False),
        ("SELECT reason FROM audit_events", False),
    ],
)
def test_sensitive_sql_classifier_is_conservative(
    statement: object,
    expected: bool,
):
    assert field_module._sensitive_sql_mutation(statement) is expected  # noqa: SLF001


def test_sensitive_boundary_rejects_invalid_engine_and_unbound_authority():
    with pytest.raises(
        PlaintextSensitiveField,
        match="^plaintext_sensitive_field$",
    ):
        field_module._bind_sensitive_sql_boundary(object())  # type: ignore[arg-type]  # noqa: SLF001

    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with pytest.raises(
            PlaintextSensitiveField,
            match="^plaintext_sensitive_field$",
        ):
            with field_module._sensitive_write_authority(engine):  # noqa: SLF001
                raise AssertionError("unbound authority must not be entered")
    finally:
        engine.dispose()


def test_cipher_binding_rejects_invalid_factory_and_engine_combinations():
    cipher = _cipher()
    with pytest.raises(SensitiveDataInvalid):
        bind_sensitive_cipher(sessionmaker(), object())  # type: ignore[arg-type]
    with pytest.raises(SensitiveDataInvalid):
        bind_sensitive_cipher(sessionmaker(), cipher)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    first_factory = sessionmaker(bind=engine)
    second_factory = sessionmaker(bind=engine)
    try:
        bind_sensitive_cipher(first_factory, cipher)
        with pytest.raises(SensitiveDataInvalid):
            bind_sensitive_cipher(
                first_factory,
                SensitiveDataCipher(
                    {RETAINED_KEY_ID: RETAINED_KEY},
                    active_key_id=RETAINED_KEY_ID,
                ),
            )
        with pytest.raises(SensitiveDataInvalid):
            bind_sensitive_cipher(
                second_factory,
                SensitiveDataCipher(
                    {RETAINED_KEY_ID: RETAINED_KEY},
                    active_key_id=RETAINED_KEY_ID,
                ),
            )
    finally:
        engine.dispose()


def test_sensitive_store_and_guard_installation_reject_missing_authority():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    cipher = _cipher()
    try:
        with Session(engine) as session:
            with pytest.raises(SensitiveDataInvalid):
                sensitive_store(session)
            for schema_version in (True, 0, "1"):
                with pytest.raises(SensitiveDataInvalid):
                    install_sensitive_field_guards(
                        session,
                        cipher,
                        schema_version=schema_version,  # type: ignore[arg-type]
                    )

            install_sensitive_field_guards(session, cipher)
            install_sensitive_field_guards(session, cipher)
            with pytest.raises(SensitiveDataInvalid):
                install_sensitive_field_guards(
                    session,
                    cipher,
                    schema_version=2,
                )
    finally:
        engine.dispose()


def test_sensitive_store_rejects_invalid_write_clear_delete_and_read_shapes():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    cipher = _cipher()
    try:
        with Session(engine) as session:
            store = SensitiveFieldStore(session, cipher)
            audit = AuditEvent(
                actor="edge",
                action="edge.test",
                target_type="test",
                target_id="1",
                request_id="edge-request",
            )

            for instance, values in (
                (AuthSession(), {"reason": "not-registered"}),
                (audit, {}),
                (audit, {"unknown": "not-registered"}),
                (audit, {"reason": "missing-required-detail"}),
            ):
                with pytest.raises(PlaintextSensitiveField):
                    store.write(instance, values)  # type: ignore[arg-type]
            with pytest.raises(SensitiveDataInvalid):
                store.write(audit, {"reason": 7})  # type: ignore[dict-item]

            already_populated = AuditEvent(
                actor="edge",
                action="edge.test",
                target_type="test",
                target_id="2",
                request_id="edge-request-2",
                reason="existing-value",
            )
            with pytest.raises(PlaintextSensitiveField):
                store.write(
                    already_populated,
                    {"reason": "new", "detail_json": "{}"},
                )

            with pytest.raises(PlaintextSensitiveField):
                store.clear(audit, {"reason"})
            with pytest.raises(PlaintextSensitiveField):
                store.delete(audit)
            with pytest.raises(SensitiveDataInvalid):
                store.read(AuthSession(), "reason")
            with pytest.raises(SensitiveDataInvalid):
                store.read(audit, "reason")
    finally:
        engine.dispose()


MODEL_FIELDS = {
    "AuditEvent": (
        "audit_events",
        frozenset({"reason", "detail_json"}),
    ),
    "RiskEvent": (
        "risk_events",
        frozenset({"reason"}),
    ),
}
TABLE_FIELDS = {
    "audit_events": frozenset({"reason", "detail_json"}),
    "risk_events": frozenset({"reason"}),
}


def test_sensitive_write_scanner_follows_aliases_helpers_and_dynamic_flows(
    tmp_path: Path,
):
    source = """
from fake_models import AuditEvent as Event, RiskEvent
from sqlalchemy import delete, insert, select as choose, update as mutate

Alias = Event
field_name = "reason"
rows = [{"actor": "edge"}]

constructed = Alias(**{field_name: "plain"})
query = session.query(Event).where(True)
update_call = query.update
update_alias = update_call
update_alias(values={"reason": "plain"})
table_update = Event.__table__.update
table_update({"detail_json": "plain"})
statement = mutate(Event).where(True).values(detail_json="plain")
session.execute(statement, params={"reason": "plain"})
run = session.execute
run_again = run
run_again(
    statement="UPDATE audit_events SET reason = :reason",
    parameters={"reason": "plain"},
)
session.bulk_insert_mappings(Event, [*rows])
custom_writer(Event, {"reason": "plain"})

unknown = factory()
unknown.reason = "plain"
unknown.detail_json = "plain"
unknown.detail_json += "more"
setattr(unknown, "detail_json", "plain")

typed: Event
typed.reason = "plain"
typed.detail_json: str = "plain"
typed.reason += "more"
setattr(typed, field_name, "plain")

def recurse(item):
    recurse(item)

def leaf(item):
    item.detail_json = "plain"
    setattr(item, field_name, "plain")

def branch(original):
    alias = original
    second = alias
    second.reason = "plain"
    leaf(second)
    leaf(item=second)
    leaf(**{"item": second})

row = session.scalar(choose(Event))
recurse(row)
branch(original=row)
session.delete(row)
session.execute(delete(Event))
"""
    path = tmp_path / "scanner_edge_fixture.py"
    path.write_text(source, encoding="utf-8")

    offenders = scan_sensitive_writes(
        [path],
        model_fields=MODEL_FIELDS,
        table_fields=TABLE_FIELDS,
    )
    diagnostics = "\n".join(offenders)

    for marker in (
        "AuditEvent.reason",
        "AuditEvent.detail_json",
        "AuditEvent.**mapping",
        "AuditEvent.**field",
        "AuditEvent.**row",
        "audit_events.reason",
        "unknown_model.reason",
        "*.detail_json",
        "unknown_model.**helper_flow",
    ):
        assert marker in diagnostics


def test_sensitive_write_scanner_can_fill_only_the_missing_default_registry(
    tmp_path: Path,
):
    path = tmp_path / "default_registry_fixture.py"
    path.write_text(
        """
session.execute(
    "UPDATE audit_events SET reason = 'plaintext' WHERE id = 1"
)
""",
        encoding="utf-8",
    )

    default_diagnostics = "\n".join(scan_sensitive_writes([path]))
    table_default_diagnostics = "\n".join(
        scan_sensitive_writes([path], model_fields={})
    )
    assert "audit_events.reason" in default_diagnostics
    assert "audit_events.reason" in table_default_diagnostics
    assert (
        scan_sensitive_writes(
            [path],
            model_fields={},
            table_fields={},
        )
        == []
    )


@pytest.mark.parametrize(
    ("origin", "port", "hosts", "message"),
    [
        (
            "http://localhost:8020",
            8020,
            ("localhost",),
            "one HTTPS loopback origin",
        ),
        (
            "https://localhost:8020/path",
            8020,
            ("localhost",),
            "one HTTPS loopback origin",
        ),
        (
            "https://localhost:8020",
            8020,
            ("127.0.0.1",),
            "origin host must be an allowed",
        ),
        (
            "https://localhost:8020",
            8020,
            ("localhost", "example.com"),
            "allowed_hosts must contain only loopback",
        ),
    ],
)
def test_production_transport_policy_rejects_ambiguous_origins(
    origin: str,
    port: int,
    hosts: tuple[str, ...],
    message: str,
):
    server = SimpleNamespace(
        secure_cookies=True,
        origin=origin,
        port=port,
        allowed_hosts=hosts,
    )

    with pytest.raises(RuntimeError, match=message):
        TransportPolicy.production(server)


def test_transport_helpers_cover_loopback_and_host_parser_edges():
    broken_policy = TransportPolicy(
        production_mode=True,
        origin="relative-origin",
        allowed_hosts=frozenset({"localhost"}),
        require_https=True,
        reject_proxy_headers=True,
    )
    with pytest.raises(RuntimeError, match="transport origin has no host"):
        _ = broken_policy.canonical_host

    assert transport_module._is_loopback_host(" [::1] ") is True  # noqa: SLF001
    assert transport_module._is_loopback_host("not-an-address") is False  # noqa: SLF001
    assert transport_module._parse_host(b"\xff", expected_port=80) is None  # noqa: SLF001
    assert transport_module._parse_host(b" localhost", expected_port=80) is None  # noqa: SLF001
    assert transport_module._parse_host(b"[::1]]:80", expected_port=80) is None  # noqa: SLF001
    assert (  # noqa: SLF001
        transport_module._parse_host(b"[2001:db8::1]:80", expected_port=80)
        is None
    )
    assert transport_module._parse_host(b"[invalid]:80", expected_port=80) is None  # noqa: SLF001
    assert transport_module._parse_host(b"192.0.2.1:80", expected_port=80) is None  # noqa: SLF001
    assert transport_module._parse_host(b"invalid:80", expected_port=80) is None  # noqa: SLF001
    for invalid_port in (b"localhost:0", b"localhost:65536", b"localhost:81"):
        assert transport_module._parse_host(invalid_port, expected_port=80) is None  # noqa: SLF001
    assert transport_module._parse_host(b"localhost:no", expected_port=80) is None  # noqa: SLF001
    assert transport_module._parse_host(b"[::1]:80", expected_port=80) == "::1"  # noqa: SLF001
    assert transport_module._parse_host(b"127.0.0.2:80", expected_port=80) == "127.0.0.2"  # noqa: SLF001

    ipv6_policy = TransportPolicy(
        production_mode=True,
        origin="https://[::1]:8020",
        allowed_hosts=frozenset({"::1"}),
        require_https=True,
        reject_proxy_headers=True,
    )
    assert transport_module._canonical_host_header(ipv6_policy) == b"[::1]:8020"  # noqa: SLF001


async def _run_boundary(
    *,
    headers: list[tuple[bytes, bytes]],
    messages: list[dict[str, object]],
    method: str = "GET",
    path: str = "/",
    inner_receive_count: int = 0,
) -> tuple[list[dict[str, object]], int, list[dict[str, object]]]:
    sent: list[dict[str, object]] = []
    replayed: list[dict[str, object]] = []
    inner_calls = 0
    queue = list(messages)

    async def receive() -> dict[str, object]:
        return queue.pop(0)

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def inner(scope, receive_inner, send_inner) -> None:
        nonlocal inner_calls
        inner_calls += 1
        assert scope["headers"]
        for _ in range(inner_receive_count):
            replayed.append(await receive_inner())
        await send_inner(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [],
            }
        )
        await send_inner({"type": "http.response.body", "body": b""})

    middleware = TransportBoundaryMiddleware(
        inner,
        policy=TransportPolicy.test(),
    )
    await middleware(
        {
            "type": "http",
            "scheme": "http",
            "method": method,
            "path": path,
            "headers": headers,
        },
        receive,
        send,
    )
    return sent, inner_calls, replayed


@pytest.mark.parametrize(
    ("headers", "messages", "method", "status", "code"),
    [
        (
            [(b"\xff", b"value")],
            [],
            "GET",
            431,
            "headers_too_large",
        ),
        (
            [(b"host", b"testserver"), (b"host", b"testserver")],
            [],
            "GET",
            400,
            "untrusted_host",
        ),
        (
            [(b"host", b"testserver"), (b"x-edge", b"1"), (b"x-edge", b"2")],
            [],
            "GET",
            400,
            "headers_too_large",
        ),
        (
            [(b"host", b"testserver"), (b"origin", b"\xff")],
            [],
            "GET",
            403,
            "origin_mismatch",
        ),
        (
            [(b"host", b"testserver"), (b"content-length", b"invalid")],
            [],
            "POST",
            413,
            "body_too_large",
        ),
        (
            [(b"host", b"testserver"), (b"content-type", b"text/plain")],
            [{"type": "http.request", "body": b"{}", "more_body": False}],
            "POST",
            415,
            "unsupported_media_type",
        ),
    ],
)
def test_transport_boundary_rejects_malformed_edge_requests_before_app(
    headers,
    messages,
    method,
    status,
    code,
):
    sent, inner_calls, _replayed = asyncio.run(
        _run_boundary(
            headers=headers,
            messages=messages,
            method=method,
        )
    )

    assert inner_calls == 0
    assert sent[0]["status"] == status
    assert code.encode("ascii") in sent[1]["body"]


def test_transport_boundary_stops_silently_on_client_disconnect():
    sent, inner_calls, _replayed = asyncio.run(
        _run_boundary(
            headers=[(b"host", b"testserver")],
            messages=[{"type": "http.disconnect"}],
        )
    )

    assert sent == []
    assert inner_calls == 0


def test_transport_boundary_replays_once_then_disconnects_and_marks_degraded():
    sent, inner_calls, replayed = asyncio.run(
        _run_boundary(
            headers=[(b"host", b"testserver")],
            messages=[
                {
                    "type": "http.request",
                    "body": b"edge",
                    "more_body": False,
                }
            ],
            inner_receive_count=2,
        )
    )

    assert inner_calls == 1
    assert replayed == [
        {"type": "http.request", "body": b"edge", "more_body": False},
        {"type": "http.disconnect"},
    ]
    assert (b"x-transport-degraded", b"test_transport") in sent[0]["headers"]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ({"type": "http.disconnect"}, (None, True)),
        ({"type": "websocket.receive"}, (None, False)),
        (
            {"type": "http.request", "body": "not-bytes", "more_body": False},
            (None, False),
        ),
    ],
)
def test_bounded_body_reader_fails_closed_for_invalid_asgi_messages(
    message,
    expected,
):
    async def run():
        async def receive():
            return message

        return await transport_module._read_bounded_body(  # noqa: SLF001
            receive,
            max_bytes=8,
        )

    assert asyncio.run(run()) == expected


def test_transport_content_type_parser_rejects_absent_and_non_ascii_values():
    assert transport_module._is_json(None) is False  # noqa: SLF001
    assert transport_module._is_json(b"\xff") is False  # noqa: SLF001
    assert transport_module._is_json(b" Application/JSON ; charset=utf-8 ") is True  # noqa: SLF001
