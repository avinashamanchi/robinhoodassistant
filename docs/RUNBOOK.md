# Alpaca Paper Control-Plane Runbook

This release is paper-only. Production startup rejects live mode and non-Alpaca
broker configuration. Human approval, the execution-time risk check, durable
outbox, and one-attempt submission service remain mandatory. A passing drill is
safety evidence only: it does not prove profitability, authorize live trading,
or guarantee returns.

## Install and configure

```bash
uv venv --python 3.11
uv sync --all-extras --dev
cp .env.example .env
openssl rand -hex 32
```

Put the generated value in `APP_API_TOKEN`. Configure the selected LLM provider
key and Alpaca paper credentials in `.env`; never commit or paste credentials
into logs or reports. Keep these committed settings unchanged:

```yaml
trading:
  mode: paper
  broker: alpaca
features:
  auto_execute_preapproved_rules: false
execution:
  prefer_bracket_orders: false
llm:
  fallback_provider: null
```

The runtime has no unofficial Robinhood login dependency or production path.

## Verified backup and migration

Create and inspect a transactionally consistent online backup before migration:

```bash
uv run python -m trading_assistant.ops.backup --destination backups
backup_file="$(ls -t backups/trading-assistant-*.sqlite3 | head -1)"
sqlite3 "$backup_file" 'PRAGMA integrity_check;'
uv run python -m trading_assistant.db.migrate status
uv run python -m trading_assistant.db.migrate upgrade
uv run python -m trading_assistant.db.migrate status
```

Proceed only when integrity reports `ok` and migration status reports current.
Backups are standalone SQLite files with owner-only permissions. Migrations
`0001` through `0007` are the frozen release history.

To recover from a bad migration, stop the app, daemon, and watchdog first. Verify
the chosen backup again, move the failed database and any `-wal`/`-shm` sidecars
aside as evidence, copy the verified backup into the configured database path,
set mode `0600`, run migration status, and restart. Never copy over a database
while a process has it open.

## Release-safety gates

The deterministic gate is offline. It uses SQLite's online backup API to create
the explicit destination, upgrades and exercises only that copy, and refuses
relative, existing, alias, symlink, hardlink, primary, non-SQLite, or in-memory
destinations.

```bash
uv run python scripts/check_release_safety.py
safety_dir="$(mktemp -d)"
uv run python -m trading_assistant.ops.safety_drill \
  --database-copy "$safety_dir/release-safety.sqlite3" --mock
```

Require every report boolean and `"safe"` to be `true`. The drill proves, on the
copy, current schema, fail-closed authentication, acceptance-unknown recovery
without a duplicate broker submission, OCO single-terminal behavior, breaker
persistence/reset across a fresh container, and clean reconciliation. Mock mode
is deterministic, offline, and must leave the primary database byte/schema/state
unchanged.

Credentialed mode is a separate, explicit paper-account mutation:

```bash
uv run pytest tests/test_alpaca_paper_integration.py -v
alpaca_safety_dir="$(mktemp -d)"
uv run python -m trading_assistant.ops.safety_drill \
  --database-copy "$alpaca_safety_dir/alpaca-paper-safety.sqlite3" \
  --alpaca-paper
```

Run it only when valid Alpaca paper credentials are already configured. It
snapshots pre-existing open-order IDs and exact position quantities, submits one
uniquely tagged one-share non-marketable GTC limit through the normal persisted
proposal, human approval, risk recheck, outbox, and submission service, then
immediately cancels it. Ordinary equity orders retain their DAY default; the
drill's whole quantity and GTC value avoid Alpaca's fractional-GTC restriction;
the GTC value is explicit persisted order data, never inferred from its tag.
If it fills, the drill compensates only the exact drill-created delta. It may
clean up only its tagged state and passes only when all tagged orders are
terminal, its net position delta is zero, and the pre-existing manifests are
unchanged. Missing credentials are a skip, never a pass. If the result is
unconfirmed, inspect Alpaca paper and keep trading blocked.

## Daily preflight and startup

```bash
uv run python -m trading_assistant.preflight
uv run uvicorn trading_assistant.app.main:create_app \
  --factory --host 127.0.0.1 --port 8000
uv run python -m trading_assistant.daemon.main
```

Preflight separately reports paper-only configuration, dangerous switches off,
current schema, WAL, breaker state, operator-secret quality, Alpaca read
dependencies, broker/local reconciliation, and the explicitly selected LLM
provider. It may reconcile local truth using broker reads, but it never submits
or cancels a broker order, calls an LLM, or sends an external notification.
`FAIL` blocks startup. `NEEDS-ME` means required credentials are absent.

Open `http://127.0.0.1:8000`, log in with `APP_API_TOKEN`, and verify liveness and
daemon freshness. Every non-liveness API route requires an opaque server-side
operator session. Sessions expire after the configured eight hours. Mutations
also require the in-memory CSRF token. Panic, breaker reset, approval, and other
high-consequence actions require recent reauthentication (five minutes by
default). The UI prompts again and never stores the operator secret, session, or
CSRF token in `localStorage`. Logout revokes the server-side session.

