# Autopilot: architecture, data policy and readiness

The autopilot is the only component that can place orders without a per-order
human approval. It is paper-only, runs only inside the daemon, and cannot place
any order until a fail-closed readiness gate passes. Passing that gate is
evidence that the configured rule ran as designed on genuine observations. It
is **not** evidence of profitability. **Live trading is not supported.**

## Modes and permissions

| Setting | What happens | Orders |
|---|---|---|
| `autopilot.mode: off` | Nothing runs | None |
| `autopilot.mode: observe` (checked-in profile) | Once per session the daemon decides and records evidence | None (simulated) |
| `autopilot.mode: paper` and readiness **not** met | Same as observe; the session is recorded as `blocked` | None |
| `autopilot.mode: paper` and readiness met | Decides and places Alpaca **paper** orders through the risk engine | Paper only |
| `trading.mode: live` | Rejected at startup by the runtime and by the gate | Never |

The daemon only runs when the operator starts it. No launchd job runs the
autopilot. The scheduled standalone job and `--once`/loop entry points were
retired.

## Single owner

```text
operator starts daemon ─► runtime:daemon tenure (one per database)
                          ├─ safety loop every 15s: order sync, loss limits, rules
                          ├─ daily analysis task (isolated)
                          └─ AutopilotRunner (isolated, never overlapping)
                               ├─ due? market open and ≥ run_after_open_minutes
                               ├─ already recorded for (mode, session, fingerprints)? → skip
                               ├─ readiness gate → paper orders only if ready
                               ├─ Autopilot.run_once(session, cancel)
                               └─ record evidence → write readiness report
```

- **Database and orders.** The daemon owns autopilot scheduling and autopilot
  orders. The app (human-approved orders) and the MCP server never run the
  autopilot. The autopilot's ownership ledger leaves their positions alone: it
  sells only what its own trusted fills bought and never buys into a position
  another workflow holds.
- **Runtime tenure.** The runner checks `runtime_tenure_guard.ensure_owned()`
  before a cycle and before recording evidence; the engine checks it at cycle
  start and before recording intent and approving each order; and the broker is
  wrapped by `TenureGuardedBroker`. A lost tenure raises `TenureLost`, which
  stops the daemon without writing anything else.
- **Installation.** Production runtimes bind a real broker only from the
  designated installation (see "Installation designation" below).
- **Failure isolation.** An autopilot failure is recorded as a `failed` cycle
  and retried at most three times per session. It never trips kill switches.
  A timed-out cycle is asked to cancel between symbols, and no new cycle starts
  until the old one exits.
- **Shutdown and restart.** Shutdown requests cancellation before releasing
  tenure. A cancelled cycle is not recorded as complete, so the next daemon
  run repeats it, which is safe because of intent keys.

## Duplicate-safe orders

Every autopilot order's idempotency key is
`action_key(strategy, symbol, side, session)`. The broker adapter sends that
key as Alpaca's `client_order_id`.

| Situation | Outcome |
|---|---|
| Cycle repeated in the same session | The existing order is in flight, so the symbol is skipped |
| Crash after recording intent, before approval | The replayed `PROPOSED` order is approved (`resumed_intent`) |
| Broker response lost | The outbox records `acceptance_unknown`. It is never resubmitted, it blocks the symbol, and reconciliation resolves it by `client_order_id` |
| Rejected, cancelled or expired intent | Not retried in that session; the next session has a new key |
| Partial fill | In flight until terminal; ownership counts only filled quantity |
| Two schedulers at once | One order (unique key and submission barrier) |

## Data policy (shared with backtests)

`signals/sessions.py` defines one decision-time policy for backtests, the
autopilot, `/analyze`, `/screen`, shadow analysis and the digest:

- **Session date.** A daily bar belongs to its session date: New York for
  equities, UTC for crypto.
- **Completion.** A session is complete per one market-clock observation. While
  the market is open, only earlier sessions count; while closed, the most
  recently opened session counts. Holidays, weekends, early closes and DST
  therefore follow the exchange calendar.
- **Finality.** A bar is final only if its frame was fetched after 20:00
  New York time on that date (crypto: 01:00 UTC the next day). A frame captured
  mid-session never supplies a close; caches fetched before the latest final
  instant are refreshed, whatever their age.
