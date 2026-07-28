# Task 8 Report — Quarantine Model Trust Boundary

Date: 2026-07-28

Branch: `codex/safety-foundation`

Plan base: `941cdb5243231aa36b13afc0f05847f2016bb4fb`

Implementation commit: `bb3928ef05ca1e5845f80893f5382605682eb81f`

## Outcome

Task 8 is implemented. Raw external text can enter only the deterministic
quarantine boundary. A separately constructed, budgeted `untrusted` backend can
produce a strictly validated `UntrustedSummary` with no tools. The privileged
analyst and planning paths accept only that typed summary, never raw news text,
and their outputs carry deterministically validated opaque source citations.

Shared provider-budget reservations now reconcile ambiguous started calls to a
durable, idempotent `unknown` state across provider failures, usage-property
failures, settlement failures, and cancellation. `KeyboardInterrupt`,
`SystemExit`, and `CancelledError` are not swallowed.

## TDD evidence

RED failures were observed before each implementation slice:

- missing `QuarantineSummarizer` and hashed child-request-ID interface;
- started reservations left unreconciled after cancellation and non-idempotent
  `mark_unknown` behavior;
- legacy raw `news: list[str]` analyst and planner seams;
- missing quarantine bootstrap composition;
- raw-response property failure and lost shadow-report citations;
- acceptance of invented injection flags and case-folded raw markers.

The failures were resolved incrementally before running the affected groups.

## Implemented boundary

- `QuarantineSummarizer.summarize()` accepts only a tuple of at most 20
  `UntrustedContent` values and assigns opaque `s1..sN` references.
- Source IDs, source names, and URLs are not sent as analyst citations.
- First and repair calls use a separate
  `BudgetedLLMBackend(category="untrusted")`, `tools=[]`, and
  `tool_choice=None`; repair consumes a distinct reservation.
- Child request IDs use bounded deterministic SHA-256 derivation and remain at
  most 64 characters.
- Quarantine output must be one exact, bounded, non-Markdown JSON object in one
  text block. Tool blocks, multiple blocks, fences, duplicate or unknown
  fields, malformed or oversized output, unknown references, invented flags,
  copied markers, and unsafe strings fail closed after at most one repair.
- Deterministic sanitizer findings are unioned into the summary and cannot be
  removed by model output. Every fact and uncertainty is re-sanitized before
  privileged use.
- Ingest persistence remains metadata/hash-only and explicitly
  `received`/`rejected`; no misleading summarized state or migration was added.
- `Analyst`, `PlanningService`, and their callers use
  `UntrustedSummary | None`; the legacy `NEWS_GUARD`,
  `format_news_context`, and raw news signatures were removed.
- `AnalysisReport` and `TradePlan` contain `cited_source_refs`. No summary
  requires an empty list; summarized facts require at least one relevant
  supplied opaque reference; unknown, duplicate, or missing references fail
  before report, plan, audit, or candidate persistence.
- Bootstrap constructs and injects the quarantine reader separately only when
  news is enabled. News remains disabled by default; no polling or fetching was
  introduced.

## Verification

- Task 8 boundary file: `34 passed in 1.97s`.
- Focused analyst/planning/budget/bootstrap/app/daemon/release group:
  `424 passed, 1 warning in 21.52s`.
- Broader affected group:
  `832 passed, 1 warning in 82.95s`.
- Compile check: PASS.
- Release static checks: PASS.
- Static raw-seam search: PASS; matches were restricted to assertions that the
  removed names are absent.
- `git diff --check`: PASS.
- Exactly one full-suite run:
  `2973 passed, 1 skipped, 1 warning in 264.83s (0:04:24)`.

The sole warning is the existing third-party
`websockets.legacy.__init__` deprecation warning under `.venv`.

## Review

Review package:
`review-941cdb5..bb3928e.diff` (90,048 bytes, one implementation commit).

A bounded final acceptance review of the exact plan-base-to-implementation diff
found no open correctness, security, or scope defects. Agent-dispatch controls
were unavailable in this session, so this was a fresh manual acceptance pass
over the packaged diff rather than a separately executed reviewer agent.

## Preserved safety boundaries and caveats

