## Summary

Describe the behavior changed and the safety boundary it preserves.

## Verification

- [ ] Focused tests cover the changed behavior and failure path.
- [ ] `uv run python scripts/verify_loopback_release.py` passes from a clean commit.
- [ ] `uv run python scripts/check_release_safety.py` passes.
- [ ] Dependency and lock checks pass.
- [ ] Migrations include upgrade, downgrade, restart, and generated-copy evidence where applicable.

## Trading and provider boundaries

- [ ] Alpaca remains paper-only; this change does not enable live mode.
- [ ] Human approval and execution-time deterministic risk checks remain mandatory.
- [ ] No autonomous execution, daemon start, breaker reset, order submission, cancellation, or broker write occurred during release verification.
- [ ] Paid model/provider paths retain durable rate, concurrency, token, and cost limits.
- [ ] Composio remains disabled; no credential is present in source, history, logs, screenshots, or test artifacts.
- [ ] Backtest output is permanently labeled simulated and cannot promote a strategy or analyst.
- [ ] No profitability or account-growth claim is made.

## Evidence

- Approved spec:
- Implementation plans:
- Threat matrix:
- Deterministic software verification:
- Operational status:
- UI evidence:
- Migration and recovery evidence:
- Known blockers:

## Publication

- [ ] Branch is `codex/safety-foundation`.
- [ ] Pull request remains draft.
- [ ] Required CI reached a terminal successful state.
- [ ] No force push or merge was performed.
