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
- Reviewer fix-round-2 quarantine implementation commit:
  `e3b6a8f5e7964fa8f4bbd50ac53e4345fd0bd22c`.
- Reviewer fix-round-3 quarantine implementation commit:
  `3833dd3ae52afb9dc714110e93fb6832c42a9119`.
- Reviewer fix-round-4 quarantine implementation commit:
  `0a4bcc626c2e8c5fa1afadb3ac0da0f46120023c`.
- Reviewer fix-round-5 quarantine implementation commit:
  `9e4482a702a660fe047f6a1c898097365b5d661f`.
- Post-round-5 exact-cue implementation commit:
  `505cdd38fe09271a3d31a09df94c33358b9a39a4`.
- The Task 7 focused and affected suites are green.
- The repository-wide full-suite gate is clean. The historical initial run
  exposed one reproducible panic-lease defect, and the historical confirmation
  after that fix passed with 2,848 passed. After reviewer fix round 1, exactly
  one new final full suite passed with 2,888 passed, 1 skipped, and 1 existing
  third-party warning. After quarantine-only reviewer fix round 2, exactly one
  new final full suite passed with 2,898 passed, 1 skipped, and the same
  existing third-party warning. After quarantine-only reviewer fix round 3,
  exactly one new final full suite passed with 2,903 passed, 1 skipped, and
  the same existing third-party warning. After quarantine-only reviewer fix
  round 4, exactly one new final full suite passed with 2,908 passed, 1
  skipped, and the same warning. After final quarantine-only reviewer fix
  round 5, exactly one new full suite passed with 2,902 passed, 1 skipped,
  and the same warning. After the post-round-5 exact-cue fix, exactly one new
  full suite passed with 2,932 passed, 1 skipped, and the same warning.
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
  standard or URL-safe Base64 payloads. Explicit encode, decode, and Base64
  cues now use a linear, bounded scanner that accepts arbitrary chunk lengths
  and all Unicode whitespace recognized by `str.isspace()`, including NBSP
  introduced by entity canonicalization. Payload text is compacted only for
  classification. Syntactically valid malicious exact spans are removed from
  transient output; malformed spans reject the entire item before any removal
  interval is created.
- Padded, explicitly delimited, and end-of-item encoded spans have exact
  boundaries. An unpadded fragmented span followed through whitespace by
  Base64-compatible prose is rejected as `ambiguous_encoding`; the gateway
  never searches shorter prefixes or mutates adjacent financial prose.
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

## Reviewer fix round 2 — quarantine only

Two Important findings were reproduced against the round-1 implementation:

1. The cued scanner accepted only 2–4-character later chunks and used an
   ASCII-only inter-token separator, so arbitrary wrapping and NBSP introduced
   by `&nbsp;` canonicalization could leave encoded instructions in output.
2. Descending-prefix guessing could classify a longer unpadded prefix that
   included an adjacent Base64-compatible prose token, changing
   `No profit warning.` into `profit warning.`.

TDD RED:

- the focused reviewer selection produced
  `9 failed, 6 passed, 49 deselected in 0.70s`;
- failures covered Base64/decode/encode cues, entity-decoded NBSP and other
  Unicode whitespace, 4/5/8-character and mixed wrapping, explicitly
  delimited unpadded payloads, ambiguous financial negation, unpadded EOF
  payloads, and token-bound behavior.

The quarantine-only implementation:

- replaces token-length assumptions and descending-prefix search with one
  linear scan over the already bounded 16 KiB item;
- bounds encoded span length at 8,192 characters, compact candidate length at
  4,096 characters, token count at 1,024, and decoded bytes at 3,072;
- uses `str.isspace()` for every payload separator decision, including NBSP,
  em-space, tabs, and line breaks;
- supports standard and URL-safe alphabets but treats a mixed alphabet,
  internal/invalid padding, illegal characters, excessive spans, and excessive
  token counts as stable `malformed_encoding` findings when an exact boundary
  exists;
- recognizes padding, explicit backtick/quote fences, and end-of-item as exact
  boundaries;
- raises stable `ambiguous_encoding` for an unpadded fragmented payload whose
  boundary would require guessing, causing metadata-only rejection rather
  than semantic mutation;
- performs no fetch, render, open, tool call, model call, or persistence of
  decoded content.

GREEN evidence:

