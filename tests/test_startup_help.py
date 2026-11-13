"""Startup failures name a stable reason and the next step, never a secret."""

from __future__ import annotations

import logging

import pytest

from trading_assistant import logging as runtime_logging
from trading_assistant.bootstrap import StartupInstallationBlocked
from trading_assistant.db.schema import SchemaOutOfDate
from trading_assistant.security.secrets import SecretUnavailable
from trading_assistant.startup_help import failure_code, remedy


@pytest.mark.parametrize(
    ("error", "code", "hint"),
    [
        (StartupInstallationBlocked("database_not_chosen"), "database_not_chosen", "choose-database"),
        (StartupInstallationBlocked("database_identity_changed"), "database_identity_changed", "--replace"),
        (StartupInstallationBlocked("installation_not_designated"), "installation_not_designated", "designate"),
        (SchemaOutOfDate("old"), "schema_out_of_date", "db.migrate upgrade"),
        (SecretUnavailable("alpaca_api_key", "missing"), "missing", "secrets audit"),
    ],
    ids=["no-database", "replaced-database", "undesignated", "old-schema", "missing-secret"],
)
def test_known_failures_have_a_code_and_a_next_step(error, code, hint):
    assert failure_code(error) == code
    assert hint in remedy(code)


def test_a_code_that_is_not_value_free_falls_back_to_the_class_name():
    class Odd(RuntimeError):
        stable_code = "/Users/someone/secret path"

    assert failure_code(Odd()) == "Odd"
    assert "runtime.log" in remedy("Odd")


def test_runtime_startup_logs_the_reason_and_tells_the_operator(monkeypatch, caplog, capsys):
    monkeypatch.setattr(runtime_logging, "configure_runtime_logging", lambda _role, _secrets: None)

    with caplog.at_level(logging.ERROR, logger="trading_assistant.startup"):
        with pytest.raises(StartupInstallationBlocked):
            with runtime_logging.runtime_startup("app", object()):
                raise StartupInstallationBlocked("database_not_chosen")

    by_logger = {(record.name, record.getMessage()) for record in caplog.records}
    assert ("trading_assistant.startup", "startup_failed role=app") in by_logger
    assert ("trading_assistant.startup.reason", "startup_refused role=app code=database_not_chosen") in by_logger
    assert "app did not start: database_not_chosen. Next: Choose the runtime database" in capsys.readouterr().err
