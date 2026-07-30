#!/usr/bin/env bash
# Stop only the HTTPS app process recorded by scripts/start.sh.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."

PROJECT="$(pwd -P)"
PY="$PROJECT/.venv/bin/python"
PID_FILE="$PROJECT/logs/app.pid"

if [[ ! -x "$PY" ]]; then
  echo "venv python not found at $PY (run 'uv sync' first)" >&2
  exit 1
fi

EXPECTED_ARGV="$("$PY" -m trading_assistant.ops.control expected-argv)"

if [[ ! -e "$PID_FILE" && ! -L "$PID_FILE" ]]; then
  if ! "$PY" -m trading_assistant.ops.control app-absent \
    --project "$PROJECT" \
    --port 8020; then
    echo "app state is unknown (startup, control artifact, listener, or inspection uncertainty exists)" >&2
    exit 1
  fi
  echo "app is not running (no cooperative control metadata)"
  exit 0
fi

if ! "$PY" -m trading_assistant.ops.control stop \
  --project "$PROJECT" \
  --pid-file "$PID_FILE" \
  --expected-argv "$EXPECTED_ARGV"; then
  echo "refusing cooperative stop for stale or unmanaged metadata" >&2
  exit 1
fi

echo "app cooperative stop requested"
