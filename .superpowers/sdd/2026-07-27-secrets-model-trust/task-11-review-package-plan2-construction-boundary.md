# Final Plan 2 Construction-Boundary Correction Review Package

## Review boundary

- Base: `a8308895799ff8393832b0ee4fe2027887325667`
- Implementation:
  `5b28a24d57b3b38b5cbc5bba0f153db7774b98f9`
- Bounded diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-a830889..5b28a24.diff`
- Diff size: 2,047 lines / 69,474 bytes
- Implementation scope: 24 production, test, and Phase 7 specification files
- Evidence-only scope: this package, bounded diff, Task 11 brief/report,
  progress ledger, plan checkbox update, and prior-package supersession
- Excluded: Plan 3, push, service startup, ignored runtime database, real
  Keychain/credentials, network, broker/provider/notifier/integration calls,
  trading, reconciliation writes, notification, and breaker reset

## Receiving-review disposition

1. **Public construction-boundary bypass — confirmed.** The ordinary public
   `build_container` still had ambient/default app semantics and could compose
   app authority without the canonical startup receipt. `create_test_app`
   accepted arbitrary explicitly injected components, including
   production-capable objects. The public builder now requires explicit
   config, secrets, and runtime role; app construction additionally requires
   and consumes the exact one-shot receipt. The test app accepts only the
   opaque container issued by `build_test_container` after fake broker and
   clock checks, and revalidates those capabilities at use.
2. **Missing app startup lifecycle — confirmed.** Automatic app construction
   and the canonical launcher did not wrap pre-build failures in
   `runtime_startup("app", ...)`. Both paths now emit only the stable
   `startup_failed role=app` marker, retain original exception identity, and
   preserve exact guard/control cleanup behavior.
3. **Phase 7 live-promotion wording — confirmed.** The specification still
   described promotion as a manual config change and referred to a live-path
   change. It now states that promotion is evaluation-only, shared changes
   apply to backtesting/paper runtime, and this release rejects live mode.

## TDD evidence

The initial exact six-node selection failed before production changes:

```text
6 failed in 4.79s
```

It proved the ambient public-builder default, explicit unguarded app build,
ordinary-container test-app acceptance, arbitrary explicit service
acceptance, and absent pre- and post-build startup lifecycle markers.
Conservative follow-up probes were also recorded RED one at a time:

```text
production broker capability: 1 failed
nested production clock capability: 1 failed
canonical launcher pre-build marker: 1 failed
unmarked no-agent composition: 1 failed
wrapped production broker capability: 1 failed
post-issuance capability mutation: 1 failed
```

The final focused construction/lifecycle file passed:

```text
34 passed, 1 warning in 1.86s
```

The full static-fixture file passed `304 passed in 92.68s`, and the
safety-drill file passed `81 passed, 1 warning in 19.33s`.

The first affected matrix correctly found one stale legacy test fixture:

```text
1 failed, 1228 passed, 1 warning in 209.83s
```

That test was intended to prove planning-startup exception identity but used
an unmarked `SimpleNamespace`, so the new boundary rejected it first. It was
changed to obtain the same fake stack through the canonical test-container
issuer. Its exact rerun passed `1 passed in 1.01s`; the complete affected
matrix then passed:

```text
1229 passed, 1 warning in 209.66s
```

## Release verification

```text
Repository static gate:
release static checks: PASS

Compileall:
PASS

git diff --check:
PASS

Exactly one no-argument full suite:
3712 passed, 1 skipped, 1 warning in 608.07s
```

Pytest exited normally. The warning is the existing third-party
`websockets.legacy` deprecation warning. The no-argument suite started only
after focused, affected, static, compile, and diff gates were green. No second
full suite ran.

## Residual hard limits

- Public app construction has no ambient/default route. One exact app receipt
  is consumed before broker or trading authority is built and cannot be
  reused.
- The HTTP test factory accepts only an opaque fake-only composition and
  rejects ordinary production containers, provider-capable brokers or clocks,
  wrapped production brokers, nested provider clocks, and post-issuance
  capability replacement.
- Canonical app startup reports a stable value-free lifecycle marker on
  pre-build and post-build failure while retaining exception and cleanup
  semantics.
- Promotion cannot enable live mode. Crypto remains a backtest/paper-runtime
  asset class only.
- Paper mode, separate human approval, execution-time risk checks, kill
  switches, broker truth, read-only general chat, no webhook, and
  Composio-disabled boundaries are unchanged.
- Verification used temporary databases and local fakes only. It did not
  start services, access real resources, make external calls, push, trade,
  reconcile, notify, or reset a breaker.

## Supersession

The deep broker-identity review against base `0062579` found that this
package's fake-only conclusion was incomplete. The validator checked
`service.broker` but not the broker references retained by snapshot,
submission, and reconciliation services, so replacing only the top-level
reference could pass. That claim is superseded by implementation `8e75752`
and `task-11-review-package-plan2-deep-broker-identity.md`. This package's
production receipt, startup lifecycle, test-container marking, clock checks,
and live-rejection conclusions remain valid.
