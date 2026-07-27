#!/usr/bin/env bash
# Start only the loopback HTTPS operator console. The strict launcher performs
# local structural checks before it constructs the application.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."

PROJECT="$(pwd -P)"
PY="$PROJECT/.venv/bin/python"
PID_FILE="$PROJECT/logs/app.pid"
RUNTIME_DIR="$PROJECT/runtime"
CONTROL_SOCKET="$RUNTIME_DIR/app-control.sock"
EXPECTED_ARGV="$PY -m trading_assistant.ops.serve"

if [[ ! -x "$PY" ]]; then
  echo "venv python not found at $PY (run 'uv sync' first)" >&2
  exit 1
fi

if [[ -e "$PID_FILE" || -L "$PID_FILE" ]]; then
  if "$PY" -m trading_assistant.ops.control validate \
    --project "$PROJECT" \
    --pid-file "$PID_FILE" \
    --expected-argv "$EXPECTED_ARGV"; then
    echo "app already running (cooperative control metadata is current)"
    exit 0
  fi
  echo "refusing to replace stale or malformed app control metadata" >&2
  exit 1
fi

if [[ -e "$CONTROL_SOCKET" || -L "$CONTROL_SOCKET" ]]; then
  echo "refusing to replace existing app control socket" >&2
  exit 1
fi

mkdir -p logs runtime
chmod 700 logs runtime
instance_id="$($PY -c 'import secrets; print(secrets.token_hex(32))')"
echo "starting HTTPS app on https://localhost:8020"
TRADING_APP_INSTANCE_ID="$instance_id" \
  nohup "$PY" -m trading_assistant.ops.serve > /dev/null 2>&1 &
for _attempt in {1..50}; do
  if "$PY" -m trading_assistant.ops.control validate \
    --project "$PROJECT" \
    --pid-file "$PID_FILE" \
    --expected-argv "$EXPECTED_ARGV"; then
    echo "Open  : https://localhost:8020"
    echo "Logs  : logs/app.runtime.log"
    echo "Stop  : ./scripts/stop.sh"
    exit 0
  fi
  sleep 0.1
done

echo "app control channel was not ready; refusing any PID fallback" >&2
exit 1
