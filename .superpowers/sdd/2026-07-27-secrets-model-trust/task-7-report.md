# Task 7 report — quarantined external content and typed Alpaca news

## Status

- Task 7 implementation is complete in
  `/Users/avi/Desktop/robinhood/trading-assistant/.worktrees/safety-foundation`.
- Quarantine implementation commit:
  `ac30a09c757bbf2d5f319f3510369ab41015da0c`.
- Panic-lease gate fix commit:
  `c5015cd4c4fa4de21e670014ca19f7a9c2e8ab0f`.
- Reviewer fix-round-1 implementation commit:
  `953b36eca1545dc63d069a100d082a77595815f7`.
- The Task 7 focused and affected suites are green.
- The repository-wide full-suite gate is clean. The historical initial run
  exposed one reproducible panic-lease defect, and the historical confirmation
  after that fix passed with 2,848 passed. After reviewer fix round 1, exactly
  one new final full suite passed with 2,888 passed, 1 skipped, and 1 existing
  third-party warning.
- PAPER-only trading, manual approval, kill switches, broker-truth checks, and
  the existing Task 8 sequencing boundary remain unchanged.
- No runtime database, Keychain, real credential, network, broker, provider,
  notification, order, breaker, app, daemon, or MCP service was accessed.
  The compromised Composio credential was not read, stored, or used.

## Implementation

### Immutable typed boundary

- Added frozen, `extra="forbid"` Pydantic models:
  `InjectionFinding`, `UntrustedContent`, `UntrustedFact`, and
  `UntrustedSummary`.
- Added bounded transient `source_name` metadata for Alpaca publisher
  provenance. Source ID, URL, publisher, publication time, and receipt time
  remain transient and are not persisted as human-readable database fields.
- Added stable schema constraints for finding codes, result codes, source
  references, timestamps, summary sizes, and content hashes.
- Added `NewsFetchResult` with enforced state invariants:
  available results cannot carry an error code; unavailable results carry no
  items and require one bounded stable code.

### Deterministic quarantine

- Added `UntrustedContentGateway.ingest()` and `ingest_many()` with a maximum
  of 20 items per request.
- Enforced 16 KiB UTF-8 limits before and after normalization, plus the
  planned 16,000-character normalized schema bound.
- Applied a bounded four-pass fixed-point canonicalization pipeline. Each pass
  decodes HTML entities, enforces the UTF-8 bound, applies NFC, removes and
  flags NUL, bidirectional, hidden `Cc`, and hidden `Cf` controls, and strips
  active containers, remaining markup, Markdown images, and embedded `data:`
  URLs. Non-convergence and entity expansion reject with stable codes.
- Runs instruction and tool-content detection only after canonicalization has
  reached a stable representation, including for encoded and double-encoded
  controls and active tags.
- Detected and removed direct instruction phrases, indirect mutable-tool
  instructions, nested or malformed tool-call JSON fragments, and suspicious
  standard or URL-safe Base64 payloads. Explicitly cued Base64 represented as
  bounded four-character chunks separated by mixed whitespace is compacted
  only for classification; malicious or malformed encoded spans are removed
  from the transient output in their original form.
- Base64 decoding is bounded and used only to assign a finding code. Decoded
  text is never returned, persisted, logged, fetched, opened, or rendered.
- The gateway has no model, tool, connector, broker, file, or network
  capability.

### Metadata-only persistence

- Reused the existing `UntrustedIngestEvent` table; no schema migration was
  needed.
- Persisted only source SHA-256, normalized-content SHA-256, byte count,
  stable finding codes, state, and receipt time.
- Used one atomic SQLite upsert against the existing source/content unique
  index. Conflicts union and sort stable finding codes, rejection dominates
  receipt, byte count resolves to the conservative maximum, and receipt time
  resolves to the deterministic minimum. Receive-then-reject and
  reject-then-receive therefore converge to identical metadata-only evidence,
  including under concurrent writers.
- Rejected oversize, invalid-URL, and invalid-publication-time inputs record
  metadata-only rejection evidence. Raw text, snippets, exception strings,
  URLs, publisher names, and human-readable source IDs never enter the row.

### Alpaca news provider

