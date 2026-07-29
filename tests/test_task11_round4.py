"""Task 11 review round 4 regression probes.

All secret, broker, and database dependencies in this module are injected
fakes or temporary fixtures.
"""

from __future__ import annotations

import base64
from decimal import Decimal
import hashlib
from pathlib import Path

import pytest

from trading_assistant.broker.models import (
    OrderResult,
    OrderStatus,
)
from trading_assistant.db.models import (
    FILL_RECONCILIATION_QUARANTINED,
    FILL_RECONCILIATION_SUPERSEDED,
    Fill,
    Order,
)
from trading_assistant.ops.preflight_probe import ReadOnlyPreflightService
from trading_assistant.security.secrets import (
    KEYCHAIN_SERVICE,
    MacOSKeychainSecretProvider,
    load_role_secrets,
    secret_is_set,
)
from trading_assistant.security.sensitive_fields import persist_sensitive


def _key(label: str) -> str:
    return base64.b64encode(hashlib.sha256(label.encode()).digest()).decode()


class RecordingKeyring:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.accounts: list[str] = []

    def get_password(self, service: str, username: str) -> str | None:
        assert service == KEYCHAIN_SERVICE
        self.accounts.append(username)
        return self.values.get(username)

    def set_password(
        self,
        service: str,
        username: str,
        password: str,
    ) -> None:
        raise AssertionError("round 4 secret fixture attempted a write")

    def delete_password(self, service: str, username: str) -> None:
        raise AssertionError("round 4 secret fixture attempted a delete")


def _role_secret_values(app_config) -> dict[str, str]:
    values = {
        "anthropic_api_key": "round4-anthropic-fixture",
        "gemini_api_key": "round4-gemini-fixture",
        "groq_api_key": "round4-groq-fixture",
        "openrouter_api_key": "",
        "app_api_token": "R4!vN3#mQ7$pL2&tX9-zC5_kW4sD6gH8",
        "alpaca_api_key": "round4-paper-key",
        "alpaca_secret_key": "round4-paper-secret",
        "database_url": "sqlite:///round4-role-fixture.db",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "candidate_signing_key": _key("round4-candidate"),
        "backup_encryption_key": _key("round4-backup"),
        "live_trading_confirm": "",
    }
    for key_id in (
        app_config.encryption.active_key_id,
        *app_config.encryption.retained_key_ids,
    ):
        values[f"field-encryption/{key_id}"] = _key(
            f"round4-field:{key_id}"
        )
    return values


def test_watchdog_keychain_load_requests_and_receives_only_database_url(
    app_config,
):
    backend = RecordingKeyring(_role_secret_values(app_config))

    loaded = load_role_secrets(
        "watchdog",
        config=app_config,
        provider=MacOSKeychainSecretProvider(backend=backend),
    )

    assert backend.accounts == ["database_url"]
    assert secret_is_set(loaded.database_url)
    assert loaded.field_encryption_keys == {}
    assert all(
        not secret_is_set(getattr(loaded, field))
        for field in (
            "anthropic_api_key",
            "gemini_api_key",
            "groq_api_key",
            "openrouter_api_key",
            "app_api_token",
            "alpaca_api_key",
            "alpaca_secret_key",
            "telegram_bot_token",
            "telegram_chat_id",
            "candidate_signing_key",
            "backup_encryption_key",
            "live_trading_confirm",
        )
    )


@pytest.mark.parametrize(
    "role",
    [
        "app",
        "backup",
        "daemon",
        "mcp",
        "migration",
        "paper-drill",
        "preflight",
        "safety-drill",
        "validate-analyst",
    ],
)
def test_other_keychain_roles_keep_exact_required_secret_projection(
    app_config,
    role,
):
    from trading_assistant.security.secrets import _required_fields

    backend = RecordingKeyring(_role_secret_values(app_config))

    loaded = load_role_secrets(
        role,
        config=app_config,
        provider=MacOSKeychainSecretProvider(backend=backend),
    )

    expected_simple = {
        *_required_fields(role, app_config),
        "candidate_signing_key",
        "backup_encryption_key",
    }
    expected_accounts = {
        *expected_simple,
        *(
            f"field-encryption/{key_id}"
            for key_id in (
                app_config.encryption.active_key_id,
                *app_config.encryption.retained_key_ids,
            )
        ),
    }
    assert set(backend.accounts) == expected_accounts
    assert len(backend.accounts) == len(expected_accounts)
    assert all(secret_is_set(getattr(loaded, field)) for field in expected_simple)
    assert set(loaded.field_encryption_keys) == {
        app_config.encryption.active_key_id,
        *app_config.encryption.retained_key_ids,
    }


class SnapshotBroker:
    def __init__(
        self,
        *,
        orders: list[OrderResult] | None = None,
        positions: list[object] | None = None,
    ) -> None:
        self.orders = list(orders or [])
        self.positions = list(positions or [])

    def get_open_orders(self) -> list[OrderResult]:
        return list(self.orders)

    def get_positions(self) -> list[object]:
        return list(self.positions)


