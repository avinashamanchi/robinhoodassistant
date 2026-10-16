# Paper prototype guide

How to repair the local installation, start the prototype against Alpaca
**paper**, run a controlled demo, and judge the results. It complements
[`current-state-2026-10-15.md`](current-state-2026-10-15.md) (the Phase 0
facts) and [`database-decision.md`](database-decision.md) (an open decision).

`$R` is `~/Desktop/project/robinhood/trading-assistant`. Every step that
changes your machine, contacts Alpaca, or writes a database is marked
**[approval]**. None has been run.

## What changed in this pass

| Change | Effect |
|---|---|
| Explicit runtime database (`installation.py choose-database`) | Startup, preflight, the installer and the report refuse any database except the recorded file (same path and inode). No more choice by working directory |
| Preflight runtime summary | Prints checkout, database file, paper endpoint, loopback bind, autopilot mode and auto-execution before any check; stops before opening the database or broker if the database was not chosen |
| `autopilot.mode: propose` | The deterministic strategy's decisions become `PROPOSED` orders with a full rationale in the pending queue. The autopilot never approves them; you do |
| Pending list shows the rationale | Console and API show why each proposal exists before you approve |
| Guardrails `risk.max_open_positions: 3`, `risk.max_orders_per_day: 5` | Enforced by the risk engine on every order; never block a reduce-only exit; an unknown daily count blocks |
| `python -m trading_assistant.ops.paper_report` | Read-only evaluation: funnel, FIFO P/L, wins and losses, drawdown, turnover, exposure, autopilot evidence |
| Consolidation guard | Refuses to discard destination history unless given its exact SHA-256 |
| Installer and uninstaller | Installer refuses while a daemon job exists or no database is chosen; uninstaller stops the watchdog first |
| Watchdog | Restarts only loaded launchd jobs; a daemon you run yourself is skipped, not kickstarted every minute |

## Verification (2026-10-16)

| Check | Commit | Result |
|---|---|---|
| Authoritative release check (clean clone, trusted toolchain, no `.env`, credentials or databases) | `f253b23` (final code) | **PASS**, all 8 stages: migration 186, security 660, safety 142, frontend 188, full suite 5072 with the one allowed skip, branch coverage at least 90 %, static gate. Branch coverage took 1569 s of its 1800 s cap on a loaded machine |
| Linux CI run 38856236602 | `54c9e00` (docs head), synthetic merge `95cb23f` onto `main@b92ec3f` | Verifier **PASS** (all 8 stages); `uv pip check` 84 packages compatible; gitleaks 29 commits, no leaks. **`pip-audit` FAILED**: `mcp` 1.28.1, CVE-2026-59951 (fixed in 1.30.0), published after the clean 2026-10-10 audit. As a result CI skipped its migration and mock-drill steps |
| Test manifest | `f253b23` | 5017 to 5072 node IDs: 0 removed, 55 added (all new tests of this pass); the four focused manifests are unchanged |
| Hermetic lifecycle acceptance (`test_paper_lifecycle_e2e.py`) | `f253b23` | Proposal gating, fill, rejection, cancellation, lost response, expiry and unexplained-fill drift: 7 of 7 pass |

**The `mcp` advisory.** In affected versions, a Streamable HTTP MCP server
never reclaims session state, so a reachable client can exhaust memory. This
project's MCP server runs over **stdio only** (`run_stdio_async`), so that
path does not apply, and `mcp` is an optional extra the paper prototype does
not use. The fix is still to upgrade to 1.30.0, a minor release within the
existing `mcp>=1.2` constraint. That needs a package download and a lock
change, so it awaits your approval. The finding is not suppressed.

## Real versus simulated

| Part | Real | Simulated or absent |
|---|---|---|
| Broker | Alpaca **paper** API only (hard-coded; live rejected at startup) | Paper fills are Alpaca's simulation of execution |
| Market data | Alpaca historical bars and quotes | — |
| Strategy | `sma_trend` on completed daily bars | No intraday logic, stops or targets |
| Orders | Submitted to the paper account after your approval | — |
| Hermetic tests | — | Alpaca-shaped doubles only; they prove logic, not connectivity |
| Evaluation | From recorded orders and fills | No fees modeled; no account-equity history (drawdown is realized-only) |

