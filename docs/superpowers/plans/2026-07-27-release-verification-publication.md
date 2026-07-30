# Release Verification and Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:verification-before-completion throughout, then superpowers:requesting-code-review before publication, and superpowers:finishing-a-development-branch for handoff. Execute tasks with superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the completed loopback security console against deterministic tests, adversarial state-integrity cases, local runtime checks, browser evidence, and fresh read-only Alpaca paper truth; publish only the verified branch and report every remaining blocker honestly.

**Architecture:** One offline release verifier aggregates immutable command evidence without touching a provider. CI runs the same deterministic gates with injected test secrets. Local operational setup then migrates Keychain/TLS/encryption in a safe order. A final read-only preflight may update local reconciliation/breaker evidence but may never reset, approve, cancel, submit, start the daemon, or run a credentialed trading drill.

**Tech Stack:** pytest, coverage.py, Alembic, SQLite WAL, Git/GitHub, GitHub Actions, local HTTPS browser inspection, Alpaca paper read APIs

## Global Constraints

- The governing specification is `docs/superpowers/specs/2026-07-27-loopback-kraken-security-console-design.md`.
- Execute only after Plans 1–3 pass their completion checkpoints.
- Keep Alpaca paper hard-lock. There is no live Robinhood or live Alpaca release in this program.
- A tripped `broker_drift`, loss, reconciliation, quote-integrity, stale-data, or operator breaker remains tripped until the operator separately investigates and explicitly resets that exact scope. This plan never resets it.
- Do not start the daemon, submit/cancel/replace an order, approve a proposal, arm a rule, invoke an LLM, send a notification, call Composio, or run the credentialed paper safety drill.
- A blocked fresh preflight means operational release is blocked. Software tests may still pass; never collapse those claims.
- Do not merge to `main`, force-push, delete branches, modify repository visibility, or publish a non-draft release.
- Do not print or commit credentials, Keychain values, certificate private keys, database contents, raw external text, session cookies, or decrypted narratives.
- Every claimed pass must be backed by output generated in this worktree after the final code change.

---

## Cross-plan specification coverage

| Approved design section | Implementation owner |
| --- | --- |
| 4–5 deployment/architecture | Plan 1 Tasks 2–9; Plan 2 Tasks 1–4 |
| 6 route/resource policy | Plan 1 Tasks 1–3 and 5–10 |
| 7 paid-provider budget | Plan 1 Tasks 1, 2, 4, 5, 8, 9 |
| 8.1 secret lifecycle | Plan 2 Tasks 1, 2, 11 |
| 8.2 database encryption | Plan 2 Tasks 5, 6, 11 |
| 9 loopback HTTPS/perimeter | Plan 2 Tasks 3, 4, 11 |
| 10 webhook/DNS posture | Plan 2 Tasks 1, 4, 11 |
| 11 prompt-injection boundary | Plan 2 Tasks 7–9, 11 |
| 12 visual system | Plan 3 Tasks 1–7 |
| 13 information architecture/interaction | Plan 3 Tasks 3–7 |
| 14 failure behavior | Plan 1 Tasks 3, 4, 6, 9; Plan 2 Tasks 2–10; Plan 3 Task 4 |
| 15 verification contract | Plan 4 Tasks 1–8 |
| 16 implementation sequence | Plans 1, 2, 3, then 4 in that order |
| 17 completion criteria | Plan 4 completion checkpoint |

No design requirement is deferred to an unnamed workstream.

---

## File map

**Create**

- `scripts/verify_loopback_release.py` — deterministic offline release orchestrator.
- `tests/test_release_verifier.py` — command ordering, no-network, redaction, and failure tests.
- `docs/release/2026-07-27-threat-matrix.md` — attack/control/test mapping.
- `docs/release/2026-07-27-verification.md` — generated evidence summary with commit/timestamps.
- `docs/release/2026-07-27-operational-status.md` — fresh local/Alpaca state, including blockers.
- `.github/pull_request_template.md` — safety evidence checklist.

**Modify**

