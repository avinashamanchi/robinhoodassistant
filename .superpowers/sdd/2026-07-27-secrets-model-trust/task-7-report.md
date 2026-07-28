# Task 7 report — quarantined external content and typed Alpaca news

## Status

- Task 7 implementation is complete in
  `/Users/avi/Desktop/robinhood/trading-assistant/.worktrees/safety-foundation`.
- Implementation commit:
  `ac30a09c757bbf2d5f319f3510369ab41015da0c`.
- The Task 7 focused and affected suites are green.
- The repository-wide full-suite gate is **not clean**. Its sole run ended
  with 2,824 passed, 1 failed, 1 skipped, and 1 existing third-party warning.
  The failure is the independently reproducible existing panic-lease timing
  test documented below; no second full suite was run.
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
- Applied Unicode NFC normalization and removed NUL, bidirectional, hidden
  `Cc`, and hidden `Cf` controls before instruction detection.
- Removed active HTML containers and their contents, remaining markup,
  Markdown images, and embedded `data:` URLs.
- Detected and removed direct instruction phrases, indirect mutable-tool
  instructions, nested or malformed tool-call JSON fragments, and suspicious
  standard or URL-safe Base64 payloads.
- Base64 decoding is bounded and used only to assign a finding code. Decoded
  text is never returned, persisted, logged, fetched, opened, or rendered.
- The gateway has no model, tool, connector, broker, file, or network
  capability.

### Metadata-only persistence

- Reused the existing `UntrustedIngestEvent` table; no schema migration was
  needed.
- Persisted only source SHA-256, normalized-content SHA-256, byte count,
  stable finding codes, state, and receipt time.
- Used SQLite conflict-ignore against the existing source/content unique
  index so identical ingestion is idempotent.
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

## Full-suite gate and diagnosis

Exactly one full suite was run:

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
  fixed sleeps. The failure is therefore independently reproducible timing
  behavior in existing route-policy code rather than a Task 7 file change.
- Task 7 owns only analyst quarantine/news files, so no route-policy or panic
  lease code was changed. Per the explicit gate, no second full suite ran.

This means Task 7's implementation evidence is green, but the branch cannot be
described as repository-wide green until the panic-lease defect is handled in
its own authorized scope and a future release gate is run.

## Changed files

- `src/trading_assistant/analyst/untrusted.py`
- `src/trading_assistant/analyst/news.py`
- `tests/test_untrusted_content.py`
- `tests/test_news.py`
- this Task 7 report
- the progress ledger
- generated review package
  `review-2afe380..ac30a09.diff`

## Caveats

- Injection detection is deterministic defense-in-depth, not a complete
  semantic prompt-injection detector. Task 8's no-tools quarantine model and
  structured-summary-only privileged path remain mandatory.
- The legacy direct analyst news seam remains until Task 8 by design.
- Transient provenance is not durably human-readable; the database stores only
  hashes and stable codes, matching the approved privacy decision.
- The existing `websockets.legacy` warning remains unchanged.
- No push was performed.
