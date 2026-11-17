"""Fixed incident codes and operator recovery guidance; no external payload text."""
COMPONENTS = {
    "maintenance": {"backup_running", "backup_stalled", "maintenance_idle", "maintenance_unknown"},
    "database": {"database_readable", "database_unreadable"},
    "daemon": {"heartbeat_fresh", "heartbeat_stale", "database_unreadable"},
    "safety": {"safety_clear", "safety_latched", "database_unreadable"},
    "reconciliation": {"reconciliation_clean", "reconciliation_required", "database_unreadable"},
    "monitor": {"monitoring_gap", "monitoring_current"},
    "power": {"ac_awake", "battery_awake", "awake_released", "power_unknown"},
    "app": {"app_live", "app_unreachable"},
    "release": {"release_verified", "release_unverified"},
    "jobs": {"jobs_loaded", "jobs_missing", "jobs_unknown"},
    "backup": {"backup_recent", "backup_missing", "backup_unknown"},
    "notifications": {"telegram_unpaired", "telegram_test_unconfirmed", "telegram_unavailable", "telegram_delivery_uncertain", "telegram_current", "telegram_unknown"},
}
RECOVERY = {
    "maintenance": "Wait for the exclusive scheduled backup to restore runtime jobs; inspect backup logs if it stalls.",
    "database": "Inspect the designated database and logs; do not replace history.",
    "daemon": "Inspect daemon logs and watchdog status; use the canonical installer if its job is missing.",
    "safety": "Review the breaker and use guarded recovery after fresh reconciliation; never auto-reset.",
    "reconciliation": "Run paper broker reconciliation and inspect any drift.",
    "monitor": "Inspect host uptime and power; the missed interval does not establish its cause.",
    "power": "Connect power and keep the lid open, or explicitly request temporary battery awake mode.",
    "app": "Inspect app logs, TLS configuration and its launchd job.",
    "release": "Finish source changes and import successful exact-commit CI evidence.",
    "jobs": "Review launchd status and reinstall the authorized runtime jobs.",
    "backup": "Inspect scheduled encrypted backup receipts and run the documented backup workflow.",
    "notifications": "Inspect the managed bot connection, private pairing and delivery receipts; uncertain sends are not retried automatically.",
}
