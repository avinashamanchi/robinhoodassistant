# Alpaca Production Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing assistant dependable for unattended Alpaca paper trading every day, with bounded network calls, correct broker reconciliation, asset-class-correct orders, watchdog recovery, backups, and an evidence-based path toward—but no promise of—live trading.

**Architecture:** Keep `TradingService` and the deterministic risk engine as the only execution path. Harden every external call at the broker/LLM boundary, keep the daemon core loop independent from slow daily analysis, reconcile Alpaca truth at startup, and add a launchd watchdog that restarts a hung daemon based on the persisted heartbeat. Robinhood execution remains out of this plan: the supported future path is Robinhood's official Trading MCP and a separately funded Agentic account.

**Tech Stack:** Python 3.11, alpaca-py, requests/httpx, asyncio, FastAPI, SQLAlchemy/SQLite WAL, pytest, macOS launchd.

## Global Constraints

- `trading.mode` remains `paper`; `LIVE_TRADING_CONFIRM` remains unset.
- No code may promise returns or automatically promote the system to live mode.
- Every order continues through the deterministic risk engine and human/pre-approved-rule gate.
- External calls must have finite timeouts; ambiguous idempotency lookups fail closed.
- Tests are written and observed failing before each production behavior is changed.
- Existing user data and paper-account state are reconciled, never discarded.

---

### Task 1: Bounded Alpaca and LLM network calls

