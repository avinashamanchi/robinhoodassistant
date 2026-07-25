# Keeping app + daemon up

## macOS (launchd)

Use the repository's canonical installer. It generates and loads the app,
daemon, watchdog, and backup jobs from the current checkout:

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
`systemctl enable --now trading-daemon` (a second unit runs the uvicorn app).

## Nightly backup (cron, 14-day retention)

```cron
0 2 * * *  cd /home/you/trading-assistant && uv run python -m trading_assistant.ops.backup --destination backups --retention-days 14
```

The watchdog probes anonymous app liveness at
`http://127.0.0.1:8000/health/live`. It reads the persisted daemon heartbeat
directly from the local database; app liveness is never treated as proof that
the daemon is alive.