## Repair transaction

Run in order. Each step names its checkpoint and rollback. Use
`.venv/bin/python`, not `uv run`: `uv run` silently rebuilds the venv.

**0. Inventory (read-only).**

```bash
mkdir -m 700 ~/trading-repair-$(date +%Y%m%d)
cp -p ~/Library/LaunchAgents/com.trading.*.plist ~/trading-repair-$(date +%Y%m%d)/
launchctl list | grep com.trading > ~/trading-repair-$(date +%Y%m%d)/launchctl-before.txt
shasum -a 256 "$R/trading_assistant.db" "$R/.worktrees/safety-foundation/trading_assistant.db" \
  > ~/trading-repair-$(date +%Y%m%d)/db-sha256.txt
```

**1. Stop and remove all five launchd jobs [approval].** The stale
KeepAlive `app` and `daemon` jobs start the moment their old path exists
again. Remove them before anything else.

```bash
"$R/scripts/launchd/uninstall.sh"     # watchdog first, then app, daemon, backup, autopilot
launchctl list | grep com.trading     # expect nothing
ls ~/Library/LaunchAgents/com.trading.* 2>/dev/null   # expect nothing
```

Rollback: copy the saved plists back and run `launchctl bootstrap gui/$(id -u) <plist>` for each.

**2. Repair the venv (A3) [approval: may download].**

```bash
cp -Rp "$R/.venv" "$R/.venv.backup-$(date +%Y%m%dT%H%M%S)"
cd "$R" && uv sync --frozen --all-extras --dev --offline || uv sync --frozen --all-extras --dev
"$R/.venv/bin/python" -I "$R/src/trading_assistant/installation.py" status   # venv import -> $R/src
```

Try `--offline` first; uv's cache already holds the six security upgrades.
Rollback: `rm -rf "$R/.venv" && mv "$R/.venv.backup-<ts>" "$R/.venv"`.

**3. Repair the worktree links (A2) [approval].** Repair, never prune.

```bash
cp -p "$R/.git/worktrees/safety-foundation/gitdir" ~/trading-repair-$(date +%Y%m%d)/
cp -p "$R/.worktrees/safety-foundation/.git" ~/trading-repair-$(date +%Y%m%d)/worktree.git
git -C "$R" worktree repair "$R/.worktrees/safety-foundation"
git -C "$R" worktree list --porcelain | grep -c prunable   # expect 0
```

Rollback: copy both files back.

**4. Designate the checkout (A1) [approval].**

```bash
"$R/.venv/bin/python" -I "$R/src/trading_assistant/installation.py" designate
```

Rollback: delete `~/Library/Application Support/trading-assistant/installation-root`.

**5. Choose the database [approval: your decision].** See
[`database-decision.md`](database-decision.md). Option A is one command.

**6. Preflight [approval: contacts Alpaca paper, read-only; switches the
chosen database to WAL mode].**

```bash
cd "$R" && .venv/bin/python -m trading_assistant.preflight
```

Expect the `RUNTIME SUMMARY` first: checkout `$R`, database
`$R/trading_assistant.db`, `mode=paper broker=alpaca
endpoint=https://paper-api.alpaca.markets`, `app bind: 127.0.0.1:8020`,
auto-execution off. Then all checks. Stop on any FAIL or NEEDS-ME.

**7. Start the app manually first [approval: startup reconciles against the
paper account].** Use launchd only after the demo works.

```bash
cd "$R" && ./scripts/start.sh            # stop with ./scripts/stop.sh
curl --cacert .local/tls/rootCA.pem https://localhost:8020/health/live
```

