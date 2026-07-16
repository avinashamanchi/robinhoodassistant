#!/usr/bin/env bash
# Stop and remove the launchd agents (app + daemon). Auto-start is disabled after
# this runs; re-run install.sh to bring it back.
set -euo pipefail
LA="$HOME/Library/LaunchAgents"
UID_="$(id -u)"
for label in com.trading.app com.trading.daemon; do
  launchctl bootout "gui/$UID_/$label" 2>/dev/null || true
  rm -f "$LA/$label.plist"
  echo "removed $label"
done
