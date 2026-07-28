# Task 6 report — COMPLETE

## Status and safety boundary

- Implemented Plan 2 Task 6 plus the user-authorized cross-role runtime tenure
  expansion from base `a09af1941ccdbf63e8d21d90d15170b25b2bb4f1`.
- Implementation commit: `ba85a7798e72b6ab936a0cf4600e132305c01c19`
  (`feat(security): migrate sensitive persistence to AES-GCM`).
- PAPER-only invariants remain enforced. No live/autonomous path was enabled.
- All database activity used pytest-created temporary SQLite databases. The
  ignored runtime `trading_assistant.db` and all existing processes/services
  were untouched.
- Real Keychain/environment credentials, the exposed Composio credential,
  providers, brokers, notifications, and the network were not accessed.
- The final full suite explicitly removed every database, broker, provider,
  notification, field-key, backup-key, live-confirmation, and Composio
  credential variable. The real Alpaca integration test was therefore skipped.

## Delivered architecture

### Database-authoritative runtime and maintenance tenure

- Alembic head `20260727_0014` adds `runtime_tenures` with constrained resource
  and role pairs for `app`, `daemon`, `mcp`, and
  `sensitive-migration:global`/`maintenance`.
- Each held tenure records a unique UUID owner, positive fencing generation,
  role, PID, stable process-start identity, and acquired/renewed/expires
  timestamps. Clock, owner generation, and process inspection are injectable.
- Runtime and maintenance acquisition serialize in `BEGIN IMMEDIATE`. App,
  daemon, and standalone MCP coexist; any of them excludes maintenance, and
  maintenance excludes all three.
- Expired ownership is reclaimable only after exact `NOT_SAME` process proof.
  A live matching process, malformed identity, inspection error, or `UNKNOWN`
  proof blocks takeover.
- Renewal and release require exact resource, held state, owner UUID,
  generation, and unexpired tenure. Reclaim increments the generation, so a
  predecessor cannot renew or release its successor.
- Guards start immediately after acquisition, before provider/broker
  construction or startup reconciliation. App, daemon, and MCP all own
  continuous renewal and exact cleanup.
- App tenure loss immediately closes the shared SQL/broker mutation barriers
  and invokes the Uvicorn launcher's deterministic controlled-shutdown callback.
  Daemon loss fences an in-flight core cycle at the broker/SQL seams and exits.
  MCP loss cancels its server task and exits.
- Normal daemon/MCP shutdown with uncertain release exits with stable
  `runtime_tenure_uncertain`; cleanup does not replace an already-raised primary
  failure.
- Runtime mutation SQL has no text-based exemption. Only the tenure service's
  exact renew/release statements set a private execution option; an UPDATE to
  another table containing `runtime_tenures` in a SQL comment is still fenced.
- Generic heartbeat rows remain observability only and never prove offline.

### Encrypted backup format and cleanup evidence

- Migration and rotation require a dedicated, validated 32-byte backup key and
  non-secret backup key ID. There is no field-key fallback.
- SQLite's online backup API creates a mode-`0600` plaintext snapshot inside a
  canonical mode-`0700` private backup directory.
- The canonical artifact is:
  `<UTC timestamp>-before-sensitive-v1.sqlite3.aesgcm`.
- Wire format:
  `TA-SENSITIVE-BACKUP\0` magic, big-endian canonical-header length, canonical
  JSON header, 12-byte nonce, streamed ciphertext, and 16-byte GCM tag.
- The authenticated header/AAD contains only version, `AES-256-GCM`,
  1,048,576-byte chunk size, UTC timestamp, source snapshot SHA-256, Alembic
  schema head, and non-secret backup key ID.
- Snapshot hashing, encryption, decryption, and quick-check verification renew
  maintenance within long operations. Encryption/decryption stream in 1 MiB
  chunks and never load the database into memory.
- Publication uses a no-overwrite hard link, file fsync, directory fsync, and
  removes the staging ciphertext. Verification decrypts into a separate
  mode-`0600` temp, checks source SHA-256, and runs `PRAGMA quick_check`.
- Strict `finally` cleanup removes snapshot, verification, WAL/SHM, and staging
  temps on success, injected failure at every stage, cancellation, and
  maintenance loss. A failed post-publication verification removes and fsyncs
  the encrypted artifact as well.
- Tests inspect the directory during failure, verify `0600` temp/artifact modes,
  exercise overwrite refusal/cancellation/every stage, stream over 2 MiB, and
  prove neither the SQLite header nor seeded plaintext markers occur in the
  artifact.

### Migration and rotation state machines

- `migrate` requires state `required` or resumable `migrating`, acquires and
  continuously renews exclusive maintenance, and verifies the encrypted backup
  before the first state or row mutation.
- Authoritative full scans derive total/completed/pending evidence; persisted
  counters are never trusted. Scans renew every 100 rows.
- At most 100 rows are rewritten per `BEGIN IMMEDIATE`. Legacy plaintext is
  encrypted using exact table/row/column/schema AAD; valid active-key envelopes
  remain unchanged; malformed, tampered, unknown-key, or wrong-key envelopes
  fail closed. Every produced envelope is decrypted before commit.
- The final exact registry rescan must equal the preceding authoritative scan
  before state becomes `complete`. State stores only active key ID, verified
  counters/timestamps, and backup path hash.
