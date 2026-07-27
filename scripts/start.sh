#!/usr/bin/env bash
# Start only the loopback HTTPS operator console. The strict launcher performs
# local structural checks before it constructs the application.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."

PROJECT="$(pwd -P)"
PY="$PROJECT/.venv/bin/python"
PID_FILE="$PROJECT/logs/app.pid"

# shellcheck source=lib/app-process-identity.sh
source "$PROJECT/scripts/lib/app-process-identity.sh"
configure_process_identity "$PROJECT" "$PY" "$PID_FILE"

if [[ -e "$PID_FILE" || -L "$PID_FILE" ]]; then
  if load_process_metadata && managed_process_matches_metadata; then
    echo "app already running (pid $PROCESS_METADATA_PID)"
    exit 0
  fi
  echo "replacing stale or malformed managed PID metadata" >&2
  rm -f "$PID_FILE"
fi

if [[ ! -x "$PY" ]]; then
  echo "venv python not found at $PY (run 'uv sync' first)" >&2
  exit 1
fi

mkdir -p logs
chmod 700 logs
echo "starting HTTPS app on https://localhost:8020"
nohup "$PY" -m trading_assistant.ops.serve > /dev/null 2>&1 &
app_pid="$!"
if ! write_process_metadata "$app_pid"; then
  echo "app launcher exited before PID ownership could be confirmed" >&2
  exit 1
fi

echo "Open  : https://localhost:8020"
echo "Logs  : logs/app.runtime.log"
echo "Stop  : ./scripts/stop.sh"
