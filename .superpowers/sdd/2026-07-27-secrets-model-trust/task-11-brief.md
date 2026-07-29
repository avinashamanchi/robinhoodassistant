### Task 11: Make trust-boundary regressions fail the release gate

**Files:**

- Modify: `scripts/check_release_safety.py`
- Modify: `tests/test_release_static.py`
- Modify: `src/trading_assistant/preflight.py`
- Modify: `tests/test_launch.py`
- Modify: `README.md`
- Modify: `docs/RUNBOOK.md`

**Interfaces:**

- Adds static checks for secret sources, webhook routes, mutable chat tools,
  plaintext field writes, unsafe URLs, proxy trust, and committed TLS/private
  state.
- Adds preflight checks `KEYCHAIN`, `LOCAL_TLS`, `FIELD_ENCRYPTION`,
  `OUTBOUND_ORIGINS`, `INTEGRATIONS_DISABLED`.

- [x] **Step 1: Add negative static fixtures**

Each fixture must fail with a stable code:

- `WEBHOOK_ROUTE_PRESENT`;
- `ENVIRONMENT_SECRETS_IN_PRODUCTION`;
- `COMPOSIO_ENABLED`;
- `MUTABLE_CHAT_TOOL`;
- `PLAINTEXT_SENSITIVE_WRITE`;
- `CROSS_ORIGIN_REDIRECT_ENABLED`;
- `PROXY_HEADERS_TRUSTED`;
- `INSECURE_COOKIE`;
- tracked `.env`, SQLite DB, TLS private key, or decrypted backup.

- [x] **Step 2: Run and verify missing checks**

```bash
uv run pytest tests/test_release_static.py tests/test_launch.py -v
```

Expected: FAIL because the new invariants are not gated.

- [x] **Step 3: Implement AST and Git-tree checks**

Parse FastAPI decorators and reject any path beginning `/webhook` or `/hooks`.
Parse `READ_ONLY_TOOL_SPECS` and forbid mutation names. Parse assignments to
the sensitive registry. Search runtime composition roots for
`EnvironmentSecretProvider`. Inspect `git ls-files` rather than only the
working tree for private artifacts.

Do not scan or print secret values. Pattern findings report path, line, and
stable rule only.

- [x] **Step 4: Extend preflight**

Normal readiness requires:

- Keychain provider and required fields present;
- local certificate valid and key mode `0600`;
- exact loopback bind/origin/hosts and secure cookies;
- encryption state complete and key ID available;
- no webhook/Composio integration;
- exact outbound HTTPS origins;
- existing paper-mode, reconciliation, breaker, and quote-integrity checks
  unchanged. Daemon freshness is a separate post-start observation.

Preflight never resets a breaker, starts a daemon, submits a new order, calls
an LLM, or sends a notification. Existing broker-truth reconciliation may
repair or cancel already-known paper-order state during the explicit
operator-controlled readiness step.

- [x] **Step 5: Document operator commands and hard limits**

README/RUNBOOK must document:

- Keychain migration and audit;
- local TLS setup;
- encrypted field migration/verification/rotation;
- HTTPS app start and separate daemon start;
- Composio disabled pending provider-side rotation;
- no webhook;
- read-only chat → explicit queue → separate approval;
- backup recovery;
- no profit guarantee and no live-mode support.

- [x] **Step 6: Run the complete trust-boundary matrix**

```bash
uv run pytest tests/test_secret_provider.py tests/test_transport_boundary.py tests/test_outbound_policy.py tests/test_sensitive_crypto.py tests/test_sensitive_migration.py tests/test_untrusted_content.py tests/test_candidate_boundary.py tests/test_security_posture.py tests/test_release_static.py tests/test_launch.py -v
uv run python scripts/check_release_safety.py
```

Expected: all tests PASS and `release static checks: PASS`.

- [x] **Step 7: Run the full suite**

```bash
uv run pytest
```

Expected: PASS with only the repository's documented skip.

Actual sole run: `3437 passed, 1 failed, 1 skipped, 1 warning`. The failure
was the lazy default-Keychain-provider compatibility path; it was corrected,
then the exact failed test passed `2/2` and the complete affected preflight,
secret, posture, and Task 9 compatibility set passed `199/199`. The full suite
was not rerun under the explicit one-run constraint.

- [x] **Step 8: Commit**

