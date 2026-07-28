from __future__ import annotations

import base64
import gc
import hashlib
import json
import weakref
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr
from sqlalchemy import event, func, inspect as sa_inspect, select
from sqlalchemy.orm import make_transient

from trading_assistant.config import EncryptionConfig
from trading_assistant.db.models import (
    AnalysisReportRow,
    AuditEvent,
    Base,
    CircuitBreakerState,
    LLMDecision,
    Order,
    PanicReceipt,
    Proposal,
    RiskEvent,
    StartupReconciliationState,
    TradePlanRow,
)
from trading_assistant.db.session import create_db_engine, make_session_factory
from trading_assistant.security.crypto import (
    SensitiveDataCipher,
    SensitiveDataInvalid,
    SensitiveFieldRef,
    build_sensitive_data_cipher,
)
from trading_assistant.security.secrets import RuntimeSecrets
import trading_assistant.security.sensitive_fields as sensitive_field_module
from trading_assistant.security.sensitive_fields import (
    SENSITIVE_FIELDS,
    PlaintextSensitiveField,
    SensitiveFieldStore,
    install_sensitive_field_guards,
)


KEY_ID = "test-key-2026-07"
RETAINED_KEY_ID = "test-key-2026-06"
KEY = hashlib.sha256(b"task-5-active-test-key").digest()
RETAINED_KEY = hashlib.sha256(b"task-5-retained-test-key").digest()
PLAINTEXT_MARKER = "plaintext-marker-9f4e3bf0"


@pytest.fixture
def cipher() -> SensitiveDataCipher:
    return SensitiveDataCipher({KEY_ID: KEY}, active_key_id=KEY_ID)


@pytest.fixture
def session_factory(engine):
    """Task 5 guard tests install their own per-case cipher explicitly."""
    return make_session_factory(engine)


def _audit_event() -> AuditEvent:
    return AuditEvent(
        actor="operator:test",
        action="sensitive.test",
        target_type="test",
        target_id="target-1",
        request_id="request-1",
        result_code="recorded",
    )


class _FailFinalCipher:
    def __init__(self, cipher: SensitiveDataCipher) -> None:
        self.cipher = cipher
        self.fail_final = True

    def encrypt(self, plaintext, ref):
        if self.fail_final and not ref.row.startswith("pending:"):
            raise RuntimeError("simulated_final_encryption_failure")
        return self.cipher.encrypt(plaintext, ref)

    def decrypt(self, envelope, ref):
        return self.cipher.decrypt(envelope, ref)


def _force_final_flush_failure(session, store, row) -> None:
    flush_count = 0

    def fail_second_flush(_session, _context, _instances):
        nonlocal flush_count
        flush_count += 1
        if flush_count == 2:
            raise RuntimeError("forced_final_flush_failure")

    event.listen(session, "before_flush", fail_second_flush)
    try:
        with pytest.raises(
            RuntimeError,
            match="^forced_final_flush_failure$",
        ):
            store.write(
                row,
                {
                    "reason": PLAINTEXT_MARKER,
                    "detail_json": '{"stage":"inserted"}',
                },
            )
    finally:
        event.remove(session, "before_flush", fail_second_flush)


def _encoded_payload(envelope: str) -> bytes:
    encoded = envelope.rsplit(":", maxsplit=1)[1]
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))


def _all_file_bytes(database_path: Path) -> bytes:
    data = bytearray()
    for candidate in (
        database_path,
        database_path.with_name(f"{database_path.name}-wal"),
        database_path.with_name(f"{database_path.name}-shm"),
    ):
        if candidate.exists():
            data.extend(candidate.read_bytes())
    return bytes(data)


def test_cipher_binds_table_row_column_and_version(cipher):
    ref = SensitiveFieldRef("audit_events", "17", "reason", 1)
    envelope = cipher.encrypt("operator context", ref)

    assert cipher.decrypt(envelope, ref) == "operator context"
    for wrong in (
        SensitiveFieldRef("orders", "17", "reason", 1),
        SensitiveFieldRef("audit_events", "18", "reason", 1),
        SensitiveFieldRef("audit_events", "17", "detail_json", 1),
        SensitiveFieldRef("audit_events", "17", "reason", 2),
    ):
        with pytest.raises(SensitiveDataInvalid, match="^sensitive_data_invalid"):
            cipher.decrypt(envelope, wrong)