**8. Later: install launchd jobs [approval].**
`./scripts/launchd/install.sh` installs exactly app, watchdog and backup. Then
`launchctl list | grep com.trading` shows exactly those three. The installer
refuses if a daemon job exists, the venv is stale, or no database is chosen.

## Demo plan: one human-approved paper trade, end to end

Preconditions: steps 0–7 done; US market open (closed-market orders are
rejected); small sizing chosen by you in `config.yaml`, for example
`autopilot.universe: [AAPL]` and `autopilot.notional_per_trade: 50`.

1. **Real smoke test of submission and cancel [approval].**
   `cd "$R" && .venv/bin/python -m trading_assistant.ops.paper_drill --symbol AAPL --notional 1.25`
   submits one tiny limit order priced so it cannot fill, then cancels it.
   - **You should see:** a submitted and then canceled order. The console's
     log shows both audit events.
   - **Success:** the order is canceled at the broker and locally, and no
     reconciliation drift is reported.
2. **Preview the decision (read-only market data).**
   `.venv/bin/python -m trading_assistant.autopilot dry-run` shows what the
   strategy would do now (signal, action, reason, holdings and data date)
   and places or records nothing.
3. **Generate a proposal.** Set `autopilot.mode: propose`. Start the daemon
   from the operator terminal (`./scripts/operator.sh`, then 7 Monitoring,
   1 Start, type `START PAPER MONITORING`) **[approval: broker reads and order
   sync every 15 s]**. About 30 minutes after the open, if the signal is LONG
   and nothing is held, a proposal appears.
4. **Review.** In the console's Pending decisions (or terminal 6, 1 List) you
   see:
   - symbol, side, notional, order type (market, notional-bounded) and expiry
     (15 minutes);
   - **Why:** the rule, SMA values, data date, size reasoning, the exit rule,
     and the limits re-checked at approval.
5. **Approve [your action].** Use Review approval, confirm, and give a reason.
   The risk engine re-runs, then the order is submitted with `client_order_id`
   equal to its intent key. Or reject it; nothing is sent.
6. **Watch the outcome.** The daemon syncs every 15 seconds, or use terminal
   8, 1 Synchronize. Status moves to filled, rejected, canceled or expired.
7. **Reconcile.** Terminal 8, 2 Reconcile positions. Expect no drift.
8. **Record and evaluate.**
   `.venv/bin/python -m trading_assistant.ops.paper_report --mark AAPL=<price>`
   shows the funnel, fills and P/L. Add `--json ~/trading-repair-<date>/report.json`
   to keep a copy (written owner-only).

**Success means** every step above is observable: the proposal with its
rationale, your approval, a broker order id, the terminal status, a recorded
fill with its order id, no drift, and the report. It says nothing about
profit.

**If reconciliation fails.** Startup or `Reconcile positions` reports drift,
for example a position the database never recorded, and a `broker_drift`
breaker blocks new orders. That is the system failing closed:

