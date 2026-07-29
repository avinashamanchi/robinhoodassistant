"""Task 11 review round 3 regression probes.

Every transport, broker, and certificate interaction in this module is a fake
or a hermetic local fixture.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def test_preflight_reconciliation_uses_one_read_only_probe():
    from trading_assistant import preflight

    calls: list[str] = []

    class ReadOnlyProbe:
        def inspect_reconciliation(self):
            calls.append("inspect")
            return SimpleNamespace(
                orders_match=True,
                positions_match=True,
                broker_open_order_count=0,
                local_open_order_count=0,
                drift_symbols=(),
            )

        def __getattr__(self, name):
            raise AssertionError(f"unexpected mutable capability: {name}")

    result = preflight._reconciliation(ReadOnlyProbe())

    assert result.status == preflight.PASS
    assert result.detail == "orders and positions match"
    assert calls == ["inspect"]


def test_preflight_builder_constructs_only_narrow_read_only_service(
    app_config,
    monkeypatch,
):
    from trading_assistant import bootstrap
    from trading_assistant.security.secrets import RuntimeSecrets

    secrets = RuntimeSecrets(
        app_api_token="round3-preflight-token",
        alpaca_api_key="round3-alpaca-key",
        alpaca_secret_key="round3-alpaca-secret",
        database_url="sqlite:///round3-unused.db",
    )
    session_factory = object()
    runtime = SimpleNamespace(
        engine=object(),
        session_factory=session_factory,
    )
    broker = object()
    service = object()
    observed: list[tuple[str, object]] = []

    monkeypatch.setattr(
        bootstrap,
        "require_configured_role_origins",
        lambda _config, role: observed.append(("origins", role)),
    )
    monkeypatch.setattr(bootstrap, "_guard_runtime", lambda *_args: None)
    monkeypatch.setattr(
        bootstrap,
        "prepare_database_runtime",
        lambda _secrets, *, runtime_role: (
            observed.append(("database", runtime_role)) or runtime
        ),
    )
    monkeypatch.setattr(
        bootstrap,
        "build_broker",
        lambda _config, _secrets, *, runtime_role: (
            observed.append(("broker", runtime_role)) or broker
        ),
    )
    monkeypatch.setattr(
        bootstrap,
        "_arm_production_paper_broker",
        lambda selected: observed.append(("paper", selected)),
    )
    monkeypatch.setattr(
        bootstrap,
        "ReadOnlyPreflightService",
        lambda selected_broker, factory: (
            observed.append(
                ("read_only_service", (selected_broker, factory))
            )
            or service
        ),
        raising=False,
    )
    for forbidden in (
        "TradingService",
        "build_clock",
        "build_sensitive_data_cipher",
        "bind_sensitive_cipher",
        "build_quarantine_summarizer",
    ):
        monkeypatch.setattr(
            bootstrap,
            forbidden,
            lambda *_args, _name=forbidden, **_kwargs: pytest.fail(
                f"preflight constructed forbidden capability: {_name}"
            ),
        )

    container = bootstrap.build_preflight_service(app_config, secrets)

    assert container.service is service
    assert observed == [
        ("origins", "preflight"),
        ("database", "preflight"),
        ("broker", "preflight"),
        ("paper", broker),
        ("read_only_service", (broker, session_factory)),
    ]


def test_read_only_preflight_probe_performs_no_database_or_broker_mutation(
    make_service,
):
    from sqlalchemy import event

    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.ops.preflight_probe import (
        ReadOnlyPreflightService,
    )

    class ReadOnlyBroker(MockBroker):
        def submit_order(self, _order):
            raise AssertionError("preflight submitted an order")

        def cancel_order(self, _order_id):
            raise AssertionError("preflight canceled an order")

    broker = ReadOnlyBroker()
    mutable_service = make_service(broker=broker)
    engine = mutable_service.session_factory.kw["bind"]
    writes: list[str] = []

    def record_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        verb = statement.lstrip().partition(" ")[0].upper()
        if verb in {"DELETE", "INSERT", "REPLACE", "UPDATE"}:
            writes.append(verb)

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        probe = ReadOnlyPreflightService(
            broker,
            mutable_service.session_factory,
        )
        snapshot = probe.inspect_reconciliation()
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert snapshot.orders_match is True
    assert snapshot.positions_match is True
    assert writes == []
    assert {
        name
        for name in dir(type(probe))
        if not name.startswith("_")
    } == {"inspect_reconciliation"}


def test_watchdog_has_no_provider_egress(app_config):
    from trading_assistant.security.outbound import (
        OUTBOUND_ORIGIN_MANIFEST,
        origins_for_role,
    )

    assert origins_for_role(app_config, "watchdog") == frozenset()
    assert all(
        "watchdog" not in rule.roles
        for rule in OUTBOUND_ORIGIN_MANIFEST
    )


def test_watchdog_secret_role_was_already_database_only(app_config):
    from trading_assistant.security.secrets import _required_fields

    # Counterexample to the review's secret-capability claim: this was already
    # narrow before round 3 and must stay that way.
    assert _required_fields("watchdog", app_config) == ("database_url",)


def test_setup_local_tls_uses_repository_declared_python_runner():
    source = Path("scripts/setup-local-tls.sh").read_text(encoding="utf-8")

    assert "\nuv run python -m trading_assistant.ops.tls inspect\n" in source
    assert "\npython -m trading_assistant.ops.tls inspect\n" not in source


def test_computed_query_keys_are_rejected_before_guarded_transport():
    from trading_assistant.security.outbound import (
        OutboundOriginDenied,
        OutboundPolicy,
        _validated_query_params,
    )

    key = "api_" + "key"
    with pytest.raises(OutboundOriginDenied):
        _validated_query_params({key: "fixture"})
    with pytest.raises(OutboundOriginDenied):
        OutboundPolicy("https://api.example.test").assert_url(
            "https://api.example.test/path?" + key + "=fixture"
        )
