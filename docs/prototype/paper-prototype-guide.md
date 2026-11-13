# Paper prototype guide

Everything needed to set up, run, demonstrate and evaluate the Alpaca
**paper** trading prototype. Live trading is not supported and has no
activation path.

`$R` is `~/Desktop/project/robinhood/trading-assistant`. Use
`.venv/bin/python` rather than `uv run`: `uv run` rebuilds the environment.

## 1. Acceptance status

Status labels:

- **IMPLEMENTED**: built; not yet tested at the level below.
- **AUTOMATED-TEST VERIFIED**: passes automated tests against Alpaca-shaped
  doubles.
- **ALPACA-PAPER VERIFIED**: observed against the real paper API. Nothing is
  at this level yet; it needs approval D2.
- **BLOCKED**: waiting on an approval.
- **NOT IMPLEMENTED**: not built.

| Area | Status | Evidence |
|---|---|---|
| Installation and startup | AUTOMATED-TEST VERIFIED; repairs L1–L4 done on 2026-10-22; optional `--with-daemon` job added | `test_installation.py`, `test_launchd_installer.py`, `test_bootstrap.py`, `test_launch.py` (preflight summary), `test_startup_help.py` |
| Database selection and recovery | AUTOMATED-TEST VERIFIED; Option C chosen and executed 2026-10-22 (fresh database); a real encrypted backup restored and validated in isolation | `test_installation.py` (choose-database), `test_bootstrap.py` (wrong, unchosen and replaced files refused), `test_restore.py` (restore, activate, backup and migrate guards), `test_runtime_consolidation.py` |
| Dependency and security checks | IMPLEMENTED: `mcp` 1.30.0 (GHSA-84m7-p3x7-pcfv fixed); CI result pending the next push | `uv.lock`; earlier CI runs 38856236602 and 38951855379 failed only on that advisory |
| Proposal creation | AUTOMATED-TEST VERIFIED | `test_operator_test_proposals.py`, `test_autopilot_daemon.py` (propose mode), `test_paper_lifecycle_e2e.py` |
| Human approval and rejection | AUTOMATED-TEST VERIFIED | `test_paper_lifecycle_e2e.py`, `test_api.py`, `test_operator_test_proposals.py` (API route) |
| Broker submission | AUTOMATED-TEST VERIFIED; ALPACA-PAPER **BLOCKED** (D2) | `test_order_submission.py`, `test_alpaca_broker.py`, `test_failure_handling.py` |
| Fill and partial-fill recording | AUTOMATED-TEST VERIFIED | `test_paper_lifecycle_e2e.py`, `test_order_submission.py` (partial, terminal partial), `test_reconciliation_service.py` |
| Cancellation | AUTOMATED-TEST VERIFIED; ALPACA-PAPER **BLOCKED** (D3) | `test_paper_lifecycle_e2e.py`, `test_operator_test_proposals.py` (demo A), cancel race tests |
| Position-reducing exit | AUTOMATED-TEST VERIFIED | `test_operator_test_proposals.py` (demo C; oversell refused), `test_prototype_guardrails.py` |
| Reconciliation | AUTOMATED-TEST VERIFIED | `test_paper_lifecycle_e2e.py` (unexplained fill reported), `test_reconciliation_service.py` |
| Restart recovery | AUTOMATED-TEST VERIFIED | `test_failure_handling.py` (open order and position), `test_autopilot_outages.py` |
| Risk enforcement | AUTOMATED-TEST VERIFIED | `test_risk_engine.py`, `test_prototype_guardrails.py`, approval re-check in `test_order_submission.py` |
| Performance reporting | AUTOMATED-TEST VERIFIED | `test_paper_report.py`, `test_equity_and_fill_quality.py` |
| Shutdown | IMPLEMENTED + AUTOMATED-TEST VERIFIED | `test_autopilot_daemon.py` (cancellation on shutdown), `test_launchd_installer.py` (uninstall order), Panic tests in `test_route_policy.py` |

## 2. Approvals you need to give (one checklist)

