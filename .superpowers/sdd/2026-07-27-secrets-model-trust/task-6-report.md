# Task 6 report — COMPLETE (review fix round 1)

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

## Original Task 6 TDD and verification evidence

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

## Original Task 6 concerns

- No Task 6 correctness or safety concern remains from the implemented scope.
- The sole test warning is an upstream `websockets.legacy` deprecation and does
  not affect tenure, backup, migration, rotation, or mutation fencing.

## Review fix round 1

### Status and commits

- Review baseline: `18756d2a52bfffc10eaf31b009bb654fd4bd7dbc`.
- Review implementation commit:
  `2bd1a27ae26f4d2db935f98a7e1ef1be0fe6bbba`
  (`fix(security): close Task 6 review findings`).
- Current Alembic head is exactly `20260727_0015`. Revision `0015` adds the
  immutable plan authority fields, the guarded `validation` runtime role, and
  the durable `fenced` tenure state needed to distinguish forced stale-owner
  reclamation from graceful release.
- PAPER-only, manual approval, kill-switch, and broker-truth boundaries remain
  unchanged. All review tests used pytest temporary databases and deterministic
  fake keys/process inspectors. No ignored runtime database, existing process,
  real credential, Keychain, provider, broker, notification, or network was
  accessed.

### Finding closure

- Historical migration counters are migration-run evidence only. Startup
  preflight and `verify_sensitive_fields` cryptographically scan every current
  non-null registered value but do not compare mutable current cardinality to
  historical `rows_total`/`rows_completed`. Encrypted inserts and deletes after
  migration pass restart/verify; plaintext, malformed, unknown-key, and
  tampered values still block.
- Every production backup entry point now invokes the verified encrypted
  whole-database path. Scheduled launchd installation and operator runbooks no
  longer create or document durable plaintext SQLite backups. Operational
  backup works for `required`, `migrating`, and `complete` migration states,
  requires a dedicated 32-byte backup key and exclusive maintenance tenure,
  emits only a redacted receipt, and publishes only
  `whole-database-v1.sqlite3.aesgcm` artifacts.
- Runtime fencing covers statement execution and outer/nested transaction
  commit for Core and ORM. A transaction that executes while owned but reaches
  commit after tenure loss raises stable `TenureLost`, rolls back at the DBAPI
  boundary, and cannot later commit. Tenure internals use an exact private
  execution capability; SQL text, comments, CTEs, or table-name substrings
  cannot grant an exemption.
- Migration/rotation backup callbacks, every batch/state transaction, scan,
  final verification, and terminal transition assert the exact maintenance
  owner and generation at the source transaction. Authority loss prevents the
  row/state commit. Failure state is not written after ownership may have
  passed; uncertain release leaves database truth fail-closed for startup.
- Runtime guard close has durable `confirmed`, `uncertain`, and
  `not-attempted` outcomes. FastAPI lifespan and the Uvicorn launcher share that
  outcome, so a callback-initiated uncertain close still exits nonzero with
  `runtime_tenure_cleanup_uncertain`. Launcher construction/run failures close
  the already-running guard.
- Alembic upgrades on an established tenure-capable schema acquire exclusive
  maintenance before mutation and create a verified encrypted backup.
  Bootstrap permits only a truly empty initial database without tenure; an
  unversioned or pre-tenure existing schema fails closed with
  `schema_maintenance_bootstrap_required`. Analyst validation owns a distinct
  guarded runtime writer role before provider or budget construction and is
  fenced on loss.
- `TenureGuardedBroker` now stores its adapter in private name-mangled state,
  exposes no arbitrary `__getattr__`, explicitly implements read methods, and
  checks the shared authority before every mutation. Release static checks
  reject raw broker/SDK escapes and mutation calls outside reviewed adapters
  and the guarded wrapper.
- The sensitive-write scanner now rejects CTE-prefixed DML, dynamic/f-string or
  concatenated SQL outside the narrow reviewed allowlist,
  `exec_driver_sql`, mapped aliases, inferred/untyped instances, `setattr`,
  inserts/updates/deletes, bulk mappings, and dictionary unpacking. The runtime
  SQLite authorizer/execution boundary independently blocks unauthorized
  sensitive-table insert/delete/update. DDL and relevant SQLite mutating
  commands (`PRAGMA`, `ATTACH`, and schema writes) are also fail-closed under
  runtime/maintenance exclusion.
