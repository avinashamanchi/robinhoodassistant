# Task 6 report — COMPLETE (final manifest and lifecycle hardening)

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

## Review fix round 5/5

### Status, scope, and commit

- Round-5 baseline:
  `b100c349a9c26d66b9006f2a83b57e07071a715e`.
- Round-5 implementation commit:
  `9f520f6894212a3c46bb27163adda8abba002b2c`
  (`fix(security): close Task 6 round 5 finding`).
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

- Backup publication is now an explicit recoverable two-phase commit. Every
  artifact has a hidden, pre-existing, fixed-size commit-state file containing
  a canonical checksummed record. The record binds the artifact name,
  transaction ID, artifact device/inode/size, state-file device/inode, phase,
  and monotonic generation.
- State records have exactly three legal generations:
  `PENDING:0`, `COMMITTED:1`, and `RETIRED:2`. Files are opened with
  `O_NOFOLLOW` and synchronous write flags, locked exclusively for transitions
  and shared for readers, written with one fixed-size `pwrite`, and explicitly
  fsynced. Torn, corrupt, malformed, copied, wrong-name, wrong-inode,
  wrong-size, missing, symlinked, or replaced state fails closed.
- The encrypted data file is fsynced before its hidden anchor is linked.
  Anchor publication and removal of the private name are directory-fsynced.
  The PENDING state file is then created exclusively, file-fsynced, read-back
  verified, and directory-fsynced before any public artifact name exists.
- The public target is linked while the authoritative record is still
  PENDING. Its parent directory is fsynced before the fixed, pre-existing state
  record transitions to COMMITTED. Official listing and header readers require
  a valid COMMITTED record plus exact target/anchor/state inode agreement;
  target plus anchor alone is never sufficient.
- Reader shared locks cannot cross the writer's exclusive transition lock.
  The state descriptor uses synchronous writes and explicit fsync, so readers
  cannot observe COMMITTED before the durability syscall completes. There is
  no post-COMMITTED hook, maintenance check, state readback, deletion, or other
  application-level fallible action.
- If a commit syscall or descriptor cleanup reports an exception after the
  durable state transition, publication reconciles against the exact
  transaction's authoritative record. Valid COMMITTED returns the already
  verified receipt; PENDING, torn, corrupt, mismatched, missing, or unknown
  state never becomes officially visible and the original cancellation or
  failure propagates.
- Failure cleanup is limited to explicitly tracked private paths and exact
  device/inode identities. Cleanup success is not part of the safety proof:
  ambiguous target links and crash images can remain on disk while PENDING or
  invalid state keeps listing and header reads fail closed. Unrelated or
  attacker-controlled names are never broadly deleted.
- Retention first performs and fsyncs the exact COMMITTED-to-RETIRED state
  transition. Only then does it remove the exact artifact and anchor, fsync the
  directory, remove the exact state file, and fsync again. Any partial deletion
  remains officially uncommitted. Legacy target-plus-anchor artifacts without
  state are ignored and preserved rather than silently adopted or pruned.
- The existing codebase has no encrypted-backup restore entry point. Its two
  official consumers—listing and canonical header reads—both enforce the new
  state protocol; direct internal backup callers receive a receipt only after
  durable COMMITTED publication.

### Changed files

- Protocol implementation:
  `src/trading_assistant/ops/backup.py`.
- Adversarial and retention coverage:
  new `tests/test_task6_round5.py` and updated `tests/test_ops.py`.

### Round-5 TDD and adversarial evidence

- Initial focused RED:
  `12 failed in 0.19s`. The failures reproduced the real-link-then-cancel
  exposure, absent post-link directory fsync, absent commit record,
  state-independent listing/header reads, absent crash boundaries,
  anchor-only retention, and absent unique state ownership.
- First protocol GREEN:
  `12 passed in 0.16s`.
