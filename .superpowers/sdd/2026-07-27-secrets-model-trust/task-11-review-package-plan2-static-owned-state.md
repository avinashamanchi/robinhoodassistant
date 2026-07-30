# Final Plan 2 Static Owned-State Review Package

## Review boundary

- Base: `504002fb3ac9a7ff592864088b5ac8765f1aee2f`
- Implementation:
  `82181ae1944b95f496638b665fe0fac491c7ed97`
- Bounded diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-504002f..82181ae.diff`
- Diff size: 450 lines / 14,444 bytes
- Implementation scope: `src/trading_assistant/bootstrap.py` and
  `tests/test_plan2_integration_correction.py`
- Evidence-only scope: this package, bounded diff, Task 11 brief/report,
  progress ledger, plan checkbox update, and prior-package supersession
- Excluded: Plan 3, unrelated production paths, push, service startup,
  ignored runtime database, real Keychain/credentials, network,
  broker/provider/notifier/integration calls, trading, reconciliation writes,
  notification, and breaker reset

## Receiving-review disposition

Both findings were confirmed.

An ordinary holder was not one of the prior scanner's explicitly modeled
types, so `_test_broker_owned_children` returned no children even when its
instance dictionary retained `AlpacaBroker`. The same holder passed again
after a delegate was inserted between marked-container issuance and
consumption.

The prior dataclass branch was also not static:
`dataclasses.is_dataclass(value)` called `hasattr` on the value's type. The
hermetic metaclass probe trapped the resulting `__dataclass_fields__` request.
The preceding `isinstance(value, BrokerClient)` also requested dynamic
`__class__`; both operations were removed.

## Canonical bounded invariant

The direct broker identity and callable-capture invariant is unchanged. Its
owned-state traversal now:

- gets raw MRO tuples and class namespaces through `type.__getattribute__`;
- accepts an instance dictionary only behind a native raw `__dict__`
  descriptor and only when `object.__getattribute__` returns exact `dict`;
- walks exact state keys/values plus raw class/MRO non-dunder values;
- statically traverses retained class objects and rejects class-level broker
  delegates;
- rejects declared slotted state without reading slot member descriptors;
- walks exact built-in dict/list/tuple/set/frozenset/deque and exact
  `collections.Counter`;
- rejects subclasses of those built-in containers before iteration;
- retains partial, bound-method, Python function closure/default, cycle,
  depth 24, node 512, and non-root `BrokerClient` checks; and
- runs at marked-container issuance and again at consumption.

The metaclass probe proves no instance `__getattribute__`, custom metaclass
`__dataclass_fields__`, or property access occurs. The custom-list probe
proves its overridden iterator is never called. A cyclic `SpyBroker` with
`threading.Event`, lock, and `Counter` remains accepted.

## TDD evidence

Primary RED:

```text
6 failed, 1 passed, 1 warning in 5.65s
```

Isolated dynamic-dataclass RED:

```text
dataclasses.is_dataclass(value)
AssertionError: dynamic dataclass lookup
1 failed in 0.62s
```

Retained-holder-class RED:

```text
1 failed, 1 warning in 0.84s
```

Focused green:

```text
Exact final selection:
8 passed, 1 warning in 1.21s

Complete focused file:
53 passed, 1 warning in 2.30s

Affected 20-file matrix:
1248 passed, 1 warning in 211.81s
```

## Release verification

```text
Static fixture suite:
304 passed in 92.77s

Repository static gate:
release static checks: PASS

Compileall:
PASS

git diff --check:
PASS

Exactly one no-argument full suite:
3731 passed, 1 skipped, 1 warning in 611.19s
```

Pytest exited normally. The warning is the existing third-party
`websockets.legacy` deprecation warning. The no-argument suite started only
after focused, affected, static, compile, and diff gates were green. No second
full suite ran.

## Explicit residual hard limit

The sole residual is the existing non-sandbox boundary: arbitrary local
method code can read a function global or construct a provider without
retaining that authority in owned object/callable state. Ordinary owned
Python instances, class-owned values, declared slots, and custom subclasses of
supported built-in containers are not part of that residual.

This correction changes no production receipt, role, transport, trading, or
approval boundary. The release remains paper-only and manually approved;
general chat remains read-only; signed queue actions still require separate
human approval; Composio remains disabled pending provider-side rotation; and
no webhook or profit guarantee exists.

Verification used temporary SQLite, ordinary mock subclasses, and inert
unconnected Alpaca-shaped objects only. It did not start services, access real
resources, call a network/broker/provider/notifier, push, submit/cancel an
order, reconcile, notify, or reset a breaker.