**Files:**
- Modify: `src/trading_assistant/config.py`
- Modify: `config.yaml`
- Modify: `src/trading_assistant/broker/alpaca.py`
- Modify: `src/trading_assistant/broker/factory.py`
- Modify: `src/trading_assistant/llm/anthropic_backend.py`
- Modify: `src/trading_assistant/llm/gemini_backend.py`
- Modify: `src/trading_assistant/llm/groq_backend.py`
- Modify: `src/trading_assistant/llm/factory.py`
- Test: `tests/test_alpaca_broker.py`
- Test: `tests/test_llm_backends.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `TradingConfig.request_timeout_seconds`, `LLMConfig.request_timeout_seconds`.
- Produces: Alpaca clients whose underlying HTTP sessions default every request to the configured timeout.
- Produces: LLM SDK clients initialized with finite request timeouts.

- [ ] Add failing tests proving an Alpaca client session receives a default timeout and all three LLM clients receive the configured timeout.
- [ ] Run the focused tests and confirm they fail because timeout configuration is absent.
- [ ] Add strict positive timeout fields, a timeout-enforcing requests session, and provider-specific timeout wiring.
- [ ] Run focused tests and the config suite.
- [ ] Commit the bounded-I/O change.

### Task 2: Correct Alpaca asset-class and idempotency behavior

**Files:**
- Modify: `src/trading_assistant/broker/alpaca.py`
- Test: `tests/test_alpaca_broker.py`

**Interfaces:**
- `AlpacaBroker` consumes both `StockHistoricalDataClient` and `CryptoHistoricalDataClient`.
- `get_quote("BTC/USD")` uses `CryptoSnapshotRequest` and preserves Alpaca's source timestamp.
- Crypto orders use `TimeInForce.GTC`; equities use `TimeInForce.DAY`.
- `_find_by_client_id` returns `None` only for a confirmed HTTP 404; all other errors propagate.

- [ ] Add failing tests for crypto quote routing, crypto GTC orders, source timestamps, and fail-closed non-404 idempotency lookup.
- [ ] Run them and confirm the existing stock-only/DAY/catch-all behavior fails.
- [ ] Implement asset-class routing, timestamps, time-in-force selection, and strict API error handling.
- [ ] Run all broker tests.
- [ ] Commit the broker correctness change.

### Task 3: Exact fill reconciliation and startup recovery

**Files:**
- Modify: `src/trading_assistant/service.py`
- Modify: `src/trading_assistant/daemon/monitor.py`
- Test: `tests/test_launch.py`
- Test: `tests/test_monitor.py`

**Interfaces:**
- `sync_open_orders()` derives the incremental fill price from cumulative quantity/notional instead of assigning the cumulative average to each partial fill.
- `Monitor.reconcile()` synchronizes broker statuses/fills first, then compares local and broker positions, and reports both results.

- [ ] Add a failing two-partial-fill test where Alpaca's cumulative average changes and assert exact local fill prices/P&L.
- [ ] Add a failing startup reconciliation test that converts a stale local `SUBMITTED` order to the broker's terminal status.
- [ ] Implement incremental-notional pricing and startup synchronization.
- [ ] Run lifecycle, hardening, and monitor tests.
- [ ] Commit the reconciliation change.

### Task 4: Daemon liveness isolation and watchdog

**Files:**
- Modify: `src/trading_assistant/config.py`
- Modify: `config.yaml`
- Modify: `src/trading_assistant/daemon/monitor.py`
- Create: `src/trading_assistant/ops/__init__.py`
- Create: `src/trading_assistant/ops/watchdog.py`
- Modify: `scripts/launchd/install.sh`
- Modify: `scripts/launchd/uninstall.sh`
- Test: `tests/test_monitor.py`
- Create: `tests/test_watchdog.py`

**Interfaces:**
- `DaemonConfig.cycle_timeout_seconds`, `daily_task_timeout_seconds`, and `heartbeat_stale_seconds` are strict positive values.
- The core reconciliation/rule cycle runs in a bounded worker thread.
- Daily shadow analysis runs as a separate bounded task and cannot stop heartbeats.
- `watchdog.needs_restart(health, stale_seconds)` is pure; the CLI restarts only `com.trading.daemon`.

- [ ] Add failing tests that a hung daily task does not block subsequent heartbeat cycles and stale health requests a restart.
- [ ] Implement bounded core/daily tasks and watchdog decision logic.
- [ ] Add a launchd `StartInterval=60` watchdog agent.
- [ ] Run daemon/watchdog tests.
- [ ] Commit liveness hardening.

### Task 5: Backups, preflight reconciliation, and paper order drill

**Files:**
- Create: `src/trading_assistant/ops/backup.py`
- Create: `src/trading_assistant/ops/paper_drill.py`
- Modify: `src/trading_assistant/preflight.py`
- Modify: `scripts/launchd/install.sh`
- Modify: `docs/RUNBOOK.md`
- Modify: `README.md`
- Create: `tests/test_ops.py`
- Modify: `tests/test_launch.py`

**Interfaces:**
- `backup_database(source, destination_dir, retention_days)` uses SQLite's online backup API and rotates only matching old backups.
- `paper_drill` refuses non-paper configuration, proposes/approves a tiny non-marketable paper limit order through `TradingService`, confirms broker acceptance, cancels it, and verifies terminal state.
- Preflight reports local/broker reconciliation drift without exposing secrets.

- [ ] Add failing tests for online backup/rotation, paper-only refusal, and reconciliation reporting.
- [ ] Implement the operations modules and preflight checks.
- [ ] Add a daily 02:00 launchd backup agent.
- [ ] Document exactly how to run the one-time paper order drill and daily checks.
- [ ] Run operations and launch tests.
- [ ] Commit operational tooling.

### Task 6: Full verification and safe activation

**Files:**
- Modify only if verification exposes a tested defect.

**Interfaces:**
- The full pytest suite passes.
- The real Alpaca read-only integration test passes.
- The explicit paper order drill submits and cancels exactly one tiny paper order.
- Startup reconciliation repairs the existing stale local order/fill state.
- launchd runs app, daemon, watchdog, and backup jobs; `/health` becomes fresh.

- [ ] Run `uv run pytest -q` and record the exact pass/skip count.
- [ ] Run `uv run pytest tests/test_alpaca_paper_integration.py -q` with the loaded `.env`.
- [ ] Run the explicit paper order drill and inspect Alpaca for zero open orders afterward.
- [ ] Reinstall launchd agents, confirm fresh daemon heartbeat, run reconciliation, and verify the online backup.
- [ ] Re-run preflight and require `READY`.
- [ ] Review the final diff against every plan item and report remaining non-code blockers: no profit guarantee, paper evidence period, and Robinhood Agentic MCP onboarding.
