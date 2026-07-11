#!/usr/bin/env bash
# Stop the trading assistant app + daemon.
cd "$(dirname "$0")/.."
pkill -f "uvicorn trading_assistant.app.main" 2>/dev/null && echo "app stopped" || echo "app not running"
pkill -f "trading_assistant.daemon.main" 2>/dev/null && echo "daemon stopped" || echo "daemon not running"