```bash
git add scripts/check_release_safety.py tests/test_release_static.py src/trading_assistant/preflight.py tests/test_launch.py README.md docs/RUNBOOK.md
git commit -m "chore(security): gate trust-boundary invariants"
```

---

## Plan 2 completion checkpoint

Run:

```bash
git status --short
uv run pytest
uv run python scripts/check_release_safety.py
```

Required result:

- clean working tree;
- complete pytest and static-gate pass;
- production roles require macOS Keychain;
- loopback HTTPS and exact-origin policy are enforced;
- registered sensitive fields are encrypted with migration state complete;
- general chat cannot mutate state;
- signed queue endpoints create proposals/rules but never execute;
- no webhook or Composio integration is enabled;
- no broker/provider calls, daemon start, breaker reset, or order submission
  occurred during verification.

## Governing preflight-review amendments

These amendments are part of Task 11 and override narrower or repeated
instructions above.

### Ownership and execution boundary

- Own Task 11 production, tests, documentation, and SDD evidence only.
- Do not start Plan 3, push, start the app/daemon/MCP server, access a real
  Keychain/credential/runtime database, or make broker/provider/notifier/network
  calls.
- Use temp fixtures and fakes only. Never read, use, store, or echo the
  compromised Composio credential.
- Use TDD with exact negative-fixture RED evidence, focused GREEN, exactly one
  no-argument `uv run pytest` after focused GREEN, then the static gate.
- Commit implementation/tests/docs first. Commit the brief, report, bounded
  review diff, plan checkboxes, and progress ledger separately as evidence.

### Static finding contract

- Introduce a strict immutable `ReleaseViolation(code, path, line)` whose
  fields contain only a stable code, root-relative path, and positive line.
- Never retain or print source snippets, matched values, URLs/query strings,
  secret-like text, exception text, or arbitrary AST representations.
- CLI violations are deterministically sorted and formatted exactly
  `CODE path:line`, followed by a generic stable failure count.

### Effective route graph

- Resolve effective FastAPI/Starlette routes, including decorator aliases,
  `getattr`, `APIRouter` prefixes, nested `include_router` prefixes, imported
  routers and app factories, imperative `add_api_route`, `add_route`, and
  `mount`, from canonical composition roots.
- Any effective `/webhook*` or `/hooks*` route emits
  `WEBHOOK_ROUTE_PRESENT`.
- Dynamic or unresolved route path/prefix/registration/import composition
  emits `ROUTE_REGISTRATION_UNPROVEN`.
- Preserve Task 10 duplicate/effective-route protection. Cover aliases,
  nested includes, factories, dynamic `getattr`/prefix/path, imperative
  mounts, and comment/string decoys.

### Chat tool boundary

- Gate the effective final `READ_ONLY_TOOL_SPECS` plus the Agent/ToolRouter
  dispatch registry and reachable call graph.
- Permit only the exact current read-tool allowlist and immutable local draft
  constructors.
- Mutation names, aliases to mutation, dynamic registry construction,
  `getattr`, dynamic imports, and execute/approve/submit/cancel/reset/notify
  paths emit `MUTABLE_CHAT_TOOL` or `CHAT_TOOL_REGISTRY_UNPROVEN`.
- Do not implement this boundary as text search.

### Sensitive-field writes

- Parse the root-local sensitive registry from the scanned fixture root with
  AST only; never import the active checkout to test a fixture.
- Trace ORM attribute assignment, constructor kwargs, `update(values)`, bulk
  operations, aliases, and helper bypasses for registered fields.
- Invalid or dynamic registry emits `SENSITIVE_REGISTRY_INVALID`; unapproved
  writes emit `PLAINTEXT_SENSITIVE_WRITE`. Findings remain value-free.

### Secret sources and disabled integrations

- Production composition roots cannot instantiate/use
  `EnvironmentSecretProvider` or raw `os.environ`/`getenv` for registered
  secrets.
- Allow `EnvironmentSecretProvider` only in explicit development CLI branches
  in `db/migrate.py` and `ops/safety_drill.py`, with exact mapping and explicit
  `allow_environment`; otherwise emit
  `ENVIRONMENT_SECRETS_IN_PRODUCTION`.
- Resolve aliases, `getattr`, and dynamic-import escapes; unresolved
  composition fails closed. macOS Keychain remains the production source.