- Plan review now returns an immutable normalized authority digest and version.
  Approval requires that exact review token, re-reads and recomputes authority
  inside `BEGIN IMMEDIATE`, and performs a status+digest+version CAS. Any
  executable payload mutation returns stable HTTP 409 without creating orders
  or rules. Narrative text is excluded from authority.
- `LocalProcessInspector` uses trusted `/bin/ps` only after `os.kill(pid, 0)`
  proves the PID exists. Only `ESRCH` proves absence; `EPERM`, stderr/tool
  failures, malformed output, and PID identity mismatch uncertainty return
  `UNKNOWN`. Exact process-start identity still detects PID reuse.
- Runtime acquisition can recover an expired maintenance row only after exact
  `NOT_SAME` proof, and forced reclamation records `fenced`, never `released`.
  Partial SQL-barrier listener installation and guard-start failures remove all
  installed listeners/ownership before propagating failure.

### State-machine and changed-call-site evidence

- Runtime roles `app`, `daemon`, `mcp`, and `validation` coexist. Exclusive
  maintenance excludes every runtime role in both serialized acquisition
  orderings. Exact owner+generation renewal/release, stale crash recovery,
  generation fencing, live PID beyond expiry, unknown process identity,
  graceful release, response-loss resolution, and in-flight broker/SQL
  mutation races are covered.
- Migration/rotation retain 100-row `BEGIN IMMEDIATE` batches, full
  authoritative rescans, periodic renewal during long scans and backup
  verification, verified backup-before-mutation, resumable mixed-key truth,
  cryptographic final verification, and read-only completed migration reruns.
  Frozen timestamps permit `completed_at == started_at`.
- Review-round production call sites changed:
  `analyst/planning.py`, `app/main.py`, `app/policy.py`,
  `app/static/js/plans.js`, `bootstrap.py`, `db/migrate.py`, `db/models.py`,
  `db/schema.py`, `ops/backup.py`, `ops/encrypt_sensitive.py`, `ops/serve.py`,
  `ops/tenure.py`, `preflight.py`, `risk/breakers.py`,
  `security/sensitive_fields.py`, `security/sensitive_write_scan.py`, and
  `validate_analyst.py`.
- Schema/release/operations surfaces changed:
  `migrations/env.py`,
  `20260727_0015_plan_authority_and_validation_tenure.py`,
  `scripts/check_release_safety.py`, `scripts/launchd/install.sh`,
  `scripts/launchd/README.md`, `README.md`, `docs/RUNBOOK.md`, and
  `docs/ops/README.md`.
- Regression coverage changed in:
  `test_auth.py`, `test_bootstrap.py`, `test_db_models.py`,
  `test_launch_features.py`, `test_migrations.py`, `test_ops.py`,
  `test_planning.py`, `test_plans_api.py`, `test_release_gate_branches.py`,
  `test_release_static.py`, `test_route_policy.py`,
  `test_runtime_tenure.py`, `test_security.py`,
  `test_sensitive_migration.py`, `test_sensitive_write_sites.py`,
  `test_startup_schema.py`, `test_task9_round2.py`, and
  `test_transport_boundary.py`.

### Review TDD evidence

- Initial focused RED counts by review area:
  cardinality `2`; operational backup `5`; tenure/commit/process/broker `13`;
  lifecycle `2`; schema/validation role `4`; sensitive static/runtime `8`;
  release probes `6`; plan authority `5`; maintenance source fencing `2`; and
  validation ordering `1`.
- Additional focused RED regressions:
  plaintext-backup release fixture `1`; unsafe schema bootstrap `2`;
  sensitive-delete static/runtime `4`; stable stale-review API `1`; expired
  maintenance recovery `1`; guard install/start cleanup `2`; source-generation
  fencing `1`; exact and generic release response loss `2`; schema-barrier
  cleanup `1`; malformed authority payload `1`; partial listener cleanup `1`;
  forced-reclaim release ambiguity `1`.
- Convergence exposed `23` adjacent failures; all were repaired without
  weakening the boundaries. A direct sensitive-store engine-boundary RED and
  two legacy direct `PanicReceipt` fixture writes were corrected through the
  canonical store.
- Focused GREEN totals after stabilization:
  runtime tenure `157 passed`; migrations `127 passed`; sensitive
  migration/write-sites/ops `81 passed`; planning/API/launch/auth/route/security
  `328 passed`; adjacent safety/domain `532 passed`; remaining brief domain
  slice `72 passed`; monitor/launch/factory/secret/hardening `194 passed`;
  release static tests `54 passed`; release checker `PASS`.
