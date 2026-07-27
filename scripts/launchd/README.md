# launchd auto-start (macOS)

Keeps the loopback HTTPS app alive without manual `start.sh`, runs a periodic
watchdog, and creates a daily database backup. The daemon is deliberately not
installed by this app installer; launch it only through its explicit operator
workflow after its own preflight.

## Install / update

```bash
./scripts/launchd/install.sh
```

Idempotent — re-run it after pulling code changes to reload with the new binary.
It regenerates both plists from the repo's current path, so it works on any
machine where the repo is checked out and `.venv` exists (`uv sync`).

## Remove

```bash
./scripts/launchd/uninstall.sh
```

## Agents

| Label | Runs |
|-------|------|
| `com.trading.app` | `python -m trading_assistant.ops.serve` |
| `com.trading.daemon` | Disabled by the app installer; explicit operator launch only |
| `com.trading.watchdog` | watchdog every 60 seconds |
| `com.trading.backup` | SQLite backup daily at 02:00 |

All jobs use `WorkingDirectory` = repo root so `.env`, `config.yaml`, and the
SQLite DB resolve correctly. Inherited stdout and stderr go to `/dev/null`;
each process writes redacted, owner-only, bounded rotating output to its own
`logs/<role>.runtime.log`.

## Manage

```bash
launchctl list | grep com.trading                 # status + pid
curl --fail --silent https://localhost:8020/health/live # anonymous app liveness only
tail -f logs/{app,daemon,watchdog,backup}.runtime.log # bounded logs

# stop/start one until next login (bootout) then reload
launchctl bootout  gui/$(id -u)/com.trading.app
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.trading.app.plist
```

The checked-in `com.trading.app.plist` / `com.trading.daemon.plist` are reference
snapshots with absolute paths for this machine; `install.sh` is the source of
truth and rewrites them on install.