def test_cipher_uses_exact_canonical_utf8_json_aad(cipher):
    ref = SensitiveFieldRef("audit_events", "17", "reason", 1)
    plaintext = "Unicode survives: café 東京 🔐"
    envelope = cipher.encrypt(plaintext, ref)
    payload = _encoded_payload(envelope)
    aad = (
        b'{"column":"reason","row":"17","schema":1,'
        b'"table":"audit_events"}'
    )

    assert AESGCM(KEY).decrypt(payload[:12], payload[12:], aad).decode("utf-8") == plaintext
    assert cipher.decrypt(envelope, ref) == plaintext


def test_cipher_uses_unique_96_bit_nonces(cipher):
    ref = SensitiveFieldRef("orders", "1", "approval_reason", 1)
    envelopes = {cipher.encrypt("same", ref) for _ in range(64)}
    nonces = {_encoded_payload(envelope)[:12] for envelope in envelopes}

    assert len(envelopes) == 64
    assert len(nonces) == 64
    assert all(len(nonce) == 12 for nonce in nonces)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda envelope: envelope.replace("enc:v1:", "enc:v2:", 1),
        lambda envelope: envelope + "=",
        lambda envelope: envelope + " ",
        lambda envelope: envelope.replace(":", "::", 1),
        lambda _envelope: f"enc:v1:{KEY_ID}:A",
        lambda _envelope: f"enc:v1:{KEY_ID}:not+base64url",
        lambda envelope: envelope.replace(KEY_ID, "short", 1),
        lambda envelope: envelope.replace(KEY_ID, f"{KEY_ID}!", 1),
    ],
)
def test_cipher_strictly_rejects_noncanonical_or_malformed_envelopes(
    cipher,
    mutate,
):
    ref = SensitiveFieldRef("audit_events", "17", "reason", 1)
    malformed = mutate(cipher.encrypt("operator context", ref))

    with pytest.raises(SensitiveDataInvalid, match="^sensitive_data_invalid"):
        cipher.decrypt(malformed, ref)


def test_cipher_rejects_tampering_and_unknown_valid_key_id(cipher):
    ref = SensitiveFieldRef("audit_events", "17", "reason", 1)
    envelope = cipher.encrypt("operator context", ref)
    prefix, encoded = envelope.rsplit(":", maxsplit=1)
    payload = bytearray(_encoded_payload(envelope))
    payload[-1] ^= 1
    tampered = (
        f"{prefix}:"
        + base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    )
    unknown = envelope.replace(KEY_ID, "unknown-key-2026", 1)

    with pytest.raises(SensitiveDataInvalid, match=f"key_id={KEY_ID}$"):
        cipher.decrypt(tampered, ref)
    with pytest.raises(
        SensitiveDataInvalid,
        match="key_id=unknown-key-2026$",
    ):
        cipher.decrypt(unknown, ref)
    assert encoded not in str(
        pytest.raises(
            SensitiveDataInvalid,
            cipher.decrypt,
            tampered,
            ref,
        ).value
    )


@pytest.mark.parametrize(
    ("keys", "active_key_id"),
    [
        ({"short": KEY}, "short"),
        ({KEY_ID: b"too-short"}, KEY_ID),
        ({KEY_ID: KEY}, "missing-key-2026"),
        ({f"{KEY_ID}!": KEY}, f"{KEY_ID}!"),
    ],
)
def test_cipher_rejects_invalid_key_ids_and_key_lengths_without_key_material(
    keys,
    active_key_id,
):
    secret_marker = next(iter(keys.values()))

    with pytest.raises(SensitiveDataInvalid) as captured:
        SensitiveDataCipher(keys, active_key_id=active_key_id)

    assert str(captured.value).startswith("sensitive_data_invalid")
    assert repr(secret_marker) not in str(captured.value)


def test_cipher_rejects_empty_plaintext_without_leaking_inputs(cipher, caplog):
    ref = SensitiveFieldRef("audit_events", "17", "reason", 1)

    with pytest.raises(
        SensitiveDataInvalid,
        match=f"^sensitive_data_invalid key_id={KEY_ID}$",
    ) as captured:
        cipher.encrypt("", ref)

    exposed = str(captured.value) + caplog.text
    assert "nonce" not in exposed
    assert "ciphertext" not in exposed
    assert KEY.hex() not in exposed