- Final pre-suite gates: `python -m compileall -q src tests` PASS;
  `scripts/check_release_safety.py` PASS; `git diff --check` PASS; Alembic
  exactly `20260727_0015 (head)`.

### Exactly one review-round full suite

- One non-overlapping full suite ran with database, provider, broker,
  notification, field-key, backup-key, live-confirmation, Composio, and runtime
  instance credential variables explicitly unset.
- Result:
  `2 failed, 2615 passed, 1 skipped, 1 warning in 350.54s (0:05:50)`.
- Both failures were legacy test incompatibilities with the new required
  boundaries, not production-path failures: one release-gate fixture attempted
  a raw DELETE from sensitive tables, and one Task 9 backup test still mocked
  the removed plaintext `.sqlite3` contract.
- Focused RED reproduced both failures: `2 failed`. The fixtures were converted
  to the exact `SensitiveFieldStore.delete` capability and the real encrypted
  backup CLI using a deterministic 32-byte test key and temporary migrated
  database. Focused GREEN: `2 passed`; complete affected/adjacent files:
  `157 passed in 19.18s`.
- Per the explicit exactly-one-full-suite constraint, the full suite was not
  rerun. No production code changed after that full run; only those two legacy
  tests were aligned with already-verified safety behavior.
- Skip: opt-in real Alpaca paper integration, disabled because credentials were
  unset. Warning: third-party `websockets.legacy` deprecation only.

### Review concerns

- No known production correctness or safety defect remains from the review
  findings.
- The one mandated full-suite run was not wholly green because it exposed two
  stale tests. Both are focused-green and their complete adjacent files are
  green, but there is intentionally no second post-repair full-suite result.
- The upstream `websockets.legacy` deprecation remains unrelated to tenure,
  backup, migration, rotation, approval authority, or mutation fencing.

## Review fix round 2

### Status, scope, and commit

- Re-review baseline:
  `a9ab7614953de257b919a873dcd5a975140bc9c4`.
- Round-2 implementation commit:
  `815d956450d85326b389ae64d786709c4f3046f6`
  (`fix(security): close Task 6 re-review findings`).
- Work was performed directly in the required shared worktree on
  `codex/safety-foundation`; no separate worktree was created and nothing was
  pushed.
- PAPER-only operation, manual approval, kill switches, execution-time risk
  checks, and broker-truth authority are unchanged.
- All database execution used pytest-created temporary SQLite databases. The
  ignored runtime `trading_assistant.db`, Keychain, credentials, existing
  processes, network, external services, brokers/providers, notifications, the
  real app/daemon/MCP, and live trading were not accessed.

### Finding closure

- All operational, sensitive-migration, rotation, and schema-upgrade backups
  use one `BackupMaintenance` protocol. Maintenance is acquired with a bounded
  TTL, but no renewal thread or source-database write runs during SQLite's
  source snapshot. Snapshot progress performs only in-memory loss/deadline
  checks. After both snapshot connections close, the exact owner/generation
  renews once, periodic renewal starts, and ownership is checked through hash,
  encrypt, verify, publication, and subsequent database work.
- Snapshot expiry or exact-renewal loss aborts before publication and removes
  snapshot, verification, encryption, WAL, and SHM temporaries. The real
  operational path completes a greater-than-256-page 2 MiB WAL source inside
  the 8-second bound, keeps the WAL bounded, and returns only after authenticated
  decrypt/hash/`PRAGMA quick_check` verification.
- Every online Alembic invocation now requires a caller-supplied exact
  SQLAlchemy `Connection` plus a sealed, connection-bound, single-use migration
  authority before migration context execution. A distinct bootstrap authority
  is issued only after the wrapper proves the database has no tables.
  Established upgrades use maintenance authority after verified encrypted
  backup; offline production migration is refused.
- Revision `0015` validates its authority at the first executable statement.
  Maintenance upgrade/downgrade additionally requires the exact schema-fence
  capability and ownership assertion. Direct plain-`Config` upgrade, no-op
  upgrade, and downgrade refuse with no tenure and with each representable
  app/daemon/MCP/validation/maintenance tenure. Tests fingerprint schema,
  version, plan rows, tenure rows, and artifact state before and after refusal.
  Validation tenure is tested on the `0015` schema because revision `0014`
  cannot structurally represent that role.
