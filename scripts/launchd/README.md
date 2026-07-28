# launchd auto-start (macOS)

Keeps the loopback HTTPS app alive without manual `start.sh`, runs a periodic
watchdog, and creates a daily encrypted database backup. The daemon is deliberately not
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
| `com.trading.backup` | Verified AES-256-GCM backup daily at 02:00 |

All jobs use `WorkingDirectory` = repo root so `.env`, `config.yaml`, and the
SQLite DB resolve correctly. Inherited stdout and stderr go to `/dev/null`;
each process writes redacted, owner-only, bounded rotating output to its own
`logs/<role>.runtime.log`.

The backup job acquires exclusive database maintenance tenure and publishes
only mode-`0600`
`.local/encrypted-backups/<timestamp>-whole-database-v1.sqlite3.aesgcm`
artifacts inside a mode-`0700` directory. It uses the dedicated configured
backup key, verifies a streamed decryption and SQLite `quick_check`, then
removes every plaintext snapshot and verification temporary file. The job
fails closed while any app, daemon, MCP, or validation writer tenure is active;
it never leaves an operational plaintext database copy.

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