1. Do not reset the breaker yet.
2. Read the drift lines (terminal 8, 3 Redacted logs; console log).
3. Compare with the Alpaca paper dashboard.
4. Decide whether the position is legacy (from the other database's history)
   or unexplained.
5. If you choose to flatten legacy positions, do it deliberately in the Alpaca
   dashboard, then re-run reconciliation, and reset the breaker only when it
   reports clean.

**Stop everything.** Terminal 7, 2 Stop (type `STOP PAPER MONITORING`), then
`./scripts/stop.sh`. If installed: `./scripts/launchd/uninstall.sh`. In an
emergency, terminal 9, 1 Panic latches the global breaker and cancels open
orders.

**Inspect without exposing secrets.**

- `logs/*.runtime.log` are redacted, owner-only and rotated.
- The report is read-only.
- For raw counts, use only plaintext columns:
  `sqlite3 "file:$R/trading_assistant.db?mode=ro" "select status, count(*) from orders group by 1"`.
- Reasons and evidence are encrypted at rest; read them through the console,
  the terminal or `paper_report --autopilot-evidence`.
- Never paste `.env` or Keychain values anywhere.

## Risk register

| Risk | Current control | Residual |
|---|---|---|
| Duplicate orders | Intent key equals `client_order_id`; unique idempotency key; submission barrier; a lost response becomes `acceptance_unknown`, never resubmitted, and reconciles by key (tested) | A second machine or account using the same paper keys is outside local control |
| Wrong database | Explicit choice bound to path and inode; startup, preflight, installer and report refuse anything else (tested) | The backup and migration commands follow the Keychain URL and do not check the choice; run them from `$R` |
| Stale launchd jobs | Uninstaller removes all five, watchdog first; installer refuses beside a daemon job (tested) | Until step 1 runs, recreating the old path would start the old app and daemon |
| Watchdog restart loops | Restarts only loaded launchd jobs (tested) | A loaded app that keeps failing is still restarted, throttled by launchd at 10 s |
| Reconciliation failures | Startup reconciliation fails closed; drift trips a breaker; unexplained fills are surfaced, never absorbed (tested) | Both databases may disagree with the current paper account. Expect drift on first start |
| Accidental live mode | Paper URL hard-coded; outbound allowlist; live rejected at startup and by the gate | None known in code. Never put live keys in the Keychain |
| Data loss | Both databases untouched; consolidation needs an exact-hash acknowledgment and makes verified encrypted backups | No restore command exists; a backup restore is a manual, untested procedure |
| Runaway trading | Approval required (propose mode); 3 open positions; 5 submissions per session; $2,000 per order; daily loss and drawdown breakers | Autonomous `paper` mode stays blocked by the readiness gate |

## Remaining blockers, by impact

1. **Your database decision.** Without it nothing starts.
2. **CI is red on a new dependency advisory** (`mcp` CVE-2026-59951).
   Upgrading `mcp` to 1.30.0 needs your approval. The verifier passes; the
   prototype does not use the affected transport.
3. **Local repair steps 1–4** need your approval. The venv repair may need a
   download.
4. **No real Alpaca run yet.** Connectivity, startup reconciliation against
   your actual paper account, and the paper drill are unverified.
5. **Probable drift on first start.** Each database holds positions the other
   lacks, and the paper account may differ from both.
6. **No direct human order entry.** Proposals come from the strategy (propose
   mode), rules, plans, or chat drafts. The demo depends on the strategy
   signalling LONG.
7. **No account-equity history.** Drawdown is realized-only. Unrealized P/L
   needs `--mark` prices or the console.
8. **No restore command** for encrypted backups.

## What would make its trades good

- **Evidence needed.** A forward record, collected after the fact and not
  backfilled, of every proposal and why, which you approved or rejected, fill
  prices against the decision-time price (slippage), exits, and P/L per
  closed trade. Compare it with a do-nothing or buy-and-hold baseline over the
  same period, and with a backtest of the same rule over the same window
  (`autopilot backtest` records one).
- **Metrics that matter.** Expectancy per trade (average winner × win rate
  minus average loser × loss rate). Max drawdown relative to exposure.
  Turnover and slippage. How often the rule is in the market. Behavior in down
  and sideways regimes. Win rate alone misleads.
- **Minimum sample.** `sma_trend` trades rarely, roughly a handful of
  round trips per symbol per year. Even 20 sessions of observation yields few
  trades. Expect to need at least about 30 closed trades, or several months
  across a few symbols, before P/L means much. Use the backtest (2+ years with
  a holdout) for statistical weight and paper trading to check that live
  behavior matches the backtest.
- **Do not conclude from a few paper trades** that the strategy is
  profitable, safe at a larger size, or likely to fill the same way live.
  Paper fills ignore queue position and market impact, and a few outcomes are
  dominated by chance.
- **Honest assessment of the strategy.** `sma_trend` is a slow, long-only,
  daily trend filter with no stops. It is evaluable: deterministic, explained
  and backtestable. It is not tuned, has no risk-based exits, and will lag
  turns. That is acceptable for a prototype whose goal is controlled,
  observable execution, not returns.
