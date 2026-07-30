# Plan 2 Static Descriptor Closure

## Review boundary

- Base: `63d966b0ab013b4dc731e8f39ad3aa7f7cadcbfe`
- Implementation: `9dc0c47`
- Scope: `src/trading_assistant/bootstrap.py` and
  `tests/test_plan2_integration_correction.py`
- Excluded: production broker calls, runtime state, credentials, services,
  daemon, orders, notifications, breaker resets, network, and publication

## Received finding

The bounded independent review found that an exact `property` retained in
class-owned state was traversed as a descriptor object, but its getter callable
was not traversed. A getter closure could therefore retain an `AlpacaBroker`
without invoking the property. The same retained-callable shape applied to
exact `staticmethod` and `classmethod` descriptors.

## Correction

The static owned-state scanner now:

- extracts exact `property` getter, setter, and deleter functions with
  `object.__getattribute__` without invoking user descriptor code;
- extracts the exact wrapped function from `staticmethod` and `classmethod`;
- traverses those functions through the existing closure/default scanner; and
- rejects subclasses of those descriptor types fail-closed.

The existing dynamic-property probe remains accepted and proves the property
getter is not invoked.

## TDD evidence

Focused RED:

```text
3 failed, 53 deselected, 1 warning in 3.69s
```

Focused GREEN:

```text
4 passed, 52 deselected, 1 warning in 1.05s
```

Complete correction file:

```text
56 passed, 1 warning in 2.35s
```

The warning is the existing third-party `websockets.legacy` deprecation
warning. Broader deterministic release verification will run after the final
UI source change, so this correction did not duplicate the full suite.

## Residual boundary

The validator is not a Python sandbox. Arbitrary method code that reads a
module global or constructs a provider without retaining that authority in
owned object, class, descriptor, or callable state remains outside this
bounded test-composition invariant.

No external or runtime action occurred.