def test_all_crypto_failures_expose_only_stable_code_and_allowed_key_id(cipher):
    ref = SensitiveFieldRef("audit_events", "17", "reason", 1)
    plaintext = "do-not-leak-operator-context"
    envelope = cipher.encrypt(plaintext, ref)

    with pytest.raises(SensitiveDataInvalid) as captured:
        cipher.decrypt(envelope[:-1] + ("A" if envelope[-1] != "A" else "B"), ref)

    message = str(captured.value)
    assert message == f"sensitive_data_invalid key_id={KEY_ID}"
    assert plaintext not in message
    assert envelope not in message
    assert base64.urlsafe_b64encode(KEY).decode() not in message


def test_cipher_wraps_internal_dependency_failures_without_leaking_them(
    cipher,
    monkeypatch,
):
    ref = SensitiveFieldRef("audit_events", "17", "reason", 1)
    marker = "internal-crypto-failure-must-not-leak"

    def fail_urandom(_length):
        raise RuntimeError(marker)

    monkeypatch.setattr(
        "trading_assistant.security.crypto.os.urandom",
        fail_urandom,
    )
    with pytest.raises(SensitiveDataInvalid) as captured:
        cipher.encrypt("operator context", ref)

    assert str(captured.value) == f"sensitive_data_invalid key_id={KEY_ID}"
    assert marker not in str(captured.value)


def test_runtime_builder_accepts_only_validated_active_and_retained_keys():
    config = EncryptionConfig(
        active_key_id=KEY_ID,
        retained_key_ids=[RETAINED_KEY_ID],
    )
    secrets = RuntimeSecrets(
        field_encryption_keys={
            KEY_ID: SecretStr(base64.b64encode(KEY).decode("ascii")),
            RETAINED_KEY_ID: SecretStr(
                base64.b64encode(RETAINED_KEY).decode("ascii")
            ),
        }
    )
    cipher = build_sensitive_data_cipher(config, secrets)
    retained_cipher = SensitiveDataCipher(
        {RETAINED_KEY_ID: RETAINED_KEY},
        active_key_id=RETAINED_KEY_ID,
    )
    ref = SensitiveFieldRef("risk_events", "41", "reason", 1)

    current = cipher.encrypt("current", ref)
    retained = retained_cipher.encrypt("retained", ref)

    assert current.startswith(f"enc:v1:{KEY_ID}:")
    assert cipher.decrypt(retained, ref) == "retained"


def test_runtime_builder_rejects_extra_or_malformed_keys_without_leaking_them():
    config = EncryptionConfig(active_key_id=KEY_ID)
    marker = "runtime-key-material-must-not-leak"
    secrets = RuntimeSecrets(
        field_encryption_keys={
            KEY_ID: SecretStr(base64.b64encode(KEY).decode("ascii")),
            "extra-key-2026": SecretStr(marker),
        }
    )

    with pytest.raises(Exception) as captured:
        build_sensitive_data_cipher(config, secrets)

    assert marker not in str(captured.value)


def test_runtime_builder_rejects_reused_active_and_retained_key_material():
    config = EncryptionConfig(
        active_key_id=KEY_ID,
        retained_key_ids=[RETAINED_KEY_ID],
    )
    encoded = base64.b64encode(KEY).decode("ascii")
    secrets = RuntimeSecrets(
        field_encryption_keys={
            KEY_ID: SecretStr(encoded),
            RETAINED_KEY_ID: SecretStr(encoded),
        }
    )

    with pytest.raises(Exception, match="shared_key_material"):
        build_sensitive_data_cipher(config, secrets)


def test_sensitive_registry_is_exact():
    assert SENSITIVE_FIELDS == {
        "orders": {"approval_reason"},
        "audit_events": {"reason", "detail_json"},
        "proposals": {"reasoning"},
        "llm_decisions": {"prompt", "tool_calls_json", "reasoning_summary"},
        "risk_events": {"reason"},
        "analysis_reports": {"report_json"},
        "trade_plans": {"plan_json", "sized_json"},
        "circuit_breaker_state": {"reason"},
        "startup_reconciliation_state": {"reason", "evidence_json"},
        "panic_receipts": {"response_json"},
    }