- A second focused RED proved that a fallible state readback still occurred
  after the COMMITTED write and fsync:
  `1 failed`. Removing post-COMMITTED readback made the exact test
  `1 passed`.
- A third focused RED supplied a correctly checksummed record with an
  unhashable malformed phase. Listing raised `TypeError` instead of failing
  closed. Exact type validation made the probe `1 passed`.
- Additional adversarial coverage proves:
  real `os.link` followed by `CancelledError` with all owned cleanup blocked;
  target-directory fsync refusal; full durable commit write followed by
  exception and receipt reconciliation; torn commit write with cleanup blocked;
  missing, corrupt, torn, malformed, copied, mismatched, and inode-replaced
  state; crash images after every PENDING/link/directory-fsync boundary;
  deterministic reader blocking through commit fsync; exact retention
  uncommit-before-delete; unrelated-file preservation; and concurrent
  same-name collision with exactly one artifact/state transaction.
- The four highest-risk link/commit/reader/collision probes passed twenty
  bounded repetitions each:
  `80/80 passed`.
- Final complete Task-6-focused convergence:
  `407 passed in 81.44s (0:01:21)`.

### Final release gates

- `.venv/bin/python -m compileall -q src tests migrations`: PASS.
- `.venv/bin/python scripts/check_release_safety.py`:
  `release static checks: PASS`.
- `.venv/bin/alembic heads`: exactly `20260727_0015 (head)`.
- `git diff --check`: PASS.
- Explicit modified/untracked text scan found no trailing whitespace and no
  missing final newline.

### Full-suite chronology

- Original Task 6:
  `2546 passed, 1 skipped, 1 warning in 326.97s (0:05:26)`.
- Review fix round 1:
  `2 failed, 2615 passed, 1 skipped, 1 warning in 350.54s (0:05:50)`.
  Both failures were stale test fixtures; their exact and adjacent focused
  repairs passed, and the then-active exactly-one rule correctly prevented a
  rerun.
- Review fix round 2:
  `2645 passed, 1 skipped, 1 warning in 370.17s (0:06:10)`.
- Review fix round 3:
  `2656 passed, 1 skipped, 1 warning in 377.03s (0:06:17)`.
- Review fix round 4 first run:
  `2 failed, 2675 passed, 1 skipped, 1 warning in 432.88s (0:07:12)`.
  The failures were brittle spawned-child readiness timing assertions.
- Review fix round 4, after explicit authorization for one confirmation run:
  `2677 passed, 1 skipped, 1 warning in 398.82s (0:06:38)`.
- Review fix round 5 used exactly one final credential-stripped full suite
  after every focused and release gate was green:
  `2695 passed, 1 skipped, 1 warning in 225.51s (0:03:45)`.
- Every listed suite removed real database, Alpaca, Robinhood, LLM/provider,
  market-data, notification, candidate-signing, field-encryption,
  backup-encryption, live-confirmation, Composio, Octen, and OpenAI credential
  variables as applicable. The skip is the opt-in real Alpaca paper
  integration. The warning is the existing third-party `websockets.legacy`
  deprecation.
- No source or test changed after the round-5 full-suite result. Only this
  report, the progress ledger, and generated review artifact were changed for
  the evidence commit.

### Round-5 remaining caveats

- No known production correctness or safety defect remains from the final
  atomicity finding.
- Legacy backup names created before round 5 do not have authoritative state
  records and are intentionally invisible to official readers and automatic
  retention. Recovery or adoption requires a separately reviewed,
  authenticate-before-adopt procedure; this round does not silently trust
  them.
- In-place production migration of a non-empty schema that predates durable
  `runtime_tenures` remains intentionally refused and requires the separately
  reviewed isolated-copy procedure.
- The sole test warning is an upstream deprecation unrelated to backup
  publication, migration authority, approval, execution safety, or broker
  truth.

## Exceptional post-round-5 hardening

### Status, scope, and commit

