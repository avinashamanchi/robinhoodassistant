#!/usr/bin/env bash
# Install the launchd agents so the app + daemon auto-start on login, restart on
# crash, and survive reboot. Idempotent: safe to re-run after a code update.
#
#   ./scripts/launchd/install.sh          # install + load
#   ./scripts/launchd/uninstall.sh        # stop + remove
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="$PROJ/.venv/bin/python"
LA="$HOME/Library/LaunchAgents"
UID_="$(id -u)"

[ -x "$PY" ] || { echo "error: venv python not found at $PY (run 'uv sync' first)"; exit 1; }
mkdir -p "$LA" "$PROJ/logs"

emit () {  # $1=label  $2...=program args
  local label="$1"; shift
  local plist="$LA/$label.plist"
  {
    printf '<?xml version="1.0" encoding="UTF-8"?>\n'
    printf '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    printf '<plist version="1.0"><dict>\n'
    printf '  <key>Label</key><string>%s</string>\n' "$label"
    printf '  <key>WorkingDirectory</key><string>%s</string>\n' "$PROJ"
    printf '  <key>ProgramArguments</key>\n  <array>\n'
    for a in "$@"; do printf '    <string>%s</string>\n' "$a"; done
    printf '  </array>\n'
    printf '  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin</string></dict>\n'
    printf '  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n  <key>ThrottleInterval</key><integer>10</integer>\n'
    printf '  <key>StandardOutPath</key><string>%s</string>\n' "$PROJ/logs/$label.launchd.log"
    printf '  <key>StandardErrorPath</key><string>%s</string>\n' "$PROJ/logs/$label.launchd.log"
    printf '</dict></plist>\n'
  } > "$plist"
  launchctl bootout "gui/$UID_/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_" "$plist"
  echo "loaded $label"
}

emit com.trading.app "$PY" -m uvicorn trading_assistant.app.main:create_app --factory --host 127.0.0.1 --port 8000
emit com.trading.daemon "$PY" -m trading_assistant.daemon.main

sleep 6
echo "=== status ==="
launchctl list | grep com.trading || true
echo "=== health ==="
curl -s http://127.0.0.1:8000/health || echo "(app not answering yet — check logs/com.trading.app.launchd.log)"
echo
