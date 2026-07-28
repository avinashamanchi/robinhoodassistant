# Task 6 report — COMPLETE (review fix round 4)

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

## Review fix round 3

### Status, scope, and commit

- Round-3 baseline:
  `b1afd8584ec65db1e90ad6ff599b0780e96f7851`.
- Round-3 implementation commit:
  `2720dbf914ea11e638d8619e300b8c9a7e44649e`
  (`fix(security): close Task 6 round 3 findings`).
- Work was performed directly in the required shared worktree on
  `codex/safety-foundation`; no separate worktree was created and nothing was
  pushed.
- PAPER-only operation, manual approval, kill switches, execution-time risk
  checks, and broker-truth authority are unchanged.
- Every database probe used a pytest-created temporary SQLite database. The
  ignored runtime `trading_assistant.db`, Keychain, credentials, existing
  processes, network, external services, brokers/providers, notifications, the
  real app/daemon/MCP, breakers, and order APIs were not accessed.

### Finding closure

- Non-empty migration authority can no longer be minted from caller assertions.
  Issuance requires the exact maintenance guard, exact durable tenure handle,
  real tenure service bound to the migration connection's engine, and its
  installed mutation barrier. Exact owner/generation/unexpired ownership is
  proved on that same connection at issuance, activation, revision entry,
  every fenced DDL seam, and transaction mutation/commit boundaries. A forged
  exact handle around a no-op service is explicitly rejected with zero held
  tenures.
- Migration authority is sealed, exact-connection-bound, and single-use.
  Wrong-connection activation, failed validation, and completed use consume the
  token. Revision `0015` derives ownership and its narrow schema-rebuild fence
  from the authority object itself; caller-supplied Config fence/assertion
  attributes are ignored. Direct and offline Alembic remain fail-closed before
  DDL or version mutation.
- Empty bootstrap authority rechecks actual emptiness at activation. An
  issue-then-create attempt refuses without creating `alembic_version`,
  preserves the injected table/row, and consumes the token. Connection misuse
  and replay are also refused. Alembic now retires authority even when
  activation itself fails.
- Historical revision tests whose schemas predate `runtime_tenures` use a
  context-scoped test-only adapter. It patches only the test process while the
  command runs and exposes no production Config flag, Boolean capability, or
  constructor. Reviewer probes and wrapper-success tests use the unpatched
  production path with real durable tenures.
- Encrypted backup ciphertext remains under a private dot-prefixed temporary
  name through authenticated decryption, exact plaintext hash comparison, and
  SQLite `PRAGMA quick_check`. The final artifact path does not exist during
  verification. After verification, ownership is checked one final time and
  the already-fsynced ciphertext is atomically linked into place. Verification
  failure, ownership loss, cancellation, collision, or later publication
  failure removes private temporaries and any incomplete final publication.
- The tracked round-2 review artifact was regenerated in valid zero-context
  form, and two legacy Markdown headers were mechanically stripped of trailing
  spaces. The round-3 review artifact was also generated with zero context.
  Neither artifact has trailing whitespace or a missing final newline.

### Changed files

- Implementation:
  `migrations/env.py`,
  `migrations/versions/20260727_0015_plan_authority_and_validation_tenure.py`,
  `src/trading_assistant/db/migrate.py`,
  `src/trading_assistant/db/migration_authority.py`,
  `src/trading_assistant/ops/backup.py`,
  `tests/test_migrations.py`, and
  `tests/test_sensitive_migration.py`.
- Evidence and whitespace:
  this report,
  `.superpowers/sdd/2026-07-27-secrets-model-trust/progress.md`,
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-a9ab761..815d956.diff`,
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-b1afd85..2720dbf.diff`,
  `docs/superpowers/specs/2026-07-24-evidence-first-trading-platform-design.md`,
  and
  `docs/superpowers/specs/2026-07-24-safety-foundation-design.md`.

### Round-3 TDD and focused evidence

- Initial focused RED:
  `6 failed in 1.23s`. The probes demonstrated zero-tenure forged
  upgrade/downgrade, bootstrap issue-then-create/reuse, bootstrap
  connection misuse, early final backup visibility, and ownership loss during
  private verification.
- First focused GREEN after the minimum authority/publication changes:
  `6 passed in 0.63s`.
- An adjacent forged-handle RED then proved that an exact
  `RuntimeTenureHandle` wrapped around a caller no-op service could still mint
  authority: `1 failed` with `DID NOT RAISE`. Requiring the real tenure service
  and exact engine binding closed that path; the forged-handle plus valid
  upgrade/downgrade and successor-loss selection became `5 passed`.
- Maintenance wrong-connection, replay, post-issuance lease loss, and valid
  fenced downgrade converged at `4 passed`. Two initial assertion failures in
  that group were test fingerprint debt: a real acquired/released tenure
  correctly changes durable tenure evidence. Schema, version, and protected
  rows remained unchanged.