Nothing in this list has been done.

**S. Database selection**

- **S1.** Choose Option A, B or C in [`database-decision.md`](database-decision.md).
  The evidence suggests C1 (fresh database plus adopted closed history) if
  the paper account is flat, otherwise A.

**P. Dependency download**

- **P1.** Download `mcp` 1.30.0 (minor upgrade; every other requirement is
  already satisfied) and update the lock.
  - Change: `pyproject.toml` `mcp = ["mcp>=1.30.0,<2"]`, then
    `uv lock --upgrade-package mcp`.
  - Effect: CI's `pip-audit` passes, so its migration and mock-drill steps
    run again.
  - Risk: low. `mcp` is an optional extra used only by the stdio MCP server.
  - Rollback: `git revert` the commit.

**L. Local repairs**, in this order, with rollbacks from section 4:

- **L1.** Stop and remove all five old launchd jobs.
- **L2.** Repair the venv: offline first, may download the six pinned
  upgrades.
- **L3.** Repair the worktree links (never prune).
- **L4.** Designate the checkout.
- **L5.** Install the app, watchdog and backup jobs. Optional; the manual
  launch below works without them.

**D. Broker access and orders**, each separately:

- **D1.** Read-only: run preflight, which reads the account, positions and
  open orders, and reconciles.
- **D2.** Demonstration B: approve one small paper entry intended to fill,
  using operator-test settings you choose.
- **D3.** Demonstration A: approve one paper limit order below the bid, then
  cancel it.
- **D4.** Demonstration C: approve one exit of that position.
- **D5.** Optional: start the daemon in `propose` mode for strategy
  proposals.

## 3. One-time setup (after S1, L1–L4)

1. Choose the database, following the decision record. Check with
   `.venv/bin/python -I src/trading_assistant/installation.py status`, which
   shows the designated checkout, venv, database and launchd lines.
2. Enable test proposals in `config.yaml`, with values you approve:
   ```yaml
   operator_test:
     enabled: true
     symbols: [AAPL]
     max_notional_per_order: 50
   ```
3. Run preflight (**D1**): `.venv/bin/python -m trading_assistant.preflight`.
   The `RUNTIME SUMMARY` comes first: checkout, database, `mode=paper
   endpoint=https://paper-api.alpaca.markets`, `127.0.0.1:8020`,
   auto-execution off. Stop on any FAIL or NEEDS-ME. A failed start prints
   `<role> did not start: <code>. Next: <what to do>`.

## 4. Repairs (L1–L5) with rollback

Record first:

```bash
mkdir -m 700 ~/trading-repair-$(date +%Y%m%d)
cp -p ~/Library/LaunchAgents/com.trading.*.plist ~/trading-repair-$(date +%Y%m%d)/
launchctl list | grep com.trading > ~/trading-repair-$(date +%Y%m%d)/launchctl-before.txt
```

- **L1.** `"$R/scripts/launchd/uninstall.sh"` removes all five jobs, watchdog
  first. Check that `launchctl list | grep com.trading` prints nothing.
  Rollback: copy the plists back and `launchctl bootstrap gui/$(id -u) <plist>`
  each.
- **L2.**
  ```bash
  cp -Rp "$R/.venv" "$R/.venv.backup-$(date +%Y%m%dT%H%M%S)"
  cd "$R" && uv sync --frozen --all-extras --dev --offline
  ```
  If offline fails, the same command without `--offline` downloads; that
  is a separate approval. Rollback: `rm -rf "$R/.venv"` and rename the
  backup back.
- **L3.** Back up `$R/.git/worktrees/safety-foundation/gitdir` and
  `$R/.worktrees/safety-foundation/.git`, then
  `git -C "$R" worktree repair "$R/.worktrees/safety-foundation"`. Rollback:
  copy both files back. Never `prune`.
- **L4.** `"$R/.venv/bin/python" -I "$R/src/trading_assistant/installation.py" designate`.
  Rollback: delete
  `~/Library/Application Support/trading-assistant/installation-root`.