- focused reviewer selection:
  `15 passed, 49 deselected in 0.62s`;
- complete quarantine module:
  `64 passed in 1.85s`;
- untrusted content, news, DB models, and migrations:
  `237 passed, 1 warning in 30.67s`;
- adversarial Base64 repeats:
  20/20 fresh processes, 300/300 cases;
- analyst, analyst-v2, outbound policy, release static, and release branches:
  `182 passed in 16.09s`;
- unchanged route policy, durable limits, and runtime tenure:
  `220 passed in 18.69s`;
- `python -m compileall`: passed;
- `scripts/check_release_safety.py`: passed;
- `git diff --check`: passed.

Test-quality audit:

- 10 new behavior cases were added, and the quarantine module collects 64
  unique node IDs.
- No duplicate names or node IDs, skips, xfails, placeholder `pass`, or dead
  parameters were found.
- Cue variants and malformed fixtures map to different parser branches;
  padded, delimited, ambiguous, and EOF fixtures map to different boundary
  proofs.

Exactly one repository-wide suite was run after all focused gates were green:

- `uv run pytest -o addopts=''`
- result:
  `2898 passed, 1 skipped, 1 warning in 394.76s`.

No second full-suite run was performed for reviewer fix round 2. No panic,
lease, persistence schema, news-provider, analyst, planning, app, daemon, MCP,
broker, or runtime code changed in this round.

Round 3 below supersedes round 2's handling of exact-but-malformed spans:
malformed cued content now rejects the item instead of returning sanitized
content.

## Reviewer fix round 3 — all-or-nothing cued payloads

The exact reviewer finding was reproduced through two partial-removal paths:

1. `decode: % <Unicode-whitespace-fragmented malicious Base64>` caused the
   bare parser to return only the illegal prefix as a malformed interval.
2. `decode: YWJj= <fragmented malicious instruction>` stopped at the early
   padding and returned that prefix without validating it before removal.

In both cases, the prefix was removed while the later fragmented malicious
payload survived because the generic fallback scanner handles contiguous
Base64 only.

TDD RED:

- the round-3 reviewer selection produced
  `10 failed, 58 deselected in 0.61s`;
- every failure returned sanitized content instead of raising a stable
  metadata-only rejection;
- RED cases covered both reviewer reproductions, illegal internal and suffix
  characters, early/internal padding, standard/URL-safe alphabet mixing,
  malformed delimited content, encoded-size limits, and fragment-token limits.

The quarantine-only implementation now:

- separates strict bounded Base64 decoding from decoded instruction
  classification so syntax validity is proven before sanitization;
- rejects illegal prefixes, illegal internal or suffix characters,
  early/internal/invalid padding, mixed alphabets, malformed continuations,
  excessive encoded spans, excessive compacted candidates, excessive token
  counts, and excessive decoded payloads as stable `malformed_encoding`;
- preserves stable `ambiguous_encoding` for Base64-compatible prose or another
  remainder whose unpadded boundary cannot be proven;
- raises before `_strip_cued_encoded_payloads()` appends a removal interval,
  so a malformed cued span can never be partially removed and returned;
- permits sanitization with trailing prose only for a syntactically valid
  padded or explicitly delimited payload;
- continues to permit a syntactically valid unpadded payload only when EOF
  proves its boundary;
- persists only source/content hashes, byte count, stable code, state, and
  receipt time for rejected items. No raw or decoded marker reaches model
  output, database text, exception text, or captured logs.

GREEN evidence:

- round-3 reviewer selection:
  `11 passed, 58 deselected in 0.44s`;
- complete quarantine module:
  `69 passed in 2.00s`;
- untrusted content, news, DB models, and migrations:
  `242 passed, 1 warning in 31.12s`;
- all-or-nothing rejection plus valid-boundary repeats:
  20/20 fresh processes, 320/320 cases;
- analyst, analyst-v2, outbound policy, release static, and release branches:
  `182 passed in 15.97s`;
- unchanged route policy, durable limits, and runtime tenure:
  `220 passed in 18.71s`;
- `python -m compileall`: passed;
- `scripts/check_release_safety.py`: passed;
- `git diff --check`: passed.

Test-quality audit:

- 11 round-3 behavior cases are exercised within 69 unique quarantine node
  IDs.
- No duplicate names or node IDs, skips, xfails, placeholder `pass`, or dead
  parameters were found.