- Exceptional-hardening baseline:
  `256c5a28b11c517bcba0c78e660af073341b660a`.
- Implementation commit:
  `c4b6094660fda2ef5d31e0f48d9fbdabd2928dc3`
  (`fix(security): harden Task 6 backup protocol`).
- Work remained in the required shared worktree on
  `codex/safety-foundation`; no separate worktree was created and nothing was
  pushed.
- PAPER-only operation, manual approval, kill switches, execution-time risk
  checks, and broker-truth authority are unchanged.
- All database activity used pytest-created temporary SQLite databases. The
  ignored runtime `trading_assistant.db`, Keychain, credentials, existing
  processes, network, external services, broker/provider/notification APIs,
  the app, daemon, MCP server, breakers, and order APIs were not accessed.

### Finding closure

- Commit-state version 2 binds the complete encrypted artifact SHA-256 in
  addition to name, device, inode, size, transaction, generation, and
  state-file identity. The digest is computed through the exact fsynced anchor
  descriptor.
- Every official consumer opens state and artifact paths with `O_NOFOLLOW` and
  `O_NONBLOCK`, validates regular type and exact fixed state size before
  locking, and uses bounded nonblocking `flock` retries. FIFO/socket sidecars,
  nonregular targets, and externally held locks therefore return stable
  unavailable/busy outcomes within the bound rather than hanging.
- Listing, header reads, retention, retirement, and verification hash through
  the already-open artifact descriptor while the authoritative state lock is
  held. They revalidate target, anchor, state path, descriptor identity,
  size, timestamps, and digest after use; same-size corruption, length changes,
  inode/hardlink/path/state swaps, and mutation between validation and use fail
  closed.
- Header parsing duplicates the already validated artifact descriptor instead
  of reopening by path. Corrupt artifacts are never treated as valid retention
  candidates.
- COMMITTED and RETIRED transitions now write, fsync, reread, and prove the
  exact state through the same still-open exclusively locked state descriptor.
  A syscall that reports failure after the durable write is reconciled on that
  descriptor before unlock. Once a valid durable transition and artifact have
  been proved locally, later unlock, close, transient reader-lock, or cleanup
  errors cannot reverse success or make the API raise.
- If a COMMITTED transition cannot be proved, the protocol durably restores
  PENDING. If that restoration also fails after a possible commit write, it
  attempts and proves RETIRED through the same descriptor before propagating.
  Official readers remain fail closed.
- State I/O handles short reads, short writes, and `EINTR`; fixed-offset
  `pread`/`pwrite` avoids descriptor-offset confusion. State records remain
  canonical, fixed-size, checksummed, exact-version, and exact-name bound.
- Conservative crash recovery runs only on exact protocol state/anchor names,
  after a 24-hour default TTL. It takes bounded locks, revalidates age and exact
  descriptor identities, and idempotently completes PENDING/RETIRED cleanup.
  It covers anchor-only, state-only, targetless PENDING, target-linked PENDING,
  and partial RETIRED images while preserving committed, recent, busy,
  malformed, corrupt, symlinked, legacy, and unrelated/operator-owned files.
- Operational backup performs destination-only recovery and retention before
  source-engine creation and maintenance acquisition. It then acquires a fresh
  full source lease immediately before snapshot work, so slow destination
  cleanup cannot consume the no-renewal snapshot window.

### Complexity and API audit

- The large `backup.py` delta was explicitly audited before the full suite.
  Reader validation, exclusive transition, and TTL recovery were retained as
  three separate paths because they have materially different locking,
  durability, and cleanup semantics.
- Duplicate-looking identity/hash checks occur at distinct pre-use,
  post-use, and post-transition race boundaries and are intentional.
- The audit removed an unused writable reader mode, an unused transaction
  filter, and a single-use boolean authority wrapper.
- AST/reference inspection found no unreferenced private helper. Comparison
  against the baseline found no added, removed, or signature-changed public
  backup function. No restore consumer exists elsewhere in the codebase.