- **L5.** `"$R/scripts/launchd/install.sh"`. It refuses beside a daemon job,
  with a stale venv, or with no chosen database, and installs exactly app,
  watchdog and backup. The watchdog never restarts a daemon that is not a
  launchd job.

## 5. Normal launch and shutdown

**Launch:**

```bash
cd ~/Desktop/project/robinhood/trading-assistant
.venv/bin/python -m trading_assistant.preflight      # read the RUNTIME SUMMARY
./scripts/start.sh                                   # loopback HTTPS console
open https://localhost:8020                          # log in with your app token
```

Optionally, for strategy proposals: set `autopilot.mode: propose`, run
`./scripts/operator.sh`, then 7 Monitoring, 1 Start, and type
`START PAPER MONITORING`.

**Shutdown:**

1. In the operator terminal: 7 Monitoring, 2 Stop (`STOP PAPER MONITORING`),
   if the daemon is running.
2. Then `./scripts/stop.sh`.

Stopping does **not** cancel orders at Alpaca or close positions. Check the
Orders panel first and cancel anything you do not want left working.

## 6. Demonstrations (D2–D4)

All three use the console's **Operator test proposal** form. Each creates
one labelled proposal (`OPERATOR TEST (not a strategy signal)`); nothing is
sent until you approve it under **Pending decisions**. The full risk check
runs again at approval: allowlist, per-order and per-ticker caps, 3 open
positions, 5 orders per session, market hours, quote freshness and spread.

**B: entry intended to fill.**

1. Scenario B, symbol AAPL, Market, Notional 25, plus a reason.
2. Approve it.
3. Orders shows `submitted`, then `filled` with a broker id; Alpaca
   positions shows the shares.
4. Then run Operations, 2 Reconcile in the terminal.

The proposal is refused if the market is closed (`market_closed`); it is
reported, never queued. A fill is likely but not guaranteed. If it does not
fill, the order stays `submitted` and you can cancel it. Nothing is changed
to force a fill.

**A: submit, then cancel.**

1. Scenario A, Limit, quantity 0.1, a limit price a little below the bid
   (the form refuses one at or above the bid), plus a reason.
2. Approve it, then **Cancel at broker** in Orders. Expect `canceled`.

It is *expected* to stay unfilled, not guaranteed: if the price drops to
your limit before the cancel, it fills, fully or partly. The lifecycle
records whichever happened (a partial fill is reported as such), and an exit
(C) removes any position.

**C: position-reducing exit.**

1. Scenario C, Market, quantity no larger than the shares held. More is
   refused (`exit_exceeds_position`), so it cannot open a short.
2. Approve it.
3. Expect `filled`, the position reduced or gone, and reconciliation clean.

Afterwards: `.venv/bin/python -m trading_assistant.ops.paper_report --with-encrypted`.
Test trades appear under `operator_test`, separately from strategy trades.

## 7. What the console shows

| Need | Where |
|---|---|
| PAPER mode, live unsupported, endpoint, checkout, database | Runtime and limits |
| Broker connectivity, quote freshness, daemon, reconciliation, breakers | Environment strip and truth panel |
| Account (equity, buying power, cash, exposure) | Account |
| Pending proposals: symbol, side, size, rationale, exit logic; approve or reject | Pending decisions, with an approval dialog that shows the resulting exposure |
| Order status, partial fills, rejections, cancellations, broker ids | Orders (Cancel at broker for working orders) |
| Positions | Alpaca positions |
| Risk limits and why an action was blocked | Runtime and limits; risk log; risk reasons on refused proposals |
| Last successful synchronization | Truth panel (last confirmed broker contact) |
| Emergency stop | Panic procedure; see "Stopping, explained" |

**Stopping, explained** (also shown in the console):

- **Stop proposal generation:** stop the daemon. Working orders continue.
- **Prevent submissions:** do not approve. Panic latches the global breaker;
  reduce-only exits are still allowed.
- **Cancel working orders:** per order, or Panic for all.
- **Close positions:** only by approving an exit. Never automatic.