- Complete focused files/groups:
  migrations `155 passed`;
  sensitive backup/migrate/rotate `48 passed`;
  operational backup callers `13 passed`;
  startup schema, runtime tenure, and release-static adjacency `125 passed`.
  These are `341` non-overlapping focused tests.

### Release gates

- `python -m compileall -q src tests`: PASS.
- `python scripts/check_release_safety.py`:
  `release static checks: PASS`.
- `alembic heads`: exactly `20260727_0015 (head)`.
- `git diff --check`: PASS.
- Tracked-text scans found no trailing whitespace and no missing final newline
  after the artifact cleanup.

### Exactly one round-3 full suite

- Exactly one final full suite ran after every focused check and release gate
  was green. Its environment explicitly unset database, Alpaca, Robinhood,
  LLM/provider, market-data, notification, candidate-signing,
  field-encryption, backup-encryption, live-confirmation, Composio, Octen, and
  OpenAI credential variables.
- Exact result:
  `2656 passed, 1 skipped, 1 warning in 377.03s (0:06:17)`.
- Skip: the opt-in real Alpaca paper integration, disabled because credentials
  were unset. Warning: the existing third-party `websockets.legacy`
  deprecation.
- No second full-suite run was started. No production code or tests changed
  after this result; only this report, progress ledger, whitespace-only
  documentation cleanup, and generated review evidence were changed.

### Round-3 remaining caveats

- No known production correctness or safety defect remains from the two
  important findings or the minor whitespace finding.
- In-place production migration of a non-empty schema that predates durable
  `runtime_tenures` remains intentionally refused and requires the separately
  reviewed isolated-copy procedure.
- The sole test warning is an upstream deprecation unrelated to backup
  publication, migration authority, approval, execution safety, or broker
  truth.

## Review fix round 4

### Status, scope, and commit

- Round-4 baseline:
  `c8ee3d809c59df178e95979a47ed3742a3d09eca`.
- Round-4 implementation commit:
  `0fb34188a45400ccce877c3284fffd12a37c1765`
  (`fix(security): close Task 6 round 4 findings`).
- Work was performed directly in the required shared worktree on
  `codex/safety-foundation`; no separate worktree was created and nothing was
  pushed.
- PAPER-only operation, manual approval, kill switches, execution-time risk
  checks, and broker-truth authority are unchanged.
- Every database probe used a pytest-created temporary SQLite database. The
  ignored runtime `trading_assistant.db`, Keychain, credentials, existing
  operational processes, network, external services, brokers/providers,
  notifications, the real app/daemon/MCP, breakers, and order APIs were not
  accessed.

### Finding closure

- Migration authority now accepts only the exact concrete
  `MigrationAuthority` type. Security decisions are made by non-overridable
  module-level validation over sealed state; hostile subclasses are rejected
  and authority is consumed on activation, assertion, completion,
  wrong-connection, operation, destination, replay, or migration-step misuse.
- Every authority is bound to one exact operation and destination. Empty
  bootstrap is limited to one upgrade to the current head and revalidates
  emptiness at use. Alembic reports structurally observed revision steps back
  through `on_version_apply`; completion requires the expected start-to-head
  transition and expected revision execution. `stamp`, downgrade, partial or
  alternate destinations, no-revision execution, and issue-then-mutate misuse
  cannot leave `SchemaStatus.ready` falsely true.
- Maintenance upgrade and downgrade continue to require a real, unexpired,
  connection-bound durable maintenance tenure at every DDL seam. Direct and
  offline Alembic remain fail-closed. Exact-type validation is used throughout
  issuance, activation, revision entry, observed-step handling, completion,
  and replay handling; no user-overridable instance dispatch remains in the
  authority path.
- Encrypted backup uses hidden pending/committed semantics. Ciphertext remains
  private while authenticated decrypt, plaintext hash verification,
  `PRAGMA quick_check`, maintenance-ownership checks, hooks, private cleanup,
  and required directory fsyncs run. The single atomic public-link operation is
  the final commit point, and no fallible hook, ownership check, or cleanup
  follows it.
- Backup readers and retention enumerate only artifacts whose public name has
  the expected same-inode pending anchor. Tenure loss at every verification
  boundary, hostile cleanup failure, target collision, precommit callback
  failure, and directory-fsync failure expose no committed artifact. A
  successful artifact remains authenticated, decryptable, and quick-check
  clean.
- The two first-full-suite failures were a test synchronization defect, not
  production lock contention. The checkpoint is emitted by test
  instrumentation immediately before the child's blocking `flock`, and no
  round-4 change touched the production submission barrier. Spawn scheduling
  was incorrectly included in a two-second per-child ordering assertion.
  Counted child processes now announce completed setup, wait on an explicit
  parent start event, and report exact lock-attempt checkpoints. A bounded
  helper waits on those events while detecting early child exit; elapsed time
  is only a deadlock watchdog, not the ordering mechanism. Production barrier
  semantics were not changed.