- PAPER mode, manual approval, kill switches, broker-truth checks, and all
  execution controls are unchanged.
- No runtime database, Keychain, network, credential, broker, provider,
  notification, order, breaker, app process, daemon process, or MCP process was
  accessed or mutated. Tests used temporary SQLite only.
- The compromised Composio credential was not read, used, logged, or modified.
- News remains OFF and this task intentionally adds no fetch or polling path.
- A provider response with missing usage remains usable under the existing
  shared-backend contract, while its reservation becomes fully charged
  `unknown`; this task did not change that established behavior.
- No push was performed.

## Fix round 1 — three Important findings

Date: 2026-07-28

Fix-round base: `a0482b38b0dbf716113b90fb773923d57a3a2fcc`

Implementation commit: `aab0c32310acea1c1cc74c9f96b70742e4b93581`

### Source copy-through

- Reproduced direct `COPYTHROUGHMARKER`, underscore-to-hyphen marker
  normalization, partial copied phrases, NFKC/case/punctuation variants, and
  copied uncertainties before changing production code.
- Added bounded source-derived lexical fingerprints over nonnumeric tokens of
  at least 12 characters and contiguous 3–5-token n-grams.
- Fingerprints use NFKC, case-folding, punctuation separation, and bounded
  128-bit digests. Work is capped by the existing 20-item/16-KiB source limits,
  per-source and aggregate token limits, and a fixed fingerprint-operation
  ceiling.
- Standalone short tickers, ordinary two-token company names, and pure numeric
  single tokens are intentionally allowed. A legitimate long proper noun can
  be rejected; that conservative false-positive is preferred to raw
  copy-through and forces repair/paraphrase.
- Facts and uncertainties use the same check. Rejected first responses consume
  the one repair attempt, and copied source material never reaches the
  privileged analyst call.

### Budget cancellation and stale-started reconciliation

- Reproduced a provider `CancelledError` masked by a secondary
  `KeyboardInterrupt` from `mark_unknown`.
- Reconciliation now catches secondary `BaseException`, attaches a stable note
  where possible, and always preserves/re-raises the original provider or
  cancellation exception.
- Added atomic, idempotent stale-started maintenance on reserve, status, and
  explicit `reconcile_expired_started()`. It acts only at
  `expires_at <= now`, retains all charged call/token reservations, transitions
  `started` to `unknown`, and latches the provider day with
  `provider_started_usage_unknown`.
- New reservations fail closed until operator/provider reconciliation.
  Unexpired calls are untouched, invalid start/expiry chronology is rejected,
  an unknown reservation cannot settle, and concurrent reapers transition
  exactly once without capacity reuse.

### Citation persistence boundary

- Added one shared `validate_source_citations()` boundary for
  `AnalysisReport` and `TradePlan`.
- `Analyst` invokes it for primary outputs. `PlanningService` invokes it
  immediately after any injected analyst returns, before risk snapshot/sizing,
  and `_store()` invokes it again before row/audit creation.
- `save_report()` accepts the summary context and validates before constructing
  a persistence row. Direct stores, alternate analysts, no-summary citations,
  missing references, and unknown references fail before report, plan, audit,
  candidate, or risk-snapshot work.
- Valid opaque `s1` persistence and no-summary/empty-reference behavior remain
  covered. Shadow/report callers remain behind `save_report`.

### Fix-round verification

- TDD RED was observed independently for all three findings before production
  changes.
- Focused Task 8/budget/analyst/planning/store/news group:
  `236 passed, 1 warning in 8.26s`.
- Cancellation plus concurrent-reaper repeats: `40/40 passed`.
- Broader app/API/bootstrap/daemon/launch/release/budget-review group:
  `516 passed, 1 warning in 50.81s`.
- Compile check: PASS.
- Release static checks: PASS.
- Raw-news seam and privileged `UntrustedContent` searches: PASS.
- `git diff --check`: PASS.
- Exactly one fix-round full-suite run:
  `2990 passed, 1 skipped, 1 warning in 266.15s (0:04:26)`.

The sole warning remains the existing third-party
`websockets.legacy.__init__` deprecation warning under `.venv`.

Review package:
`review-a0482b3..aab0c32.diff` (60,303 bytes, one implementation commit).