def test_store_first_insert_and_update_never_send_plaintext_to_sql_or_disk(
    tmp_path,
    cipher,
):
    database_path = tmp_path / "sensitive.db"
    engine = create_db_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    statements: list[tuple[str, object]] = []

    def capture_sql(_conn, _cursor, statement, parameters, _context, _many):
        statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        with session_factory() as session:
            store = SensitiveFieldStore(session, cipher)
            row = _audit_event()
            store.write(
                row,
                {
                    "reason": PLAINTEXT_MARKER,
                    "detail_json": json.dumps({"marker": PLAINTEXT_MARKER}),
                },
            )
            assert PLAINTEXT_MARKER not in repr(session.info)
            session.commit()

            store.write(
                row,
                {"reason": f"{PLAINTEXT_MARKER}-updated"},
            )
            session.commit()
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)

    rendered_sql = repr(statements)
    assert PLAINTEXT_MARKER not in rendered_sql
    insert_parameters = [
        parameters
        for statement, parameters in statements
        if statement.lstrip().upper().startswith("INSERT INTO AUDIT_EVENTS")
    ]
    assert len(insert_parameters) == 1
    assert repr(insert_parameters[0]).count("enc:v1:") >= 2
    assert PLAINTEXT_MARKER.encode() not in _all_file_bytes(database_path)


def test_store_stages_ciphertext_then_persists_only_actual_row_bound_envelopes(
    session_factory,
    cipher,
):
    seen_refs: list[SensitiveFieldRef] = []

    class RecordingCipher:
        def encrypt(self, plaintext, ref):
            seen_refs.append(ref)
            return cipher.encrypt(plaintext, ref)

        def decrypt(self, envelope, ref):
            return cipher.decrypt(envelope, ref)

    with session_factory() as session:
        store = SensitiveFieldStore(session, RecordingCipher())
        row = _audit_event()
        store.write(
            row,
            {
                "reason": PLAINTEXT_MARKER,
                "detail_json": '{"safe":"value"}',
            },
        )

        assert row.id is not None
        assert any(ref.row.startswith("pending:") for ref in seen_refs)
        assert {ref.row for ref in seen_refs[-2:]} == {str(row.id)}
        assert store.read(row, "reason") == PLAINTEXT_MARKER
        assert store.read(row, "detail_json") == '{"safe":"value"}'
        session.commit()


def test_store_reads_require_exact_mapped_row_and_reject_swaps(
    session_factory,
    cipher,
):
    with session_factory() as session:
        store = SensitiveFieldStore(session, cipher)
        first = _audit_event()
        second = _audit_event()
        second.request_id = "request-2"
        store.write(
            first,
            {"reason": "first", "detail_json": '{"row":1}'},
        )
        store.write(
            second,
            {"reason": "second", "detail_json": '{"row":2}'},
        )
        session.commit()

        first.reason, second.reason = second.reason, first.reason
        with pytest.raises(SensitiveDataInvalid):
            store.read(first, "reason")
        with pytest.raises(SensitiveDataInvalid):
            store.read(second, "reason")

        first.reason = first.detail_json
        with pytest.raises(SensitiveDataInvalid):
            store.read(first, "reason")


def test_manual_plaintext_flush_and_commit_fail_before_sql(
    session_factory,
    cipher,
):
    with session_factory() as session:
        install_sensitive_field_guards(session, cipher)
        row = _audit_event()
        row.reason = PLAINTEXT_MARKER
        row.detail_json = "{}"
        session.add(row)

        with pytest.raises(
            PlaintextSensitiveField,
            match="^plaintext_sensitive_field$",
        ):
            session.flush()
        session.rollback()

        row = _audit_event()
        row.reason = PLAINTEXT_MARKER
        row.detail_json = "{}"
        session.add(row)
        with pytest.raises(
            PlaintextSensitiveField,
            match="^plaintext_sensitive_field$",
        ):
            session.commit()


