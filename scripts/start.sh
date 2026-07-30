#!/usr/bin/env bash
# Start only the loopback HTTPS operator console. The strict launcher performs
# local structural checks before it constructs the application.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."

PROJECT="$(pwd -P)"
PY="$PROJECT/.venv/bin/python"
PID_FILE="$PROJECT/logs/app.pid"
TLS_CA="$PROJECT/.local/tls/rootCA.pem"
CURL="/usr/bin/curl"
instance_id=""
child_pid=""

if [[ ! -x "$PY" ]]; then
  echo "venv python not found at $PY (run 'uv sync' first)" >&2
  exit 1
fi

EXPECTED_ARGV="$("$PY" -m trading_assistant.ops.control expected-argv)"

cleanup_start_intent () {
  if [[ -n "$instance_id" && -n "$child_pid" ]]; then
    "$PY" -m trading_assistant.ops.control abandon-start \
      --project "$PROJECT" \
      --instance-id "$instance_id" \
      --child-pid "$child_pid" \
      >/dev/null 2>&1 || true
  fi
}
trap cleanup_start_intent EXIT

if [[ ! -x "$CURL" || ! -f "$TLS_CA" ]]; then
  echo "local HTTPS readiness dependencies are unavailable" >&2
  exit 1
fi

health_is_live () {
  "$CURL" --fail --silent --show-error \
    --cacert "$TLS_CA" \
    "https://localhost:8020/health/live" \
    | "$PY" -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
expected = {"alive": True, "database_reachable": True}
raise SystemExit(0 if type(payload) is dict and payload == expected else 1)
'
}

control_was_current=false
if [[ -e "$PID_FILE" || -L "$PID_FILE" ]]; then
  if "$PY" -m trading_assistant.ops.control validate \
    --project "$PROJECT" \
    --pid-file "$PID_FILE" \
    --expected-argv "$EXPECTED_ARGV"; then
    control_was_current=true
  fi
fi

mkdir -p logs runtime
chmod 700 logs runtime
if [[ "$control_was_current" == false ]]; then
  instance_id="$($PY -c 'import secrets; print(secrets.token_hex(32))')"
  "$PY" -m trading_assistant.ops.control begin-start \
    --project "$PROJECT" \
    --instance-id "$instance_id"
  echo "starting HTTPS app on https://localhost:8020"
  TRADING_APP_INSTANCE_ID="$instance_id" \
    nohup "$PY" -m trading_assistant.ops.serve > /dev/null 2>&1 &
  child_pid=$!
else
  echo "waiting for controlled HTTPS app readiness"
fi
for _attempt in {1..150}; do
  if "$PY" -m trading_assistant.ops.control ready \
    --project "$PROJECT" \
    --pid-file "$PID_FILE" \
    --expected-argv "$EXPECTED_ARGV" \
    --port 8020 \
    && health_is_live \
    && "$PY" -m trading_assistant.ops.control ready \
      --project "$PROJECT" \
      --pid-file "$PID_FILE" \
      --expected-argv "$EXPECTED_ARGV" \
      --port 8020; then
    echo "Open  : https://localhost:8020"
    echo "Logs  : logs/app.runtime.log"
    echo "Stop  : ./scripts/stop.sh"
    exit 0
  fi
  sleep 0.1
done

echo "controlled HTTPS app was not ready; refusing any PID fallback" >&2
exit 1
