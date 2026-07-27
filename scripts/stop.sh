#!/usr/bin/env bash
# Stop only the HTTPS app process recorded by scripts/start.sh.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."

PROJECT="$(pwd -P)"
PY="$PROJECT/.venv/bin/python"
PID_FILE="$PROJECT/logs/app.pid"

# shellcheck source=lib/app-process-identity.sh
source "$PROJECT/scripts/lib/app-process-identity.sh"
configure_process_identity "$PROJECT" "$PY" "$PID_FILE"

if [[ ! -e "$PID_FILE" && ! -L "$PID_FILE" ]]; then
  echo "app is not running (no managed PID file)"
  exit 0
fi

if ! signal_managed_process TERM; then
  echo "refusing to signal unmanaged PID from $PID_FILE" >&2
  exit 1
fi

rm -f "$PID_FILE"
echo "app stop requested (pid $PROCESS_METADATA_PID)"