@pytest.mark.parametrize("explicit_none", [False, True])
def test_guard_rejects_omitted_or_none_audit_defaults_before_insert(
    cipher,
    explicit_none,
):
    engine = create_db_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    inserts: list[tuple[str, object]] = []

    def capture_sql(_conn, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("INSERT INTO AUDIT_EVENTS"):
            inserts.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        with session_factory() as session:
            install_sensitive_field_guards(session, cipher)
            row = _audit_event()
            if explicit_none:
                row.reason = None
                row.detail_json = None
            session.add(row)

            with pytest.raises(
                PlaintextSensitiveField,
                match="^plaintext_sensitive_field$",
            ):
                session.commit()
            session.rollback()

            assert session.scalar(
                select(func.count()).select_from(AuditEvent)
            ) == 0
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)

    assert inserts == []
    assert PLAINTEXT_MARKER not in repr(inserts)


@pytest.mark.parametrize(
    "instance_factory",
    [
        pytest.param(
            lambda: Order(
                id=1001,
                idempotency_key="missing-order-reason",
                ticker="AAPL",
                side="buy",
                order_type="market",
            ),
            id="order-client-default",
        ),
        pytest.param(
            lambda: Proposal(
                id=1002,
                order_id=1002,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            ),
            id="proposal-client-default",
        ),
        pytest.param(
            lambda: LLMDecision(id=1003),
            id="llm-mixed-required-and-client-defaults",
        ),
        pytest.param(
            lambda: RiskEvent(id=1004, event_type="rejection"),
            id="risk-required",
        ),
        pytest.param(
            lambda: AnalysisReportRow(
                id=1005,
                symbol="AAPL",
                as_of=datetime.now(timezone.utc),
                action="hold",
                confidence=Decimal("0.5"),
                analyst_version="v1",
            ),
            id="analysis-required",
        ),
        pytest.param(
            lambda: TradePlanRow(
                id=1006,
                symbol="AAPL",
                action="hold",
            ),
            id="plan-required",
        ),
        pytest.param(
            lambda: CircuitBreakerState(
                scope_key="missing-breaker-reason",
                kind="global",
            ),
            id="breaker-client-default",
        ),
        pytest.param(
            lambda: StartupReconciliationState(
                broker="missing-startup-evidence",
            ),
            id="startup-client-defaults",
        ),
    ],
)
def test_guard_rejects_missing_sensitive_values_across_registered_models(
    engine,
    session_factory,
    cipher,
    instance_factory,
):
    inserts: list[tuple[str, object]] = []

    def capture_sql(_conn, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("INSERT"):
            inserts.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        with session_factory() as session:
            install_sensitive_field_guards(session, cipher)
            session.add(instance_factory())

            with pytest.raises(
                PlaintextSensitiveField,
                match="^plaintext_sensitive_field$",
            ):
                session.commit()
            session.rollback()
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)

    assert inserts == []


def test_guard_allows_truly_nullable_no_default_sensitive_none(
    session_factory,
    cipher,
):
    now = datetime.now(timezone.utc)
    with session_factory() as session:
        install_sensitive_field_guards(session, cipher)
        row = PanicReceipt(
            account_scope="paper:test",
            request_id="nullable-sensitive-none",
            state="started",
            response_json=None,
            expires_at=now + timedelta(minutes=5),
        )
        session.add(row)
        session.commit()

        assert row.response_json is None


def test_guard_rejects_forged_pending_envelope_and_session_metadata(
    session_factory,
    cipher,
):
    pending_reason = SensitiveFieldRef(
        "audit_events",
        "pending:forged-token-2026",
        "reason",
        1,
    )
    pending_detail = SensitiveFieldRef(
        "audit_events",
        "pending:forged-token-2026",
        "detail_json",
        1,
    )
    with session_factory() as session:
        install_sensitive_field_guards(session, cipher)
        session.info["_sensitive_field_staging"] = {
            "forged": "metadata",
        }
        row = _audit_event()
        row.reason = cipher.encrypt("forged", pending_reason)
        row.detail_json = cipher.encrypt("forged", pending_detail)
        session.add(row)

        with pytest.raises(PlaintextSensitiveField):
            session.flush()


