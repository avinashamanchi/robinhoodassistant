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
