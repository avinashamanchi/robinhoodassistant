#!/usr/bin/env bash
# Start only the loopback HTTPS operator console. The strict launcher performs
# local structural checks before it constructs the application.
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

if [[ -f "$PID_FILE" ]]; then
  existing_pid="$(tr -d '[:space:]' < "$PID_FILE")"
  if pid_belongs_to_app "$existing_pid"; then
    echo "app already running (pid $existing_pid)"
    exit 0
  fi
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
if ! pid_belongs_to_app "$app_pid"; then
  echo "app launcher exited before PID ownership could be confirmed" >&2
  exit 1
fi
printf '%s\n' "$app_pid" > "$PID_FILE"

echo "Open  : https://localhost:8020"
echo "Logs  : logs/app.runtime.log"
echo "Stop  : ./scripts/stop.sh"