def test_failure_between_flushes_cannot_commit_and_rollback_cleans_staging(
    session_factory,
    cipher,
):
    failing_cipher = _FailFinalCipher(cipher)
    with session_factory() as session:
        store = SensitiveFieldStore(session, failing_cipher)
        with pytest.raises(RuntimeError, match="simulated_final"):
            store.write(
                _audit_event(),
                {
                    "reason": PLAINTEXT_MARKER,
                    "detail_json": '{"stage":"only"}',
                },
            )

        with pytest.raises(
            PlaintextSensitiveField,
            match="^plaintext_sensitive_field$",
        ):
            session.commit()
        session.rollback()

        failing_cipher.fail_final = False
        row = _audit_event()
        row.request_id = "request-after-rollback"
        store.write(
            row,
            {
                "reason": "after rollback",
                "detail_json": '{"stage":"cleared"}',
            },
        )
        session.commit()
        assert session.scalar(select(AuditEvent).where(AuditEvent.id == row.id))


def test_final_flush_failure_latch_survives_detach_and_prevents_commit(
    tmp_path,
    cipher,
):
    database_path = tmp_path / "failed-final-flush.db"
    engine = create_db_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    flush_count = 0
    statements: list[tuple[str, object]] = []

    def capture_sql(_conn, _cursor, statement, parameters, _context, _many):
        statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        with session_factory() as session:
            def fail_second_flush(_session, _context, _instances):
                nonlocal flush_count
                flush_count += 1
                if flush_count == 2:
                    raise RuntimeError("forced_final_flush_failure")

            event.listen(session, "before_flush", fail_second_flush)
            store = SensitiveFieldStore(session, cipher)
            row = _audit_event()

            with pytest.raises(
                RuntimeError,
                match="^forced_final_flush_failure$",
            ):
                store.write(
                    row,
                    {
                        "reason": PLAINTEXT_MARKER,
                        "detail_json": '{"stage":"inserted"}',
                    },
                )

            session.expire(row)
            session.expunge(row)
            make_transient(row)
            with pytest.raises(
                PlaintextSensitiveField,
                match="^plaintext_sensitive_field$",
            ):
                session.commit()
            session.rollback()
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)

    inserts = [
        parameters
        for statement, parameters in statements
        if statement.lstrip().upper().startswith("INSERT INTO AUDIT_EVENTS")
    ]
    updates = [
        parameters
        for statement, parameters in statements
        if statement.lstrip().upper().startswith("UPDATE AUDIT_EVENTS")
    ]
    assert len(inserts) == 1
    assert repr(inserts[0]).count("enc:v1:") >= 2
    assert updates == []
    assert PLAINTEXT_MARKER not in repr(statements)
    assert PLAINTEXT_MARKER.encode() not in _all_file_bytes(database_path)
    with session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(AuditEvent)
        ) == 0


