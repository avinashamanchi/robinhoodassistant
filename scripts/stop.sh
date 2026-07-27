#!/usr/bin/env bash
# Stop only the HTTPS app process recorded by scripts/start.sh.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."

PROJECT="$(pwd -P)"
PY="$PROJECT/.venv/bin/python"
PID_FILE="$PROJECT/logs/app.pid"

pid_belongs_to_app () {
  local pid="$1"
  local command
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  command="$(ps -ww -p "$pid" -o command= 2>/dev/null || true)"
  [[ "$command" == *"$PY -m trading_assistant.ops.serve"* ]]
}

if [[ ! -f "$PID_FILE" ]]; then
  echo "app is not running (no managed PID file)"
  exit 0
fi

pid="$(tr -d '[:space:]' < "$PID_FILE")"
if ! pid_belongs_to_app "$pid"; then
  echo "refusing to signal unmanaged PID from $PID_FILE" >&2
  exit 1
fi

kill -TERM "$pid"
rm -f "$PID_FILE"
echo "app stop requested (pid $pid)"
