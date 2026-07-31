#!/usr/bin/env bash
# Launch the fixed-root terminal only after the loopback HTTPS app is proven.
set -euo pipefail
umask 077

CANONICAL_PROJECT="/Users/avi/Desktop/robinhood/trading-assistant"
CURL="/usr/bin/curl"
LIVENESS_URL="https://localhost:8020/health/live"

if (( $# != 0 )); then
  echo "operator launcher accepts no arguments" >&2
  exit 2
fi

if [[ "${PWD:-}" != "$CANONICAL_PROJECT" ]]; then
  echo "run ./scripts/operator.sh from $CANONICAL_PROJECT" >&2
  exit 1
fi

PROJECT="$(pwd -P)"
if [[ "$PROJECT" != "$CANONICAL_PROJECT" || -L "$CANONICAL_PROJECT" ]]; then
  echo "canonical project root validation failed" >&2
  exit 1
fi

SCRIPT_PATH="${BASH_SOURCE[0]}"
if [[ "$SCRIPT_PATH" != */* || -L "$SCRIPT_PATH" ]]; then
  echo "operator launcher path validation failed" >&2
  exit 1
fi
SCRIPT_DIRECTORY="$(cd -P -- "${SCRIPT_PATH%/*}" && pwd -P)"
if [[ "$SCRIPT_DIRECTORY" != "$PROJECT/scripts" ]]; then
  echo "operator launcher is outside the canonical project root" >&2
  exit 1
fi

PY="$PROJECT/.venv/bin/python"
TLS_CA="$PROJECT/.local/tls/rootCA.pem"
PID_FILE="$PROJECT/logs/app.pid"
START_SCRIPT="$PROJECT/scripts/start.sh"

if [[ ! -x "$PY" ]]; then
  echo "venv python not found at $PY (run 'uv sync' first)" >&2
  exit 1
fi
if [[ ! -f "$TLS_CA" || -L "$TLS_CA" ]]; then
  echo "local HTTPS CA is unavailable" >&2
  exit 1
fi
if [[ ! -x "$CURL" ]]; then
  echo "absolute curl dependency is unavailable" >&2
  exit 1
fi

EXPECTED_ARGV="$("$PY" -m trading_assistant.ops.control expected-argv)"

control_is_ready () {
  "$PY" -m trading_assistant.ops.control ready \
    --project "$PROJECT" \
    --pid-file "$PID_FILE" \
    --expected-argv "$EXPECTED_ARGV" \
    --port 8020
}

health_is_live () {
  "$CURL" --fail --silent --show-error \
    --cacert "$TLS_CA" \
    "$LIVENESS_URL" \
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

app_is_ready () {
  control_is_ready \
    && health_is_live \
    && control_is_ready
}

if ! app_is_ready; then
  if ! "$PY" -m trading_assistant.ops.control app-absent \
    --project "$PROJECT" \
    --port 8020; then
    echo "app state is not safely absent or ready" >&2
    exit 1
  fi
  if [[ ! -x "$START_SCRIPT" || -L "$START_SCRIPT" ]]; then
    echo "controlled start launcher is unavailable" >&2
    exit 1
  fi
  if ! "$START_SCRIPT"; then
    echo "controlled HTTPS app start failed" >&2
    exit 1
  fi
  if ! app_is_ready; then
    echo "controlled HTTPS app failed post-start validation" >&2
    exit 1
  fi
fi

exec "$PY" -m trading_assistant.ops.operator_terminal