@pytest.mark.parametrize(
    "retry_kind",
    ["same", "transient-same", "new", "manual-final"],
)
@pytest.mark.parametrize(
    "nested_rollback",
    [False, True],
    ids=["outer-active", "after-savepoint-rollback"],
)
def test_incomplete_staging_poisons_every_retry_until_outer_rollback(
    tmp_path,
    cipher,
    retry_kind,
    nested_rollback,
):
    database_path = tmp_path / (
        f"poisoned-{retry_kind}-{nested_rollback}.db"
    )
    engine = create_db_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    statements: list[tuple[str, object]] = []

    def capture_sql(_conn, _cursor, statement, parameters, _context, _many):
        if (
            statement.lstrip().upper().startswith("INSERT INTO AUDIT_EVENTS")
            or statement.lstrip().upper().startswith("UPDATE AUDIT_EVENTS")
        ):
            statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        with session_factory() as session:
            store = SensitiveFieldStore(session, cipher)
            if nested_rollback:
                session.begin()
                nested = session.begin_nested()
            original = _audit_event()
            original.request_id = (
                f"poisoned-original-{retry_kind}-{nested_rollback}"
            )
            _force_final_flush_failure(session, store, original)
            staging = sensitive_field_module._STAGING_STATES[session]
            staged_before_retry = dict(staging.staged)

            if nested_rollback:
                nested.rollback()

            writes_before_retry = len(statements)
            retry = _audit_event()
            retry.request_id = (
                f"poisoned-retry-{retry_kind}-{nested_rollback}"
            )

            if retry_kind == "same":
                attempt = lambda: store.write(
                    original,
                    {"reason": "retry", "detail_json": '{"retry":true}'},
                )
            elif retry_kind == "transient-same":
                if sa_inspect(original).session is session:
                    session.expunge(original)
                if not sa_inspect(original).transient:
                    make_transient(original)
                assert sa_inspect(original).transient
                attempt = lambda: store.write(
                    original,
                    {"reason": "retry", "detail_json": '{"retry":true}'},
                )
            elif retry_kind == "new":
                assert sa_inspect(retry).transient
                attempt = lambda: store.write(
                    retry,
                    {"reason": "retry", "detail_json": '{"retry":true}'},
                )
            else:
                original.reason = cipher.encrypt(
                    "manual final",
                    SensitiveFieldRef(
                        "audit_events",
                        str(original.id),
                        "reason",
                        1,
                    ),
                )
                original.detail_json = cipher.encrypt(
                    '{"manual":true}',
                    SensitiveFieldRef(
                        "audit_events",
                        str(original.id),
                        "detail_json",
                        1,
                    ),
                )
                if sa_inspect(original).transient:
                    session.add(original)
                attempt = session.flush

            with pytest.raises(
                PlaintextSensitiveField,
                match="^plaintext_sensitive_field$",
            ):
                attempt()

            assert staging.staged == staged_before_retry
            assert len(statements) == writes_before_retry
            if retry_kind == "new":
                assert sa_inspect(retry).transient
                assert retry.reason is None
                assert retry.detail_json is None

            with pytest.raises(
                PlaintextSensitiveField,
                match="^plaintext_sensitive_field$",
            ):
                session.commit()
            assert staging.staged == staged_before_retry
            assert len(statements) == writes_before_retry

            session.rollback()
            assert staging.staged == {}

            clean = _audit_event()
            clean.request_id = (
                f"clean-retry-{retry_kind}-{nested_rollback}"
            )
            store.write(
                clean,
                {
                    "reason": "clean retry",
                    "detail_json": '{"clean":true}',
                },
            )
            session.commit()
            assert store.read(clean, "reason") == "clean retry"
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)

    with session_factory() as session:
        request_ids = session.scalars(
            select(AuditEvent.request_id)
        ).all()
    assert request_ids == [
        f"clean-retry-{retry_kind}-{nested_rollback}"
    ]
    assert PLAINTEXT_MARKER not in repr(statements)
    assert PLAINTEXT_MARKER.encode() not in _all_file_bytes(database_path)


@pytest.mark.parametrize("lifecycle", ["close", "reset"])
@pytest.mark.parametrize(
    "inside_savepoint",
    [False, True],
    ids=["outer", "savepoint"],
)
def test_close_and_reset_clear_latch_only_after_outer_transaction_end(
    session_factory,
    cipher,
    lifecycle,
    inside_savepoint,
):
    session = session_factory()
    try:
        store = SensitiveFieldStore(session, cipher)
        if inside_savepoint:
            session.begin()
            nested = session.begin_nested()
        original = _audit_event()
        original.request_id = f"{lifecycle}-{inside_savepoint}-original"
        _force_final_flush_failure(session, store, original)
        staging = sensitive_field_module._STAGING_STATES[session]
        assert staging.staged

        getattr(session, lifecycle)()

        assert not session.in_transaction()
        assert staging.staged == {}
        if inside_savepoint:
            assert not nested.is_active

        clean = _audit_event()
        clean.request_id = f"{lifecycle}-{inside_savepoint}-clean"
        store.write(
            clean,
            {
                "reason": "clean lifecycle retry",
                "detail_json": '{"clean":true}',
            },
        )
        session.commit()
        assert store.read(clean, "reason") == "clean lifecycle retry"
        assert session.scalars(
            select(AuditEvent.request_id)
        ).all() == [clean.request_id]
    finally:
        session.close()


def test_nested_transaction_close_preserves_poison_until_session_close(
    session_factory,
    cipher,
):
    session = session_factory()
    try:
        store = SensitiveFieldStore(session, cipher)
        session.begin()
        nested = session.begin_nested()
        original = _audit_event()
        original.request_id = "nested-close-original"
        _force_final_flush_failure(session, store, original)
        staging = sensitive_field_module._STAGING_STATES[session]
        staged_before_nested = dict(staging.staged)

        nested.close()

        assert session.in_transaction()
        assert staging.staged == staged_before_nested
        with pytest.raises(
            PlaintextSensitiveField,
            match="^plaintext_sensitive_field$",
        ):
            store.write(
                _audit_event(),
                {
                    "reason": "must reject",
                    "detail_json": '{"reject":true}',
                },
            )
        with pytest.raises(PlaintextSensitiveField):
            session.commit()

        session.close()
        assert staging.staged == {}

        clean = _audit_event()
        clean.request_id = "nested-close-clean"
        store.write(
            clean,
            {
                "reason": "clean after close",
                "detail_json": '{"clean":true}',
            },
        )
        session.commit()
        assert session.scalars(
            select(AuditEvent.request_id)
        ).all() == ["nested-close-clean"]
    finally:
        session.close()