- Reviewer repros, bare versus delimited corruption, each syntax failure, and
  each valid boundary map to separate observable branches.

Exactly one repository-wide suite was run after all focused gates were green:

- `uv run pytest -o addopts=''`
- result:
  `2903 passed, 1 skipped, 1 warning in 395.23s`.

No second full-suite run was performed for reviewer fix round 3. No panic,
lease, persistence schema, news-provider, analyst, planning, app, daemon, MCP,
broker, or runtime code changed in this round.

## Reviewer fix round 4 — bare cued payloads require EOF

The exact reviewer finding was reproduced against the round-3 parser:

- `decode: YWJjZA== <Unicode-whitespace-fragmented malicious Base64>`
  treated the valid padding as the end of the bare payload;
- the parser removed only the cue and padded prefix, then forwarded the
  fragmented malicious continuation because the generic fallback scanner
  handles contiguous Base64 only.

TDD RED:

- the round-4 reviewer selection produced
  `3 failed, 6 passed, 65 deselected in 1.85s`;
- all three failures returned sanitized content instead of rejecting the item;
- the failing cases covered the exact padded-prefix continuation, a padded
  malicious payload followed by financial prose, and a bare padded payload
  followed by a second explicitly delimited payload;
- explicit-delimiter and EOF-only control cases passed in the same RED run.

The quarantine-only implementation now:

- accepts an unquoted or unbackticked cued Base64 payload only when the bare
  parser consumes the remainder of the item after Unicode-whitespace
  normalization;
- keeps scanning after valid padding and raises stable
  `ambiguous_encoding` if any non-whitespace continuation follows;
- keeps malformed prefixes and malformed padding classified as stable
  `malformed_encoding`;
- returns an interval ending at EOF for every accepted bare payload, so no
  partial bare interval can be removed and returned;
- permits trailing prose only when the payload is bounded by an explicit
  quote or backtick delimiter;
- safely sanitizes multiple explicitly delimited payloads while preserving
  outside prose, but rejects the whole item if a bare payload is followed by
  another payload;
- continues to decode only for bounded classification and persists only
  metadata hashes, byte count, stable codes, state, and receipt time.

GREEN evidence:

- round-4 reviewer selection:
  `9 passed, 65 deselected in 0.35s`;
- complete quarantine module:
  `74 passed in 2.22s`;
- untrusted content, news, DB models, and migrations:
  `247 passed, 1 warning in 31.27s`;
- round-4 rejection, delimiter, multiple-payload, financial-negation, and EOF
  controls:
  20/20 fresh processes, 220/220 cases;
- analyst, analyst-v2, outbound policy, release static, and release branches:
  `182 passed in 15.95s`;
- unchanged route policy, durable limits, and runtime tenure:
  `220 passed in 18.66s`;
- `python -m compileall`: passed;
- `scripts/check_release_safety.py`: passed;
- `git diff --check`: passed.

Test-quality audit:

- five round-4 behavior cases were added, producing 74 unique quarantine node
  IDs;
- no duplicate names or node IDs, skips, xfails, placeholder `pass`, or dead
  parameters were found;
- the padded-prefix continuation, padded financial prose, EOF-only padded
  acceptance, multiple delimited preservation, and bare-then-delimited
  rejection cases each exercise a separate observable boundary.

Exactly one repository-wide suite was run after all focused gates were green:

- `uv run pytest -o addopts=''`
- result:
  `2908 passed, 1 skipped, 1 warning in 394.98s`.

No second full-suite run was performed for reviewer fix round 4. No panic,
lease, persistence schema, news-provider, analyst, planning, app, daemon, MCP,
broker, or runtime code changed in this round.

## Reviewer fix round 5 FINAL — reject every cued encoded item

The final reviewer direction intentionally replaced the increasingly fragile
cued Base64 boundary grammar with a smaller fail-closed rule:

- after bounded fixed-point canonicalization, an explicit case-insensitive
  encode, decode, Base64, or encoded-payload/instruction cue with any
  nonempty suffix rejects the entire item with stable
  `ambiguous_encoding`;
- classification uses a whitespace-collapsed transient view, so entity-decoded
  NBSP, Unicode whitespace, line wrapping, case changes, and hidden-control
  removal cannot reopen a fragmented boundary;