- Replaced `AlpacaNews.headlines()` with `AlpacaNewsProvider.fetch()`.
- Preserved the exact pinned `https://data.alpaca.markets` endpoint, TLS
  verification, finite timeout, and no-redirect session.
- Enforced limits from 1 through 20 and sent
  `NewsRequest(include_content=False)`.
- Consumed only ID, headline, summary, source, URL, and creation time.
  Full article content and image metadata are not accessed.
- Returned typed stable unavailable states for request, provider, malformed
  response, rejected content, and quarantine-store failures without exposing
  provider exception text.

## Task 8 sequencing boundary

- Task 7 intentionally does not claim end-to-end raw-text closure.
- `Analyst.analyze_plan(..., news: list[str])`, `NEWS_GUARD`, and
  `format_news_context()` remain as an explicit compatibility seam.
- `test_task7_documents_legacy_raw_news_seam_for_task8` proves that the seam
  still exists. Task 8 must replace it with `UntrustedSummary` and prove that
  no raw marker reaches the privileged analyst.

## TDD evidence

Initial RED:

- `uv run pytest tests/test_untrusted_content.py tests/test_news.py -v`
- Expected collection failures:
  `trading_assistant.analyst.untrusted` did not exist and
  `AlpacaNewsProvider` was not exported.

Incremental RED cases then reproduced:

- data-URL classification hidden by Markdown-image removal;
- nested tool-call JSON bypass;
- same-sentence raw marker retention;
- missing rejection persistence for naive publication time;
- quarantine-store exception leakage;
- backtick-wrapped mutable-tool instruction bypass;
- inconsistent typed result states;
- response-property and clock exception leakage;
- missing Alpaca source-ID acceptance;
- apostrophe-confused tool-JSON scanning;
- unrestricted unavailable-result error codes.

Final GREEN:

- Focused Task 7:
  `38 passed, 1 existing warning in 1.64s`.
- Affected analyst/news/DB/migration/outbound/release/startup group:
  `367 passed, 1 existing warning in 48.55s`.
- Compile:
  `python -m compileall` passed.
- Release static:
  `release static checks: PASS`.
- `git diff --check`: passed.

## Full-suite gate, diagnosis, and closure

The original Task 7 full suite was:

- `uv run pytest`
- Result:
  `1 failed, 2824 passed, 1 skipped, 1 warning in 397.92s`.
- Failure:
  `tests/test_route_policy.py::`
  `test_long_panic_keeps_one_lease_owner_and_executes_once`.
- Observed symptom:
  the follower received HTTP 503 and the owner logged
  `route_lease_renewal_uncertain`.

Systematic focused diagnosis:

- The failed test passed once in isolation.
- Twenty fresh focused repetitions produced `18/20 passed`; the same two
  failures logged `route_lease_renewal_uncertain`.
- The test deliberately uses a one-second lease, 100 ms renewal interval, and
  fixed sleeps.
- Instrumentation separated successful durable lease renewal, receipt renewal,
  follower waiting, and handler release. It showed no genuine tenure loss and
  no required DB-busy condition: the durable concurrency lease could renew
  successfully, then the owner handler could complete and set the stop event
  before the paired panic receipt was renewed.
- `_maintain_lease` incorrectly used the pre-renewal lease horizon for the
  receipt deadline and returned `store` solely because the stop event was set.
  That abandoned the second half of a successfully started two-part fence and
  produced the follower 503.

The authorized fix:

- uses the successfully renewed durable lease horizon for the paired receipt;
- permits that already-started renewal to complete its matching receipt fence
  even if the handler has just completed;
- still returns `lost` on owner/generation mismatch;
- still returns `store` when receipt persistence is genuinely uncertain after
  stop, so uncertainty is not swallowed.

Deterministic and bounded regression evidence:

- the stop-between-fences and old-horizon tests failed before the fix and
  passed afterward;
- the exact fencing selection passed:
  `25 passed in 2.12s`;
- the synchronized long-panic endpoint regression passed in 20/20 fresh
  subprocess repetitions;
- route policy, durable limits, and runtime tenure passed:
  `208 passed in 18.23s`;
- Task 7 analyst, news, outbound, and release groups passed:
  `220 passed, 1 warning in 16.64s`;
- compile, release-static, and diff checks passed.

