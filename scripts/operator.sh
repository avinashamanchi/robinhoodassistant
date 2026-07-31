#!/bin/bash -p
# Launch the fixed-root terminal only after the loopback HTTPS app is proven.
set -euo pipefail
umask 077

case "$-" in
  *p*) ;;
  *)
    echo "operator launcher requires privileged Bash mode" >&2
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

PROJECT="$(builtin pwd -P)"
if [[ "$PROJECT" != "$CANONICAL_PROJECT" || -L "$CANONICAL_PROJECT" ]]; then
  echo "canonical project root validation failed" >&2
  exit 1
fi

SCRIPT_PATH="${BASH_SOURCE[0]}"
if [[ "$SCRIPT_PATH" != */* || -L "$SCRIPT_PATH" ]]; then
  echo "operator launcher path validation failed" >&2
  exit 1
fi
SCRIPT_DIRECTORY="$(
  builtin cd -P -- "${SCRIPT_PATH%/*}" \
    && builtin pwd -P
)"
if [[ "$SCRIPT_DIRECTORY" != "$PROJECT/scripts" ]]; then
  echo "operator launcher is outside the canonical project root" >&2
  exit 1
fi

PY="$PROJECT/.venv/bin/python"
TLS_CA="$PROJECT/.local/tls/rootCA.pem"
PID_FILE="$PROJECT/logs/app.pid"
START_SCRIPT="$PROJECT/scripts/start.sh"

for trusted_directory in \
  "$PROJECT/.venv" \
  "$PROJECT/.venv/bin" \
  "$PROJECT/.local" \
  "$PROJECT/.local/tls" \
  "$PROJECT/scripts"; do
  if [[ ! -d "$trusted_directory" || -L "$trusted_directory" ]]; then
    echo "trusted launcher directory validation failed" >&2
    exit 1
  fi
done

CURRENT_UID="$(/usr/bin/id -u)"
if [[ ! "$CURRENT_UID" =~ ^[0-9]+$ ]]; then
  echo "current user identity is unavailable" >&2
  exit 1
fi

if [[ ! -L "$PY" || ! -x "$PY" ]]; then
  echo "venv python link not found at $PY (run 'uv sync' first)" >&2
  exit 1
fi
if ! PY_METADATA="$(
  /usr/bin/stat -L -f '%u:%p:%l' -- "$PY"
)"; then
  echo "venv python target metadata is unavailable" >&2
  exit 1
fi
IFS=: read -r PY_OWNER PY_MODE PY_LINKS <<< "$PY_METADATA"
if [[ ! "$PY_OWNER" =~ ^[0-9]+$ ]] \
  || [[ "$PY_OWNER" != "$CURRENT_UID" && "$PY_OWNER" != "0" ]] \
  || [[ ! "$PY_MODE" =~ ^[0-7]+$ ]] \
  || [[ "$PY_LINKS" != "1" ]] \
  || (( (8#$PY_MODE & 0170000) != 0100000 )) \
  || (( (8#$PY_MODE & 0022) != 0 )); then
  echo "venv python target is not trusted" >&2
  exit 1
fi

if [[ ! -d "$PROJECT/.local/tls" || -L "$PROJECT/.local/tls" ]]; then
  echo "local TLS directory is unavailable" >&2
  exit 1
fi
if ! TLS_METADATA="$(
  /usr/bin/stat -f '%u:%p' -- "$PROJECT/.local/tls"
)"; then
  echo "local TLS directory metadata is unavailable" >&2
  exit 1
fi
IFS=: read -r TLS_OWNER TLS_MODE <<< "$TLS_METADATA"
if [[ "$TLS_OWNER" != "$CURRENT_UID" || "$TLS_MODE" != "40700" ]]; then
  echo "local TLS directory is not private" >&2
  exit 1
fi

if [[ ! -f "$TLS_CA" || -L "$TLS_CA" ]]; then
  echo "local HTTPS CA is unavailable" >&2
  exit 1
fi
if ! CA_METADATA="$(
  /usr/bin/stat -f '%u:%p:%l' -- "$TLS_CA"
)"; then
  echo "local HTTPS CA metadata is unavailable" >&2
  exit 1
fi
IFS=: read -r CA_OWNER CA_MODE CA_LINKS <<< "$CA_METADATA"
if [[ "$CA_OWNER" != "$CURRENT_UID" ]] \
  || [[ "$CA_MODE" != "100644" ]] \
  || [[ "$CA_LINKS" != "1" ]]; then
  echo "local HTTPS CA is not trusted" >&2
  exit 1
fi

if [[ ! -f "$START_SCRIPT" || -L "$START_SCRIPT" || ! -x "$START_SCRIPT" ]]; then
  echo "controlled start launcher is unavailable" >&2
  exit 1
fi
if ! START_METADATA="$(
  /usr/bin/stat -f '%u:%p:%l' -- "$START_SCRIPT"
)"; then
  echo "controlled start launcher metadata is unavailable" >&2
  exit 1
fi
IFS=: read -r START_OWNER START_MODE START_LINKS <<< "$START_METADATA"
if [[ "$START_OWNER" != "$CURRENT_UID" ]] \
  || [[ ! "$START_MODE" =~ ^[0-7]+$ ]] \
  || [[ "$START_LINKS" != "1" ]] \
  || (( (8#$START_MODE & 0170000) != 0100000 )) \
  || (( (8#$START_MODE & 0022) != 0 )); then
  echo "controlled start launcher is not trusted" >&2
  exit 1
fi

if [[ ! -x "$CURL" ]]; then
  echo "absolute curl dependency is unavailable" >&2
  exit 1
fi

EXPECTED_ARGV="$(
  "$PY" -I -m trading_assistant.ops.control expected-argv
)"

control_is_ready () {
  "$PY" -I -m trading_assistant.ops.control ready \
    --project "$PROJECT" \
    --pid-file "$PID_FILE" \
    --expected-argv "$EXPECTED_ARGV" \
    --port 8020
}

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
    "$LIVENESS_URL" \
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

app_is_ready () {
  control_is_ready \
    && health_is_live \
    && control_is_ready
}

if ! app_is_ready; then
  if ! "$PY" -I -m trading_assistant.ops.control app-absent \
    --project "$PROJECT" \
    --port 8020; then
    echo "app state is not safely absent or ready" >&2
    exit 1
  fi
  if ! PYTHONNOUSERSITE=1 PYTHONSAFEPATH=1 \
    /bin/bash -p "$START_SCRIPT"; then
    echo "controlled HTTPS app start failed" >&2
    exit 1
  fi
  if ! app_is_ready; then
    echo "controlled HTTPS app failed post-start validation" >&2
    exit 1
  fi
fi

exec "$PY" -I -m trading_assistant.ops.operator_terminal
