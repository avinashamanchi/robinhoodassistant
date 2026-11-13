# Prototype current state — 2026-10-15

Phase 0 of the paper-prototype pass. Everything below was established by
reading code, configuration, launchd definitions and database metadata. No job
was started or stopped, no Keychain item or `.env` value was read, the broker
was not contacted, and neither database was modified.

## Commands that exist today

| Purpose | Command | Side effects |
|---|---|---|
| Preflight | `uv run python -m trading_assistant.preflight` | Local structural checks plus **read-only paper broker reconciliation readiness** (contacts Alpaca paper). Never repairs or cancels |
| App (loopback HTTPS console) | `./scripts/start.sh`, stop with `./scripts/stop.sh` | Starts the app on `127.0.0.1:8020`. Startup runs **broker reconciliation** and writes its result to the database |
| Operator terminal | `./scripts/operator.sh` | Text console over the app API (monitoring, account, pending approvals, plans, rules, emergency) |
| Daemon (safety loop, rule worker, autopilot) | `uv run python -m trading_assistant.daemon.main` (foreground, operator-started) | Broker reads and order sync every 15 s; rule worker proposes; autopilot per `autopilot.mode` |
| Watchdog | launchd job running `python -m trading_assistant.ops.watchdog` every 60 s | Probes `/health/live` and the daemon heartbeat; `launchctl kickstart -k` on app or daemon |
| Install launchd jobs | `./scripts/launchd/install.sh` | Generates and loads app, watchdog and backup jobs from the designated checkout |
| Remove launchd jobs | `./scripts/launchd/uninstall.sh` | Boots out and deletes all five `com.trading.*` jobs |
| Tests | `uv run pytest` | None |
| Static release gate | `uv run python scripts/check_release_safety.py` | None |
| Authoritative release check | `python3.11 -I -S scripts/verify_loopback_release.py` in a clean clone (refuses a checkout with a root `.env`) | None |
| Offline lifecycle drill | `uv run python -m trading_assistant.ops.safety_drill --database-copy <tmp>/drill.sqlite3 --mock` | Mock broker only |
| Real paper smoke test | `uv run python -m trading_assistant.ops.paper_drill --symbol AAPL --notional 1.25` | Submits and cancels one non-marketable paper limit order. Needs approval |
| Autopilot | `uv run python -m trading_assistant.autopilot readiness` (file read only), `dry-run` and `backtest` (broker or market-data reads) | — |

**Hidden side effect.** `uv run` syncs the virtual environment first. This
machine's `.venv` still has the pre-upgrade versions of the six patched
packages, and its editable install points at the old path
(`~/Desktop/robinhood/trading-assistant`). Any `uv run …` above would download
packages and rewrite `.venv`, which is approval A3. Until A3 is approved, use
`uv run --no-sync` or `.venv/bin/python` with `PYTHONPATH=src` for read-only
commands.

## Which database each component uses

- Every production role (app, daemon, watchdog probe, backup, MCP, preflight,
  paper-drill) reads `database_url` from the macOS Keychain (service
  `io.local.trading-assistant`, account `database_url`). Its value was **not
  read**. A missing value loads as empty and fails; there is no silent default
  in production.
- A relative SQLite URL resolves against the process working directory. The
  installed jobs ran from two directories:
  - app, daemon, watchdog, backup: `~/Desktop/robinhood/trading-assistant/.worktrees/safety-foundation` (old path);
  - retired autopilot: `~/Desktop/robinhood/trading-assistant` (old path).
- File activity shows each job wrote the database **in its own working
  directory**: the worktree database has the app's sessions and rate windows
  and stops on 2026-07-30; the main database was written by the autopilot job
  until 2026-10-05 07:02:54, the same second as its log. So the Keychain URL is
  almost certainly relative, and **the database is chosen by the working
  directory**. Nothing pins the runtime to an explicitly chosen file.

| | Worktree `trading_assistant.db` | Main `trading_assistant.db` |
|---|---|---|
| Schema | `20260729_0017` (one behind) | `20260730_0018` (head) |
| Last written | 2026-07-30 | 2026-10-05 |
| Rules | 5 active, 1 processing, 1 triggered | none |
| Trade plans | 1 approved, 2 proposed, 1 canceled | none |
| Orders | 4 filled, 2 expired, 1 proposed | 2 filled, 47 rejected |
| Fills | 4 | 3 |
| Breakers | `operator_global` **tripped** | none tripped |
| Shared history with the other | **none** (not one audit event, order or fill in common) | **none** |