- quoted, backticked, padded, unpadded, fragmented, malformed, mixed-alphabet,
  multi-payload, and payload-plus-prose forms all take the same metadata-only
  rejection path;
- a cue with no payload remains ordinary sanitized text;
- the bounded generic scanner remains only for uncued contiguous Base64.
  Decoded bytes are classification-only, and a malicious exact candidate is
  removed and flagged without forwarding decoded content;
- no cued decoder, boundary guess, partial interval, URL fetch, render, tool
  call, or external action remains.

TDD RED evidence before the production simplification:

- the initial reviewer selection produced
  `12 failed, 4 passed, 74 deselected in 0.92s`;
- the no-payload controls isolated the overbroad old behavior with
  `3 failed, 2 passed, 87 deselected in 1.87s`;
- a 32-separator Unicode-whitespace obfuscation then produced
  `1 failed, 64 deselected in 0.19s`, proving the classification view still
  needed bounded normalization.

Final GREEN evidence:

- final reviewer selection:
  `23 passed, 45 deselected in 0.90s`;
- complete quarantine module:
  `68 passed in 1.97s`;
- reviewer selection in 20 fresh processes:
  460/460 cases;
- final untrusted-content, news, and DB-model group:
  `86 passed, 1 warning in 3.06s`;
- final migration group:
  `155 passed in 27.16s`;
- analyst, analyst-v2, outbound policy, release static, and release branches:
  `182 passed in 15.79s`;
- unchanged route policy, durable limits, and runtime tenure:
  `220 passed in 18.96s`;
- `python -m compileall`: passed;
- `scripts/check_release_safety.py`: passed;
- `git diff --check`: passed.

One adjacent migration-lock timing test exposed a pre-existing race during the
focused gate:

- the first combined DB/migration run produced
  `1 failed, 237 passed, 1 warning in 30.77s`;
- the exact case then passed once, followed by 13 fresh-process passes and the
  same failure on iteration 14;
- the failure was the migration's fail-closed
  `sensitive_trust_downgrade_blocked` outcome rather than an unsafe
  downgrade;
- the complete migration file subsequently passed 155/155, and the exact
  case also passed in the sole final repository run;
- this final quarantine-only round did not change migration, lease, panic, or
  runtime code, and the reproducible timing evidence is retained rather than
  hidden by repeated full-suite runs.

Simplification and test-quality audit:

- removed the cued fragmented scanner, delimiter parser, bare parser, compact
  candidate parser, span/token limits, alphabet/delimiter tables, and all
  partial-interval logic;
- removed obsolete tests that expected cued payload sanitization or trailing
  prose preservation;
- implementation changed by 25 additions and 196 deletions; tests changed by
  114 additions and 447 deletions, for 504 net lines removed;
- 68 unique quarantine node IDs were collected;
- no duplicate node IDs, skips, xfails, placeholder `pass`, dead parameters,
  or references to deleted parser helpers were found.

Exactly one repository-wide suite was run after all focused gates were green:

- `uv run pytest -o addopts=''`
- result:
  `2902 passed, 1 skipped, 1 warning in 394.41s`.

No second full-suite run was performed for reviewer fix round 5. No panic,
lease, migration, persistence schema, news-provider, analyst, planning, app,
daemon, MCP, broker, or runtime code changed in this round. The Task 8 legacy
analyst-news seam remains explicitly open.

## Post-round-5 exact cue fix — bounded filler and Unicode separators

The exact reviewer gap was in cue recognition, not payload parsing:

- `decode this:` and `decode the following:` were not recognized because the
  round-5 filler branch required an object word;
- strong forms such as `decode and obey this payload` still required one of
  four ASCII terminal tokens;
- `content` was missing from the object vocabulary;
- Unicode punctuation between cue words or at the terminal was not accepted.

The quarantine-only fix now:

- runs after the existing bounded fixed-point canonicalization and uses the
  existing Unicode-whitespace-collapsed transient classification view;
- recognizes case-insensitive `decode`, `encode`, `base64`, and `encoded`
  action forms with optional `this` / `the following`, `and obey`, and one or
  two `base64` / `payload` / `instruction` / `content` / `data` object words;
- accepts Unicode whitespace, punctuation, symbols, and underscore as cue
  joins, with every join and terminal run capped at eight characters;
