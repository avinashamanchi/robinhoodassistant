#!/usr/bin/env bash
# Stop and remove the launchd agents (watchdog, app, daemon, backup, autopilot).
# Auto-start is disabled after this runs; re-run install.sh to bring it back.
# The watchdog goes first so it cannot restart a job that is being removed.
set -euo pipefail
umask 077
LA="$HOME/Library/LaunchAgents"
UID_="$(id -u)"
for label in com.trading.watchdog com.trading.app com.trading.daemon com.trading.backup com.trading.autopilot; do
  launchctl bootout "gui/$UID_/$label" 2>/dev/null || true
  rm -f "$LA/$label.plist"
  echo "removed $label"
done