- `.github/workflows/ci.yml` — new migrations, explicit CI secret-provider mode, security suites, and artifact-free checks.
- `scripts/check_release_safety.py` — complete tracked/history/private-artifact and route/tool invariants.
- `tests/test_release_static.py` — final negative fixtures.
- `tests/test_release_gate_branches.py` — fail-closed release-state combinations.
- `tests/test_safety_drill.py` — preserve offline safety drill and explicitly exclude it from this release verifier.
- `README.md` — verified setup, screenshots, and status boundary.
- `docs/RUNBOOK.md` — Keychain/TLS/encryption/start/stop/recovery and blocked-preflight procedure.

---

### Task 1: Build an offline verifier that cannot touch external systems

**Files:**

- Create: `scripts/verify_loopback_release.py`
- Create: `tests/test_release_verifier.py`
- Modify: `scripts/check_release_safety.py`
- Modify: `tests/test_release_static.py`

**Interfaces:**

- Produces `VerificationStep`, `VerificationResult`, and `ReleaseVerifier`.
- Writes redacted JSON to `.local/verification/release-results.json`.
- Runs no integration-marked or credentialed command.

- [ ] **Step 1: Write no-network and failure-propagation tests**

```python
def test_release_verifier_has_only_offline_commands():
    commands = ReleaseVerifier.default_commands()
    flat = "\n".join(" ".join(command.argv) for command in commands)
    assert "alpaca_paper_integration" not in flat
    assert "safety_drill --armed" not in flat
    assert "daemon.main" not in flat
    assert "ops.secrets" not in flat
    assert all(command.network is False for command in commands)


def test_failed_step_stops_success_claim(tmp_path):
    runner = ScriptedRunner([completed(0), completed(1)])
    result = ReleaseVerifier(runner=runner, output_dir=tmp_path).run()
    assert result.passed is False
    assert result.steps[-1].status == "failed"
    assert not (tmp_path / "PASS").exists()
```

Test secret-like stdout/stderr redaction, timeout, signal termination, dirty
tree, migration-head mismatch, skipped expected suite, and output write mode
`0600`.

- [ ] **Step 2: Run and verify missing verifier**