After all focused gates were green, exactly one confirmation full suite was
run:

- `uv run pytest`
- Result:
  `2848 passed, 1 skipped, 1 warning in 391.10s`.

No additional full-suite run was performed.

## Reviewer fix round 1

All four quarantine findings were reproduced with RED tests before production
changes:

1. HTML entity decoding could introduce controls or active tags after the
   corresponding passes.
2. Whitespace-fragmented standard and URL-safe Base64 could evade bounded
   classification.
3. Conflict-ignore persistence made the first observation win, so later
   rejection evidence could be lost.
4. Panic receipt renewal, completion, and follower replay did not prove the
   authoritative lease owner, generation, and live horizon in the same
   transaction.

The implementation now:

- canonicalizes entity/control/markup layers to a bounded fixed point and
  rejects non-convergence or expansion beyond the item bound;
- classifies explicitly cued fragmented Base64 without retaining decoded
  material and removes the entire original malicious encoded span;
- merges metadata-only ingest evidence with a single atomic SQLite upsert and
  conservative, order-independent conflict rules, without a migration;
- validates the exact `ConcurrencyLease` key, owner, generation, and unexpired
  horizon in each receipt write transaction, flushes the write, then proves
  clock monotonicity and the lease fence again before commit;
- reads receipt and lease as one follower snapshot, rejecting expired reads
  and A1 receipts after B2 takeover while permitting replay after the exact
  A1 release transition;
- keeps genuine DB-busy, unknown-store, clock, cancellation, and tenure
  uncertainty fail closed without allowing a second panic execution.

Focused GREEN evidence after all production changes:

- untrusted content, news, DB models, and migrations:
  `227 passed, 1 warning in 30.49s`;
- route policy, durable limits, and runtime tenure:
  `220 passed in 19.08s`;
- analyst, analyst-v2, outbound policy, release static, and release branches:
  `182 passed in 15.97s`;
- panic-fence critical selection:
  10/10 fresh processes, 120/120 cases;
- synchronized long-panic endpoint:
  20/20 fresh processes;
- concurrent ingest merge:
  5/5 fresh processes, 60/60 races;
- `python -m compileall`: passed;
- `scripts/check_release_safety.py`: passed;
- `git diff --check`: passed.

Test-quality audit:

- 41 new regression cases were added: 28 quarantine/persistence cases and 13
  panic-fence cases.
- The two modified test modules collect 157 unique node IDs.
- No duplicate test names or node IDs, skips, xfails, placeholder `pass`,
  unreachable definitions, or dead parameter values were found.
- Similar-looking cases are intentionally distinct safety boundaries:
  renew versus finish, takeover versus exact release, pre- versus post-flush
  clock proof, sequential versus concurrent evidence merge, and contiguous
  versus whitespace-fragmented Base64.

Full-suite chronology is preserved:

- original Task 7 gate:
  `1 failed, 2824 passed, 1 skipped, 1 warning in 397.92s`;
- historical confirmation after the panic-lease defect fix:
  `2848 passed, 1 skipped, 1 warning in 391.10s`;
- reviewer fix round 1, exactly one final full-suite run:
  `2888 passed, 1 skipped, 1 warning in 394.41s`.

No second full-suite run was performed for reviewer fix round 1.

## Changed files

- `src/trading_assistant/analyst/untrusted.py`
- `src/trading_assistant/analyst/news.py`
- `src/trading_assistant/app/policy.py`
- `tests/test_untrusted_content.py`
- `tests/test_news.py`
- `tests/test_route_policy.py`
- this Task 7 report
- the progress ledger
- generated review package
  `review-2afe380..ac30a09.diff`
- generated panic-gate review package
  `review-515f87b..c5015cd.diff`
- generated reviewer-fix-round-1 review package
  `review-0463078..953b36e.diff`

## Caveats

- Injection detection is deterministic defense-in-depth, not a complete
  semantic prompt-injection detector. Task 8's no-tools quarantine model and
  structured-summary-only privileged path remain mandatory.
- The legacy direct analyst news seam remains until Task 8 by design.
- Transient provenance is not durably human-readable; the database stores only
  hashes and stable codes, matching the approved privacy decision.
- The existing `websockets.legacy` warning remains unchanged.
- No push was performed.
