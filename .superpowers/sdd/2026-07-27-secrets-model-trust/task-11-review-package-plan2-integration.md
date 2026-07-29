# Whole-Plan-2 Integration Correction Review Package

## Review boundary

- Base: `b1e20161ae3e589338c96c1038ab147a298aa6b4`
- Implementation:
  `5d34837ef461b12ad4c5e7f8ea49f5e700cee2c1`
- Bounded diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-b1e2016..5d34837.diff`
- Diff size: 1,878 lines / 69,500 bytes
- Implementation scope: 44 production, test, config, and executable
  documentation files
- Evidence-only scope: this package, bounded diff, Task 11 brief/report,
  progress ledger, plan completion checkboxes, and prior-package supersession
  notice
- Excluded: Plan 3, push, service startup, ignored runtime database, real
  Keychain/credentials, network, broker/provider/notifier/integration calls,
  trading, reconciliation writes, notification, and breaker reset

## Receiving-review disposition

1. **Automatic production app factory bypass — confirmed.**
   `build_default_container` loaded ambient config and app-role secrets and
   called the ordinary container builder without the startup/TLS receipt used
   by `ops.serve`. Public `create_app()` inherited that bypass. The public
   path now requires and consumes the exact one-shot receipt before composing
   broker or trading authority. Reuse fails. Explicit injected fakes moved to
   `create_test_app`.
2. **Optional-news role mismatch — confirmed, with a narrower call-trace
   disposition.** MCP, paper-drill, and safety-drill do not consume the
   quarantine summarizer. The prior role-visibility correction's selected
   LLM credentials for those roles were excess authority and are removed.
   App and daemon are the only supported news roots and pass their exact roles
   to each selected provider. Paper-drill no longer borrows app and uses
   mutually exclusive maintenance tenure while retaining the paper-drill
   outbound/adapter role. Safety-drill retains its exact role in explicit
   fake and optional Alpaca-paper boundaries.
3. **Affirmative live contract — confirmed.** One exact legacy
   config/confirmation pair returned true and planning could persist
   non-paper-only authority. The compatibility predicate now always returns
   false, production bootstrap still rejects live mode, and plan approval
   always persists paper-only authority. Legacy fields remain only for
   explicit rejection and dangerous-switch diagnostics.

## TDD evidence

The pre-implementation command selected the new integration probes plus the
affected round-4 secret, safety-drill, config, factory, and planning cases:

```text
26 failed, 5 passed
```

Production code was untouched at that point. The failures proved:

- ambient app factory reads and construction;
- missing receipt parameters and receipt-reuse enforcement;
- unguarded public explicit injection;
- nine non-news-role LLM constructions;
- paper-drill app-role borrowing;
- nine excess selected-provider secret projections;
- safety-drill role loss;
- the one affirmative legacy live combination; and
- live planning promotion.

The final focused proof was:

```text
78 passed, 1 warning
22 passed
```

The second line is the final complete integration-correction file after adding
the direct public-factory receipt, maintenance-tenure, and test-only
safety-drill-role characterizations.

## Affected and release verification

```text
Explicit 33-file affected matrix:
2022 passed, 1 warning

Repository static gate:
release static checks: PASS

Compileall:
PASS

git diff --check:
PASS

Shell syntax:
PASS

Exactly one no-argument full suite:
3700 passed, 1 skipped, 1 warning in 607.63s
```

Pytest exited normally. The warning is the existing third-party
`websockets.legacy` deprecation warning. The no-argument full suite began only
after focused, affected, static, compile, diff, and shell gates were green. No
second no-argument full suite ran.

## Residual hard limits

- Public automatic app construction requires the exact startup receipt.
  Direct Uvicorn factory invocation is unsupported and fails before ambient
  secret or authority construction.
- `create_test_app` requires explicit injected test components. It does not
  provide an ambient production fallback.
- App and daemon are the only optional-news roots. MCP, paper-drill, and
  safety-drill cannot retrieve or construct LLM capability.
- Paper-drill retains its role through Keychain, outbound, and adapters while
  using maintenance tenure. Safety-drill production container construction is
  rejected; its explicit fake and credentialed paper paths retain the
  safety-drill role.
- Live inputs cannot enable live authority. Crypto remains supported only in
  the paper runtime. Human approval, execution-time risk checks, kill
  switches, and broker-truth checks remain mandatory.
- General chat remains read-only. Immutable drafts require an explicit signed
  queue action and separate human approval.
- Composio remains disabled pending provider-side revocation/rotation. It has
  no origin, route, caller, toolkit, MCP surface, or chat tool. No webhook or
  profit guarantee exists.
- Verification used temporary SQLite databases, fake Keychain/providers,
  fake brokers, and local fixtures only. It did not start services, access
  real resources, make external calls, push, trade, reconcile, notify, or
  reset a breaker.