- Check config defaults, env examples, runtime imports/callers/toolkits/MCP,
  URLs, and docs for explicit webhook/Composio disablement. Emit
  `COMPOSIO_ENABLED` or `WEBHOOK_ROUTE_PRESENT`; install or call no integration.

### Outbound manifest and MarketStack removal

- Define exact allowed HTTPS origins by adapter and role.
- Reject direct `requests`/`httpx`/`aiohttp`/OpenAI clients outside approved
  wrappers, redirects, proxies/`trust_env`, dynamic or unpinned base URLs,
  query-string credentials, HTTP, and unknown clients/origins.
- Emit `OUTBOUND_ORIGIN_UNAPPROVED`, `OUTBOUND_CLIENT_UNAPPROVED`, or
  `QUERY_SECRET`.
- Remove the MarketStack runtime/data path and use Alpaca historical data;
  update config, models, tests, and docs without network calls.
- Allow only feature/role-appropriate Alpaca, Anthropic, and Telegram origins.
  Composio has no origin or tool.

### Transport and tracked artifacts

- AST/config-check cross-origin redirects, trusted proxy/forwarded headers,
  insecure cookies, wildcard hosts/origins, and disabled TLS.
- Inspect tracked artifacts via `git ls-files -z` rooted at the scanned fixture.
  Emit separate stable codes for tracked `.env`, SQLite DB/WAL/SHM, TLS private
  key, private certificate, decrypted backup, logs, and raw exports.
- Never open or print private artifact contents. Git inspection failure emits
  `GIT_TREE_UNPROVEN`.

### Structural preflight

- Add checks named exactly `KEYCHAIN`, `LOCAL_TLS`, `FIELD_ENCRYPTION`,
  `OUTBOUND_ORIGINS`, and `INTEGRATIONS_DISABLED`.
- Run every local structural check before constructing or calling any
  broker/provider/notifier. Any structural failure exits before outbound
  construction.
- `KEYCHAIN` uses an injected provider and required role fields and returns
  only stable detail.
- `LOCAL_TLS` verifies exact loopback host/origin/allowed hosts, secure cookie,
  key mode, and certificate validity.
- `FIELD_ENCRYPTION` verifies completed migration and configured key with local
  safe checks.
- `OUTBOUND_ORIGINS` verifies the exact manifest/config.
- `INTEGRATIONS_DISABLED` verifies webhook and Composio are off.
- Existing paper-mode, reconciliation, breaker, and quote checks remain
  unchanged and run only after the structural gate. Daemon freshness is
  observed separately after startup; there is no daemon-health preflight row.
- Tests use fakes that raise if outbound construction or calls occur.

### Documentation and verification

- Update `README.md`, `docs/RUNBOOK.md`, `docs/ops/README.md`, and
  `scripts/launchd/README.md` with safe Keychain migration/audit, local TLS,
  encrypted-field migration/verification/rotation, HTTPS app and separate
  daemon start, Composio disabled pending provider-side rotation, no webhook,
  read-only chat to explicit signed queue to separate approval, backup limits,
  no profit guarantee, and no live mode.
- Add exact negative fixtures for every requested stable code, one clean-root
  positive fixture, false-positive decoys, root-isolation coverage, and
  deterministic value-free output coverage.
- Run the plan trust matrix plus affected provider/backtest/config/preflight
  tests. The repository static gate must pass. Run exactly one full suite;
  reuse that result for the completion checkpoint rather than running it
  again.

## Fix round 1 completion

- [x] Canonical final authorities reject duplicate/conditional definitions,
  rebinding, direct mutation, alias mutation, and dynamic construction.
- [x] Route branch unions, route-list mutation, `__getattribute__`
  registration, recursive chat helpers, sensitive-write bypasses, environment
  mapping access, direct clients, option provenance, TLS/cookie drift,
  hermetic-root failure, safe output, and broad artifact names have exact
  negative fixtures.
- [x] Watchdog uses the injected pinned local-liveness transport; every runtime
  adapter/role checks the outbound manifest before production client
  construction.
- [x] Preflight requires explicit macOS Keychain provenance, executes all five
  local rows independently, uses canonical TLS paths, and keeps its encryption
  check metadata-only.
- [x] All four operator documents and the executable plan remove stale
  MarketStack instructions and accurately state preflight and daemon limits.