## Paper-only enforcement and how to prove it

- `broker/factory.py` always builds Alpaca with `paper=True`, and the adapter
  overrides the URL to `https://paper-api.alpaca.markets`.
- The outbound policy allowlists only the paper trading host (plus market data).
- `trading.mode: live` is rejected at startup; `live_trading_enabled()` always
  returns false.
- Proof at runtime: the console's environment strip and `/health` report the
  mode; `/security/posture` reports the policy. Proof offline: the broker and
  outbound-policy tests.

## Credentials and configuration

- `config.yaml` holds all non-secret configuration.
- Secrets live in the Keychain, one account per field: Alpaca keys,
  `database_url`, app token, signing, backup and field-encryption keys, LLM
  keys. `ops.secrets audit` reports presence only.
- `.env.example` is an inventory of key names. A root `.env` exists (mode
  0600). It is a migration source only; runtime roles never read secrets from
  the environment. It was not opened.

## Vocabulary as implemented

- **Observe mode** (`autopilot.mode: observe`). Once per session, 30 minutes
  after the open, the daemon runs the deterministic strategy (`sma_trend`) and
  records intended actions as encrypted `autopilot.cycle` evidence. It creates
  **no proposals and no orders**.
- **Proposal.** An order row in status `PROPOSED`, plus a `proposals` row with
  reasoning, expiring after 15 minutes. Proposals come from:
  - a human (`POST /propose`);
  - LLM chat drafts that a human puts in the signed queue;
  - rule triggers (rule worker);
  - autopilot paper mode, which approves its own after the readiness gate.
- **Trade plan.** An analyst-generated plan with entry, sizing and exits.
  Approving it creates rules, not orders.
- **Approval.** `POST /approve/{order_id}` from an authenticated,
  recently re-authenticated session with CSRF and a written reason. A
  confirmation endpoint shows the exact order being approved. Approval reruns
  the full risk engine and then submits through the duplicate-safe outbox.
- **Auto-execution.** Two paths, both off:
  - `trading.auto_execute_preapproved_rules: false`, which also refuses to
    create pre-approved rules;
  - autopilot `paper` mode, blocked by the readiness gate.

## Implemented versus missing for an end-to-end paper demo

Implemented:

- Order lifecycle: propose, approve, run the execution-time risk engine,
  submit with a deterministic `client_order_id`, mark `acceptance_unknown` on a
  lost response, sync orders (daemon every 15 s or `POST /sync`), record fills,
  update positions, run startup reconciliation, cancel, panic.
- Per-order guardrails: allowlist, $2,000 per order, $5,000 per ticker,
  $10,000 exposure, daily loss, drawdown, price sanity, quote age, spread,
  market closed, broker reconciled.
- Console: environment strip, account, pending decisions with approve and
  reject, plans and rules pages, audit log, security posture.
- Mock end-to-end safety drill, and a real submit-and-cancel paper drill.

Missing or weak:

1. **Explicit database choice.** The runtime uses whatever file its working
   directory and Keychain URL point at; startup prints no summary of it.
2. **No path from the deterministic strategy to a human-approvable proposal.**
   Observe mode only records evidence.
3. **Guardrails:** no maximum number of open positions, and no daily order
   count for human or rule orders (only the autopilot has one).
4. **Evaluation:** no report of paper P/L, wins and losses, drawdown,
   turnover, exposure or an equity series. The analyst scorecard grades
   analyst calls only.
5. **Observe-period export:** evidence is encrypted audit events with no
   export command.
6. No restore command for encrypted backups.
7. The watchdog tries to restart a daemon that is not a launchd job every
   minute.
8. Local installation is broken (old paths, stale venv, worktree links).

## Safety risks and startup failure causes

- **Stale KeepAlive daemon job.** Still loaded; it fails only because the old
  path is missing (82,000+ spawn attempts each for app and daemon). Recreating
  the old path would start the old app and daemon at once.
- **Database ambiguity.** Two independent histories; selection by working
  directory.
- **Silent venv rebuild** by `uv run`.
- **Watchdog restart loop** (7,790 logged restart failures).
- **Main database positions.** It records 2 filled autopilot orders. The
  broker likely holds positions the worktree database does not know about,
  and startup reconciliation may fail closed either way.
- **Startup failures.** Today all five jobs exit 78 because the old path is
  gone. Before the move, the worktree app and daemon failed at startup
  continuously (app since 2026-07-25, daemon at least since 2026-09-20). Those
  log lines carry no reason by design; finding the cause needs a preflight run
  against Alpaca paper (approval needed).