```bash
uv run pytest tests/test_release_verifier.py tests/test_release_static.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement exact offline command order**

```python
DEFAULT_COMMANDS = (
    Command("compile", ("uv", "run", "python", "-m", "compileall", "-q", "src")),
    Command("migration-tests", ("uv", "run", "pytest", "tests/test_migrations.py", "tests/test_startup_schema.py", "-v")),
    Command("security-tests", ("uv", "run", "pytest", "tests/test_secret_provider.py", "tests/test_transport_boundary.py", "tests/test_outbound_policy.py", "tests/test_sensitive_crypto.py", "tests/test_sensitive_migration.py", "tests/test_untrusted_content.py", "tests/test_candidate_boundary.py", "tests/test_security_posture.py", "-v")),
    Command("safety-tests", ("uv", "run", "pytest", "tests/test_risk_engine.py", "tests/test_killswitch.py", "tests/test_breakers.py", "tests/test_submission_barrier.py", "tests/test_order_submission.py", "tests/stress/test_stress_scenarios.py", "-v")),
    Command("frontend-tests", ("uv", "run", "pytest", "tests/test_frontend_ui.py", "tests/test_security.py", "tests/test_security_headers.py", "-v")),
    Command("full-tests", ("uv", "run", "pytest")),
    Command("branch-coverage", ("uv", "run", "pytest", "--cov=trading_assistant.risk", "--cov=trading_assistant.orders", "--cov=trading_assistant.rules", "--cov=trading_assistant.app.auth", "--cov=trading_assistant.security", "--cov-branch", "--cov-fail-under=90")),
    Command("static-gate", ("uv", "run", "python", "scripts/check_release_safety.py")),
)
```

Each command has a finite timeout. The runner starts with a sanitized
environment containing only path/locale/test variables, sets
`TRADING_ASSISTANT_OFFLINE_VERIFY=1`, and rejects any command marked
`network=True`.

- [ ] **Step 4: Expand static gate to inspect tracked history safely**

Current-tree rules remain authoritative. Add:

- tracked `.env*`, `*.db`, `*.sqlite*`, `*.pem`, `*.key`, `*.p12`,
  `.local/**`, and decrypted backup rejection;
- prohibited credential-prefix fingerprints without printing matched text;
- FastAPI route inventory/no-webhook;
- no environment-secret production root;
- no proxy trust/CORS/public bind;
- no mutable chat tool;
- no direct plaintext-sensitive assignment;
- no raw provider backend outside the budget factory;
- no remote frontend asset/unsafe DOM API;
- no live-mode or unofficial Robinhood dependency.

Use `git log --all --format=%H` plus `git grep`-style blob inspection through
Python APIs or `git show`, reporting only commit/path/rule. Do not echo matched
lines.

- [ ] **Step 5: Run verifier tests and static gate**

```bash
uv run pytest tests/test_release_verifier.py tests/test_release_static.py -v
uv run python scripts/check_release_safety.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/verify_loopback_release.py tests/test_release_verifier.py scripts/check_release_safety.py tests/test_release_static.py
git commit -m "test(release): add offline security verifier"
```

---

### Task 2: Encode the threat matrix and fail-closed release states

**Files:**

- Create: `docs/release/2026-07-27-threat-matrix.md`
- Modify: `tests/test_release_gate_branches.py`
- Modify: `tests/test_safety_drill.py`
- Modify: `tests/test_release_static.py`

**Interfaces:**

- Maps each threat to prevention, detection, recovery, owner, and exact tests.
- Defines software verification separately from operational readiness.

- [ ] **Step 1: Write failing release-state tests**

Parametrize:

```text
tests pass + preflight ready        -> software verified, operational ready
tests pass + breaker tripped        -> software verified, operational blocked
tests pass + broker truth unknown   -> software verified, operational blocked
tests pass + daemon stale           -> software verified, operational blocked
tests pass + encryption mixed       -> software blocked, operational blocked
tests fail + preflight ready        -> software blocked, operational blocked
```

No state combination may produce `live`, `profitable`, `autonomous`, or
`daemon running` without direct evidence.

- [ ] **Step 2: Run and verify current gate vocabulary is insufficient**

```bash
uv run pytest tests/test_release_gate_branches.py tests/test_safety_drill.py -v
```

Expected: FAIL until the release status model distinguishes both dimensions.

- [ ] **Step 3: Write the complete threat matrix**

Rows:

1. paid-call/resource exhaustion;
2. credential exposure/rotation;
3. public/host/origin/proxy endpoint exposure;
4. plaintext sensitive persistence/backup;
5. direct and indirect prompt injection;
6. webhook replay/DNS/redirect abuse;
7. order duplicate/acceptance-unknown/partial fill;
8. stale quote/reconciliation/daemon state;
9. breaker scope/reset race;
10. backtest lookahead/holdout misuse;
11. UI stale-state or paper/live deception;
12. dependency/build/publication compromise.

Each row includes exact implementing modules and test names. Recovery steps
must not weaken a breaker or resubmit an unknown order.

- [ ] **Step 4: Preserve the credentialed drill as separately authorized**

Static and runtime tests assert `verify_loopback_release.py` cannot import or
invoke the armed paper drill. README/RUNBOOK label that drill as a separate,
explicitly authorized activity capable of broker writes; it is not required or
run by this release plan.

- [ ] **Step 5: Run gate and safety tests**

```bash
uv run pytest tests/test_release_gate_branches.py tests/test_safety_drill.py tests/test_release_static.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add docs/release/2026-07-27-threat-matrix.md tests/test_release_gate_branches.py tests/test_safety_drill.py tests/test_release_static.py
git commit -m "docs(security): map threats to release evidence"
```

---

### Task 3: Make CI exercise the same deterministic security program

**Files:**

- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_release_static.py`
- Modify: `docs/RUNBOOK.md`

**Interfaces:**

- Linux CI uses only explicit test/development secret-provider mode.
- CI never expects macOS Keychain or a trusted local certificate.
- CI runs new migrations and security tests without network credentials.

- [ ] **Step 1: Add workflow contract tests**

Parse YAML and assert:

- checkout `fetch-depth: 0`;
- `uv sync --all-extras --dev`;
- deterministic full suite;
- security package included in branch coverage;
- static release gate;
- migration to head;
- offline mock safety drill only;
- no armed drill, daemon, Alpaca integration, Keychain mutation, TLS trust
  mutation, or provider key;
- gitleaks current/history scan.

- [ ] **Step 2: Run and verify old workflow gaps**

```bash
uv run pytest tests/test_release_static.py -k workflow -v
```

Expected: FAIL until CI includes the new gates and explicit secret mode.

- [ ] **Step 3: Update migration/drill commands**

Use explicit test-only flags:

```bash
uv run python -m trading_assistant.db.migrate \
  --development-environment-secrets upgrade
uv run python -m trading_assistant.ops.safety_drill \
  --development-environment-secrets \
  --database-copy "$ci_safety_dir/drill.sqlite3" \
  --mock
```

The role loader accepts this flag only for these offline commands. Normal app,
daemon, MCP, watchdog, and preflight roles still require Keychain.

- [ ] **Step 4: Add focused security and coverage steps**

Run the same `security-tests`, `frontend-tests`, full suite, branch coverage,
and static gate as the offline verifier. Set no real provider credential.

Keep gitleaks with full history. Pin the reviewed tags to these immutable
commits and retain the tag in comments:

```yaml
- uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
- uses: astral-sh/setup-uv@0c5e2b8115b80b4c7c5ddf6ffdd634974642d182 # v5.4.1
- uses: gitleaks/gitleaks-action@ff98106e4c7b2bc287b24eaf42907196329070c7 # v2.3.9
```

- [ ] **Step 5: Run local workflow/static tests**

```bash
uv run pytest tests/test_release_static.py tests/test_release_verifier.py -v
uv run python scripts/check_release_safety.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ci.yml tests/test_release_static.py docs/RUNBOOK.md
git commit -m "ci(security): run deterministic trust boundaries"
```

---

### Task 4: Rehearse the irreversible local migration on copies

**Files:**

- Modify: `tests/test_sensitive_migration.py`
- Modify: `docs/RUNBOOK.md`
- Create local untracked artifacts only under `.local/verification/migration-rehearsal/`

**Interfaces:**

- Uses a database copy and fake Keychain provider first.
- Produces a redacted rehearsal manifest.
- Makes no change to the user's normal database.

- [ ] **Step 1: Capture pre-rehearsal facts without secret values**

Record:

- active worktree/commit;
- source DB path hash and byte size;
- Alembic current/head;
- WAL checkpoint status;
- encryption state;
- row counts per sensitive table;
- app/daemon PID validation.

If the app or daemon owns the copied source during snapshot, use SQLite online
backup. Never copy a live SQLite file with plain filesystem `cp`.

- [ ] **Step 2: Create an isolated database copy**

Use the existing safe backup/copy service into:

```text
.local/verification/migration-rehearsal/source-copy.sqlite3
```

with mode `0600`. Point an injected test `RuntimeSecrets.database_url` to this
copy and use generated in-memory test keys.

- [ ] **Step 3: Run schema and sensitive migration on the copy**

```bash
uv run python -m trading_assistant.db.migrate \
  --development-environment-secrets \
  --database-url sqlite:///<absolute-copy-path> upgrade
uv run python -m trading_assistant.ops.encrypt_sensitive \
  --development-environment-secrets \
  --database-url sqlite:///<absolute-copy-path> migrate
uv run python -m trading_assistant.ops.encrypt_sensitive \
  --development-environment-secrets \
  --database-url sqlite:///<absolute-copy-path> verify
```

The generated test keys are passed through a temporary mode-`0600` environment
file used only by the explicit development provider, then removed after the
process exits. The verifier output contains no values.

- [ ] **Step 4: Compare safety truth before/after**

On the copy, assert:

- domain row counts unchanged except new security tables/state;
- order statuses, fills, positions, breaker scopes/generations, rule states,
  and reconciliation cursors unchanged;
- every registered sensitive field is an authenticated envelope;
- no proposal approved, order submitted/canceled, breaker reset, or daemon
  heartbeat written;
- encrypted backup verifies.

- [ ] **Step 5: Run migration tests again**

```bash
uv run pytest tests/test_sensitive_migration.py tests/test_migrations.py tests/test_startup_schema.py tests/test_breakers.py tests/test_order_state_machine.py -v
```

Expected: PASS.

- [ ] **Step 6: Document exact normal-database procedure**

RUNBOOK order:

1. validate and stop only owned app/daemon PIDs;
2. Keychain audit;
3. local TLS inspect;
4. SQLite online backup;
5. Alembic upgrade;
6. encrypted sensitive migration;
7. verify envelopes;
8. read-only preflight;
9. start HTTPS app only if ready;
10. daemon remains a separate decision.

- [ ] **Step 7: Commit tests/runbook only**

```bash
git add tests/test_sensitive_migration.py docs/RUNBOOK.md
git commit -m "test(migration): rehearse encrypted local upgrade"
```

---

### Task 5: Run the final deterministic release gates and record evidence

**Files:**

- Create: `docs/release/2026-07-27-verification.md`
- Modify: `README.md`
- Use untracked `.local/verification/release-results.json`

**Interfaces:**

- Evidence identifies exact commit, UTC timestamps, command, status, duration,
  test totals, and documented skips.
- Evidence contains no credentials or machine-specific private paths.

- [ ] **Step 1: Require a clean candidate commit**

```bash
git status --short
git diff --check
git log -1 --format='%H %cI %s'
```

Expected: clean worktree before running evidence commands.

- [ ] **Step 2: Run the offline verifier**

```bash
uv run python scripts/verify_loopback_release.py
```

Expected: every step PASS. If any step fails, stop publication, fix through a
new failing test, rerun focused tests, commit, then rerun the entire verifier
from the beginning.

- [ ] **Step 3: Run dependency and lock checks**

```bash
uv lock --check
uv pip check
uv run --with pip-audit pip-audit
```

Record vulnerability IDs and disposition without suppressing findings. A known
exploitable runtime vulnerability blocks release. Network inability to refresh
the advisory database is recorded as `unknown` and blocks publication until a
successful audit is rerun.

- [ ] **Step 4: Generate the verification document**

`docs/release/2026-07-27-verification.md` contains:

- commit SHA;
- environment versions;
- migration head;
- command/result table;
- test pass/skip/fail totals;
- branch coverage;
- static-gate result;
- dependency-audit result;
- UI viewport/browser result from Plan 3;
- explicit `No broker writes, model calls, notifications, daemon start, or
  breaker reset occurred`;
- software status only, not operational status.

Generate values from `.local/verification/release-results.json`; do not type
unverified pass counts manually.

- [ ] **Step 5: Commit the evidence**

```bash
git add docs/release/2026-07-27-verification.md README.md
git commit -m "docs(release): record deterministic verification"
```

- [ ] **Step 6: Rerun fast post-evidence checks**

```bash
git diff --check HEAD^
uv run pytest tests/test_release_verifier.py tests/test_release_static.py tests/test_frontend_ui.py -v
uv run python scripts/check_release_safety.py
```

Expected: PASS.

---

### Task 6: Migrate the normal local runtime only after deterministic proof

**Files:**

- Create: `docs/release/2026-07-27-operational-status.md`
- Modify no source file in this task.

**Interfaces:**

- Operates only through reviewed CLI commands from Plan 2.
- Preserves a verified encrypted backup.
- Stops on missing Keychain/TLS/migration proof.

- [ ] **Step 1: Audit, but never print, secret state**

```bash
uv run python -m trading_assistant.ops.secrets audit
```

If required fields are absent, run the interactive `migrate-env` command. Do
not inspect `.env` with `cat`, `grep`, shell expansion, or logs. Confirm only
field-level `stored`/`verified` results.

Composio stays absent/disabled. Record that provider-side revocation of the
previous credential is unverified unless the operator supplies an authenticated
provider receipt; do not claim rotation from repository evidence.

- [ ] **Step 2: Establish local TLS**

```bash
./scripts/setup-local-tls.sh
uv run python -m trading_assistant.ops.tls inspect
```

If trust-store mutation needs interactive macOS approval, pause only for that
OS prompt. Certificate/key files remain under ignored `.local/tls`; key mode is
`0600`.

- [ ] **Step 3: Stop only validated owned processes**

```bash
./scripts/stop.sh
```

Confirm no daemon PID owned by this repository is running. Do not use broad
`pkill`.

- [ ] **Step 4: Back up, upgrade, encrypt, and verify**

```bash
uv run python -m trading_assistant.ops.backup create --encrypted
uv run python -m trading_assistant.db.migrate upgrade
uv run python -m trading_assistant.ops.encrypt_sensitive migrate
uv run python -m trading_assistant.ops.encrypt_sensitive verify
```

Each command must finish successfully before the next. Preserve backup path
hash and verification receipt, not private path/key values.

- [ ] **Step 5: Run local preflight without starting**

```bash
uv run python -m trading_assistant.preflight
```

This preflight may use Alpaca reads and persist reconciliation/breaker evidence.
It does not call an LLM, notify, submit, cancel, approve, reset, or start a
process.

- [ ] **Step 6: Write operational status from observed evidence**

Record each check as:

```text
status | observed_at UTC | detail_code
```

Include Keychain, TLS, schema, encryption, paper target, account read, open
orders, positions, reconciliation, quote integrity, breakers, daemon, and
provider-budget state.

If preflight is `NOT READY`, title the document `Operational release blocked`,
list exact stable detail codes, leave all breakers intact, and do not start the
normal app or daemon.

- [ ] **Step 7: Commit only redacted status**

```bash
git add docs/release/2026-07-27-operational-status.md
git commit -m "docs(operations): record fresh paper preflight"
```

---

### Task 7: Start and inspect the HTTPS app when the structural guard is ready

**Files:**

- Modify: `docs/release/2026-07-27-operational-status.md`
- Modify: `README.md`

**Interfaces:**

- Starts only `ops.serve` through `scripts/start.sh`.
- Does not start daemon or create trading activity.

- [ ] **Step 1: Branch on structural and operational status separately**

If the local structural startup guard passes:

```bash
./scripts/start.sh
```

The app may start when operational preflight remains blocked by broker drift,
reconciliation, daemon, quote, or breaker state; it must display that state and
the submission barrier must remain closed. If Keychain, TLS, schema, encryption,
paper target, bind, host, or origin structural checks fail, skip normal-runtime
start and retain the blocked operational document. Use only the isolated mock
browser evidence from Plan 3.

- [ ] **Step 2: Verify process and local HTTPS identity**

```bash
curl --fail --silent --show-error \
  --cacert .local/tls/rootCA.pem \
  https://localhost:8020/health/live
```

Validate PID ownership, exact bind sockets (`127.0.0.1`/`::1` only), certificate
SAN, and no HTTP listener. Do not add `-k`.

- [ ] **Step 3: Perform authenticated read-only browser inspection**

Inspect Operations, Plans, and Backtests through
`https://localhost:8020`. Do not click approval, reject, cancellation, breaker
reset, panic, candidate queue, paid analysis, backtest start, or daemon start.

Verify:

- paper label;
- actual breaker/daemon/reconciliation state;
- security posture;
- budget state;
- positions/account/log reads;
- no browser console error or failed local request;
- stale values clear when a read is intentionally blocked in an isolated test
  session, not against the normal broker session.

- [ ] **Step 4: Leave only the validated HTTPS app running**

The operator already requested a running loopback console. Leave the validated
HTTPS app running after inspection and record its validated PID. Do not start
the daemon. If process identity, TLS, or bind validation fails, stop only that
owned PID and record startup failure.

- [ ] **Step 5: Update status/evidence**

Record URL, process identity result, TLS result, browser result, and whether the
app was stopped. Never write cookie/token values.

- [ ] **Step 6: Commit**

```bash
git add docs/release/2026-07-27-operational-status.md README.md
git commit -m "docs(operations): verify loopback HTTPS console"
```

If startup was skipped, use commit message:

```bash
git commit -m "docs(operations): retain blocked runtime status"
```

---

### Task 8: Review, push the branch, and update the draft pull request

**Files:**

- Create or modify: `.github/pull_request_template.md`
- Modify: no product source unless review finds an issue.

**Interfaces:**

- Remote must remain `https://github.com/avinashamanchi/robinhoodassistant.git`.
- Publishes `codex/safety-foundation`.
- Updates draft PR `#1`; does not merge.

- [ ] **Step 1: Request code review against the approved spec**

Review scopes:

- policy/budget atomicity and zero-call denials;
- secret/transport/encryption trust boundaries;
- model-tool/candidate separation;
- broker submission and breaker non-regression;
- UI stale-state and paper/simulation honesty;
- migration/recovery;
- CI/release evidence.

Every actionable finding gets a failing test and separate fix commit. Rerun the
complete offline verifier after the final fix.

- [ ] **Step 2: Verify branch/remote/status**

```bash
git status --short
git branch --show-current
git remote get-url --push origin
git log --oneline --decorate -20
```

Required:

- clean worktree;
- branch `codex/safety-foundation`;
- exact approved remote;
- no merge commit from `main` created by this task.

- [ ] **Step 3: Push without force**

```bash
git push origin codex/safety-foundation
```

Then verify:

```bash
test "$(git rev-parse HEAD)" = \
  "$(git ls-remote origin refs/heads/codex/safety-foundation | cut -f1)"
```

- [ ] **Step 4: Update draft PR**

PR body must link:

- approved spec;
- four implementation plans;
- threat matrix;
- deterministic verification;
- operational status;
- screenshots;
- migrations and rollback/recovery;
- known blockers.

Checklist states:

- paper-only;
- daemon not started unless separately evidenced;
- no broker writes in this release verification;
- no profitability guarantee;
- Composio disabled and old credential rotation unverified without provider
  receipt;
- draft/no merge.

- [ ] **Step 5: Inspect CI to terminal state**

Wait for all required checks. If a check fails, inspect logs, reproduce locally,
add a failing test/fix, rerun offline verifier, commit, push normally, and wait
again. Do not dismiss, rerun blindly, or weaken a gate.

- [ ] **Step 6: Record the publication checkpoint**

Report:

- branch and remote SHA;
- draft PR URL;
- CI terminal status;
- software verification status;
- operational status and exact blockers;
- app running/stopped state;
- daemon state;
- broker-write count during release verification (`0`);
- remaining manual provider credential rotation.

Do not say “fully working,” “live,” “making money,” or “ready to trade” unless
the exact narrower claim is directly supported—and this program never claims
profitability.

---

## Plan 4 completion checkpoint

Required evidence:

```bash
git status --short
git rev-parse HEAD
git ls-remote origin refs/heads/codex/safety-foundation
uv run python scripts/verify_loopback_release.py
uv run python scripts/check_release_safety.py
```

Completion means:

- clean local/remote branch equality;
- draft PR updated and required CI terminal;
- deterministic software verification passed after the last source change;
- operational status is fresh and either ready or explicitly blocked;
- no breaker reset, daemon start, model/provider call, notification, Composio
  call, credentialed drill, approval, cancellation, or order submission occurred
  in release verification;
- the old Composio credential remains treated as compromised and disabled until
  independently rotated;
- no claim of live trading, autonomous trading, profitability, or million-dollar
  outcomes appears anywhere.