### Changed files

- Protocol implementation:
  `src/trading_assistant/ops/backup.py`.
- Regression coverage:
  `tests/test_task6_round5.py` and new
  `tests/test_task6_exceptional_hardening.py`.

### TDD and focused evidence

- Initial digest/reconciliation RED:
  `12 failed, 4 passed` across 16 selected cases.
- Additional focused RED probes independently demonstrated:
  broken-symlink namespace cleanup (`2 failed`), descriptor-age recovery race
  (`1 failed`), FIFO artifact blocking (`3 failed`), state-path swap during
  header use (`1 failed`), and failed PENDING restoration exposing a later
  COMMITTED listing (`1 failed`).
- Final backup, migration, and adversarial group:
  `143 passed in 10.93s`.
- Final migration, runtime-tenure, startup-schema, release-static, and
  submission-barrier adjacency group:
  `307 passed in 117.66s (0:01:57)`.
- These are `450` non-overlapping focused tests.
- Ten repetitions of 14 highest-risk corruption, state-swap, durable-commit,
  held-lock, child-crash recovery, and lease-sequencing cases passed:
  `140/140`.

### Final release gates

- `.venv/bin/python -m compileall -q src tests migrations`: PASS.
- `.venv/bin/python scripts/check_release_safety.py`:
  `release static checks: PASS`.
- `.venv/bin/alembic heads`: exactly `20260727_0015 (head)`.
- `git diff --check`: PASS.
- Explicit modified/untracked text scan found no trailing whitespace and no
  missing final newline.
- Public backup API comparison: unchanged.
- Private-helper reference audit: no dead helper found.

### Full-suite chronology

- The complete original-through-round-5 chronology is preserved above,
  including both failed suites and every authorized confirmation run.
- This exceptional hardening used exactly one new final full suite after all
  focused, repeated-adversarial, compile, static, Alembic, API, diff, and
  whitespace gates were green.
- Credential/provider variables were explicitly removed from the command
  environment, and the worktree had no `.env`.
- Exact result:
  `2738 passed, 1 skipped, 1 warning in 393.48s (0:06:33)`.
- Skip: the opt-in real Alpaca paper integration, disabled because credentials
  were unset. Warning: the existing third-party `websockets.legacy`
  deprecation.
- No second exceptional-hardening full suite ran. No source or test changed
  after this result; only this report, the progress ledger, and generated
  review evidence were changed.

### Remaining caveats

- Commit-state SHA-256 and the record checksum detect accidental/adversarial
  content replacement inside the tested filesystem race model; they are not a
  keyed MAC. Authenticity against an attacker who can rewrite every artifact
  and state byte still relies on the private mode-`0700` directory, mode-`0600`
  files, and operating-system account boundary.
- State version 1 and legacy target/anchor-only backups remain intentionally
  invisible and are preserved. Adoption requires a separately reviewed,
  authenticate-before-adopt procedure.
- Orphan recovery is deliberately conservative and TTL-delayed. Busy,
  malformed, ambiguous, or operator-owned names are preserved for manual
  inspection rather than deleted.
- In-place production migration of a non-empty schema predating durable
  `runtime_tenures` remains intentionally refused.
- The sole warning is an upstream deprecation unrelated to backup durability,
  migration authority, approval, execution safety, or broker truth.

## Exceptional plaintext-orphan and final-commit hardening

### Status, safety boundary, and commit

- Review baseline:
  `dfbe577c2186fed942bf9834c64486f0e933ede1`.
- Implementation commit:
  `f37638af92b58ed0e999c7c63fa6f8effe2b6826`
  (`fix(security): harden Task 6 crash transactions`).
- Work remained in the required shared worktree on
  `codex/safety-foundation`; no separate worktree was created and nothing was
  pushed.
- PAPER-only operation, manual approval, kill switches, execution-time risk
  checks, and broker-truth authority are unchanged.