- [x] Implementation/tests/operator docs committed separately as
  `1f63080bc18894d025a20690dfbd6b4e7d6dd946`.
- [x] Final static-fixture file: `245 passed`.
- [x] Final trust/affected matrix: `1575 passed, 1 warning`.
- [x] Repository static gate: `release static checks: PASS`.
- [x] Exactly one full suite:
  `3535 passed, 7 failed, 1 skipped, 1 warning`; all seven stale fixture
  interfaces were corrected, then the exact set passed `8/8` and complete
  affected files passed `228/228`. No second full run was made.
- [x] Bounded review diff and round-1 review package prepared with zero open
  code findings.

Residual release evidence is explicit: the single full-suite artifact is not
green, while the final tree has exact focused, complete affected, matrix,
compile, diff, and repository-static proof. Composio remains disabled pending
external provider-side revocation/rotation; no webhook, live mode, autonomous
execution, or profit guarantee was introduced.

## Fix round 2 completion

The round-1 statement that its bounded review had zero open code findings is
historical and superseded. Fresh review found additional statically reachable
authority, helper-effect, mapping-option, transport, artifact, TLS, and
preflight-composition gaps. Two exact reviewer examples were already
fail-closed and are retained as counterexamples: inline literal provider
`**kwargs` and route-registrar list/subscript indirection.

- [x] Add minimal round-2 fixtures first and record the pre-fix RED:
  `22 failed, 2 passed` for the static bundle, `18 failed, 12 passed` for the
  runtime/TLS/preflight bundle, `.F` for direct wrapper requests, and one
  missing dedicated-builder failure.
- [x] Reject nested/dynamic final-authority mutation, all reachable chat helper
  state mutation, unproven wrapper URL flow, unresolved or unsafe network
  mappings, sensitive/session aliases, environment-copy aliases, stdlib
  clients, middleware/cookie/SSL aliases, and newly requested tracked backup
  names without introducing non-network false positives.
- [x] Prove hermetically that a localhost leaf is not a usable CA bundle and
  use the canonical public `rootCA.pem` chain for watchdog and TLS validation.
- [x] Reject credential-like query keys before requests/HTTPX transport and
  construct preflight through a dedicated non-LLM role/container.
- [x] Put all runtime, setup, operator, and executable-plan corrections in
  implementation commit
  `d7c9576146ec205f454a8fd7b8db1425a2ce91d0`.
- [x] Focused new static probes: `25 passed in 7.40s`.
- [x] Focused runtime set: `326 passed, 1 warning in 16.72s`.
- [x] Full affected trust matrix:
  `1680 passed, 1 warning in 261.01s`.
- [x] Repository static gate: `release static checks: PASS`; compileall and
  `git diff --check` exited zero.
- [x] Exactly one no-argument full suite:
  `3582 passed, 1 skipped, 1 warning in 522.86s`.
- [x] Prepare the bounded implementation diff and round-2 review/evidence
  package in a truly evidence-only follow-up commit.

Residual hard limits remain: unsupported or dynamic trust-boundary constructs
fail closed; Composio remains disabled pending external provider-side
revocation/rotation; there is no webhook or live-mode path; trading remains
paper-only and separately human-approved; no profit guarantee is made.

## Fix round 3 completion

The round-2 conclusion is historical and superseded. Fresh review found
additional final-authority, root-dispatch-effect, wrapper-ordering,
mapping-provenance, environment, sensitive-write, transport-identity,
artifact, TLS, direct-networking, and preflight-capability gaps.

- [x] Verify all 14 findings against
  `8de8bd96783750500baccbd51f27b7561b505194` before changing code.
- [x] Record minimal pre-fix RED: initial static bundle `18 failed`;
  sensitive-write bundle `3 failed`; runtime/TLS/preflight bundle
  `6 failed, 1 passed`; URL-rebinding `.F`; unknown dispatch effect
  `1 failed`; and missing explicit server-auth EKU `1 failed`.
- [x] Record the round-3 watchdog disposition: provider egress was excess
  capability and was removed, while the role requirement tuple was already
  exactly `("database_url",)`. Round 4 supersedes the latter conclusion:
  generic provider loading still requested every Keychain account before
  projecting that tuple.
- [x] Reject dynamic/nested authority access, root and recursively reachable
  chat effects, unproven wrapper control flow, chained mappings, full
  environment unpacking, sensitive helper/execute/query aliases,
  security-call indirection, direct stdlib transports, computed query
  credentials, conventional SQL dumps, and non-network option false
  positives.