- An interrupted `migrating` operation resumes from mixed database truth.
  A completed rerun performs only state/envelope verification and is a genuinely
  read-only `verified_noop`.
- All ordinary failures become durable `failed`. Maintenance renewal/release
  uncertainty also durably marks failure and leaves startup blocked.
- Frozen deterministic clocks may set `completed_at == started_at`, matching the
  database invariant; backward or malformed evidence remains blocked.
- `rotate` requires complete/resumable rotating state with database and config
  still naming the old active key, exact requested new key ID already retained,
  and both old/new material present. It creates a fresh verified backup, resumes
  mixed old/new rows in 100-row batches, fully verifies new-key envelopes, then
  updates database active key.
- Rotation never deletes key material and emits only stable IDs/status:
  `old_key_status=retained`. Startup remains blocked by the intentional
  config/state mismatch until a manual non-secret config patch.
- `verify` is read-only. CLI receipts contain stable IDs, counts, status, and
  backup path hash only; no value, secret, or path is logged.

### Startup, composition, and sensitive persistence

- Production composition requires Alembic head and, under an already-running
  runtime guard, validates singleton migration state, schema version, complete
  counters/timestamps/backup evidence, configured/database active-key match,
  and every non-null registered envelope using its exact row reference.
- Plaintext, mixed key, malformed/tampered envelope, missing/unknown key,
  incomplete migration, bad counters/evidence, and stale schema return stable
  redacted blocking codes before broker/provider construction or HTTP serving.
- Direct app, daemon, MCP, preflight, and paper-drill composition paths use the
  same runtime gate and exact cleanup. Tests prove no broker/provider factory is
  called before acquisition/inspection.
- `SensitiveFieldStore` is factory-bound to the validated runtime cipher. New
  generated-PK rows first flush only staged ciphertext, then replace it with
  exact-row ciphertext before commit; before-flush and before-commit guards are
  the runtime backstop.
- Changed sensitive write/read call sites:
  `analyst/planning.py`, `analyst/shadow.py`, `analyst/store.py`,
  `app/agent.py`, `app/limits.py`, `app/policy.py`,
  `backtest/evaluate.py`, `backtest/runner.py`, `operations/audit.py`,
  `orders/reconciliation.py`, `orders/repository.py`, `orders/startup.py`,
  `risk/breakers.py`, `risk/killswitch.py`, `rules/application.py`,
  `rules/repository.py`, `service.py`, and model seed helpers.
- Reads decrypt only exact response/domain narratives. Behavioral tests confirm
  decrypted narrative text does not drive risk authority, idempotency,
  reconciliation matching, state transitions, or broker submission.
- The release static checker now runs the AST sensitive-write scanner. It rejects
  mapped-class aliases, constructor mappings/unpacking, inferred object
  assignments including `reason`, `setattr`, SQLAlchemy insert/update/bulk
  mappings, dynamic dictionaries, and raw SQL DML naming registered columns.
  The runtime guards still reject any syntactic bypass at flush/commit.

## TDD and verification evidence

### RED evidence

- Long-backup renewal test: `1 failed`; only one renewal was observed where at
  least four were required. Hashing/decryption/quick-check renewal fixed it.
- Daemon normal-release uncertainty test: `1 failed`; no
  `TenureUncertain` was raised. Lifecycle cleanup semantics fixed it.
- MCP-role migration initially expected a successor revision and failed before
  schema support; per review, MCP was folded into uncommitted `0014`.
- Static-write scanner, encrypted CLI, and direct-plaintext runtime backstop each
  began as failing focused tests before their implementations.
- Broad convergence run: `45 failed, 311 passed`; lifecycle convergence:
  `9 failed, 294 passed`; launch convergence: `19 failed, 140 passed`.
- Adjacent-domain convergence: `110 failed, 850 passed`; failures exposed legacy
  plaintext fixture writes/reads and unbound spawned test workers, then were
  converted to deterministic fake-cipher persistence without weakening
  production guards.

### GREEN focused evidence

- Runtime tenure checkpoint: `53 passed, 186 deselected`.
- Backup scan-renewal/frozen-clock slice: `16 passed`.
- First consolidated Task 6 run: `843 passed, 1 failed`; the one fake-field-key
  fixture failure was corrected and rerun: `1 passed`.
- Adjacent repairs: `360 passed`; submission barrier/safety drill:
  `108 passed`; remaining domain slice: `89 passed`.
- Stabilized consolidated Task 6/domain run:
  `844 passed, 1 warning in 74.14s`.
- Final lifecycle delta after self-review:
  `67 passed, 1 warning`; daemon RED repair confirmation: `2 passed`.
- `python -m compileall -q src tests`: PASS.
- `scripts/check_release_safety.py`: PASS.
- `alembic heads`: exactly `20260727_0014 (head)`.
- `git diff --check`: PASS.

### Exactly one final full suite

- Command environment explicitly unset real database/provider/broker/notification
  and encryption credential variables.
- Result: `2546 passed, 1 skipped, 1 warning in 326.97s (0:05:26)`.
- Skip: opt-in real Alpaca paper read-only integration, disabled because its
  credential variables were removed.
- Warning: third-party `websockets.legacy` deprecation only.
- No overlapping or second full-suite run was performed.

## Concerns

- No Task 6 correctness or safety concern remains from the implemented scope.
- The sole test warning is an upstream `websockets.legacy` deprecation and does
  not affect tenure, backup, migration, rotation, or mutation fencing.
