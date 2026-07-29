# Task 11 Fix Round 4 Review Package

## Review boundary

- Base: `c12ce6f1c5df914f5f40e48d100bdfa0bf3fdb4c`
- Implementation:
  `2093b8049dc85e9d02b73fb424d4a648de8f3a1d`
- Bounded diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-c12ce6f..2093b80.diff`
- Diff size: 1,370 lines / 49,857 bytes
- Scope: Task 11 production, tests, operator docs, plus the bounded Task 6
  test-only process-fixture stabilization required for a terminating full
  release proof
- Evidence-only scope: this package, bounded diff, Task 11 brief/report,
  round-3 supersession, plan completion section, and progress ledger
- Excluded: Plan 3, push, app/daemon/MCP startup, ignored runtime database,
  real Keychain/credentials, network, broker/provider/notifier/integration
  calls, trading, reconciliation writes, notification, and breaker reset

This package supersedes the round-3 watchdog-secret counterexample and
full-suite migration caveat without rewriting their historical results.

## Finding disposition

1. **Watchdog secret retrieval — confirmed.** A narrow requirement tuple did
   not prevent generic loading from requesting every provider account.
   Provider-level `load_for_role` now projects before retrieval. The watchdog
   fake proves its account access and returned mapping are exactly
   `database_url`; all other roles retain exact requirements.
2. **Superseded fill tombstones — confirmed.** Preflight excludes them from
   trusted arithmetic. Quarantined and otherwise untrusted states still fail
   closed.
3. **Remote broker truth — confirmed.** IDs must be nonempty and unique.
   Filled quantities must be finite, nonnegative, status-consistent,
   quantity-bounded, and consistent with the local snapshot.
4. **Mutation-built authority collections — confirmed.** Security identities
   inserted through `append`, `insert`, `extend`, or mapping mutation cannot
   be recovered by subscript to bypass final-authority checks.
5. **Bound ORM update aliases — confirmed.** Query method provenance and
   keyword `values` are preserved.
6. **Keyword-only helper propagation — confirmed.** Nested local helper calls
   carry model provenance; unsupported flows fail closed.
7. **Security-call collections and decoys — confirmed.** Mutation-built
   CORS, HTTP-client, cookie, and SSL identities fail closed. A canonically
   local non-network `Client(verify=False)` remains clean.
8. **Migration race synchronization — confirmed test-only defect.** The hook
   now stops at the exact revision 0013 sensitive drop/lock point rather than
   an earlier revision 0014 index drop. Production migration behavior is
   unchanged.

The operator invariant is no trading-table DML. SQLite setup may configure WAL
and secure sidecar modes; that local maintenance is not weakened.

## RED evidence

```text
Runtime/provider/preflight/docs:
22 failed, 2 passed

Exact static aliases:
8 failed

Exact migration synchronization:
2 failed
```

The runtime passes were already-closed quarantined-fill and empty-ID
subclaims. The first migration failure proved the wrong revision-0014
`drop_index` boundary. The second followed the first aborted worker and was
contaminated by Alembic authority state; it is not claimed as an independent
production failure.

The later process-fixture correction also began RED:

```text
Fresh-interpreter context plus bounded reap helper:
2 failed
```

The fixture returned `fork`, and the helper did not exist.

## Full-suite hang disposition

The first round-4 no-argument run reported:

```text
1 failed, 3651 passed, 1 skipped, 1 warning in 537.89s
```

The exact failed node was
`test_recovery_preserves_unrecorded_old_sidecar_name[verification_opened]`.
Its `ForkProcess-107` child, PID `56072`, remained alive after
`join(timeout=10)`, so `exitcode` stayed `None`. The assertion failed without
terminating or reaping the child, and pytest later waited during
multiprocessing shutdown.

The stage hook did not reproduce as a deterministic production hang:

```text
Pre-fix complete parameter group:
10 passed in 3.41s

Pre-fix complete file:
57 passed in 6.28s
```

At the stop request, parent PID `53460`, child PID `56072`, and resource
tracker PID `55923` had already exited. No signal was sent.

The test-only correction uses `spawn` and a common bounded join helper. On
timeout it terminates and joins, escalates to kill only if necessary, joins
again, then fails. All crash-process waits in the file use that helper.
Production backup and migration behavior is unchanged.

## Verification

Initial round-4 focused and release gates:

```text
Runtime/docs: 24 passed
Exact static: 8 passed
Exact migration: 2 passed
Complete static fixtures: 304 passed
Secret/watchdog/round-3: 187 passed
Sensitive migration: 17 passed
Preflight: 30 passed
31-file affected matrix: 2036 passed, 1 warning in 308.48s
Repository static gate: release static checks: PASS
Compileall, diff, and shell syntax: PASS
```

Hang-fix focused proof:

```text
New spawn/reap regressions: 2 passed
Exact prior node: 1 passed in 0.65s
Complete crash-fixture file: 59 passed
Combined focused collection: 841 passed, 1 warning
```

Final prerequisites:

```text
32-file Task 11 plus crash-fixture matrix:
2095 passed, 1 warning in 368.08s

Repository static gate:
release static checks: PASS

Compileall:
PASS

git diff --check:
PASS

Five shell syntax checks:
PASS
```

After those gates were green, the user expressly authorized one replacement
no-argument full run:

```text
uv run pytest
3654 passed, 1 skipped, 1 warning in 615.81s
```

Pytest exited normally. The warning is the existing third-party
`websockets.legacy` deprecation warning.

## Residual hard limits

- Unsupported or dynamic security-boundary Python remains a stable fail-closed
  release violation until it has an explicit model and negative/positive
  fixtures.
- Watchdog receives only its database role field. Preflight performs no
  trading-table DML and cannot approve, submit, cancel, repair, notify, or call
  an LLM.
- Composio remains disabled pending provider-side rotation, with no origin,
  webhook, runtime caller, toolkit, MCP surface, or chat tool.
- General chat remains read-only. Immutable drafts require an explicit signed
  queue action and separate human approval.
- Paper mode, kill switches, broker-truth checks, and the no-profit-guarantee
  boundary are unchanged. No live path was added.
- Verification used temporary Git roots, fakes, temporary SQLite, and
  hermetic child processes only. It used no real resource or external call.