- uses atomic phrase cores and atomic terminal runs so a maximal no-payload
  phrase cannot backtrack into a shorter cue and reclassify its own trailing
  punctuation as payload;
- requires a non-whitespace remainder before rejection;
- rejects the entire item through stable `ambiguous_encoding`, preserving the
  existing metadata-only database and log boundary;
- performs no Base64 boundary parsing, decoding, payload extraction, URL
  access, model call, tool call, or external action.

The recognizer is deliberately conservative. Standalone `decode`, empty
punctuated cues, complete cue phrases with no payload, and ordinary
`Analysts decode more data...` prose remain accepted. Prose that resembles an
explicit encoded-action cue followed by content can be rejected as a false
positive; this is the chosen fail-closed tradeoff for untrusted external text.

TDD evidence:

- after correcting a RED performance fixture that exceeded the pre-existing
  16 KiB UTF-8 ingress ceiling, the reviewer/control selection produced
  `18 failed, 15 passed, 63 deselected in 1.28s`; all 18 failures were the
  expected missing whole-item rejection;
- the first implementation attempt produced
  `1 failed, 32 passed, 63 deselected in 1.13s`, exposing terminal
  backtracking that treated a trailing em dash as payload;
- an added punctuation-only malformed-payload case then produced
  `1 failed, 19 passed in 0.79s`, proving the strong terminal could still
  consume both delimiter and payload punctuation;
- atomic maximal terminals and a structured strong terminal resolved both
  defects without adding a payload parser.

Final GREEN evidence:

- exact reviewer, no-payload, and maximum-size performance selection:
  `35 passed, 63 deselected in 1.20s`;
- complete quarantine module:
  `98 passed in 2.84s`;
- reviewer/control selection in 20 fresh processes:
  700/700 cases;
- untrusted-content, news, DB-model, and migration group:
  `271 passed, 1 warning in 31.84s`;
- analyst, analyst-v2, outbound policy, release static, and release branches:
  `182 passed in 15.92s`;
- unchanged route policy, durable limits, and runtime tenure:
  `220 passed in 18.94s`;
- 98 unique quarantine node IDs; no duplicates, skips, xfails, placeholder
  `pass`, or dead parameters;
- `python -m compileall`: passed;
- `scripts/check_release_safety.py`: passed;
- `git diff --check`: passed.

The maximum-size near-miss test exercises 15,900 bytes of repeated
cue-prefix near misses and requires the complete public gateway ingest to
finish within one second. Together with the 16 KiB input ceiling, bounded
eight-character quantifiers, and atomic groups, this guards against
catastrophic backtracking.

No local independent reviewer subagent was available; the discovered review
tools required an existing GitHub pull request and were not used because this
task forbids push and external state. The exact-requirements self-review found
no open Critical or Important issue.

Exactly one repository-wide suite was run after all focused gates were green:

- `uv run pytest -o addopts=''`
- result:
  `2932 passed, 1 skipped, 1 warning in 395.07s`.

No second full-suite run was performed. No panic, lease, migration,
persistence schema, news-provider, analyst, planning, app, daemon, MCP,
broker, or runtime code changed. The Task 8 legacy analyst-news seam remains
explicitly open.

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
- generated reviewer-fix-round-2 review package
  `review-849b6ad..e3b6a8f.diff`
- generated reviewer-fix-round-3 review package
  `review-34f56c0..3833dd3.diff`
- generated reviewer-fix-round-4 review package
  `review-ceb013e..0a4bcc6.diff`
- generated reviewer-fix-round-5 review package
  `review-b0ddb67..9e4482a.diff`
- generated post-round-5 exact-cue review package
  `review-7c4a6b4..505cdd3.diff`

## Caveats

- Injection detection is deterministic defense-in-depth, not a complete
  semantic prompt-injection detector. Task 8's no-tools quarantine model and
  structured-summary-only privileged path remain mandatory.
- The legacy direct analyst news seam remains until Task 8 by design.
- Transient provenance is not durably human-readable; the database stores only
  hashes and stable codes, matching the approved privacy decision.
- The existing `websockets.legacy` warning remains unchanged.
- A pre-existing migration-lock timing test can fail closed under contention,
  as recorded in reviewer fix round 5; this round did not alter that path.
- Explicit cue-like financial prose may be rejected conservatively; the
  post-round-5 section records the accepted false-positive tradeoff.
- No push was performed.