- All database activity used pytest-created temporary SQLite databases. The
  ignored runtime `trading_assistant.db`, Keychain, credentials, existing
  processes, network, external services, broker/provider/notification APIs,
  the app, daemon, MCP server, breakers, and order APIs were not accessed.

### Finding closure

- Every operation-owned snapshot, verification database, encrypted
  ciphertext, and known SQLite sidecar now lives under one exact hidden
  `.backup-txn-<128-bit-id>` directory. No new `.sensitive-snapshot-*` or
  `.sensitive-verify-*` root temporary file is created.
- A fixed-size canonical checksummed manifest binds the exact transaction ID,
  directory name, device, and inode. The mode-`0700` directory, mode-`0600`
  manifest, manifest contents, and both containing-directory entries are
  fsynced before the first plaintext member can exist.
- Transaction members use a fixed allowlist and dirfd-relative
  `O_NOFOLLOW`/`O_NONBLOCK` opens. Creation, reopening, hashing, verification,
  and deletion revalidate regular type, mode, exact directory/manifest
  identity, and the allowed member set.
- The operation holds an exclusive manifest lock throughout normal work.
  Recovery considers only exact transaction-directory names older than the
  conservative TTL, takes the same lock with a bounded monotonic deadline,
  and revalidates manifest, inode, member names, types, modes, and ages before
  removing only exact members. It is idempotent and preserves recent, busy,
  malformed, corrupt, symlinked, copied-manifest, extra-member, and unrelated
  operator-owned paths.
- Real child-process `_exit` probes cover every exposed plaintext/ciphertext
  lifecycle hook from durable manifest through snapshot, encryption,
  verification, hash, and SQLite `quick_check`. After TTL, two recovery passes
  leave no operation-owned plaintext or ciphertext.
- Publication remains officially PENDING while target and anchor links are
  created and the destination directory is fsynced. The complete encrypted
  artifact is authenticated, hashed, inode-checked, header-checked, privately
  decrypted, hash-compared, and SQLite-checked before publication can commit.
- The transaction directory is completely removed and both relevant
  directories are fsynced before the last maintenance check, optional hook,
  and receipt construction.
- The same-descriptor durable COMMITTED transition is now the irrevocable final
  operation. All artifact verification and receipt construction occur before
  it. Once COMMITTED is proved through the still-exclusively-locked state
  descriptor, only unlock/close remain; their reported failures are suppressed
  because kernel descriptor closure releases ownership and cannot reverse the
  durable state. No hook, maintenance check, verification, restoration, or
  cleanup can run after that proof.
- Ambiguous state-write/fsync failures are reconciled on the same locked
  descriptor. The exact chained probe—durable COMMITTED write plus hostile
  post-commit verification, PENDING/RETIRED restoration, and cleanup
  failures—returns the prebuilt receipt, performs none of those post-commit
  operations, and lists exactly one committed backup.
- Operational recovery and retention still run before source-engine creation
  and maintenance acquisition, preserving the fresh full lease immediately
  before the no-renewal SQLite snapshot.

### Complexity and public-API audit

- `backup.py` is now `1,891` lines, down from `2,133` at the review baseline.
  The `914`-line `backup_transaction.py` contains the single private
  transaction filesystem protocol instead of leaving an approximately
  2,800-line mixed-responsibility module.
- Shadowing copies of lock, record, descriptor-I/O, hash, transaction-member,
  cleanup, and transaction-recovery helpers were removed from `backup.py`.
  AST/reference checks found no unused import, duplicate private definition, or
  single-reference dead private helper in either module.
- A smaller collapse was rejected because transaction ownership/recovery and
  committed-state reading/transition/retention have different locks,
  identities, failure semantics, and durable commit points. Combining them
  would reintroduce the ambiguity this review removed.
