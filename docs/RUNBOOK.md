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

Put the generated value in `APP_API_TOKEN`. Preflight rejects common
placeholders, low-diversity values, and obvious repeated patterns, but this is
only a basic format check—not proof of entropy. Generate the value rather than
inventing it. Configure the selected LLM provider key and Alpaca paper
credentials in `.env`; never commit or paste credentials into logs or reports.
Keep these committed settings unchanged:

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
`0001` through `0010` are the frozen release history. Migration `0008` adds
the durable process-start broker-reconciliation generation latch; a runtime
cannot submit until the newest generation has reconciled orders, fills, open
orders, positions, and local state. Migration `0009` records fill-activated
plan exits and progressive targets. Protective exits remain pending until a
trusted broker fill confirms an entry, then are sized from confirmed filled
quantity. The same migration adds a monotonic plan residual generation; delayed
fills invalidate and cancel every older live exit intent without relying on
broker timestamps. It conservatively refuses any legacy plan-linked proposal,
because the older schema cannot prove which rule owned its fills. Downgrade
refuses while specialized state exists because dropping it would silently
remove protection semantics.
Migration `0010` separates durable plan-order cancellation intent from transient
broker error classification. Requested or indeterminate cancellation survives a
restart and prevents startup readiness and daemon rule evaluation until the
order is terminal and exact fill truth has reconciled.

To recover from a bad migration, stop the app, daemon, and watchdog first. Verify
the chosen backup again, move the failed database and any `-wal`/`-shm` sidecars
aside as evidence, copy the verified backup into the configured database path,
set mode `0600`, run migration status, and restart. Never copy over a database
while a process has it open.

## Release-safety gates

The deterministic gate is offline. It uses SQLite's online backup API to create
the explicit destination, upgrades and exercises only that copy, and refuses
relative, existing, alias, symlink, hardlink, primary, non-SQLite, or in-memory
destinations. It holds the primary and every present SQLite sidecar through
directory-relative `O_NOFOLLOW` descriptors and rejects a group/world-writable
source directory. While those descriptors remain held, it creates a random
private `0700` binding directory in the verified source parent, hard-links each
present main/WAL/SHM inode under one alias basename, verifies every alias
against its held descriptor, and opens SQLite read-only through the alias URI
with `mode=ro&nofollow=1`. This binds connection open to the original inodes
even if the original pathname is swapped and restored. Controlled link counts
and identities are checked during cleanup; cleanup failure is unsafe,
unverified paths are never unlinked, and source link counts must return to
baseline. If creation succeeds but the private-directory open fails, cleanup
removes only the still-empty `0700` directory whose inode matches the one just
created; an unverified replacement is preserved and the drill fails closed.
Destination parents and staging are private, symlink-resistant, and published
without overwrite.

An active WAL source is supported when its regular `-wal` and `-shm` files
already exist. The drill requires those sidecars rather than creating recovery
or coordination state beside the primary; a closed WAL-mode source without
them and a hot rollback journal fail closed. With an otherwise quiescent
source, main-database and WAL inode/content plus schema/state remain unchanged.
SQLite may update ephemeral SHM read marks while taking read locks, so SHM
bytes and mtime are not logical-state evidence; its identity and presence must
remain fixed. This does not claim byte stability against unrelated concurrent
application writes. A release rehearsal must therefore use either a non-WAL
source or a held-open WAL source with both regular sidecars; do not run it
against a closed WAL-mode file whose sidecars have disappeared.

These controls close the tested pathname, symlink, hardlink, and
swap/open/restore races. They do not claim protection from a malicious
same-user process that directly mutates a held inode or interferes with the
private binding directory. Such interference must fail the
identity/fingerprint/cleanup gates. Stop untrusted same-user processes before
collecting release evidence.

```bash
uv run python scripts/check_release_safety.py
safety_stage="$(mktemp -d)"
safety_dir="$(cd "$safety_stage" && pwd -P)"
uv run python -m trading_assistant.ops.safety_drill \
  --database-copy "$safety_dir/release-safety.sqlite3" --mock
```

