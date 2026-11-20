# Operational improvements implementation plan

Spec: `docs/superpowers/specs/2026-11-15-operational-improvements-design.md`

Execute sequentially in the current designated feature checkout. Preserve the
paper-only and risk/approval boundaries. Do not move the production database or
install a second broker runtime. The user has authorized the six milestones;
the user selected the existing Mac with AC-powered unattended operation and
Telegram incident alerts. Current power and delivery evidence must be observed,
not inferred from those choices.

## Task 1: verification handoff

Files: `.github/workflows/ci.yml`, new
`src/trading_assistant/ops/release_evidence.py`, corresponding
operator script `scripts/sync_release_evidence.py`,
`tests/test_release_evidence.py`, `tests/test_release_static.py`, release
verification collection manifest, and `docs/autopilot.md`.

Interfaces: `validate_report(payload, expected_commit)` validates the canonical
verifier result; `publish_report(report_bytes, root, provenance=None)` accepts
only a clean exact-commit checkout and installs private evidence atomically.
The authenticated importer uses a separately injectable Actions client and
validates metadata/digest/archive before calling this publisher.

1. Write tests rejecting a bare pass flag, failed/partial/duplicate stages,
   wrong SHA, invalid isolation/schema/counts, unsafe paths and a changed checkout.
   Expected: RED because no publisher exists.
2. Implement validation and atomic publication; verify those tests GREEN.
3. Test importer with real ZIP bytes and fake API boundary, including wrong run,
   repository, workflow, attempt, digest, archive traversal/duplicates/size limits,
   failed jobs and network errors. Expected: RED for missing importer behavior.
4. Implement authenticated transport and importer. Verify metadata and artifact
   authentication through real GitHub tools; never log tokens or signed URLs.
5. Pin exact checkout and artifact upload action, add workflow contract tests,
   update manifest and documentation, then run focused tests and full CI.
6. Download the actual passing report for this commit, import it, and inspect
   persisted readiness. Expected: release gate passes for the exact current SHA;
   any other unmet gate remains visible. Commit and ledger the evidence.

## Task 2: runtime reliability

Files: new operational doctor/incident module, existing watchdog/control/launchd
components, operational docs, corresponding tests. Confirm host answer before
host-dependent setup. Define incident identity/dedup/recovery persistence first.
Test fresh, stale, missing, malformed and sleeping-host states and notification
dry-run/delivery failures. Install only the selected host configuration, and prove
restart/backup recovery and current health. Record physical uptime as ongoing.

## Task 3: forward evaluation

Files: extend `ops/paper_report.py` or a small shared evaluation module; add fixture
tests for capital flows, FIFO/partial fills, costs, benchmark dates, missing marks,
exposure and drawdown. Export report with explicit sample size and completeness.
Expected: deterministic fixture calculations match hand-worked results and a real
read-only report uses genuine broker-fill history and dated benchmark data.

## Task 4: operational acceptance campaign

Files: acceptance campaign service/CLI and operational report integration. Persist
versioned, scoped check receipts and session expectations; distinguish simulated
checks from broker reads/mutations. Test missed/duplicate/weekend sessions and
restarts. Run authorized checks through existing paper-only service boundaries.
Expected: real current baseline and an accurately incomplete month-long campaign.

## Task 4b: partial-fill reconciliation convergence

Corrective work discovered during the real Task 4 baseline, before Task 5.
The November 17 natural paper cycle exposed a non-atomic broker-read sequence:
exact activities were fetched, then newer cumulative order quantities were read.
The mismatch tripped broker drift and the daemon stop; later reconciliation
converged. Preserve that real incident and all order/fill history.

Investigate and reproduce this sequence with a deterministic fake broker before
changing implementation. Trace order/status/activity/position read ordering,
cursor overlap, submission-barrier ownership and daemon restart behavior. Fix
only a demonstrated feed-convergence defect with bounded fresh read-only broker
evidence. A coherent exact activity ledger is required before success. Never
infer missing fills from averages, tolerate unexplained quantities, automatically
clear an existing breaker, or weaken identity, overfill, chronology, precision,
unknown-order and cancellation checks. A persistent mismatch must still block
and trip as appropriate. Keep retries within explicit call/time bounds and
existing transport deadlines; do not retry order submission or cancellation.

Test advancing partial fills between reads, eventually consistent exact feeds,
persistent disagreement, invalid/mutated activity, contradictory cumulative
values, deduplication/restarts and unchanged fail-closed execution snapshots.
Preserve existing transaction-local audit and lifecycle proofs. No real provider
or broker calls, credentials, new trades, cancellations, resets, strategy/risk
changes, second runtime or push by the implementer. Controller handles actual
read-only acceptance and guarded recovery after current release evidence passes.
Commit only corrective task files; controller owns release manifests/progress.
Use focused RED/GREEN tests and report any unresolved source limitation honestly.

## Task 5: research reliability

Files: `llm/base.py`, `llm/gemini_backend.py`, analyst/report integration, tests.
Define sanitized rejection classification and bounded retry policy. Test a fresh
reservation for every attempt; no retry on ambiguous transport, auth, validation,
budget denial or cancellation. Record structured-plan success rate without raw
prompt leakage. Run bounded real-provider validation; report upstream outages.

## Task 6: operator console

Files: authenticated read models, existing console HTML/JS/CSS and browser tests.
Use task 1-5 results for unified status, specific recovery, next eligible cycle,
incidents, readable cycles and evaluation. Test unknown/stale/blocked/healthy
states, escaping, authentication and responsive browser flows. Expected: genuine
current states are understandable and no action implies unearned trading authority.

## Final acceptance

Independent review; address important findings with regression tests. Run complete
CI and relevant real local workflows, update the ledger, and report completed work
separately from host/provider dependencies and future operational observation.