- Existing public backup function/class names and all public function
  signatures are unchanged. `EncryptedBackupError` remains available from
  `trading_assistant.ops.backup`; only its implementation storage moved to the
  private transaction module.
- Superseded root-temp and impossible anchor-only recovery helpers/tests were
  removed. Their safety properties are covered by real child crash images from
  states that the current protocol can actually emit.

### Changed files

- Protocol implementation:
  `src/trading_assistant/ops/backup.py` and
  `src/trading_assistant/ops/backup_transaction.py`.
- Regression coverage:
  `tests/test_sensitive_migration.py`,
  `tests/test_task6_round4.py`,
  `tests/test_task6_round5.py`,
  `tests/test_task6_exceptional_hardening.py`, and new
  `tests/test_task6_transaction_directory_hardening.py`.

### TDD and focused evidence

- Initial exact transaction/final-commit RED:
  `15 failed, 1 passed in 0.61s`.
- First implementation convergence:
  `16 passed in 1.99s`.
- Expanded crash, malformed-namespace, successful-cleanup,
  pre-commit-crash, partial-cleanup-retry, and chained-final-commit coverage:
  `19 passed`.
- Complete backup, sensitive-migration, round-4, round-5, exceptional, and
  transaction-directory focused group:
  `157 passed`.
- Migration, runtime-tenure, startup-schema, release-static, and
  submission-barrier adjacency group:
  `307 passed`.
- Three bounded repetitions of all 19 transaction/crash cases:
  `57/57 passed`.
- Three bounded repetitions of nine durable-commit, held-lock, fresh-lease,
  and concurrent-collision cases:
  `27/27 passed`.

### Final release gates

- `.venv/bin/python -m compileall -q src tests migrations`: PASS.
- `.venv/bin/python scripts/check_release_safety.py`:
  `release static checks: PASS`.
- `.venv/bin/alembic heads`: exactly `20260727_0015 (head)`.
- `git diff --check`: PASS.
- Explicit modified/untracked text scan:
  `whitespace-eof: ok (7 files)`.
- Public API/signature comparison: unchanged at the import boundary.
- Duplicate/dead-code/import audit: clean.

### Full-suite chronology

- The complete original-through-round-5 and first exceptional-hardening
  chronology is preserved above, including every failed and authorized
  confirmation run.
- This exceptional crash-transaction pass ran exactly one new full suite,
  only after focused, repeated-adversarial, compile, release-static, Alembic,
  API, diff, and whitespace gates were green.
- Database, Alpaca, Robinhood, LLM/provider, market-data, notification,
  candidate-signing, field-encryption, backup-encryption, live-confirmation,
  runtime-instance, Composio, Octen, and OpenAI credential variables were
  explicitly removed from the command environment.
- Exact result:
  `2752 passed, 1 skipped, 1 warning in 396.18s (0:06:36)`.
- Skip: the opt-in real Alpaca paper integration, disabled because credentials
  were unset. Warning: the existing third-party `websockets.legacy`
  deprecation.
- No second full suite ran. No source or test changed after this result; only
  this report, the progress ledger, and generated review evidence changed.

### Remaining caveats

- Manifest and commit-state checksums and artifact SHA-256 are not keyed MACs.
  Authenticity against an attacker able to rewrite every byte under the same
  OS account still relies on the mode-`0700` directory, mode-`0600` files, and
  operating-system account boundary.
- A crash after transaction-directory creation but before the manifest becomes
  durable can leave an empty exact-name directory. Recovery intentionally
  preserves a manifest-less directory because it cannot prove protocol
  ownership; no plaintext member is permitted before manifest durability.
- Root-level temporary files from pre-hardening versions are not adopted or
  broadly deleted. New code does not create them; any legacy cleanup requires
  a separately reviewed ownership procedure.
- Orphan recovery is deliberately conservative and TTL-delayed. Busy,
  malformed, ambiguous, or operator-owned paths remain for manual inspection.