Require every report boolean and `"safe"` to be `true`. The drill proves, on the
copy, current schema and fail-closed authentication. It deliberately terminates
the first submission path with a drill-only process-death exception after broker
acceptance, leaves the persisted order `SUBMITTING`, disposes the first engine,
constructs a new container from the copy, and reconciles by client ID while
proving one broker submission. Two independent `RuleRepository` instances and
bounded, non-daemon threads then compete for different OCO sibling terminal
claims; exactly one must win. Repository writer-lock acquisition and both join
phases are bounded, and no later gate runs until both workers have terminated.
Breaker persistence/reset also crosses a fresh container, and final
reconciliation must be clean. Mock mode is deterministic, offline, and
broker-write-free outside its injected fake. CI uses a temporary non-WAL source;
the focused online-copy test uses a held-open WAL source with real WAL/SHM.

Credentialed mode is a separate, explicit paper-account mutation:

```bash
uv run pytest tests/test_alpaca_paper_integration.py -v
alpaca_safety_stage="$(mktemp -d)"
alpaca_safety_dir="$(cd "$alpaca_safety_stage" && pwd -P)"
uv run python -m trading_assistant.ops.safety_drill \
  --database-copy "$alpaca_safety_dir/alpaca-paper-safety.sqlite3" \
  --alpaca-paper
```

Run it only while the equity market is open under the current risk configuration
and valid Alpaca paper credentials are configured. Before copy or broker access,
the gate requires the exact `AlpacaBroker` and exact initialized SDK
`TradingClient`, dynamically re-derives the execution target from the client's
current `_sandbox` and `_base_url`, and requires the exact official paper
endpoint. Initial validation arms the broker's paper-only mutation guard.
After preliminary idempotency lookup and request preparation, `AlpacaBroker`
dynamically rechecks the exact SDK target while holding a broker-owned `RLock`
across the actual SDK submit or cancel call, including compensation. The outer
drill checks remain defense in depth. Live, uninitialized, subclassed,
sandbox-false, or URL-overridden clients are refused. The guard is armed only
by this explicit credentialed drill, so it does not globally disable
intentionally configured live mode. Offline doubles can exercise mock behavior
only and can never produce `alpaca_paper:passed`.

The gate snapshots pre-existing open-order IDs and exact position quantities,
derives a limit strictly below the current ask and inside configured
price-sanity bounds, and submits one uniquely tagged one-share GTC limit through
the normal persisted proposal, human approval, risk recheck, outbox, and
submission service. It immediately cancels the tagged order. Ordinary equity
orders retain their DAY default; whole quantity plus explicitly persisted GTC
avoids Alpaca's fractional-GTC restriction and is never inferred from a tag.

If the order fills, compensation is allowed only when exact `BrokerFill` records
for the tagged broker-order ID aggregate to broker cumulative `filled_qty` and
the full position-manifest drift equals that signed exposure. The terminal,
fill, and full-manifest evidence is collected after terminal checks and is
repeated after the local compensation proposal is created. A one-shot guard,
keyed to that compensation client ID, repeats stable terminal, exact-fill, and
full-manifest checks after execution-risk evaluation and immediately before the
drill wrapper delegates to the broker. A changed invariant raises a
deterministic rejection, records the local compensation `REJECTED`, and
performs no broker submission. Post-proposal failures are also rejected through
the normal service and audit path. The original must first be identity-verified
terminal with two stable cumulative-fill observations. If cancellation is
failed or unconfirmed, compensation is not submitted and the drill remains
unsafe for operator intervention.

This last check cannot make external account activity atomic with Alpaca order
acceptance: another actor can still change the account after the callback and
before or after provider acceptance. Final broker-order, fill, and full-position
reconciliation remains authoritative; any mismatch keeps the drill unsafe and
requires operator intervention.

