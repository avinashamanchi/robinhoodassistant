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
./scripts/setup-local-tls.sh
uv run python -m trading_assistant.ops.secrets migrate-env \
  --env-file /absolute/path/to/private-migration.env
uv run python -m trading_assistant.ops.secrets audit
```

The one-time migration source must be a non-symlinked regular file with mode
`0600`. Migration prompts for omitted values, verifies every macOS Keychain
write, and leaves the source untouched for an operator-controlled archive or
disposal decision. `audit` reports only presence and validation metadata.
Never put a secret value in a command line, committed file, log, report, or
chat. `.env.example` is an inventory for migration and tests; it is not a
production runtime source. The operator login secret quality check rejects
common placeholders, low-diversity values, and obvious repeated patterns, but
is not proof of entropy.

`setup-local-tls.sh` creates the only accepted local certificate layout:
mode-`0700` `.local/tls`, mode-`0644` `rootCA.pem` and `localhost.pem`, and
mode-`0600` `localhost-key.pem`. The leaf must be current, signed by that CA,
match the private key, and contain the exact SANs `localhost`, `127.0.0.1`,
and `::1`. The watchdog trusts only `rootCA.pem`; the leaf is never treated as
its CA bundle. The app binds only loopback and serves only
`https://localhost:8020`; proxy/forwarded trust, wildcard hosts, cross-origin
redirects, insecure cookies, and plaintext HTTP are release-gate failures.

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

## Trust-boundary hard limits

There is no webhook receiver. Composio is disabled in config, runtime, MCP,
tool registries, and the outbound manifest pending provider-side revocation and
rotation of the previously exposed credential. Do not install, connect, or test
that integration during this release.

General chat can call only the reviewed read tools and immutable draft
constructors. The state transition is deliberately split:

1. chat returns a non-executing immutable draft;
2. an authenticated operator explicitly places it in the signed, expiring
   queue;
3. a separate human approval request revalidates signature, freshness,
   idempotency, and deterministic risk;
4. only the normal submission service can attempt an Alpaca paper order.

Chat cannot execute, approve, submit, cancel, reset a breaker, send a
notification, or bypass the queue. This release has no live-mode support.
Paper trading, tests, drills, backtests, and a passing preflight do not prove
profitability and do not guarantee returns.

## Verified backup and migration

Stop the app, daemon, MCP, validation writer, watchdog, and every other database
writer. Create a transactionally consistent encrypted backup, upgrade the
schema, migrate registered sensitive fields, and verify every envelope before
restarting:

```bash
uv run python -m trading_assistant.ops.backup \
  --destination .local/encrypted-backups
uv run python -m trading_assistant.db.migrate status
uv run python -m trading_assistant.db.migrate upgrade
uv run python -m trading_assistant.db.migrate status
uv run python -m trading_assistant.ops.encrypt_sensitive migrate
uv run python -m trading_assistant.ops.encrypt_sensitive verify
```

Proceed only when backup emits a redacted `status=verified` receipt and
schema status reports current, sensitive migration reports `complete`, the
configured active key ID is available in Keychain, and verification succeeds.
The backup command acquires exclusive
maintenance tenure, snapshots online into a private temporary, streams
AES-256-GCM with the dedicated backup key, decrypts into a separate private
verification temporary, checks its hash and SQLite `quick_check`, and removes
all plaintext temporaries. It publishes only mode-`0600`
`.local/encrypted-backups/<timestamp>-whole-database-v1.sqlite3.aesgcm`
artifacts in a mode-`0700` directory, without overwrite. It works independently
of whether sensitive-field migration state is required, migrating, or complete.

Key rotation is a stopped-writer maintenance operation, not an online config
toggle. First prepare and review the coordinated config change that adds the
new key ID to `retained_key_ids`; then prompt for that configured key without
putting material on the command line:

```bash
uv run python -m trading_assistant.ops.secrets set-encryption-key \
  reviewed-new-key-id
uv run python -m trading_assistant.ops.encrypt_sensitive rotate \
  --new-key-id reviewed-new-key-id
```

After the verified rotation receipt, complete the reviewed config transition so
the new ID is active and every still-required old ID is retained. Run the
Keychain audit and field verification again before preflight. Never remove an
old key account until envelope verification and the retention decision are
independently reviewed:

```bash
uv run python -m trading_assistant.ops.secrets audit
uv run python -m trading_assistant.ops.encrypt_sensitive verify
```