- [x] Replace preflight's mutable `TradingService` with a dedicated one-method
  read-only probe using only broker open-order/position reads and local
  database inspection with no trading-table DML. SQLite connection setup may
  still establish WAL and secure sidecar modes. It constructs no clock, field
  cipher, LLM, notifier, proposal, approval, cancellation, submission, repair,
  or writer-tenure capability.
- [x] Require CA `keyCertSign`, standards chain verification, and an explicit
  leaf `serverAuth` EKU. The standards verifier rejects client-only EKU but
  treats an absent EKU as unconstrained, so the local validator separately
  requires the explicit extension.
- [x] Remove all watchdog provider origins and use the repository-declared
  `uv run python` TLS setup command.
- [x] Final complete focused set: `432 passed in 101.29s`.
- [x] Final 30-file affected trust matrix:
  `1754 passed, 1 warning in 269.73s`.
- [x] Repository static gate: `release static checks: PASS`; compileall,
  `git diff --check`, and setup-script syntax exited zero.
- [x] Run exactly one no-argument full suite:
  `3619 passed, 1 failed, 1 skipped, 1 warning in 527.43s`. The sole failure
  was the pre-existing, untouched SQLite sensitive-downgrade timing test's
  `dependent-insert` case; its exact focused rerun passed `1/1 in 3.51s`.
  No migration code was changed and no second full suite was run.
- [x] Commit implementation/tests/operator/executable docs as
  `b51e8ee0d5ece8bcde3701e4dd4b9adf58089c5c`, then package the bounded
  implementation diff and this evidence in a truly evidence-only follow-up.

Residual release evidence is explicit: the sole full-suite artifact contains
one nondeterministic out-of-scope migration-race failure even though the exact
node passed immediately afterward and the final Task 11 focused, matrix,
static, compile, shell, and diff gates are green. Unsupported dynamic
trust-boundary constructs remain intentionally rejected. Composio remains
disabled pending provider-side rotation; no webhook, live mode, autonomous
execution, profit guarantee, service start, real resource access, or push was
introduced.

## Fix round 4 completion

Round 4 supersedes the round-3 watchdog-secret counterexample and the
round-3 full-suite migration-race caveat. It also records the separately
diagnosed pytest process-fixture hang found during the first round-4 full run.

- [x] Verify all eight bounded round-4 findings against base
  `c12ce6f1c5df914f5f40e48d100bdfa0bf3fdb4c`.
- [x] Record exact pre-implementation RED:
  `22 failed, 2 passed` for runtime/provider/preflight/docs;
  `8 failed` for static alias fixtures; and `2 failed` for the migration
  synchronization command. The first migration failure proved that the old
  hook stopped in revision 0014 instead of revision 0013; the second was
  follow-on Alembic contamination after the first worker aborted, not an
  independent production defect.
- [x] Load Keychain fields by exact runtime role before retrieval. A watchdog
  fake proves it requests and receives only `database_url`; app, daemon, MCP,
  preflight, migration, drill, and other roles retain their exact
  startup-required projections. The final bounded correction below
  supersedes the earlier inference that startup-required fields alone also
  proved every optional branch-visible field.
- [x] Exclude legitimate superseded fill tombstones from reconciliation
  arithmetic while retaining fail-closed quarantine handling. Reject empty or
  duplicate remote broker IDs and non-finite, negative, status-inconsistent,
  quantity-inconsistent, or locally inconsistent fill truth.
- [x] Fail closed on mutation-built collections carrying final authorities or
  security call identities; preserve bound ORM update provenance and nested
  keyword-only model provenance; keep canonically local
  `Client(verify=False)` calls clean.
- [x] Stabilize the sensitive-downgrade race test at the exact revision 0013
  drop/lock point without changing migration production behavior.
- [x] Correct operator wording to “no trading-table DML”; SQLite WAL and
  sidecar setup remain intentional.
- [x] Initial round-4 focused gates: runtime/docs `24 passed`; exact static
  `8 passed`; exact migration race `2 passed`; full static fixtures
  `304 passed`; secret/watchdog/round-3 `187 passed`; sensitive migration
  `17 passed`; preflight `30 passed`.
- [x] Initial 31-file Task 11 affected matrix:
  `2036 passed, 1 warning in 308.48s`.