A fresh bounded acceptance review of the packaged base-to-implementation diff
found zero open findings. Agent-dispatch controls were unavailable, so this was
a manual packaged-diff review rather than a separately executed reviewer
agent.

PAPER mode, manual approval, kill switches, broker truth, news-disabled
defaults, and no-tools/raw-seam closure remain unchanged. No migration was
needed. Only temporary SQLite test databases were used; no runtime database,
Keychain, network, credentials, broker/provider, notification, order, breaker,
app, daemon, or MCP state was accessed or changed. No push was performed.

## Fix round 2 — two Important findings

Date: 2026-07-28

Fix-round base: `9c77c2816734625a77b4d99bfc59cf5031c0b654`

Implementation commit: `0fa8b58d49f3bbc72d074b59389e870ae2e3208c`

### Compact copy-through fingerprinting

- Reproduced punctuation/case compaction, output-as-source-substring,
  source-as-output-substring with added prefixes/suffixes, long partial-token
  copy, NFKC compatibility forms, and copied uncertainties before changing
  production code.
- Added a second bounded copy-through boundary over NFKC/casefolded Unicode
  alphanumerics. It rejects any output of at least 12 compact characters found
  in a source and any 12-character source window found inside an output.
- Fixed-width source windows use bounded 128-bit digests, so reverse substring
  detection is linear in the capped source/output sizes; a collision can only
  cause a conservative rejection.
- Source characters, output characters, source comparisons, source windows,
  and output windows all have explicit limits under the existing
  20-item/16-KiB source boundary. Existing 3–5-token checks remain in force.
- This intentionally accepts false-positive repair or rejection for a
  legitimate shared 12-character sequence. Rewritten short tickers and
  ordinary short numbers remain allowed. Facts and uncertainties share the
  same boundary, and copied material was proven absent from the privileged
  analyst call.

### Atomic unknown/expiry convergence

- Added injectable `now`/clock handling to `mark_unknown()`.
- `mark_unknown()` and stale-started reaping now call one helper inside the
  same `BEGIN IMMEDIATE` transaction. A started reservation moved to unknown,
  or an idempotently revisited unknown reservation, latches its provider day
  once `expires_at <= now`.
- Both lock orderings and simultaneous threads converge to one charged
  `unknown` reservation plus the stable
  `provider_started_usage_unknown` reconciliation latch.
- Call/token aggregates are never released or double-mutated, status exposes
  the latch, and subsequent reservations fail closed. Unexpired calls are not
  reaped; settled reservations remain final.

### Fix-round verification

- Compact-copy RED was observed before implementation; the final dedicated
  compact/control slice passed `8` tests.
- Expiry-race RED was observed as `4 failed, 1 passed` before implementation.
- Task 8 plus shared-budget files: `129 passed`.
- Focused trust/budget/analyst/planning/store/news group: `248 passed`,
  `1 warning`.
- Both lock orderings plus simultaneous-thread repetition: `60/60 passed`.
- Broader app/API/bootstrap/daemon/launch/release/budget-review group:
  `516 passed`, `1 warning`.
- Compile check: PASS.
- Release static checks: `54 passed`.
- Raw-news seam and privileged `UntrustedContent` searches: PASS.
- `git diff --check`: PASS.
- Exactly one fix-round full-suite run:
  `3002 passed, 1 skipped, 1 warning in 245.68s (0:04:05)`.

The sole warning remains the existing third-party
`websockets.legacy.__init__` deprecation warning under `.venv`.

Review package:
`review-9c77c28..0fa8b58.diff` (28,988 bytes, one implementation commit).

A fresh bounded acceptance review of the exact packaged diff found zero open
correctness, security, or scope findings. Agent-dispatch controls were
unavailable, so this was a manual packaged-diff review rather than a separately
executed reviewer agent.

PAPER mode, manual approval, kill switches, broker truth, news-disabled
defaults, and no-tools/raw-seam closure remain unchanged. No migration was
needed. Only temporary SQLite test databases were used; no runtime database,
Keychain, network, credentials, broker/provider, notification, order, breaker,
app, daemon, or MCP state was accessed or changed. The compromised Composio
credential was not read, used, logged, or modified. No push was performed.
