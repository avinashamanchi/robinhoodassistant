# Operational improvements

User request: complete the six recommended improvements sequentially, accurately,
with evidence before marking each complete. The existing paper strategy, sizing,
approval fingerprint and risk limits remain in force. Design implementation
choices below are the implementer's calls, within that authorized scope.

## Milestones and completion standards

1. **Verification handoff.** CI checks out the exact candidate head, retains its
   canonical verifier report, and a separate operator utility validates and
   atomically publishes that evidence for a clean designated checkout. Reject
   failed/partial/mismatched reports, an unsuccessful CI run, wrong repository or
   workflow, expired/mismatched artifacts, changed checkout, unsafe paths and
   interrupted publication. Existing evidence survives failed imports. The app
   and daemon receive no GitHub credentials. Completion requires passing tests
   and an actual successful artifact handoff for the deployed commit.
2. **Runtime reliability.** Unify runtime health and recovery checks, detect sleep
   and missed sessions, persist actionable incidents, and expose/test notification
   delivery without unsolicited external messages. Keep one active runtime and
   test recovery/backup behavior. The user selected the existing Mac, awake while
   plugged in, without hosting costs. Physical uptime cannot be
   claimed from configuration alone.
3. **Forward evaluation.** Add a read-only, reproducible paper-performance report
   against an explicit buy-and-hold benchmark, including capital, costs,
   turnover, exposure, drawdown and period/session coverage. Preserve the already
   evaluated holdout. Missing data is an explicit incomplete result. Completion
   means tested calculations and a genuine current report, not a profitability
   claim.
4. **Operational acceptance campaign.** Add persisted check results for real
   account reconciliation and controlled lifecycle/restart/recovery checks, plus
   daily tracking of expected vs completed sessions. Mock evidence and broker
   evidence remain distinct. Live credentials and order mutation retain the
   project's existing authorization boundaries. One month of uninterrupted
   operation is an ongoing evidence requirement, never fabricated as complete.
5. **Research reliability.** Retry only confirmed transient provider rejections,
   with bounded backoff and a fresh budget reservation per attempt. Ambiguous
   acceptance stays fail-closed. Record completion/failure and valid-plan rates;
   provider problems are visible. Test error classification, caps, cancellation,
   exhaustion and successful structured output. Do not enable silent provider
   fallback or change trade authority.
6. **Operator console.** Present one clear trading/readiness status, exact blocking
   reasons and recovery steps, next eligible cycle, incident state and evidence
   age; provide readable cycle/trade/skip explanations and the forward evaluation
   result. Reuse existing data and authentication. Verify browser flows and
   responsive layout, keeping implementation details out of normal user flows.

## Approaches

- Build incrementally on the current designated runtime (selected by implementer):
  preserves tested brokerage/ownership boundaries and allows each milestone to be
  accepted independently.
- Rewrite as a cloud service: requires a deployment target, new secret provider,
  distributed ownership and an operational migration before any benefit. This is
  outside the selected Mac approach; no hosting spending is assumed.
- Add more trading strategies first: does not resolve evidence, uptime or operator
  recovery and is outside these six milestones.

## First milestone design

The release verifier remains the authority for its eight mandatory checks. CI
uses the PR head SHA (or push SHA), rather than GitHub's synthetic merge commit.
The successful verification job uploads only the canonical JSON report after
all of its checks, dependency audit and isolated drill pass. The artifact has a
fixed name, immutable digest, run identity and bounded retention.

A standalone release-evidence module validates report schema, exact commit,
migration head, completed state, isolation claims, unique required stages,
successful return codes, and test counts. Publication uses a private temporary
file, fsync, a second clean-HEAD check and atomic replacement; it never changes
strategy settings, approvals, breakers or database rows. Local clean-clone
verification uses this same publisher.

The GitHub importer queries run/job/artifact metadata through authenticated
operator tooling, validates repository/workflow/head/run/attempt and successful
jobs, checks the ZIP digest and bounded single-report content, then invokes the
publisher. No report is synthesized from a green badge or reconstructed logs.
The importer records provenance separately from the canonical report. Network or
authentication failure leaves the prior report intact and the readiness gate
continues to reject evidence for an older commit.

## Validation and remaining physical constraints

Use meaningful RED/GREEN regression tests at each boundary, targeted integration
checks during development, and the complete release verifier before acceptance.
Update its pinned collection manifest when adding tests. Keep a progress ledger
with actual commands/results and decisions, and perform independent review.

An always-on machine, internet connectivity, an available upstream model and a
month of future sessions require real external state. Code/configuration can
support and measure those conditions but cannot stand in as evidence of them.