A tagged opposite exact-quantity order then uses the same human-gated service
and must be boundedly reconciled to terminal fill truth. An outer cleanup path
resolves stale `SUBMITTING` or `acceptance_unknown` local state, validates
remote identity before cancellation, requires every tagged remote/read/cancel
result to match the known drill symbol, isolates per-order provider failures so
later tagged IDs are still considered, and never touches a pre-existing ID.
Each tagged broker-order ID receives at most one cancel API attempt; after an
exception or nonterminal response, later cleanup phases only read and reconcile
that ID. A newly discovered tagged order may receive its own first cancel
attempt. Tagged `PROPOSED` and `APPROVAL_RECORDED` rows are also unsafe final
state. Any unavailable evidence, nonterminal compensation, or final-manifest
mismatch remains unsafe. The
`alpaca_paper:passed` label requires every report gate plus one final dynamic
paper-target validation; clean reconciliation alone is insufficient. Missing
credentials are a skip, never a pass.

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
provider. It is broker-write-free: it never submits or cancels a broker order,
calls an LLM, or sends an external notification. It may update local
reconciliation, audit, and breaker state while using broker reads as intentional
startup repair. Both `FAIL` and `NEEDS-ME` print `NOT READY` and return nonzero;
missing Alpaca or selected-LLM credentials can never produce `READY`.

Open `http://127.0.0.1:8000`, log in with `APP_API_TOKEN`, and verify liveness and
daemon freshness. Every non-liveness API route requires an opaque server-side
operator session. Sessions expire after the configured eight hours. Mutations
also require the in-memory CSRF token. Panic, breaker reset, approval, and other
high-consequence actions require recent reauthentication (five minutes by
default). The UI prompts again and never stores the operator secret, session, or
CSRF token in `localStorage`. Logout revokes the server-side session.

## Durable request and provider budgets

Request-limit windows and concurrency state are durable: process restart does
not reset a principal or global window. A `429 rate_limit_exceeded` response is
a refusal, not a retry instruction. Its `Retry-After` header is the whole number
of seconds until the applicable durable request window permits another attempt;
wait at least that long and do not create parallel retries.

Paid-model limits are also durable, provider-scoped, and reset at UTC midnight.
Calls plus input and output token ceilings are charged before a provider
attempt. If provider acceptance is unknown after the attempt starts, its
reservation remains charged until durable reconciliation proves otherwise; do
not retry merely because the response was lost.

To inspect budget state without changing it, take the verified online backup
described above and query the copy only:

```bash
sqlite3 "$backup_file" \
  'SELECT provider, budget_day, calls_used, input_tokens_used, output_tokens_used, reconciliation_required, reconciliation_code FROM provider_budget_days ORDER BY budget_day DESC, provider;'
sqlite3 "$backup_file" \
  "SELECT provider, budget_day, category, request_id, state, input_reserved, output_reserved FROM provider_reservations WHERE state = 'unknown' ORDER BY created_at;"
```

Never edit `provider_budget_days` or `provider_reservations` counters manually.
Reservations and aggregate counters are updated atomically; manual edits break
their audit trail and can make metering unavailable or permit an unsafe budget
decision. Preserve the copy and investigate reconciliation-required or unknown
rows instead.

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

The sole submission exception is a freshly rechecked reduce-only order. It may
ignore only `loss:<asset>`, `drawdown:<asset>`, and `operator_global` for that
one claim, while atomically persisting any newly observed breach. It can never
ignore `data:*`, `liquidity:*`, or `broker_drift`. A plan exit additionally
requires exact trusted residual-generation and aggregate broker-position
allocation proof.
Unrelated manual reductions may consume only broker quantity left after exact
plan residual allocation; they cannot consume plan-owned shares.

Plan proposal TTLs are swept by the rule worker even when nobody opens the
approval screen. An expired protective proposal becomes `EXPIRED`, its rule is
re-armed with a new idempotency attempt, and the operator-global breaker remains
latched for review. Direct `cancel_rule` is forbidden for plan-owned rules.

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