## 8. Restore from an encrypted backup

```bash
.venv/bin/python -m trading_assistant.ops.restore verify \
  --artifact .local/encrypted-backups/<artifact>.aesgcm --into .local/restore-$(date +%Y%m%d)
```

`verify` decrypts into a new private directory and validates:
authentication, SHA-256, integrity, foreign keys, schema compatibility and
permissions. It never touches the live database.

To use the restore, stop the app and daemon, then:

```bash
.venv/bin/python -m trading_assistant.ops.restore activate --restored <restored.sqlite3> \
  --restored-sha256 <from verify> --current-sha256 "$(shasum -a 256 trading_assistant.db | cut -c1-64)"
```

The current file is kept as `trading_assistant.db.before-restore-<time>`,
and the restored file is re-chosen. To roll back: stop, rename the preserved
file back, then `choose-database --replace`.

## 9. Evaluating the strategy honestly

**Exit policy (both strategies).** `autopilot.stop_loss_pct` sells the
autopilot's own shares once a completed daily close is at or below the
average entry price less that percentage; `autopilot.reentry_cooldown_days`
blocks re-buying a name for that many calendar days after selling it. Both
are applied identically by the backtester (`strategies/exit_policy.py`). A
gap down fills below the stop price. After the risk engine refuses an
autopilot order, the same symbol and side is not proposed again for
`retry_after_rejection_days`, and nothing is proposed while a broker-drift,
global, loss or drawdown latch is tripped.

**Development-window results (2026-10-23).** Seven allowlisted names, daily
Alpaca bars, 2017-08 to 2025-10 in two halves; the last 12 months were not
used. Mean per-symbol total return, mean max drawdown, round trips:

| Rule | First half | Second half |
|---|---|---|
| Buy and hold | 349%, −40%, 0 | 165%, −53%, 0 |
| `sma_trend` | 103%, −32%, 94 | 88%, −31%, 101 |
| `sma_crossover` (50/200) | 267%, −39%, 16 | 130%, −35%, 19 |
| `sma_crossover`, 20% stop, 30-day cooldown | 264%, −39%, 16 | 130%, −35%, 20 |

What this does and does not say:

- The crossover beat `sma_trend` in both halves with about a fifth of the
  trades, so it was chosen for this Mac (`config.local.yaml`).
- Stops on daily closes barely changed results; the 20% stop is a disaster
  backstop, not a source of return.
- **Buy and hold beat every rule.** These names were picked knowing they
  went up, so every result here is flattered by hindsight.
- The holdout year is evaluated once, when readiness evidence is recorded.

**The original rule (`sma_trend`)** uses completed daily bars and a 320-bar
window:

- **Long** when the 20-day SMA is above the 50-day *and* the close is at or
  above the 200-day; the 200-day filter is skipped if it is unavailable.
- **Flat** when the 20-day is at or below the 50-day, or the close is below
  the 200-day.
- **Hold** (no change) when the 20- or 50-day is missing.
- **Entry size:** `autopilot.notional_per_trade` (config.yaml: $1,000;
  consider lowering it).
- **Exit:** sells only the shares the autopilot's own fills bought.
- **The rule itself has no stop-loss or profit target**
  (`test_there_is_no_stop_loss_a_deep_drawdown_alone_never_exits`); the
  exit policy above adds one.

**The report** (`ops.paper_report`):

- Sources are kept apart: `autopilot`, `operator_test`,
  `human_rule_or_chat`.
- **Realized P/L** is first-in, first-out over trusted fills, without fees.
- **Unrealized P/L** uses only `--mark` prices you supply, noted as such.
- **Realized drawdown** is shown separately from **equity drawdown**, which
  comes from daemon snapshots every 15 minutes, only while the daemon runs;
  the largest gap is reported.
- **Benchmark:** SPY over the same span as the snapshots.
- **Fill quality:** fill price against the decision-time mid in signed
  basis points, plus approval and fill delays.
