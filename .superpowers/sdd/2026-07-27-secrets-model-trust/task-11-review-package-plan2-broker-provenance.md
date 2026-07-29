# Final Plan 2 Fake-Broker Provenance Review Package

## Review boundary

- Base: `f7cb48594c7fdc1c03ecb10bc8d38e58f7c94f47`
- Implementation:
  `2eb2f65b0a0326c40663dd594b069e3464bf3ab2`
- Bounded diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-f7cb485..2eb2f65.diff`
- Diff size: 390 lines / 12,796 bytes
- Implementation scope: `src/trading_assistant/bootstrap.py` and
  `tests/test_plan2_integration_correction.py`
- Evidence-only scope: this package, bounded diff, Task 11 brief/report,
  progress ledger, plan checkbox update, and prior-package supersession
- Excluded: Plan 3, unrelated production paths, push, service startup,
  ignored runtime database, real Keychain/credentials, network,
  broker/provider/notifier/integration calls, trading, reconciliation writes,
  notification, and breaker reset

## Receiving-review disposition

**Delegating `MockBroker` subclass — confirmed.**

The prior deep identity helper checked that every app-reachable broker holder
referenced the same object and that the object was an instance of
`MockBroker`. It did not inspect authority retained by that object. A
`ProductionDelegatingMock` could retain an inert `AlpacaBroker` in an instance
attribute, override `submit_order` to forward to it, and pass issuance and
consumption.

Switching to exact `MockBroker` type was rejected after inventory because the
suite deliberately uses ordinary subclasses for call recording, outage
simulation, acceptance uncertainty, race injection, and other hermetic
scenarios.

## Canonical bounded invariant

The original direct identity helper remains canonical for the service graph.
It now first invokes one bounded fake-owned graph check:

- root state begins with `vars(root).values()`;
- broker methods are resolved from instance/class dictionaries without
  descriptor binding;
- supported recursive shapes are exact dictionaries, list/tuple/set/frozenset
  and deque containers, `SimpleNamespace`, ordinary dataclass-owned fields,
  partial function/argument/keyword state, bound-method owner/function state,
  and Python function closure/default/keyword-default state;
- every encountered `BrokerClient` other than the root mock rejects;
- unsupported broker-method callable shapes reject;
- cycles terminate by object identity; depth greater than 24, more than 512
  visited nodes, or an over-budget immediate child set rejects; and
- the check runs during issuance and again at test-app consumption.

The direct service/subservice/container broker references must still be the
same exact object. A normal cyclic `SpyBroker` carrying a
`threading.Event` remains valid because arbitrary framework objects are not
recursively traversed or invoked.

## TDD evidence

The exact new selection failed before production changes:

```text
7 failed, 1 passed, 1 warning in 1.35s
```

The failures proved direct and nested delegates, bound method, partial, and
Python-closure captures, post-issuance insertion, and depth exhaustion were
accepted. The legitimate cyclic `SpyBroker` control already passed.

After implementation:

```text
Exact new selection:
8 passed, 1 warning in 1.40s

Complete focused file:
46 passed, 1 warning in 2.00s

Affected 20-file matrix:
1241 passed, 1 warning in 210.30s
```

## Release verification

```text
Static fixture suite:
304 passed in 92.35s

Repository static gate:
release static checks: PASS

Compileall:
PASS

git diff --check:
PASS

Exactly one no-argument full suite:
3724 passed, 1 skipped, 1 warning in 606.77s
```

Pytest exited normally. The warning is the existing third-party
`websockets.legacy` deprecation warning. The no-argument suite started only
after focused, affected, static, compile, and diff gates were green. No second
full suite ran.

## Explicit residual hard limit

This validator is not a Python sandbox. Arbitrary local method code can read a
module global or construct a provider without retaining that authority in the
modeled object graph. Function globals are intentionally not traversed.
Therefore this package claims only bounded retained-authority validation, not
containment of arbitrary Python. Source-level provider/network behavior
remains governed by the repository static gate and code review.

This correction changes no production receipt, role, transport, trading, or
approval boundary. The release remains paper-only and manually approved;
general chat remains read-only; signed queue actions still require separate
human approval; Composio remains disabled pending provider-side rotation; and
no webhook or profit guarantee exists.

Verification used temporary SQLite, ordinary mock subclasses, and inert
unconnected Alpaca-shaped objects only. It did not start services, access real
resources, call a network/broker/provider/notifier, push, submit/cancel an
order, reconcile, notify, or reset a breaker.
