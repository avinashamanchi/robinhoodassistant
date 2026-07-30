# Task 11 Fix Round 2 Review Package

## Review boundary

- Base: `7cc5c91d42b0349a7235ddf65cc21626250a0ccc`
- Implementation: `d7c9576146ec205f454a8fd7b8db1425a2ce91d0`
- Bounded diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-7cc5c91..d7c9576.diff`
- Diff size: 2,184 lines / 86,981 bytes
- Scope: Task 11 production, tests, setup, operator/executable documentation,
  and SDD evidence
- Excluded: Plan 3, push, service startup, real Keychain/credentials/runtime
  databases, broker/provider/notifier/integration/network calls, trading, and
  breaker reset

This package supersedes the round-1 “no open code finding” conclusion. It also
corrects round-1 provenance: evidence commit
`7cc5c91d42b0349a7235ddf65cc21626250a0ccc` changed executable MarketStack plan
instructions and therefore was not strictly evidence-only. Every round-2
runtime, test, setup, operator, and executable-plan change is in the
implementation commit above. The round-2 follow-up commit contains evidence
files only.

## Finding disposition

1. **Sensitive authority:** confirmed. Nested aliases and dynamic
   `globals()`/`vars()` rebinding now fail closed under the single canonical
   authority rule.
2. **Reachable chat effects:** confirmed. Assign, AnnAssign, AugAssign, Delete,
   and NamedExpr effects in reachable local helpers are rejected as mutable or
   unproven.
3. **Wrapper dynamic URLs:** confirmed. Dynamic requests inside the wrapper are
   accepted only for the narrowly modeled `NoRedirectSession.request` flow
   where the same URL variable reaches `OutboundPolicy.assert_url`; direct
   module or instance-client requests fail closed.
4. **Network `**kwargs`:** mixed. The exact inline literal Anthropic example
   already failed closed as `OUTBOUND_CLIENT_UNAPPROVED` and remains a
   counterexample. Named/shared mapping contents, mutations, and unpacked
   Uvicorn options were real gaps and are now resolved or rejected.
5. **Sensitive aliases:** confirmed. Existing object aliases, unknown object
   aliases, mutation-call aliases, and `session.execute` aliases are tracked.
6. **Environment aliases:** confirmed. Module-qualified provider aliases and
   `dict(os.environ)` through callable/mapping aliases are rejected.
7. **Route registrar indirection:** the exact list/subscript fixture was
   already `ROUTE_REGISTRATION_UNPROVEN` and remains a counterexample. No
   permissive pseudo-interpretation was added.
8. **Stdlib clients:** confirmed. `http.client` connection/request surfaces
   and unverified SSL-context factories are gated.
9. **Shared query mappings:** confirmed. Aliases share one mapping component,
   so a credential-key mutation through either alias reaches the effective
   network `params`.
10. **Transport call shapes:** confirmed. Actual middleware registration,
    aliased `set_cookie`, aliased SSL factories, and unpacked Uvicorn proxy
    settings are recognized.
11. **Tracked production SQL:** confirmed. Backup-directory SQL and production
    SQL payload names fail the tracked-artifact gate.
12. **Stale evidence:** confirmed and superseded in the brief, report, round-1
    package banner, plan, and progress ledger.
13. **Non-network verify decoy:** confirmed false-positive gap. `verify=False`
    and `params` are interpreted only at proven network call sites.
14. **Plaintext documentation decoy:** confirmed false-positive gap.
    `docs/plaintext-format.md` remains clean while decrypted/plaintext backup
    payloads remain blocked.
15. **mkcert leaf trust:** reviewer correct. A hermetic `ssl.MemoryBIO`
    handshake rejected the generated localhost leaf as a CA file with
    `SSLCertVerificationError`; the generated root CA verified the same leaf.
    Watchdog and preflight now use and validate canonical public
    `.local/tls/rootCA.pem`.
16. **Preflight capability role:** confirmed. The dedicated `preflight`
    composition constructs the paper broker/clock/reconciliation service but
    no LLM provider, agent, app, or notifier.
17. **Prebuilt query credentials:** confirmed. Credential-like query names are
    rejected before requests/HTTPX transport and denial text contains no URL,
    query, or value.
18. **Evidence provenance:** confirmed. Implementation and executable
    documentation are isolated in the implementation commit; this evidence
    commit changes only SDD evidence and completion records.

## RED evidence

```text
Initial round-2 static bundle:
22 failed, 2 passed

Initial runtime/TLS/preflight bundle:
18 failed, 12 passed

Wrapper direct/instance request probe:
.F

Dedicated preflight builder probe:
1 failed
AttributeError: bootstrap has no build_preflight_service
```

The two initial static passes are the retained counterexamples described in
items 4 and 7. The TLS characterization passed before implementation because
it establishes the platform behavior that required the CA-chain correction.

## Final verification

```text
New static probes:
25 passed in 7.40s

Runtime-focused files:
326 passed, 1 warning in 16.72s

Affected trust matrix:
1680 passed, 1 warning in 261.01s

Repository static gate:
release static checks: PASS

Compileall:
PASS

git diff --check:
PASS

Sole no-argument full suite:
3582 passed, 1 skipped, 1 warning in 522.86s
```

The warning is the existing third-party `websockets.legacy` deprecation
warning. The sole full run preceded one documentation-only correction to the
executable plan's preflight wording. No production/test code changed afterward;
the repository static gate and diff check were rerun on the final
implementation tree. No second full suite was run.

## Technical review result

All requested round-2 reproducer cases now have the intended stable
fail-closed result or a retained hermetic counterexample. The bounded diff was
reviewed for authority finality, helper side effects, URL/mapping provenance,
sensitive/environment aliases, client and SSL surfaces, route fail-closure,
artifact classification, value-free output, local CA-chain trust, preflight
capability composition, query rejection before transport, documentation truth,
provenance, and Plan 3 scope.

No requested reproducer remains failing at this implementation gate. This does
not claim that static analysis proves arbitrary Python semantics: unsupported
or dynamic security-boundary constructs intentionally fail closed and require
explicit modeling plus negative and positive fixtures before support.

## Residual hard limits

- Provider-side revocation/rotation of the previously exposed integration
  credential is external. Composio remains disabled with no route, origin,
  caller, toolkit, MCP surface, or chat tool.
- Preflight has no daemon-health row; daemon freshness is a separate post-start
  observation.
- General chat remains read-only. Immutable drafts require an explicit signed
  queue action and separate human approval.
- The release remains paper-only, webhook-free, and has no live-mode path or
  profit guarantee.
- Verification used generated certificates, in-memory TLS, temporary Git roots,
  and fakes only. No service, real secret store, credential, runtime database,
  external transport, or trading path was accessed, and nothing was pushed.