## Orders and acceptance-unknown recovery

Every broker submission originates in `OrderSubmissionService`, after human
approval, an execution-time risk recheck, exposure reservation, and the
submission barrier. There is one broker submission attempt. Cancellations and
reconciliation use their separate service paths. A timeout or lost response
after submission is `acceptance_unknown`, not a rejection and not permission to
retry. The reserved exposure stays active until reconciliation looks up the
durable client ID and confirms broker truth.

When acceptance is unknown:

1. Stop new approvals and keep all applicable breakers intact.
2. Run `POST /reconcile` from an authenticated UI/API session.
3. Match by client ID; never submit a replacement merely because the response
   was lost.
4. Confirm local status, broker order ID, exact fills, and position truth.
5. If truth remains unavailable, leave the order reserved/unconfirmed and keep
   trading blocked.

## Circuit breakers

Breaker state is durable and scoped:

- `operator_global`: all symbols; panic/operator emergency.
- `broker_drift`: all symbols; broker/local truth is not reconciled.
- `data:<asset>`: equity or crypto data is stale/invalid.
- `loss:<asset>`: realized-loss threshold breached.
- `drawdown:<asset>`: account drawdown threshold breached.
- `liquidity:<SYMBOL>`: quote spread/liquidity failure for one symbol.

An active relevant scope blocks submission. Reset only the investigated scope,
using its current generation, a reason, recent reauthentication, and confirmed
healthy evidence. A reset conflict means state changed; refresh and reassess.
Resetting one scope never clears another.

## Panic truth

Panic latches the operator-global breaker, disables rules, and attempts to cancel
known open orders. A `safe: true` receipt means both local and broker evidence
confirmed the safe state. HTTP `503 panic_incomplete`, `remote_enumeration:
unknown`, any unconfirmed order IDs, or any unsafe local category means safety
was not proven. Do not describe that state as canceled or safe. Inspect Alpaca
paper, reconcile by client ID, and leave the global breaker tripped until every
category is confirmed.

## Logs and alerts

All production roles (`app`, `daemon`, `mcp`, `preflight`, `paper-drill`,
`safety-drill`, `watchdog`, and `backup`) write redacted rotating logs in
`logs/*.runtime.log`. The directory is mode `0700`; active and rotated files are
mode `0600`; each role is bounded to five 5 MiB files by default. Provider
exception text and registered secrets must not enter reports.

Investigate `circuit_breaker.trip`, rejection, acceptance-unknown,
reconciliation drift, stale heartbeat, and `panic_incomplete` immediately.

## launchd operation and troubleshooting

```bash
./scripts/launchd/install.sh
launchctl list | grep com.trading
launchctl print "gui/$(id -u)/com.trading.app"
launchctl print "gui/$(id -u)/com.trading.daemon"
launchctl print "gui/$(id -u)/com.trading.watchdog"
launchctl print "gui/$(id -u)/com.trading.backup"
```

The agents run the loopback API, daemon, one-minute liveness watchdog, and 02:00
online backup. The watchdog may restart a stale process; it never clears a
breaker or changes trading mode. On failure, inspect the role-specific bounded
runtime log, verify `.env`/database permissions, run migration status and
preflight manually, then reload only the affected plist. Use
`./scripts/launchd/uninstall.sh` to remove all four agents.

Keep FileVault enabled and off-device backups encrypted.

## Analyst version and future evidence gates

The analyst is versioned by `analyst.version` (currently `v2`). The scorecard
grades only the current version; evidence from an earlier analyst must not carry
into changed logic or prompts. After a material analyst change, bump the version
(for example, `v2` to `v3`) so shadow-mode grading restarts from zero.

The current v2 operating rules suppress directional calls in `RANGING` regimes
and record confidence for calibration without using it for sizing, weighting, or
filtering. Do not promote confidence into execution logic until calibration
evidence supports it.

These are future manual research/evidence gates, not live-trading authorization:

- At least 60 graded calls for the asset class; the code's 50-call status is only
  a lower advisory threshold.
- A hit-rate confidence interval that clears 50%, not merely a point estimate.
- Brier score below 0.25.
- Performance that does not lose to buy-and-hold over the same evaluation
  window.

Ration sacred-holdout evaluation to once per major analyst version. Do not tune
prompts repeatedly against holdout results, add indicators/data sources, or add
second-model voting merely to chase measured accuracy; those choices add
overfitting degrees of freedom. A rising hit rate in an uptrend may only be
buy-and-hold exposure in disguise. Even if every evidence gate is met, this
release remains paper-only and provides no live-mode authorization path.

## Known dependency warning

Starlette's test client uses its supported `httpx2` dependency, eliminating the
plain-`httpx` deprecation warning. The lock retains latest Alpaca-py `0.43.5`,
which still imports `websockets.legacy` in `alpaca.trading.stream`. That upstream
deprecation warning remains visible by design; do not add a blanket warning
filter. Reassess when Alpaca publishes a compatible release.