def test_scoped_idempotent_guards_do_not_retain_closed_sessions(
    session_factory,
    cipher,
):
    gc.collect()
    unrelated_session = session_factory()
    install_sensitive_field_guards(unrelated_session, cipher)
    unrelated_reference = weakref.ref(unrelated_session)

    session = session_factory()
    install_sensitive_field_guards(session, cipher)
    install_sensitive_field_guards(session, cipher)
    store = SensitiveFieldStore(session, cipher)
    session_reference = weakref.ref(session)

    session.close()
    del store
    del session
    gc.collect()

    try:
        assert session_reference() is None
        assert unrelated_session in sensitive_field_module._STAGING_STATES
        assert (
            unrelated_session
            in sensitive_field_module._GUARD_INSTALLATIONS
        )
    finally:
        unrelated_session.close()
        del unrelated_session
    gc.collect()

    assert unrelated_reference() is None


def test_nested_rollback_cannot_clear_outer_staging_latch(
    session_factory,
    cipher,
):
    failing_cipher = _FailFinalCipher(cipher)
    with session_factory() as session:
        store = SensitiveFieldStore(session, failing_cipher)
        with pytest.raises(RuntimeError, match="simulated_final"):
            store.write(
                _audit_event(),
                {
                    "reason": "outer staged",
                    "detail_json": '{"scope":"outer"}',
                },
            )

        nested = session.begin_nested()
        nested.rollback()
        with pytest.raises(
            PlaintextSensitiveField,
            match="^plaintext_sensitive_field$",
        ):
            session.commit()

        session.rollback()
        failing_cipher.fail_final = False
        retry = _audit_event()
        retry.request_id = "outer-rollback-clean-retry"
        store.write(
            retry,
            {
                "reason": "clean retry",
                "detail_json": '{"retry":true}',
            },
        )
        session.commit()
        assert store.read(retry, "reason") == "clean retry"


def test_savepoint_staging_requires_full_outer_rollback_before_retry(
    session_factory,
    cipher,
):
    failing_cipher = _FailFinalCipher(cipher)
    with session_factory() as session:
        store = SensitiveFieldStore(session, failing_cipher)
        session.begin()
        nested = session.begin_nested()

        with pytest.raises(RuntimeError, match="simulated_final"):
            store.write(
                _audit_event(),
                {
                    "reason": "savepoint staged",
                    "detail_json": '{"scope":"savepoint"}',
                },
            )
        nested.rollback()
        with pytest.raises(
            PlaintextSensitiveField,
            match="^plaintext_sensitive_field$",
        ):
            session.commit()

        session.rollback()
        failing_cipher.fail_final = False
        retry = _audit_event()
        retry.request_id = "savepoint-full-rollback-retry"
        store.write(
            retry,
            {
                "reason": "clean savepoint retry",
                "detail_json": '{"retry":true}',
            },
        )
        session.commit()
        assert store.read(retry, "reason") == "clean savepoint retry"


def test_guard_installation_is_idempotent_for_one_session(
    session_factory,
    cipher,
):
    with session_factory() as session:
        install_sensitive_field_guards(session, cipher)
        install_sensitive_field_guards(session, cipher)
        store = SensitiveFieldStore(session, cipher)
        row = _audit_event()
        store.write(
            row,
            {"reason": "idempotent", "detail_json": '{"ok":true}'},
        )
        session.commit()
        assert store.read(row, "reason") == "idempotent"


def test_staging_registry_contains_no_cipher_or_key_material(
    session_factory,
    cipher,
):
    with session_factory() as session:
        install_sensitive_field_guards(session, cipher)
        state = sensitive_field_module._STAGING_STATES[session]

        assert not hasattr(state, "cipher")
        assert KEY not in repr(vars(state)).encode()