- Runtime PID birth identity is now versioned as `ps-lstart-v1`, obtained only
  through exact `/bin/ps` under minimal fixed
  `TZ=UTC`, `LC_ALL=C`, `LANG=C`. Cross-caller timezone/locale changes compare
  as `SAME`; canonical mismatch proves PID reuse as `NOT_SAME`; live legacy
  identity, `EPERM`, malformed/tool errors, and ambiguous results remain
  `UNKNOWN`; only `os.kill(pid, 0)` `ESRCH` proves process absence.
- The adjacent cooperative-stop process verifier now uses the same absolute,
  fixed-environment, versioned birth identity. Legacy metadata therefore fails
  closed rather than becoming a false match.
- `RuntimeTenureGuard.start()` owns cleanup after thread-start failure.
  Bootstrap honors the durable close result: confirmed release preserves the
  original start exception, while only genuinely uncertain release becomes
  `TenureUncertain`. The real database test proves the row is durably released
  and no mutation-barrier listeners remain after confirmed failure.

### Changed files

- Migration boundary:
  `migrations/env.py`,
  `migrations/versions/20260727_0015_plan_authority_and_validation_tenure.py`,
  `src/trading_assistant/db/migrate.py`, and new
  `src/trading_assistant/db/migration_authority.py`.
- Backup/lifecycle boundary:
  `src/trading_assistant/ops/backup.py`,
  `src/trading_assistant/ops/encrypt_sensitive.py`,
  `src/trading_assistant/ops/tenure.py`,
  `src/trading_assistant/ops/control.py`, and
  `src/trading_assistant/bootstrap.py`.
- Regression coverage:
  new `tests/safety_helpers.py`, plus
  `tests/test_cooperative_control.py`, `tests/test_launch.py`,
  `tests/test_migrations.py`, `tests/test_ops.py`,
  `tests/test_runtime_tenure.py`, `tests/test_safety_drill.py`,
  `tests/test_sensitive_migration.py`, and
  `tests/test_startup_schema.py`.

### Round-2 TDD evidence

- Focused RED:
  process identity/start cleanup `5 failed`; real 2 MiB operational backup
  `1 failed` by the 8-second timeout with a zero-byte snapshot and growing WAL;
  shared migrate/rotate/schema sequencing `3 failed`; direct Alembic authority
  matrix `18 failed`; cooperative-control timezone/locale adjacency `1 failed`.
- An adjacent safety-drill run exposed `14` failures caused by the new test-only
  bootstrap helper leaving a closed fixture in WAL header mode without
  sidecars. The helper was changed to match historical Alembic
  `NullPool`/rollback-journal fixture semantics; production safety-drill copy
  checks were not weakened.
- Focused GREEN checkpoints:
  process/start cleanup `9 passed`; real bounded backup `1 passed`;
  shared sequencing `3 passed`; direct authority matrix `18 passed`;
  snapshot-expiry plus real large backup `2 passed`; migration
  refusal/loss matrix `18 passed`; cooperative control `9 passed`.
- Consolidated runtime/backup/migration/Alembic/bootstrap/startup/launch/drill
  convergence:
  `456 passed, 1 warning in 66.25s`.
- Pre-full gates:
  `python -m compileall -q src tests` PASS;
  `python scripts/check_release_safety.py` PASS;
  `git diff --check` PASS; Alembic exactly
  `20260727_0015 (head)`.

### Exactly one round-2 full suite

- Exactly one non-overlapping full suite ran after every focused check and
  release gate was green. Its environment explicitly unset database, Alpaca,
  Robinhood, LLM/provider, market-data, notification, candidate-signing,
  field-encryption, backup-encryption, live-confirmation, Composio, Octen, and
  OpenAI credential variables.
- Result:
  `2645 passed, 1 skipped, 1 warning in 370.17s (0:06:10)`.
- Skip: the opt-in real Alpaca paper integration, disabled because credentials
  were unset. Warning: the existing third-party `websockets.legacy`
  deprecation.
- No second full-suite run was started. No production code or tests changed
  after the full-suite result; only this report and its generated review
  artifact remain for the evidence commit.

### Round-2 concerns

- No known production correctness or safety defect remains from the four
  re-review findings or the adjacent process-identity caller.
- The sole warning is an upstream deprecation unrelated to backup, migration
  authority, process identity, tenure cleanup, approval, or execution safety.
