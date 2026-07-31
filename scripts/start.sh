#!/bin/bash -p
# Start only the loopback HTTPS operator console. The strict launcher performs
# local structural checks before it constructs the application.
set -euo pipefail
umask 077

case "$-" in
  *p*) ;;
  *)
    echo "controlled start requires privileged Bash mode" >&2
    exit 1
    ;;
esac

PATH="/usr/bin:/bin:/usr/sbin:/sbin"
export PATH
IFS=$' \t\n'
unset BASH_ENV ENV CDPATH
unset \
  PYTHONBREAKPOINT \
  PYTHONCASEOK \
  PYTHONEXECUTABLE \
  PYTHONHOME \
  PYTHONINSPECT \
  PYTHONNOUSERSITE \
  PYTHONPATH \
  PYTHONPLATLIBDIR \
  PYTHONSAFEPATH \
  PYTHONSTARTUP \
  PYTHONUSERBASE \
  PYTHONWARNINGS
PYTHONNOUSERSITE=1
PYTHONSAFEPATH=1
export PYTHONNOUSERSITE PYTHONSAFEPATH

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
  "$CURL" --disable \
    --fail \
    --silent \
    --show-error \
    --noproxy "*" \
    --resolve "localhost:8020:127.0.0.1" \
    --proto "=https" \
    --connect-timeout 2 \
    --max-time 3 \
    --max-filesize 1024 \
    --cacert "$TLS_CA" \
    "https://localhost:8020/health/live" \
    | "$PY" -I -c '
import json
import sys

maximum_bytes = 1024
encoded = sys.stdin.buffer.read(maximum_bytes + 1)
if len(encoded) > maximum_bytes:
    raise SystemExit(1)
try:
    document = encoded.decode("utf-8", errors="strict")
except UnicodeDecodeError:
    raise SystemExit(1)

def exact_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result

try:
    payload = json.loads(document, object_pairs_hook=exact_object)
except (TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
expected_keys = {"alive", "database_reachable"}
if type(payload) is not dict or set(payload) != expected_keys:
    raise SystemExit(1)
if (
    type(payload["alive"]) is not bool
    or type(payload["database_reachable"]) is not bool
):
    raise SystemExit(1)
raise SystemExit(
    0
    if payload["alive"] is True
    and payload["database_reachable"] is True
    else 1
)
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