- [x] Initial repository gate: `release static checks: PASS`; compileall,
  diff, and shell syntax checks passed.
- [x] Preserve the first full-run result:
  `3651 passed, 1 failed, 1 skipped, 1 warning in 537.89s`. The failed
  `verification_opened` crash-fixture process remained alive after
  `join(timeout=10)`, which left pytest waiting during interpreter shutdown.
- [x] Record hang-fix RED before changing the fixture:
  `2 failed`—the context was `fork`, and no bounded terminate/reap helper
  existed.
- [x] Use a fresh `spawn` interpreter for crash fixtures and route every
  process wait in that file through a bounded terminate/kill/reap helper.
  Production backup and migration behavior is unchanged.
- [x] Hang-focused proof: new regression tests `2 passed`; exact prior node
  `1 passed in 0.65s`; complete crash-fixture file `59 passed`; combined
  focused collection `841 passed, 1 warning`.
- [x] Final 32-file Task 11 plus crash-fixture matrix:
  `2095 passed, 1 warning in 368.08s`.
- [x] Final repository gate: `release static checks: PASS`; compileall,
  `git diff --check`, and all five shell syntax checks exited zero.
- [x] Run the one replacement no-argument full suite expressly authorized
  after the hang fix:
  `3654 passed, 1 skipped, 1 warning in 615.81s`; pytest exited normally.
- [x] Commit implementation/tests/operator docs as
  `2093b8049dc85e9d02b73fb424d4a648de8f3a1d`; package bounded diff
  `review-c12ce6f..2093b80.diff` and round-4 evidence separately.

At the stop request, PIDs `53460`, `56072`, and `55923` no longer existed, so
no signal was sent and no unrelated process was touched. The hang was at the
test fixture's parent/child join boundary, not in production backup logic.
Unsupported dynamic trust-boundary constructs remain intentionally rejected.
Composio remains disabled pending provider-side rotation; no webhook, live
mode, autonomous execution, profit guarantee, service start, real resource
access, or push was introduced.

## Final bounded role-visibility correction

This correction supersedes only the round-4 overclaim that the
startup-required-field map was a complete role-capability projection. The
provider-before-retrieval architecture and database-only watchdog result
remain valid.

- [x] Retain the pre-existing fake-Keychain watchdog counterexample: before
  this correction it already passed and proved the provider requested and
  returned only `database_url`. No false RED is claimed.
- [x] Record the real RED command before implementation:
  `9 failed, 6 passed`. It proved missing optional Alpaca paper credentials
  for `safety-drill`, missing `live_trading_confirm` for `app`, `daemon`,
  `preflight`, and `safety-drill`, and missing selected news-provider
  credentials for `mcp`, `paper-drill`, and `safety-drill`.
- [x] Separate immutable canonical role-visible authority from immutable
  startup-required authority. Resolve only the configured LLM provider and
  enabled Telegram/news branches; retain existing key-material validation;
  use no broad all-fields fallback.
- [x] Prove `safety-drill` receives optional Alpaca paper credentials while
  those fields remain optional at ordinary startup. Prove preflight receives
  `live_trading_confirm` and therefore reports `enabled=live_confirmation`
  instead of a false all-disabled result.
- [x] Prove exact fake-Keychain account access and returned values for every
  production role under the default feature set, plus selected-provider-only
  news visibility for `mcp`, `paper-drill`, and `safety-drill`.
- [x] Final focused secret/preflight/safety-drill/watchdog set:
  `353 passed, 1 warning`.
- [x] Final 33-file affected trust matrix:
  `2182 passed, 1 warning in 385.43s`.
- [x] Repository gate reported `release static checks: PASS`; compileall and
  `git diff --check` exited zero.
- [x] Exactly one no-argument full suite for this correction:
  `3660 passed, 1 skipped, 1 warning in 609.41s`; pytest exited normally.
- [x] Commit production/tests as
  `b6cee46bbddc3cae147c1cdaa9b3f970a96d6dbb`; package bounded diff
  `review-35eee00..b6cee46.diff` and corrected evidence separately.

No required-field validation was weakened. No Plan 3 work, app/daemon/MCP
start, ignored runtime database, real Keychain/credential, network,
broker/provider/notifier/integration call, trading action, reconciliation
write, notification, breaker reset, or push occurred.
