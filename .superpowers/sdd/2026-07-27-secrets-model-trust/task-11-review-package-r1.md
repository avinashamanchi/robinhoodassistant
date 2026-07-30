# Task 11 Fix Round 1 Review Package

> Superseded evidence: fix round 2 found additional gaps after this package
> was written. Its “no open code finding” conclusion is not current; use
> `task-11-review-package-r2.md`. The round-1 evidence commit also changed
> executable MarketStack plan instructions, so its evidence-only provenance
> claim was inaccurate and is corrected by the round-2 package.

## Review boundary

- Base: `1f25c102c7886fb8425c88198dbaf1618ddb090a`
- Implementation: `1f63080bc18894d025a20690dfbd6b4e7d6dd946`
- Bounded diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-1f25c10..1f63080.diff`
- Diff size: 5,487 lines / 199,838 bytes
- Scope: Task 11 production, tests, operator docs, and evidence only
- Excluded: Plan 3, service startup, real Keychain/credentials/runtime
  databases, broker/provider/notifier/network calls, integration installation,
  trading, push, reconciliation, notification, and breaker reset

## Finding disposition

1. Canonical chat, sensitive, secret, outbound, and integration authorities now
   require exactly one unconditional definition and reject rebinding, direct or
   bound-method alias mutation, conditional definitions, and dynamic forms.
2. Route analysis unions branch outcomes, catches an unsafe branch hidden by a
   safe alternative, and fails closed on unresolved branch side effects.
3. Direct `app/router.routes` augmentation, element/slice assignment,
   append/extend/insert, and static/dynamic `__getattribute__` registration are
   gated; runtime fixtures prove the registration escape is real.
4. Chat registry mutation is rejected and reachable local dispatch helpers are
   recursively checked; external, lambda, import, `getattr`, and mutable calls
   fail closed.
5. Sensitive-write analysis covers `Query.update`, table-update/value chains,
   helper-returned or unknown aliases, execute aliases, raw/text DML, and bulk
   mappings.
6. Environment analysis covers mapping copies/views/getitem plus module and
   callable aliases. Development providers cannot escape the exact approved
   migration/safety-drill load chain.
7. Module-qualified and aliased Anthropic/OpenAI, requests/httpx/aiohttp,
   urllib, WebSocket, and socket client/call surfaces are rejected outside the
   approved wrappers.
8. Unresolved/mutated network `**kwargs` and post-construction credential query
   mappings fail closed; non-network `verify`/`params` decoys stay clean.
9. Insecure or incomplete cookies, wildcard CORS regexes, disabled certificate
   validation/hostname checking, weakened TLS versions, redirects, and proxy
   trust are gated.
10. The scanned root must resolve exactly to the Git toplevel. Security
    symlinks, outside-root resolution, Git failure, and root symlink loops fail
    closed without file-content access.
11. Finding paths use a conservative ASCII POSIX subset or `unsafe-path`.
    Malformed CLI/root failures emit only
    `INTERNAL_GATE_ERROR internal:1` plus the generic count.
12. Tracked artifact rules cover broad environment, database, WAL/SHM, private
    key/certificate, decrypted backup, log, and raw-export names while public
    certificates remain accepted.
13. Watchdog uses one injected local-liveness transport with no proxy/env trust
    or redirects and exact loopback HTTPS URL/final-URL/certificate checks.
14. Preflight requires explicit `MacOSKeychainSecretProvider` provenance;
    `provider=None` cannot pass. One provider is constructed, loaded, and
    passed through.
15. All five structural validators execute independently on provider
    construction/load failure and block later broker/provider/notifier paths.
16. Runtime `require_origin`/role checks make the manifest authoritative at
    composition and adapter boundaries. MCP has Alpaca-data authority;
    preflight has no LLM origin.
17. Preflight requires canonical TLS certificate/key paths and keeps field
    encryption metadata-only; startup owns the full envelope scan.
18. Operator docs state the independent checks, separate daemon freshness,
    Keychain/TLS/encryption procedures, Composio/webhook disablement, signed
    queue/separate approval, backup limits, paper-only mode, and no profit
    guarantee. Executable MarketStack instructions are removed.

## Verification evidence

```text
Round-1 primary RED: RED_FAILED=72
Strict authority RED: 2 failed, 11 passed
CLI safe-output RED: 1 failed

Static-fixture GREEN: 245 passed in 41.84s
Exact post-full correction: 8 passed, 1 warning in 0.86s
Complete affected GREEN: 228 passed, 1 warning in 6.66s
Trust/affected matrix GREEN: 1575 passed, 1 warning in 132.37s
Repository static gate: release static checks: PASS
Compile gate: PASS
Diff check: PASS
```

Exactly one no-argument full suite was run after the pre-full focused/static
gate:

```text
3535 passed, 7 failed, 1 skipped, 1 warning in 289.19s
```

The seven failures were legacy fake signatures at explicit Task 11
interfaces. They were corrected without relaxing production boundaries. The
full suite was not rerun; the exact and complete affected proofs above are the
final-tree evidence.

## Review result

The bounded diff was reviewed for stable/value-free output, authority
finality, branch/effective-route composition, chat reachability, sensitive
write provenance, environment escape, direct clients, origin/query/option
pinning, TLS/cookie/proxy/redirect controls, Git/artifact hermeticity,
Keychain provenance, independent preflight ordering, adapter-role coverage,
MarketStack remnants, documentation truth, and Task 12/Plan 3 scope.

No open code finding remains at the implementation gate.

## Residuals

- There is no green no-argument full-suite artifact for this round because the
  explicit one-run constraint prohibited a second run after correcting seven
  stale test fakes.
- Unsupported/dynamic trust-boundary constructs intentionally fail closed.
- Provider-side revocation/rotation of the compromised integration credential
  is external; Composio remains fully disabled with no webhook.
- Preflight has no daemon-health row; daemon freshness is separate after
  startup.
- The release remains paper-only and human-approved, with no live mode or
  profit guarantee.
