#!/usr/bin/env bash
# Stop only the HTTPS app process recorded by scripts/start.sh.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."

PROJECT="$(pwd -P)"
PY="$PROJECT/.venv/bin/python"
PID_FILE="$PROJECT/logs/app.pid"
EXPECTED_ARGV="$PY -m trading_assistant.ops.serve"

if [[ ! -e "$PID_FILE" && ! -L "$PID_FILE" ]]; then
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