- In-place production migration of a non-empty schema predating durable
  `runtime_tenures` remains intentionally refused.
- The sole warning is an upstream deprecation unrelated to backup durability,
  migration authority, approval, execution safety, or broker truth.

## Final manifest-identity, commit-reconciliation, and lifecycle hardening

### Status, safety boundary, and implementation

- Review baseline:
  `c94d34770cac28dda159d0c1a08a7cdeae2ba5e3`.
- Implementation commit:
  `992e36a1198041477b6ee555048ca342b055e9f5`
  (`fix(security): complete Task 6 hardening`).
- Work remained in the required shared worktree on
  `codex/safety-foundation`; no separate worktree was created and nothing was
  pushed.
- PAPER-only operation, manual approval, kill switches, execution-time risk
  checks, and broker-truth authority are unchanged.
- All database work used pytest-created temporary SQLite databases. The
  ignored runtime `trading_assistant.db`, Keychain, credentials, network,
  external services, broker/provider/notification APIs, app, daemon, MCP
  server, breakers, and order APIs were not accessed.

### Finding closure

- Transaction manifest version 2 now binds every fixed operation member
  (`snapshot.sqlite3`, `verification.sqlite3`, and `encrypted.aesgcm`) by
  exact name, regular-file type, mode, device, and inode.
- All members are created mode `0600`, individually fsynced, captured, and
  directory-fsynced before the checksummed manifest is written and fsynced.
  Sensitive content therefore cannot precede durable ownership metadata.
- Snapshot and verification SQLite connections use journal mode `OFF` and
  memory temp storage, so no unmanifested WAL, SHM, or journal sidecar can be
  created. Every open and cleanup revalidates the exact recorded member
  through no-follow descriptors.
- Recovery requires the complete exact namespace and all recorded identities.
  A missing, replaced, injected, symlinked, or extra member preserves the
  directory unchanged and fail-closed. Real child `_exit` tests cover all ten
  plaintext/ciphertext lifecycle stages, replacement at every stage, and
  unrecorded sidecars.
- The durable COMMITTED transition no longer relies on a Python boolean.
  Every `BaseException`, including one injected between the nested committed
  readback and its caller assignment, reconciles through the same still-open,
  exclusively locked state descriptor.
- A valid durable COMMITTED record returns the already-built receipt even when
  the injected chain also reports transient read, unlock, close, restoration,
  and cleanup failures. No restoration or cleanup runs after committed proof.
- If COMMITTED cannot be proved, the transition restores or retires state
  durably before propagating. Official readers remain fail-closed.

### Full-suite failure diagnosis

- The first full suite in this hardening pass completed with:
  `1 failed, 2775 passed, 1 skipped, 1 warning in 392.71s`.
- The sole failure was
  `tests/test_sensitive_crypto.py::test_scoped_idempotent_guards_do_not_retain_closed_sessions`.
  Its suite-global registry-size assertion observed two guarded sessions
  opened concurrently by stale `app-tenure-renewal` workers.
- The target passed standalone, across its full file, and in likely predecessor
  order. A per-test thread/session ownership probe then identified three
  direct-container bootstrap tests that started real renewal workers without
  closing their guards:
  `test_production_container_arms_exact_dynamic_alpaca_paper_guard`,
  `test_app_container_serves_console_with_failed_startup_reconciliation`, and
  `test_app_wires_runtime_renewal_loss_to_controlled_shutdown`.
- This was not classified as flaky. The scoped session was collectable; the
  full-suite failure exposed real test-owned worker leaks plus an assertion
  that incorrectly treated unrelated concurrent sessions as target retention.
- Direct-container tests now use an exact ownership context that always closes
  the guard and asserts its captured renewal worker is stopped. The complete
  bootstrap file leaves no renewal worker according to the same ownership
  probe.
- The sensitive-session regression now keeps a separate guarded session alive
  while proving the exact closed target is collectable, then proves the
  unrelated session is also collectable after its own close. It does not clear
  globals, use a `<=` allowance, or assume suite-global emptiness.