Only a completely empty database may bootstrap directly to the current schema.
An existing database at migration `0014` or later is upgraded while an
exclusive, continuously renewed maintenance tenure is held. Older unversioned
or pre-tenure schemas fail with `schema_maintenance_bootstrap_required`; move
their data through a separately reviewed isolated-copy import instead of
stamping or altering them in place. A stale held maintenance row also remains
startup-blocking after expiry. A new maintenance operation may reclaim it only
after exact process-identity inspection proves the recorded process is gone;
an app, daemon, MCP, or validation writer may never infer recovery from expiry.
Migrations
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
Migrations `0014` and `0015` add the cross-role runtime/maintenance tenure
protocol, the independent validation-writer role, and immutable plan-review
authority evidence. App, daemon, MCP, and validation roles may coexist, but
each is fenced from commit after ownership loss and all exclude maintenance.

To recover from a bad migration, stop the app, daemon, MCP, validation writer,
and watchdog first. Preserve the encrypted artifact and failed database
sidecars as evidence. Recovery requires a reviewed restore procedure that
decrypts only to a new mode-`0600` staging file under a private mode-`0700`
directory, verifies the authenticated header, snapshot hash, and SQLite
`quick_check`, then atomically installs it while exclusive maintenance tenure
is held. The production backup command intentionally has no option that
persists a decrypted copy. Never copy over a database while a process has it
open.

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

Runtime roles load their required secrets from the verified macOS Keychain
backend; development environment loading is only available through explicit
CLI opt-in. Use `uv run python -m trading_assistant.ops.secrets audit` to report
presence and validation metadata without printing values, and use
`migrate-env` only with an exact-mode-`0600` private migration file. Migration
verifies each Keychain write and deliberately leaves the source file in place
for an operator-controlled cleanup decision.

`RuntimeSecrets` and `SecretStr` prevent routine serialization and accidental
display, but Python cannot guarantee complete in-memory erasure: immutable
strings and interpreter-managed copies may survive until garbage collection.
The key loader wipes mutable decoded key buffers in `finally` blocks, limits
plaintext unwrapping to trusted integration boundaries, and registers loaded
values for redaction. Treat process memory and crash dumps as sensitive.

```bash
uv run python -m trading_assistant.preflight
./scripts/start.sh
```

The app command starts only the loopback HTTPS operator process. Start the
monitoring daemon separately, only after the same preflight reports `READY`:

```bash
uv run python -m trading_assistant.daemon.main
```

Preflight always evaluates the local structural checks `KEYCHAIN`, `LOCAL_TLS`,
`FIELD_ENCRYPTION`, `OUTBOUND_ORIGINS`, and `INTEGRATIONS_DISABLED` first. It
evaluates all five independently, including when Keychain provider construction
or loading fails, and constructs no broker, outbound provider, or notifier if
any structural check fails. `LOCAL_TLS` requires the exact
`.local/tls/rootCA.pem`, `.local/tls/localhost.pem`, and
`.local/tls/localhost-key.pem` paths. The CA and leaf must be current, the CA
must be authorized for certificate signing, and the leaf must have server
authentication usage, match the private key, chain under the CA, and carry the
exact loopback SANs. The watchdog trusts `rootCA.pem`, not the leaf as a CA file.
`FIELD_ENCRYPTION` reads migration metadata and key availability only; it does
not decrypt rows. The startup guard performs the one full envelope scan. Only
after all five pass does preflight run the paper-mode, schema, WAL, breaker,
Alpaca-read, quote, and broker/local reconciliation checks. Reconciliation uses
a dedicated read-only `preflight` service exposing one snapshot probe. It calls
only broker open-order/position reads and performs no trading-table DML. Local
SQLite setup still establishes WAL and applies sidecar permissions before the
probe. The probe constructs no mutable `TradingService`, clock client, field
cipher, LLM provider, agent, app, or notifier. There is no daemon-health
preflight row; daemon freshness is observed separately after startup.

Preflight never submits a new order, calls an LLM, or sends an external
notification, repairs order state, cancels an order, or writes reconciliation
results. Any mismatch prints `NOT READY`; repair remains a separate
operator-controlled runtime action under the normal writer-tenure and audit
boundaries. Both `FAIL` and `NEEDS-ME` return nonzero, and missing required
credentials can never produce `READY`.

Open `https://localhost:8020`, log in without displaying or storing the
operator secret, and verify liveness and daemon freshness. Every non-liveness
API route requires an opaque server-side
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

Inspect budget state through the authenticated control plane. Do not decrypt an
operational backup or create a retained plaintext database copy for ad-hoc
analysis.

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

The installer runs the loopback API, one-minute liveness watchdog, and 02:00
verified encrypted backup; the daemon remains an explicit operator workflow.
The watchdog may restart a stale process; it never clears a
breaker or changes trading mode. On failure, inspect the role-specific bounded
runtime log, audit Keychain, validate TLS and database permissions, run
migration/field verification and preflight manually, then reload only the
affected plist. Use
`./scripts/launchd/uninstall.sh` to remove all four agents.

The scheduled artifact name ends in
`whole-database-v1.sqlite3.aesgcm`; no plaintext operational backup is
retained. Keep FileVault enabled and off-device backups encrypted.

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
