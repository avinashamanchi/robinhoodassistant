# launchd auto-start (macOS)

Keeps the loopback HTTPS app alive without manual `start.sh`, runs a periodic
watchdog, and creates a daily encrypted database backup. The daemon is deliberately not
installed by this app installer; launch it only through its explicit operator
workflow after its own preflight.

This release is paper-only and offers no profit or return guarantee. It has no
live-mode support and no webhook receiver. Composio remains disabled pending
provider-side revocation and rotation of the previously exposed credential.
Chat can read or construct an immutable draft only; explicit signed queueing
and a separate authenticated human approval are required before deterministic
risk checks can reach paper submission.

## Install / update

```bash
uv run python -m trading_assistant.ops.secrets audit
./scripts/setup-local-tls.sh
uv run python -m trading_assistant.ops.encrypt_sensitive verify
uv run python scripts/check_release_safety.py
uv run python -m trading_assistant.preflight
./scripts/launchd/install.sh
```

Idempotent — re-run it after pulling code changes to reload with the new binary.
It regenerates both plists from the repo's current path, so it works on any
machine where the repo is checked out and `.venv` exists (`uv sync`). Do not
install unless `KEYCHAIN`, `LOCAL_TLS`, `FIELD_ENCRYPTION`,
`OUTBOUND_ORIGINS`, and `INTEGRATIONS_DISABLED` all pass. The five rows execute
independently even after a Keychain construction/load
failure and stop before any broker, provider, or notifier is constructed.
Field preflight is metadata-only; the startup guard owns the full envelope
scan. Local TLS requires `rootCA.pem`, `localhost.pem`, and
`localhost-key.pem`; the watchdog trusts only the CA file. After structural
success, preflight uses a dedicated read-only snapshot service that performs
only broker open-order/position reads and performs no trading-table DML. SQLite
setup still establishes WAL and applies sidecar permissions. The probe cannot
repair or cancel orders and constructs no mutable trading service, LLM
provider, agent, app, or notifier. Keychain migration, field
migration/rotation, and encrypted restore procedures are in
`docs/RUNBOOK.md`; no secret value belongs on a command line.
For rotation, keep every writer stopped, configure and prompt for the reviewed
retained key ID, run the field rotation, complete the coordinated
active/retained config transition, then audit Keychain and verify all envelopes
before reinstalling or restarting jobs.

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

All jobs use `WorkingDirectory` = repo root so `config.yaml` and the SQLite DB
resolve correctly. Runtime secrets come only from macOS Keychain; `.env` is not
a production source. Inherited stdout and stderr go to `/dev/null`;
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

The installer does not start the daemon. After preflight passes, launch it as a
separate operator-controlled process:

```bash
uv run python -m trading_assistant.daemon.main
```

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
