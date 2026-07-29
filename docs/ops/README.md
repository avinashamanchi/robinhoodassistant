# Operating the HTTPS app and separate daemon

This release is paper-only and has no live-mode support or profit guarantee.
There is no webhook receiver. Composio remains disabled pending provider-side
revocation and rotation of the previously exposed credential. General chat is
read-only plus immutable drafts; an operator must explicitly place a signed
draft in the queue and a separate human approval must pass deterministic risk
checks before paper submission is possible.

Before installing or starting any role, complete the Keychain migration/audit,
local TLS setup, schema migration, sensitive-field migration/verification, and
static gate:

```bash
uv run python -m trading_assistant.ops.secrets audit
./scripts/setup-local-tls.sh
uv run python -m trading_assistant.db.migrate status
uv run python -m trading_assistant.ops.encrypt_sensitive verify
uv run python scripts/check_release_safety.py
uv run python -m trading_assistant.preflight
```

`KEYCHAIN`, `LOCAL_TLS`, `FIELD_ENCRYPTION`, `OUTBOUND_ORIGINS`, and
`INTEGRATIONS_DISABLED` must all pass before preflight constructs any broker,
provider, or notifier. All five execute independently even when Keychain
construction or loading fails; TLS and encryption-metadata checks remain local
and the preflight field check never decrypts rows. See `docs/RUNBOOK.md` for the
one-time private-file Keychain migration, stopped-writer field migration and
rotation, and reviewed encrypted restore procedure. Never place secret material
in these commands.
The TLS setup installs the canonical `rootCA.pem`, `localhost.pem`, and
`localhost-key.pem` layout; watchdog verification uses the CA file, never the
leaf as a trust bundle. After the five structural rows pass, preflight uses a
dedicated read-only reconciliation service. That service exposes one snapshot
probe, calls only broker open-order/position reads and local SQL `SELECT`s, and
cannot repair, cancel, submit, notify, or construct a mutable trading service.
Rotation requires a reviewed new retained key ID, an interactive
`set-encryption-key`, stopped-writer `encrypt_sensitive rotate`, a coordinated
active/retained config transition, and a final Keychain audit plus envelope
verification.

## macOS (launchd)

Use the repository's canonical installer. It generates and loads the HTTPS app,
watchdog, and encrypted-backup jobs from the current checkout. It deliberately
does not start the trading daemon:

```bash
./scripts/launchd/install.sh
```

Every generated job includes this private stream contract:

```xml
<key>Umask</key><integer>63</integer>
<key>StandardOutPath</key><string>/dev/null</string>
<key>StandardErrorPath</key><string>/dev/null</string>
```

The decimal plist value `63` is octal `077`: newly created files grant no
group or other permissions. Inherited stdout and stderr are discarded so
launchd cannot create unbounded stream files. Application logs remain
available in private, bounded rotating files:

- `logs/app.runtime.log`
- `logs/daemon.runtime.log`
- `logs/watchdog.runtime.log`
- `logs/backup.runtime.log`
- `logs/mcp.runtime.log`
- `logs/preflight.runtime.log`
- `logs/paper-drill.runtime.log`

Do not replace the `/dev/null` paths with regular files. Re-run the canonical
installer after an update; see `scripts/launchd/README.md` for management
commands.

After preflight passes, launch the daemon as a distinct operator action in a
separate terminal:

```bash
uv run python -m trading_assistant.daemon.main
```

## Linux (systemd)

`/etc/systemd/system/trading-daemon.service`:

```ini
[Unit]
Description=Trading daemon
After=network-online.target
[Service]
WorkingDirectory=/home/you/trading-assistant
ExecStart=/usr/local/bin/uv run python -m trading_assistant.daemon.main
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
```
Do not enable this unit until the local structural and operational preflight has
passed. The HTTPS app is a separate unit running
`python -m trading_assistant.ops.serve`; starting app liveness never implies the
daemon is ready.

## Nightly encrypted backup (cron, 14-day retention)

```cron
0 2 * * *  cd /home/you/trading-assistant && uv run python -m trading_assistant.ops.backup --destination .local/encrypted-backups --retention-days 14
```

The job acquires exclusive maintenance tenure and publishes only
`<timestamp>-whole-database-v1.sqlite3.aesgcm` artifacts. AES-256-GCM uses the
dedicated configured backup key; the command verifies a streamed decryption,
snapshot hash, and SQLite `quick_check` before success. The destination is mode
`0700`, artifacts are mode `0600`, publication never overwrites, and every
plaintext snapshot/verification temporary is removed on success or failure.
Do not create an operational plaintext database copy for retention or analysis.

The watchdog probes anonymous app liveness at
`https://localhost:8020/health/live`. It reads the persisted daemon heartbeat
directly from the local database; app liveness is never treated as proof that
the daemon is alive.
