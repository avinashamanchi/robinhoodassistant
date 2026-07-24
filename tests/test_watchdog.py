"""Heartbeat watchdog decisions are deterministic and fail safe."""

from trading_assistant.ops.watchdog import needs_restart


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