- **Window.** Features use the backtest's 320-bar window (`FEATURE_LOOKBACK`).
  Identical eligible inputs give identical features and decisions in the
  backtest engine and the live path (`tests/test_sessions.py`).
- **Clock.** Order paths require a market clock (`require_market_clock`). The
  conservative no-clock fallback, which never treats today as complete, is for
  analysis only.
- **Freshness per consumer.** Autopilot decisions skip symbols whose newest bar
  is older than `max_feature_age_hours`. Screen sources rebuild when a new
  session becomes final, with a four-hour backstop. A failed rebuild serves
  nothing stale.

## Readiness gate

`python -m trading_assistant.autopilot readiness` prints the report the daemon
writes to `.local/autopilot/readiness.json` (mode 0600). Every requirement must
pass:

| Requirement | Passes when |
|---|---|
| `paper_trading_only` | `trading.mode` is paper |
| `installation_designated` | This checkout is the designated installation |
| `runtime_ownership` | The running daemon holds its tenure |
| `configuration_valid` | Universe and sizing pass startup checks |
| `backtest_evidence` | A succeeded **real-data** backtest of this strategy and decision code exists: covers the universe, ≥ `min_backtest_calendar_days`, has a holdout, ≤ `backtest_max_age_days` old |
| `observed_sessions` | ≥ `min_observed_sessions` distinct sessions with a clean cycle, spanning ≥ `min_observation_calendar_days` |
| `observation_failures` | Failed cycles ≤ `max_failed_cycles` |
| `degraded_sessions` | Sessions with stale or unavailable data ≤ `max_degraded_sessions` |
| `evidence_recent` | Latest clean observation ≤ `max_evidence_age_hours` old |
| `release_verification` | The release verifier passed for the **running commit** (`release_evidence_path`) |
| `no_blocking_safety_latch` | No drift, loss, drawdown or operator-global breaker is tripped |
| `operator_approval` | `approved_fingerprint` equals the report's approval fingerprint |

Evidence counts only under the current **configuration fingerprint** (strategy,
universe, sizing, cadence, risk limits) and **decision-code fingerprint** (the
rule, feature pipeline, session policy and engine source). The approval binds to
both. Any relevant change restarts observation and voids the approval.
Observation is measured in distinct sessions and, separately, in calendar days,
so a burst of cycles cannot stand in for elapsed time.

Defaults (configurable, deliberately conservative): 20 sessions over 28
calendar days, at most 2 degraded sessions, 0 failed cycles, evidence at most
96h old, and a backtest at most 90 days old covering at least 730 days.

### Evidence

Evidence is stored as `audit_events` with encrypted `detail_json`:

- `autopilot.cycle` records the session, timestamps, mode and execution kind
  (`simulated` or `paper`), the fingerprints and running commit, and each
  symbol's decision and reason with its data age. It also records intended
  actions (observe) or orders (paper), risk rejections and the readiness
  verdict.
- `autopilot.backtest` records the strategy, data source, window, symbols,
  holdout, cost model, fingerprints, running commit, per-window results against
  buy-and-hold, and the "simulated" disclaimer.

`python -m trading_assistant.autopilot backtest` records backtest evidence. It
downloads real bars with the operator's Alpaca credentials and needs the
`paper-drill` role, so stop the app and daemon first.

The verifier refuses to run in a checkout that has a root `.env`. Run it in a
clean clone of the same commit and point `release_evidence_path` at that
clone's `.local/verification/release-results.json`.

## Installation designation

`python -m trading_assistant.installation status|check|designate` manages
`~/Library/Application Support/trading-assistant/installation-root`: one
absolute, symlink-free path in a private (0600) file inside a private (0700)
directory. `status` and `check` are read-only; only `designate` changes it, and
it refuses to replace a different valid designation without `--replace`.

The following refuse to act from any other checkout:

- the operator launcher (`scripts/operator.sh`);
- the operator terminal;
- runtime consolidation;
- the launchd installer;
- production broker binding.

The launcher and the installer also detect a virtual environment that imports
another checkout (stale after a move) and print the repair command.

**Scope.** The designation guarantees one designated checkout per macOS user.
It cannot stop another user account, another machine, a second `HOME`, or a
deliberate re-designation from running a second runtime against the same paper
account; nothing local can. Within one database, runtime tenure still prevents
two writers.