def _persist_open_order(
    session_factory,
    *,
    client_id: str,
    broker_id: str,
    status: OrderStatus = OrderStatus.SUBMITTED,
    qty: Decimal | None = Decimal("2"),
) -> None:
    with session_factory() as session:
        persist_sensitive(
            session,
            Order(
                idempotency_key=client_id,
                ticker="AAPL",
                side="buy",
                order_type="market",
                qty=qty,
                status=status.value,
                broker_order_id=broker_id,
            ),
            {"approval_reason": "round 4 fixture"},
        )
        session.commit()


def test_preflight_excludes_superseded_fill_tombstones_from_arithmetic(
    session_factory,
):
    with session_factory() as session:
        session.add(
            Fill(
                ticker="AAPL",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("100"),
                broker_fill_id=None,
                reconciliation_state=FILL_RECONCILIATION_SUPERSEDED,
            )
        )
        session.commit()

    snapshot = ReadOnlyPreflightService(
        SnapshotBroker(),
        session_factory,
    ).inspect_reconciliation()

    assert snapshot.positions_match is True
    assert snapshot.drift_symbols == ()


def test_preflight_still_fails_closed_for_quarantined_fill(
    session_factory,
):
    with session_factory() as session:
        session.add(
            Fill(
                ticker="AAPL",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("100"),
                broker_fill_id=None,
                reconciliation_state=FILL_RECONCILIATION_QUARANTINED,
            )
        )
        session.commit()

    snapshot = ReadOnlyPreflightService(
        SnapshotBroker(),
        session_factory,
    ).inspect_reconciliation()

    assert snapshot.positions_match is False
    assert snapshot.drift_symbols == ("AAPL",)


def test_preflight_rejects_duplicate_remote_broker_order_ids(session_factory):
    for client_id in ("round4-client-1", "round4-client-2"):
        _persist_open_order(
            session_factory,
            client_id=client_id,
            broker_id="round4-duplicate-broker-id",
        )
    broker = SnapshotBroker(
        orders=[
            OrderResult(
                client_id,
                "round4-duplicate-broker-id",
                OrderStatus.SUBMITTED,
                filled_qty=Decimal("0"),
                ticker="AAPL",
            )
            for client_id in ("round4-client-1", "round4-client-2")
        ]
    )

    snapshot = ReadOnlyPreflightService(
        broker,
        session_factory,
    ).inspect_reconciliation()

    assert snapshot.orders_match is False


@pytest.mark.parametrize(
    ("status", "order_qty", "filled_qty"),
    [
        (OrderStatus.SUBMITTED, Decimal("2"), Decimal("NaN")),
        (OrderStatus.SUBMITTED, Decimal("2"), Decimal("-1")),
        (OrderStatus.SUBMITTED, Decimal("2"), Decimal("1")),
        (OrderStatus.PARTIALLY_FILLED, Decimal("2"), Decimal("0")),
        (OrderStatus.PARTIALLY_FILLED, Decimal("2"), Decimal("2")),
        (OrderStatus.PARTIALLY_FILLED, Decimal("2"), Decimal("3")),
        (OrderStatus.SUBMITTED, None, Decimal("0")),
    ],
    ids=(
        "nan",
        "negative",
        "submitted-nonzero",
        "partial-zero",
        "partial-equals-qty",
        "partial-exceeds-qty",
        "missing-local-qty",
    ),
)
def test_preflight_rejects_malformed_remote_filled_quantity_truth(
    session_factory,
    status,
    order_qty,
    filled_qty,
):
    _persist_open_order(
        session_factory,
        client_id="round4-client",
        broker_id="round4-broker",
        status=status,
        qty=order_qty,
    )
    broker = SnapshotBroker(
        orders=[
            OrderResult(
                "round4-client",
                "round4-broker",
                status,
                filled_qty=filled_qty,
                ticker="AAPL",
            )
        ]
    )

    snapshot = ReadOnlyPreflightService(
        broker,
        session_factory,
    ).inspect_reconciliation()

    assert snapshot.orders_match is False


def test_preflight_already_rejects_empty_remote_broker_id(session_factory):
    """Retain the hermetic counterexample for the already-closed subclaim."""

    _persist_open_order(
        session_factory,
        client_id="round4-client",
        broker_id="round4-local-broker",
    )
    broker = SnapshotBroker(
        orders=[
            OrderResult(
                "round4-client",
                "",
                OrderStatus.SUBMITTED,
                filled_qty=Decimal("0"),
                ticker="AAPL",
            )
        ]
    )

    snapshot = ReadOnlyPreflightService(
        broker,
        session_factory,
    ).inspect_reconciliation()

    assert snapshot.orders_match is False


@pytest.mark.parametrize(
    "relative",
    (
        "docs/RUNBOOK.md",
        "docs/ops/README.md",
        "scripts/launchd/README.md",
    ),
)
def test_preflight_docs_describe_no_trading_table_dml(relative):
    source = Path(relative).read_text(encoding="utf-8")

    assert "local SQL `SELECT`s" not in source
    assert "no trading-table DML" in source
