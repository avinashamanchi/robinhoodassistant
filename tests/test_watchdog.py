"""Heartbeat watchdog decisions are deterministic and fail safe."""

from trading_assistant.ops.watchdog import labels_to_restart, needs_restart


def test_watchdog_restarts_only_when_health_is_stale_or_broken():
    assert needs_restart(
        {"db_ok": True, "heartbeat_age_seconds": 30}, stale_seconds=180
    ) is False
    assert needs_restart(
        {"db_ok": True, "heartbeat_age_seconds": 181}, stale_seconds=180
    ) is True
    assert needs_restart(
        {"db_ok": True, "heartbeat_age_seconds": None}, stale_seconds=180
    ) is True
    assert needs_restart(
        {"db_ok": False, "heartbeat_age_seconds": 1}, stale_seconds=180
    ) is True


def test_api_outage_restarts_app_and_uses_db_heartbeat_for_daemon():
    assert labels_to_restart(
        api_health=None,
        database_health={"db_ok": True, "heartbeat_age_seconds": 10},
        stale_seconds=180,
    ) == {"com.trading.app"}
    assert labels_to_restart(
        api_health=None,
        database_health={"db_ok": True, "heartbeat_age_seconds": 999},
        stale_seconds=180,
    ) == {"com.trading.app", "com.trading.daemon"}
