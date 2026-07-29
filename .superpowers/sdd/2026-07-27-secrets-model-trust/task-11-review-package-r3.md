# Task 11 Fix Round 3 Review Package

## Review boundary

- Base: `8de8bd96783750500baccbd51f27b7561b505194`
- Implementation: `b51e8ee0d5ece8bcde3701e4dd4b9adf58089c5c`
- Bounded diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-8de8bd9..b51e8ee.diff`
- Diff size: 2,479 lines / 92,782 bytes
- Scope: Task 11 production, tests, setup, operator/executable documentation
- Evidence commit scope: this package, bounded diff, Task 11 brief/report,
  plan completion section, and progress ledger only
- Excluded: Plan 3, push, service startup, real Keychain/credentials/runtime
  database, broker/provider/notifier/integration/network calls, trading,
  reconciliation writes, notification, and breaker reset

This package supersedes the round-2 no-remaining-reproducer conclusion. It
does not rewrite the historical round-2 results.

## Finding disposition

1. Final authorities: confirmed; dynamic namespace access, execution, nested
   aliases, and noncanonical rebinding fail closed.
2. Chat root: confirmed; root assignments/deletes and unknown effects join
   the recursively reachable helper checks. A direct mutable root call was
   already rejected and remains a counterexample to that narrower claim.
3. Wrapper dominance: confirmed; only the exact assertion then inline-query-
   validated return transport is supported.
4. Chained mappings: confirmed; every target shares mutation provenance.
5. Environment unpack: confirmed; full mapping unpack and escape fail closed.
6. Sensitive writes: confirmed; keyword execute/query mappings and local
   helper model provenance are covered.
7. Security identities: confirmed; module/attribute/collection indirection is
   rejected when the call identity cannot be proven safe.
8. Root SQL/dumps: confirmed; conventional backup artifacts are blocked while
   benign plaintext-format documentation remains clean.
9. Non-network verify: confirmed false-positive gap; options are interpreted
   only for proven network-shaped calls.
10. TLS: confirmed with nuance; standards verification rejects missing
    `keyCertSign` and client-only EKU, while explicit local enforcement is
    required for an absent EKU because standards treat it as unconstrained.
11. Direct stdlib networking/query keys: confirmed and gated.
12. Preflight capability: confirmed; full mutable `TradingService` replaced
    by the one-method `PreflightReconciliationProbe`.
13. Watchdog: mixed; provider origins removed, but its secret role was already
    exactly database-only and is retained as a hermetic counterexample.
14. TLS runner: confirmed; setup uses `uv run python`.

## RED evidence

```text
Initial static bundle:
18 failed

Sensitive-write bundle:
3 failed

Runtime/TLS/preflight bundle:
6 failed, 1 passed

Final conservative audit:
.F
1 failed
1 failed
```

The runtime-bundle pass was the existing computed-query runtime rejection.
The `.F` pass was the existing mutable-call rejection; URL rebinding was RED.
The final single failures were unknown root effect and absent explicit
server-auth EKU.

## Final verification

```text
Focused release/static/TLS/round-3 set:
432 passed in 101.29s

Affected trust matrix:
1754 passed, 1 warning in 269.73s

Repository static gate:
release static checks: PASS

Compileall:
PASS

git diff --check:
PASS

setup-local-tls.sh syntax:
PASS

Sole no-argument full suite:
3619 passed, 1 failed, 1 skipped, 1 warning in 527.43s

Exact failed migration-race node:
1 passed in 3.51s
```

The warning is the existing third-party `websockets.legacy` deprecation
warning.

The sole full-suite failure is an untouched nondeterministic SQLite downgrade
test. Its synchronization patches the first index drop while downgrading from
head, which can now occur in a later revision before the sensitive-state lock;
the dependent insert can then correctly cause the sensitive downgrade to fail
closed. Round 3 changes neither that test nor migration code. No second full
suite was run.

## TLS technical disposition

- A generated CA with `BasicConstraints(ca=True)` but
  `KeyUsage.key_cert_sign=False` passed the old manual subject/signature checks.
  It is now rejected as `tls_ca_invalid`.
- A generated leaf with only `clientAuth` passed the old manual issuer,
  signature, SAN, validity, and key-match checks. Cryptography's server
  verifier rejects it; the local gate now reports `tls_ca_chain_invalid`.
- A generated leaf with no EKU is accepted by the standards verifier as
  unconstrained. Task 11 requires explicit server authorization, so the local
  gate separately requires `ExtendedKeyUsageOID.SERVER_AUTH`.
- Verification used generated temporary certificates only. No TLS server,
  network connection, or real key material was used.

## Residual hard limits

- Unsupported/dynamic security-boundary Python remains rejected by design.
- The sole full-suite artifact retains the recorded out-of-scope migration
  timing failure; focused/matrix/static/compile/diff/shell evidence is green.
- Composio remains disabled pending provider-side rotation. No webhook,
  Composio origin/caller/toolkit/MCP tool, or live-mode path exists.
- General chat remains read-only. Immutable drafts require explicit signed
  queueing and separate human approval. Paper mode, kill switches,
  broker-truth checks, and no-profit-guarantee limits are unchanged.
- No service, real resource, credential, external transport, trading action,
  notification, breaker reset, or push occurred.
