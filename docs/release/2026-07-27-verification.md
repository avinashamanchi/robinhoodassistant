# Software release verification evidence

Observed at: `2026-07-29T19:13:46Z`

This record covers deterministic software verification only. It does not prove
an Alpaca account, runtime credentials, normal database, quote freshness,
reconciliation state, persisted breakers, or a running app or daemon.

## Verified candidate

- Commit: `faa2af9cfd743a91e96fc7069e92b691c49d04b7`
- Verifier run: `c89705b24cbe4ae8930507f41d15cc63`
- Result: `PASS`
- Migration head: `20260729_0017`
- Exact full-suite manifest: `4204` tests,
  `sha256:7f3e1bb708e3605d5f1dc8efc52fb0850e8471ae71cf3373d617a7aa750a18c4`

| Stage | Result | Evidence |
| --- | --- | --- |
| Compile | passed | Python source compiled |
| Migration tests | passed | 182 passed |
| Security tests | passed | 660 passed |
| Safety tests | passed | 142 passed |
| Frontend tests | passed | 188 passed |
| Full test suite | passed | 4,203 passed, 1 allowlisted skip |
| Branch coverage | passed | 93.98% total against a 90% minimum |
| Static release gate | passed | release static checks passed |

The single allowlisted skip was the credentialed Alpaca paper integration test,
`tests/test_alpaca_paper_integration.py::test_paper_account_and_quote`. Its skip
is expected in the isolated release verifier and is also why this result cannot
be described as an account preflight.

The verifier pinned the Git commit, migration head, exact collected test
identity, and canonical fingerprints of Git, Node, Python, and uv. It used a
Python socket guard for test-process isolation. That guard is not an
OS-enforced hostile-local-process boundary; a clean external CI run remains
required before release.

An independent read-only review of the added edge tests found no remaining
actionable safety or test-integrity issue. The review and verifier accessed no
credential store, broker account, normal database, existing runtime process, or
order mutation path.

## Final documentation commit

This evidence was produced before this checked-in record existed. Therefore the
documentation commit must receive the same complete verifier run, with no
tracked edits afterward, and fresh remote CI must pass. The exact final commit
and final run result belong in the pull-request handoff so this document does
not pretend a commit can contain evidence of its own hash.

## Explicitly not proven

- profitability or future returns;
- live-trading readiness;
- autonomous execution safety;
- intended Alpaca paper-account identity or health;
- normal runtime secret, TLS, schema, encryption, reconciliation, quote, or
  breaker state;
- app or daemon startup.

Operational status remains **BLOCKED / NOT STARTED** until the separately
authorized, credentialed, read-only paper preflight succeeds.
