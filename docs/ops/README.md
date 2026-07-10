# Keeping app + daemon up

## macOS (launchd)

Save as `~/Library/LaunchAgents/com.trading.app.plist` (and a second for the daemon,
swapping the command). `launchctl load` it. Adjust paths.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>com.trading.app</string>
  <key>WorkingDirectory</key><string>/Users/you/trading-assistant</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/uv</string><string>run</string>
    <string>uvicorn</string><string>trading_assistant.app.main:create_app</string>
    <string>--factory</string><string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8000</string>
  </array>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/tmp/trading-app.err</string>
</dict></plist>
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
`systemctl enable --now trading-daemon` (a second unit runs the uvicorn app).

## Nightly backup (cron, 14-day retention)

```cron
0 2 * * *  cd /home/you/trading-assistant && sqlite3 trading_assistant.db ".backup backups/ta-$(date +\%F).db" && find backups -name 'ta-*.db' -mtime +14 -delete
```

A watchdog can `curl -fs http://127.0.0.1:8000/health` and alert if `daemon_alive`
is false or `heartbeat_age_seconds` is large.
