#!/usr/bin/env bash
# Install the loopback HTTPS app, watchdog, and backup jobs. The daemon is not
# installed here; it remains an explicit operator workflow after preflight.
#
#   ./scripts/launchd/install.sh                  # install + load
#   ./scripts/launchd/uninstall.sh                # stop + remove
#
# No trading job is installed. The autopilot runs only inside the daemon,
# which the operator starts explicitly (autopilot.mode in config.yaml).
set -euo pipefail
umask 077

for arg in "$@"; do
  case "$arg" in
    --with-autopilot)
      echo "error: --with-autopilot was retired: the daemon hosts the autopilot" >&2
      echo "       (autopilot.mode observe|paper); no scheduled trading job exists" >&2
      exit 2
      ;;
    *) echo "error: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
PY="$PROJ/.venv/bin/python"
LA="$HOME/Library/LaunchAgents"
UID_="$(id -u)"

[ -x "$PY" ] || { echo "error: venv python not found at $PY (run 'uv sync' first)"; exit 1; }

# Before any side effect: every generated job runs from $PROJ, so it must be
# the operator's designated runtime installation and its venv must import
# this checkout (a moved checkout's venv still imports the old path). The
# check is the stdlib-only module run as a file, so it works even when the
# package cannot be imported.
if ! "$PY" -I "$PROJ/src/trading_assistant/installation.py" check \
    --project "$PROJ" --venv-python "$PY"; then
  echo "error: refusing to install launchd jobs for $PROJ" >&2
  echo "       inspect: $PY -I $PROJ/src/trading_assistant/installation.py status" >&2
  exit 1
fi

mkdir -p "$LA" "$PROJ/logs" "$PROJ/.local/encrypted-backups"
chmod 700 "$PROJ/logs" "$PROJ/.local" "$PROJ/.local/encrypted-backups"
[ ! -f "$PROJ/.env" ] || chmod 600 "$PROJ/.env"

reload_plist () {  # launchd can briefly return EIO immediately after bootout
  local label="$1"
  local plist="$2"
  launchctl bootout "gui/$UID_/$label" 2>/dev/null || true
  for attempt in 1 2 3; do
    if launchctl bootstrap "gui/$UID_" "$plist"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

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
    printf '  <key>Umask</key><integer>63</integer>\n'
    printf '  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n  <key>ThrottleInterval</key><integer>10</integer>\n'
    printf '  <key>StandardOutPath</key><string>/dev/null</string>\n'
    printf '  <key>StandardErrorPath</key><string>/dev/null</string>\n'
    printf '</dict></plist>\n'
  } > "$plist"
  reload_plist "$label" "$plist"
  echo "loaded $label"
}

emit_periodic () {  # $1=label $2=interval_seconds $3...=program args
  local label="$1"; shift
  local interval="$1"; shift
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
    printf '  <key>Umask</key><integer>63</integer>\n'
    printf '  <key>RunAtLoad</key><true/>\n'
    printf '  <key>StartInterval</key><integer>%s</integer>\n' "$interval"
    printf '  <key>StandardOutPath</key><string>/dev/null</string>\n'
    printf '  <key>StandardErrorPath</key><string>/dev/null</string>\n'
    printf '</dict></plist>\n'
  } > "$plist"
  reload_plist "$label" "$plist"
  echo "loaded $label"
}

emit_daily () {  # $1=label $2=hour $3=minute $4...=program args
  local label="$1"; shift
  local hour="$1"; shift
  local minute="$1"; shift
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
    printf '  <key>Umask</key><integer>63</integer>\n'
    printf '  <key>StartCalendarInterval</key><dict>\n'
    printf '    <key>Hour</key><integer>%s</integer>\n' "$hour"
    printf '    <key>Minute</key><integer>%s</integer>\n' "$minute"
    printf '  </dict>\n'
    printf '  <key>StandardOutPath</key><string>/dev/null</string>\n'
    printf '  <key>StandardErrorPath</key><string>/dev/null</string>\n'
    printf '</dict></plist>\n'
  } > "$plist"
  reload_plist "$label" "$plist"
  echo "loaded $label"
}

emit com.trading.app "$PY" -m trading_assistant.ops.serve
emit_periodic com.trading.watchdog 60 "$PY" -m trading_assistant.ops.watchdog
emit_daily com.trading.backup 2 0 "$PY" -m trading_assistant.ops.backup --destination "$PROJ/.local/encrypted-backups" --retention-days 14
if [ -f "$LA/com.trading.autopilot.plist" ]; then
  echo "note: a retired com.trading.autopilot job is still installed; it would run"
  echo "      the removed standalone loop. Remove it with scripts/launchd/uninstall.sh"
fi

sleep 6
echo "=== status ==="
launchctl list | grep com.trading || true
echo "=== health ==="
curl --fail --silent https://localhost:8020/health/live || echo "(app not answering yet — check logs/app.runtime.log)"
echo
