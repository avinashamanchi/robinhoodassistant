"""Operational backups and the explicit Alpaca paper order drill."""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from decimal import Decimal

import pytest

from trading_assistant.config import BrokerKind, TradingMode
from trading_assistant.ops.backup import backup_database
from trading_assistant.ops.paper_drill import PaperDrillError, run_paper_drill


def test_online_backup_is_valid_and_rotates_only_matching_old_files(tmp_path):
    source = tmp_path / "live.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('preserved')")
        connection.commit()

    destination = tmp_path / "backups"
    destination.mkdir()
    old_match = destination / "trading-assistant-20000101T000000Z.sqlite3"
    old_match.write_bytes(b"old")
    unrelated = destination / "keep-me.sqlite3"
    unrelated.write_bytes(b"unrelated")
    old_time = time.time() - 30 * 86400
    os.utime(old_match, (old_time, old_time))
    os.utime(unrelated, (old_time, old_time))

    created = backup_database(source, destination, retention_days=14)

    with sqlite3.connect(created) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone() == (
            "preserved",
        )
    assert not old_match.exists()
    assert unrelated.exists()
    assert list(destination.glob("*.tmp*")) == []
    assert not created.with_name(f"{created.name}-wal").exists()
    assert not created.with_name(f"{created.name}-shm").exists()
    assert stat.S_IMODE(created.stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700


def test_paper_drill_refuses_live_configuration(app_config):
    live_config = app_config.model_copy(
        update={
            "trading": app_config.trading.model_copy(
                update={"mode": TradingMode.LIVE, "broker": BrokerKind.ALPACA}
            )
        }
    )

    with pytest.raises(PaperDrillError, match="paper"):
        run_paper_drill(live_config, service=None)


def test_paper_drill_proposes_accepts_and_cancels_through_service(
    app_config, make_service
):
    paper_config = app_config.model_copy(
        update={
            "trading": app_config.trading.model_copy(
                update={"mode": TradingMode.PAPER, "broker": BrokerKind.ALPACA}
            )
        }
    )
    service = make_service()

    result = run_paper_drill(
        paper_config, service=service, symbol="AAPL", test_notional=Decimal("1.25")
    )

    assert result["broker_accepted"] is True
    assert result["terminal_status"] == "canceled"
    assert service.broker.submit_calls == 1