### Changed files

- Migration authority:
  `migrations/env.py`,
  `migrations/versions/20260727_0015_plan_authority_and_validation_tenure.py`,
  and `src/trading_assistant/db/migration_authority.py`.
- Backup publication:
  `src/trading_assistant/ops/backup.py`.
- Regression infrastructure and tests:
  `tests/safety_helpers.py`,
  `tests/test_migrations.py`,
  `tests/test_ops.py`,
  `tests/test_sensitive_migration.py`,
  `tests/test_submission_barrier.py`, and new
  `tests/test_task6_round4.py`.

### Round-4 TDD and focused evidence

- Initial migration-authority RED:
  `7 failed`. The probes demonstrated hostile-subclass authority, bootstrap
  operation/destination confusion, `stamp` false-readiness, downgrade
  reinterpretation, replay, wrong-connection, and no-observed-revision misuse.
- Initial backup-publication RED failed during collection because the required
  committed-artifact reader did not yet exist. This established that the old
  public-name model could not express pending versus committed artifacts.
- The dedicated round-4 regression file converged at `20 passed`.
- Pre-full non-overlapping authority/backup/tenure/startup/static convergence:
  `362 passed in 57.18s`. The final changed-area rerun was `82 passed`.
- The first full suite exposed only the two multiprocessing timing assertions
  described below. Before changing the tests, both exact cases passed alone
  (`2 passed in 3.51s`), passed ten bounded isolated repetitions (`20/20`),
  and the complete production-barrier module passed
  `27 passed in 47.25s`. This evidence, plus the pre-`flock` checkpoint
  location, ruled out production lock contention.
- After deterministic child-ready/start synchronization, both exact cases
  passed (`2 passed in 4.13s`), ten bounded repetitions passed (`20/20`), and
  the complete barrier module passed `27 passed in 47.08s`.
- Final affected focused groups:
  `389 passed in 140.60s (0:02:20)`.

### Full-suite round 1 — failed and preserved

- The first full suite ran only after its then-current focused and release
  gates were green, with database, Alpaca, Robinhood, LLM/provider,
  market-data, notification, candidate-signing, field-encryption,
  backup-encryption, live-confirmation, Composio, Octen, and OpenAI credential
  variables explicitly unset.
- Exact result:
  `2 failed, 2675 passed, 1 skipped, 1 warning in 432.88s (0:07:12)`.
- Failed nodes:
  `test_submission_waiters_make_progress_without_invalidating_active_submission[3]`
  and
  `test_real_writer_has_priority_over_queued_submission_waiters_without_deadlock[3]`.
  Both observed `[False, True, True]` from sequential two-second waits for
  child pre-`flock` instrumentation. No migration-authority, backup,
  production-barrier, or other safety test failed.
- Per the prior one-suite rule, work stopped at that result. A new full-suite
  round was run only after the user explicitly authorized systematic focused
  diagnosis, a synchronization fix, fresh focused convergence, fresh release
  gates, and exactly one confirmation suite.

### Final release gates

- `.venv/bin/python -m compileall -q src tests migrations`: PASS.
- `.venv/bin/python scripts/check_release_safety.py`:
  `release static checks: PASS`.
- `.venv/bin/alembic heads`: exactly `20260727_0015 (head)`.
- `git diff --check`: PASS.
- Explicit modified/untracked text scan found no trailing whitespace and no
  missing final newline.

### Full-suite round 2 — sole confirmation run

- Exactly one new full suite ran after deterministic synchronization, all
  affected focused groups, and every final release gate were green. Its
  environment explicitly unset database, Alpaca, Robinhood, LLM/provider,
  market-data, notification, candidate-signing, field-encryption,
  backup-encryption, live-confirmation, Composio, Octen, and OpenAI credential
  variables.
- Exact result:
  `2677 passed, 1 skipped, 1 warning in 398.82s (0:06:38)`.
- Skip: the opt-in real Alpaca paper integration, disabled because credentials
  were unset. Warning: the existing third-party `websockets.legacy`
  deprecation.
- No source or test changed after this result. Only this report, the progress
  ledger, and generated review artifact were changed for the evidence commit.

### Round-4 remaining caveats

- No known production correctness or safety defect remains from the three
  round-4 findings or the full-suite synchronization failure.
- In-place production migration of a non-empty schema that predates durable
  `runtime_tenures` remains intentionally refused and requires the separately
  reviewed isolated-copy procedure.
- The sole test warning is an upstream deprecation unrelated to migration
  authority, backup publication, approval, execution safety, or broker truth.
