#!/usr/bin/env bash
# Start the trading assistant (paper). Runs preflight first and refuses to start
# if it isn't READY. Starts the app + daemon in the background with logs.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."

echo "== stopping any existing instances =="
pkill -f "uvicorn trading_assistant.app.main" 2>/dev/null || true
pkill -f "trading_assistant.daemon.main" 2>/dev/null || true
sleep 1

echo "== preflight =="
if ! .venv/bin/python -m trading_assistant.preflight; then
  echo ">> Preflight NOT READY — fix the FAIL items above. Not starting." >&2
  exit 1
fi

mkdir -p logs
chmod 700 logs
echo "== starting app on http://127.0.0.1:8000 =="
nohup .venv/bin/python -m uvicorn trading_assistant.app.main:create_app \
  --factory --host 127.0.0.1 --port 8000 > /dev/null 2>&1 &
echo $! > logs/app.pid

echo "== starting daemon (shadow mode) =="
nohup .venv/bin/python -m trading_assistant.daemon.main > /dev/null 2>&1 &
echo $! > logs/daemon.pid

sleep 4
echo "== health =="
curl -s http://127.0.0.1:8000/health/live || echo "(app still warming up — check logs/app.runtime.log)"
echo
echo "Open  : http://127.0.0.1:8000"
echo "Token : run  grep APP_API_TOKEN .env   and paste the value when the page asks"
echo "Logs  : logs/app.runtime.log   logs/daemon.runtime.log"
echo "Stop  : ./scripts/stop.sh"
