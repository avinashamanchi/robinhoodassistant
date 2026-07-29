# Final Plan 2 Deep Broker-Identity Review Package

## Review boundary

- Base: `0062579231b7d718afd123eaf6e65766a973cb24`
- Implementation:
  `8e75752cee387384da2edad614fa206a2095da65`
- Bounded diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-0062579..8e75752.diff`
- Diff size: 263 lines / 9,410 bytes
- Implementation scope: one production composition file and two test files
- Evidence-only scope: this package, bounded diff, Task 11 brief/report,
  progress ledger, plan checkbox update, and prior-package supersession
- Excluded: every Plan 3 or unrelated production path, push, service startup,
  ignored runtime database, real Keychain/credentials, network,
  broker/provider/notifier/integration calls, trading, reconciliation writes,
  notification, and breaker reset

## Receiving-review disposition

**Shallow fake-container broker validation — confirmed.**

`TradingService` copies its broker into three directly broker-bearing
subservices at construction:

1. `PortfolioSnapshotService`;
2. `OrderSubmissionService`; and
3. `ReconciliationService`.

The prior validator checked only `service.broker`. Constructing the service
with an `AlpacaBroker` and later replacing only `service.broker` with a
`MockBroker` therefore passed issuance and consumption while the snapshot,
approval/submission, and reconciliation paths retained the production-like
broker.

The typed inventory also confirmed that operations, rule application,
candidate services, and the agent router retain the canonical
`TradingService` rather than separate broker objects. Container aliases expose
the three broker-bearing subservices and therefore belong to the invariant.

## Canonical invariant

One `_require_test_broker_identity` helper now runs:

- against the supplied service before a marked container can be issued;
- against the completed source/container aliases at issuance; and
- again when `create_test_app` consumes the marked container.

It requires:

- a real `TradingService` and typed snapshot, submission, and reconciliation
  services rather than duck-typed substitutes;
- one `MockBroker` object shared by `TradingService` and all three direct
  broker holders;
- exact submission-to-snapshot identity;
- reconciliation/startup broker keys derived from that same mock identity;
- exact container broker/subservice aliases; and
- exact operations-to-service identity.

`MockBroker` subclasses remain valid test doubles, preserving existing
failure/race test ergonomics, but every reference must be the same object.
Production, wrapped, mismatched, or post-issuance replaced references fail
with the existing stable boundary codes.

## TDD evidence

The exact new selection failed before production changes:

```text
4 failed, 1 warning in 1.39s
```

One failure proved the shallow top-level broker replacement; three
parameterized failures proved post-issuance tampering of snapshot,
submission, and reconciliation broker references. The same selection passed:

```text
4 passed, 1 warning in 1.83s
```

The capability/error-order selection passed
`6 passed, 1 warning in 1.12s`, and the complete construction-boundary file
passed `38 passed, 1 warning in 1.63s`.

The first affected matrix exposed three incomplete legacy
`_injected_container` fixtures:

```text
3 failed, 1230 passed, 1 warning in 210.07s
```

Their service graphs were safe, but the fixture omitted the production
container's broker/subservice aliases. The fixture was completed rather than
weakening the invariant. The exact three nodes passed
`3 passed, 1 warning in 1.33s`; the final matrix passed:

```text
1233 passed, 1 warning in 209.69s
```

## Release verification

```text
Static fixture suite:
304 passed in 92.98s

Repository static gate:
release static checks: PASS

Compileall:
PASS

git diff --check:
PASS

Exactly one no-argument full suite:
3716 passed, 1 skipped, 1 warning in 606.85s
```

Pytest exited normally. The warning is the existing third-party
`websockets.legacy` deprecation warning. The no-argument suite started only
after focused, affected, static, compile, and diff gates were green. No second
full suite ran.

## Residual hard limits

- This correction changes only test-app broker identity validation. Production
  receipt, startup, runtime-role, transport, and trading boundaries are
  unchanged.
- The release remains paper-only and manually approved. General chat remains
  read-only; immutable drafts still require an explicit signed queue action
  and separate human approval.
- Composio remains disabled pending provider-side rotation, no webhook exists,
  and no profitability claim is made.
- Verification used temporary SQLite and inert local broker doubles only. It
  did not start services, access real resources, make external calls, push,
  trade, reconcile, notify, or reset a breaker.