### TDD and focused evidence

- Deterministic ownership RED:
  the exact sensitive-session test failed `1 failed` after an unrelated
  guarded session was deliberately kept alive; the old assertion observed
  registry size `1` versus baseline `0`.
- Narrow lifecycle convergence:
  `4/4` exact owner/target tests passed.
- Complete bootstrap ownership probe:
  `53/53` passed with zero surviving renewal workers.
- Bootstrap, sensitive-field, and runtime-tenure group:
  `161/161` passed (`53 + 59 + 49`).
- Complete backup/operations/adversarial group before the first full suite:
  `181/181` passed.
- Post-diagnosis backup/adversarial confirmation:
  `167/167 passed in 9.39s`.
- Migration, runtime-tenure, startup-schema, release-static, and
  submission-barrier adjacency:
  `307/307 passed in 114.33s`.
- Prior bounded transaction/crash repetitions remained green:
  `129/129`; durable-commit repetitions remained green: `27/27`.

### Complexity and public-API audit

- Current sizes are `1,899` lines for `backup.py` and `1,154` lines for the
  private `backup_transaction.py`.
- `backup.py` retains the same public classes, functions, and signatures as
  the review baseline. No source or test imports `backup_transaction.py`
  directly; it remains the private filesystem-transaction implementation
  behind `backup.py`.
- AST and reference checks found no duplicate top-level definition and no
  zero-reference private helper in either module.
- The additional transaction code is one coherent responsibility: immutable
  member identity capture/open/removal. It does not duplicate commit-state
  reading, retention, or source-maintenance logic.

### Final release gates and confirmation suite

- `.venv/bin/python -m compileall -q src tests migrations`: PASS.
- `.venv/bin/python scripts/check_release_safety.py`:
  `release static checks: PASS`.
- `.venv/bin/alembic heads`: exactly `20260727_0015 (head)`.
- `git diff --check`: PASS.
- Explicit modified/untracked text scan:
  `whitespace-eof: ok (5 files)`.
- Exactly one confirmation full suite ran after the lifecycle fix and all
  focused/static gates were green. Database, broker, provider, market-data,
  notification, encryption, signing, live-confirmation, runtime-instance,
  Composio, Octen, and OpenAI credential variables were explicitly unset; no
  worktree `.env` existed.
- Exact confirmation result:
  `2776 passed, 1 skipped, 1 warning in 390.41s (0:06:30)`.
- Skip: the opt-in real Alpaca paper integration, disabled because credentials
  were unset. Warning: the existing third-party `websockets.legacy`
  deprecation.
- Both full-suite rounds are recorded above. No source or test changed after
  the green confirmation; only this report, the progress ledger, and generated
  review evidence changed.

### Changed files

- Protocol:
  `src/trading_assistant/ops/backup.py` and
  `src/trading_assistant/ops/backup_transaction.py`.
- Backup regressions:
  `tests/test_task6_transaction_directory_hardening.py`.
- Lifecycle and exact session-ownership regressions:
  `tests/test_bootstrap.py` and `tests/test_sensitive_crypto.py`.

### Remaining caveats

- Manifest and commit-state checksums and artifact SHA-256 remain unkeyed.
  Authenticity against an attacker able to rewrite every file under the same
  OS account relies on the mode-`0700` directory, mode-`0600` files, and
  operating-system account boundary.
- A crash before the manifest becomes durable can leave only empty precreated
  files in an exact-name transaction directory. Recovery preserves that
  unprovable namespace; sensitive content is not written before manifest
  durability.
- Recovery remains deliberately conservative and TTL-delayed. Busy,
  malformed, partial, or operator-modified namespaces require manual
  inspection.
- The sole warning is an upstream deprecation unrelated to backup durability,
  migration authority, session collectability, approval, execution safety, or
  broker truth.