- **Exposure:** peak gross exposure at cost and from snapshots.
- **Turnover,** holding periods and trade counts.

**Out of sample.** `python -m trading_assistant.autopilot backtest` (market
data only; needs approval like D1) records a real-data backtest with a
chronological holdout. Do not tune parameters on the period you then
evaluate.

**What not to conclude.** No fixed number of trades or months establishes
that the strategy is profitable. Paper fills ignore queue position and
market impact, and a small sample is dominated by chance. Treat paper
results as evidence that the system behaves as designed and as a rough check
against the backtest, not as a forecast.

## 10. Risk register

| Risk | Control |
|---|---|
| Duplicate orders | `client_order_id` equals the intent key. A lost response, 401, 403, 429 or 5xx on submit becomes *acceptance unknown*, never resubmitted and resolved by key (`test_failure_handling.py`). |
| Wrong database | Path-and-inode choice enforced by startup, preflight, installer, report, backup and migration; re-choose explicitly after a restore |
| Stale launchd jobs | Uninstall removes all five, watchdog first; install refuses beside a daemon job. Live until L1 runs |
| Watchdog loops | Restarts only loaded launchd jobs |
| Reconciliation failure | Fails closed; drift trips a breaker; never cleared automatically |
| Accidental live trading | Hard-coded paper endpoint, outbound allowlist, live rejected at startup |
| Data loss | Old databases untouched; consolidation needs exact-hash acknowledgment; restore activation preserves the replaced file |
| Runaway orders | Autonomous orders only after the readiness gate and your fingerprint approval; caps on open positions and orders per session; per-order notional cap; no repeat of a refused order for 3 days; nothing proposed while a safety latch is tripped |
| Repeated trades in one name | 30-day re-entry cooldown after an exit; at most one intent per symbol, side and session |
| Snapshot hang | Equity snapshots run outside the safety loop with a timeout |

## 11. Known limitations

- Nothing has run against Alpaca paper yet. Every ALPACA-PAPER item is
  blocked on D1–D4.
- Proposals expire after 15 minutes; approve promptly.
- Autopilot orders are notional market orders; operator tests may use
  limits.
- Equity snapshots and drawdown cover only daemon uptime.
- The `mcp` advisory blocks CI's audit, migration and mock-drill steps until
  P1.

## 12. Autonomous paper trading on this Mac

Machine-specific settings live in the git-ignored `config.local.yaml`, merged
over `config.yaml` (preflight prints `config: config.yaml + config.local.yaml`).
The checked-in default stays `autopilot.mode: observe`.

1. **The paper account must reconcile.** A fresh database plus positions it
   never recorded is drift, and drift blocks every order. Close the positions
   in the Alpaca paper dashboard. This account's history spans two databases
   and notional orders, so `ops.paper_history` cannot import it exactly;
   instead record a baseline, review its receipt, then reset the
   `broker_drift` breaker in the console or operator terminal:
   `.venv/bin/python -m trading_assistant.ops.paper_baseline`. Earlier P/L
   stays in the archived databases and the Alpaca dashboard.
2. **Record backtest evidence** (one-shot holdout):
   `.venv/bin/python -m trading_assistant.autopilot backtest --years 10`.
3. **Release evidence** for the running commit at
   `.local/verification/release-results.json` (from
   `scripts/verify_loopback_release.py`).
4. **Start the daemon** and keep it running:
   `./scripts/launchd/install.sh --with-daemon`. It restarts after a crash at
   most every 5 minutes, and the watchdog restarts it on a stale heartbeat.
5. **Approve.** After the first session, run
   `.venv/bin/python -m trading_assistant.autopilot readiness`, review it, and
   set `autopilot.readiness.approved_fingerprint` in `config.local.yaml`.
   Any change to the rule, universe, sizing, limits or thresholds voids it.
6. **Watch** the console's Autopilot panel: each session's decisions, orders
   and the readiness report. Stop with `./scripts/launchd/uninstall.sh` or
   `autopilot.mode: observe`; neither cancels orders or closes positions.
