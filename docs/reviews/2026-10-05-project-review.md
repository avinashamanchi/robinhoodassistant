# Project review — 2026-10-05

Scope: the whole repository at `b92ec3f`, with a deep pass on the newest and
least-hardened component (the autonomous paper **autopilot**, commits
`51ec49b`..`b92ec3f`). I also checked the installed state on the operator
machine: LaunchAgents, the bar cache, and the autopilot log. Everything changed
in this review is listed under [Improved version](#improved-version).

This is a software and operations review. It does not judge whether any
strategy is profitable and is not investment advice.

## Executive summary

**Goal.** A local, single-operator trading assistant for an Alpaca *paper*
account. The LLM may propose and a human approves. A deterministic risk engine
has the final say on every order. Research and backtests stay visibly separate
from broker evidence. A recent addition, the opt-in autopilot, decides and
executes paper trades with no human involved.

**Assessment.** The safety foundation is unusually strong: fail-closed startup,
per-scope circuit breakers, an idempotent order outbox, encrypted sensitive
fields, pinned outbound origins, a 9k-line static release gate, and about 114k
lines of tests. The autopilot was not built to that standard. It looked healthy
but was not doing useful work:

* **Its market data was frozen.** The live feature path reused a parquet
  cache with no expiry. The newest cached bar was **2026-07-10** (2026-07-17 for
  GOOG/META/TSLA), so every daily run through October decided on July prices
  and logged `placed 0 order(s)`. The same stale data fed `/analyze`, `/screen`,
  and shadow-mode grading.
* **The backtest could not support it.** The autopilot's `sma_crossover`
  (20/50 with a 200-day filter) is a different rule from the backtester's
  `sma_crossover` (50/200). No backtest had ever evaluated the rule that trades.
* **It could interfere with the rest of the system.** It sold the *entire*
  position on a flat signal, including positions opened by plans or human
  approvals. It also treated missing data as an exit signal, could resubmit
  while an earlier order was still in flight, and never synced fills in loop
  mode.
* **Its operations had drifted.** Moving the checkout to
  `~/Desktop/project/robinhood` left all five LaunchAgents and the venv's
  editable install pointing at the old path. The hand-written autopilot plist
  wrote unbounded, world-readable (`0644`) logs, which breaks the project's own
  logging invariant. Each line was also logged twice.
* **Its runtime role conflicts with the app.** The autopilot reuses the
  `paper-drill` role, which takes the *exclusive maintenance* tenure. It
  therefore cannot run while the app, daemon, or MCP server is up.

The data, strategy, ownership, logging, and scheduling problems are fixed in
this change, with tests. The runtime-role conflict is designed below but left
for a reviewed follow-up because it touches the audited security boundary.

## What is working well (preserve)

* **Layered, fail-closed safety.** `propose_order` → `approve_order` → the risk
  engine at execution time, plus persistent breakers per asset class and per
  scope. The autopilot correctly reuses this path instead of calling the broker
  directly.
* **Evidence vocabulary.** `DESIGN.md`'s Verified / Caution / Blocked / Unknown /
  Stale / Simulated states, and the rule that unknown never looks verified.
* **No-lookahead by construction.** `DataView` cannot return future rows, and
  the holdout guard logs every access.
* **Strict configuration.** Unknown keys fail to load, and the risk section is
  documented as "the law".
* **Secrets discipline.** Keychain-only runtime secrets, a redaction filter,
  query-string credentials banned, and pinned origins for each adapter.
* **Release gate and CI.** Pinned action SHAs, `uv lock --check`, pip-audit,
  gitleaks over full history, an isolated mock safety drill.
* **Honest disclaimers.** The README never implies profitability or live
  readiness.

## Issues to fix

Severity reflects impact on correctness and safety for this paper setup.

| # | Severity | Issue | Why it matters | Status |
|---|---|---|---|---|
| 1 | Critical | `download_alpaca_bars` / `CoinGeckoClient.bars` reuse a cache file forever; live callers never refresh | Autopilot, `/analyze`, `/screen` and shadow grading all ran on July bars for 12 weeks | **Fixed** |
| 2 | Critical (ops) | Checkout moved; LaunchAgents and `.venv` editable install still reference `~/Desktop/robinhood/...` | Every scheduled job now fails; `python -m trading_assistant.*` raises `ModuleNotFoundError` | **Operator action** (below) |
| 3 | High | Autopilot `sma_crossover` ≠ backtester `sma_crossover` | No backtest evidence exists for the traded rule; names mislead | **Fixed** (`sma_trend` shared class) |
| 4 | High | Missing SMAs returned `flat`, which sells a held position | A data glitch liquidates positions; docstring claimed the opposite | **Fixed** (HOLD) |
| 5 | High | Flat signal sold the *whole* broker position | Liquidates plan- or human-owned positions; strands plan exit rules and invites drift | **Fixed** (ownership ledger) |
| 6 | High | No in-flight check; loop never synced broker fills | Unfilled market buy could be repeated; loop mode never sees its own fills | **Fixed** (`sync_open_orders` + in-flight guard) |
| 7 | High | Autopilot runs as `paper-drill` → exclusive maintenance tenure | Cannot coexist with app/daemon/MCP; with a healthy app every scheduled run fails | **Documented**; design below |
| 8 | Medium | `logging.basicConfig` + runtime handler duplicated every line; plist streamed to `~/Library/Logs` at `0644`, unbounded; only "placed N orders" logged | Violates the "redacted, owner-only, bounded" invariant; no way to see *why* nothing traded | **Fixed** |
| 9 | Medium | One transient broker failure at startup lost the day (2026-10-05 run); degraded runs exited `0` | Silent missed days | **Fixed** (bounded retry, transient-only; exit `1` when degraded) |
| 10 | Medium | Autopilot plist hand-written with a machine path, not produced by `install.sh`, not covered by the bounded-stream test | Source of the stale-path and log-permission bugs | **Fixed** |
| 11 | Medium | Breaker self-heal ran regardless of market state, logged at INFO | Clearing a safety latch should be visible and only attempted when conditions are observable | **Fixed** (open markets only, WARNING) |
| 12 | Medium | Docs contradict the checked-in config: "everything dangerous defaults OFF" while `config.yaml` sets `autopilot.enabled: true`; shadow mode's "zero orders" does not apply to the autopilot | Readers will believe nothing trades autonomously | **Fixed** |
| 13 | Medium | Plaintext DB copies (`trading_assistant.db.{legacy,tripped,breaker}-*`) sit in the repo root, not ignored | One `git add .` from committing trading data | **Ignored** (`*.db.*`); move/encrypt them yourself |
| 14 | Medium | Autopilot was enabled without the evidence gate the README demands for execution features | Undercuts the project's central "evidence before execution" principle | Recommended |
| 15 | Medium | Release evidence (`docs/release/2026-07-27-*`) predates the autopilot | "Verified" docs describe a different system | Recommended |
| 16 | Low | Very large modules (`service.py` 3.2k, `rules/repository.py` 2.6k, `app/policy.py` 2.3k, `check_release_safety.py` 9.1k lines) | Review cost; risk of missed interactions like #5 | Recommended |
| 17 | Low | 11 test files named after process rounds (`test_task6_round5.py`, `test_llm_budget_review_3.py`, ...) | Names say nothing about behavior; hard to find coverage | Recommended |
| 18 | Low | Bar cache key ignores `years` (live 2y, backtests may request 5y) | Live refresh can shrink a backtest's cached history | Recommended |
| 19 | Low | Repo is named `robinhoodassistant` but ships no Robinhood integration (Alpaca only) | Sets wrong expectations | Recommended |

## Recommended improvements

### High priority (next)

1. **Repair the local install after the move** (operator, five minutes):

   ```bash
   uv sync --all-extras --dev   # re-points the editable install at the new path
   uv run python -m trading_assistant.autopilot --dry-run
   ./scripts/launchd/install.sh --with-autopilot   # only after reading #2
   ```

   If `uv sync` does not fix the stale script shebangs, recreate the venv with
   `uv venv --python 3.11` and then run `uv sync` again.

2. **Give the autopilot its own runtime, not the drill's maintenance lock.**
   There are two viable designs:

   | | A. New `autopilot` runtime role | B. Run the cycle inside the daemon |
   |---|---|---|
   | Tenure | New `runtime:autopilot` resource; coexists with app, excludes maintenance | Reuses `runtime:daemon` |
   | Order/fill sync | Autopilot calls `sync_open_orders` (done in this change) | Daemon already syncs every 15s |
   | Supervision | Needs its own heartbeat/watchdog wiring | Daemon heartbeat + watchdog already exist |
   | Security delta | New role in secrets map, outbound policy, logging roles, release gate | None; daemon already has Alpaca access |
   | Human start | Separate process / launchd job | Starts only when the operator starts the daemon |

   **Recommendation: B.** Add an autopilot step to the daemon loop, gated by
   `autopilot.enabled` and a new `autopilot.host: daemon` setting, and keep the
   standalone CLI for `--dry-run`. The daemon already does the sync, breaker,
   heartbeat, and supervision work this needs, and the daemon's
   explicit-operator-start policy keeps a human decision in front of autonomous
   trading. Write it up through the existing `docs/superpowers/specs` →
   `plans` flow, because it changes runtime topology.

3. **Add an evidence gate before `autopilot.enabled` takes effect.** Require a
   persisted backtest run id for `autopilot.strategy` (holdout compared with
   buy-and-hold, which the backtester already reports) and N weeks of clean
   `--dry-run` decision logs. Check both at startup the same way preflight
   checks Keychain rows.

### Medium priority

4. Regenerate `docs/release/*` verification and operational status for the
   current HEAD, including the autopilot.
5. Move the three plaintext DB copies out of the repo, or into the encrypted
   backup flow, and delete them through your normal procedure.
6. Include `years` and the timeframe window in the bar cache key, or store
   one superset frame and slice it.
7. Rename the round-named test files by behavior, for example
   `test_task6_round5.py` → `test_backup_transaction_recovery.py`.
8. Split `service.py` along its existing seams (orders facade, reconciliation
   facade, positions/exposure, plan cancellation).
9. Restructure the README (see below).

### Low priority

10. Rename the GitHub repository to match the product, or add a one-line note
    at the top of the README.
11. Use the configured clock's session boundary (not UTC midnight) for
    `max_orders_per_day`, so the cap tracks the trading day for every asset
    class.

## Improved version

### Changes made in this review

| Area | Change | Files |
|---|---|---|
| Market data | `max_cache_age_seconds` on Alpaca and CoinGecko loaders; live callers default to a 4h bound; atomic parquet publication | `backtest/data.py`, `backtest/coingecko.py`, `analyst/live_features.py` |
| Strategy parity | New `SmaTrend` (`sma_trend`) holding the autopilot's original rule, registered with the backtest runner; autopilot strategies are now the backtester's classes | `strategies/sma_trend.py`, `backtest/runner.py`, `autopilot.py`, `config.py`, `config.yaml` |
| Autopilot correctness | HOLD on incomplete data; broker order sync before deciding; in-flight guard; trusted-fill ownership ledger; positions read once per cycle; feature freshness guard (`max_feature_age_hours`, default 120) | `autopilot.py`, `config.py` |
| Autopilot ops | Per-symbol `autopilot decision ...` log lines; no duplicate logging; transient-only startup retry; exit `1` on degraded runs; self-heal only for open markets at WARNING | `autopilot.py` |
| Scheduling | `install.sh --with-autopilot` (+ `AUTOPILOT_AT`) generates the weekday job with discarded launchd streams; `uninstall.sh` removes it; reference plist regenerated | `scripts/launchd/*` |
| Hygiene | `*.db.*` ignored | `.gitignore` |
| Docs | README autopilot section rewritten to match behavior; launchd README covers scheduling, moves, and the tenure constraint | `README.md`, `scripts/launchd/README.md` |
| Tests | Cache expiry, `sma_trend`, ownership/in-flight/staleness/sync/hold, startup retry, installer generation | `tests/test_marketdata.py`, `tests/test_strategies.py`, `tests/test_autopilot.py`, `tests/test_task9_round2.py`, `tests/test_launch.py` |

The checked-in profile keeps `autopilot.enabled: true` and switches
`strategy: sma_crossover` → `strategy: sma_trend`. That preserves the rule the
autopilot already traded. Only the name changed, so a backtest of the name now
matches.

### Autopilot decision table (new behavior)

| Signal | Account holds symbol? | Autopilot-owned qty | Action | Logged reason |
|---|---|---|---|---|
| long | none | — | buy `notional_per_trade` | `enter_long` |
| long | yes, autopilot share > 0 | > 0 | none | `already_long` |
| long | yes, none autopilot-owned (or short) | 0 | none | `position_managed_elsewhere` |
| flat | none | — | none | `already_flat` |
| flat | long | > 0 | sell `min(owned, held)` | `exit_long` |
| flat | long or short | 0 | none | `position_managed_elsewhere` |
| hold | any | any | none | `signal_hold` |
| any | any order in flight | any | none | `order_in_flight` |

Cycle-level skips: `market_closed`, `features_unavailable`, `features_stale`,
`positions_unavailable`, `order_sync_unavailable`, `daily_cap_reached`.

### Proposed README structure

The current README is about 280 lines that mix phase history, operator
procedure, and safety claims. Proposed structure:

1. **What this is / is not** (5 lines: paper-only, human-gated, the autopilot
   exception, not advice).
2. **Quickstart** (install, preflight, start app).
3. **Operating modes** (table: human-gated, rule worker, shadow, autopilot;
   what each can do; default; checked-in value).
4. **Safety model** (the numbered list, kept).
5. **Autopilot** (behavior table above, scheduling, constraints).
6. **Backtesting**.
7. **Links**: RUNBOOK, launchd README, release evidence, design contract.
8. **History** (phase list moved to `docs/HISTORY.md`).

## Questions and assumptions

* **Time zone.** I assumed the operator machine runs on US Pacific time, so
  07:00 local is 10:00 New York. If not, set `AUTOPILOT_AT`.
* **App and autopilot together?** If you want the web app running while the
  autopilot trades, issue #7 is the next thing to fix. Today they cannot hold
  tenure at the same time.
* **Ownership.** I assumed the autopilot should never buy into or sell out of
  a position another workflow opened. If you want it to manage everything in
  its universe, that should be an explicit config flag, not the default.
* **Committed opt-in.** `config.yaml` is shared through git, yet it enables
  autonomous trading. Is that intended for every checkout, or should it be a
  local override?
* **Freshness bounds.** I picked 4 hours (cache reuse) and 120 hours (feature
  age) to tolerate weekends and holidays. Shorten them if you run intraday.
* **Not run.** I did not run `--dry-run` against the paper account or reinstall
  LaunchAgents. Both touch your credentials and persistent system
  configuration, so they are left for you.
