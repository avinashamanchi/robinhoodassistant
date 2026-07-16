# launchd auto-start (macOS)

Keeps the app + monitoring daemon alive without manual `start.sh`: they start on
login, restart on crash (`KeepAlive`), and survive reboot/sleep.

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
| `com.trading.app` | `uvicorn ... --host 127.0.0.1 --port 8000` |
| `com.trading.daemon` | `python -m trading_assistant.daemon.main` |

Both use `WorkingDirectory` = repo root so `.env`, `config.yaml`, and the SQLite
DB resolve correctly.

## Manage

```bash
launchctl list | grep com.trading                 # status + pid
curl -s http://127.0.0.1:8000/health              # liveness
tail -f logs/com.trading.{app,daemon}.launchd.log # logs

# stop/start one until next login (bootout) then reload
launchctl bootout  gui/$(id -u)/com.trading.app
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.trading.app.plist
```

The checked-in `com.trading.app.plist` / `com.trading.daemon.plist` are reference
snapshots with absolute paths for this machine; `install.sh` is the source of
truth and rewrites them on install.
