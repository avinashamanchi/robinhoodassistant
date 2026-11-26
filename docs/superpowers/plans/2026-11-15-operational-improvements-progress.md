# Progress — operational improvements, 2026-11-15

Plan: `docs/superpowers/plans/2026-11-15-operational-improvements.md`
Starting commit: `3257456efa76189506b483b0e4b4ca69a5ee2f74`

Authorization: user requested all six recommendations, one by one, with accurate
completion. Existing strategy, sizing, approval and risk settings remain intact.

Ruling: the runtime remains the designated checkout. After Task 4b, later code
changes use an isolated managed development checkout with its own test environment.
Private configuration, credentials and the production database stay at the runtime.
Each candidate requires review and CI before deployment to the designated source.

Ruling: continue within the approved six-item scope without another design
approval round — user explicitly requested implementation of that roadmap.

Host selected by user: existing Mac, no hosting cost. On November 16 the user
confirmed no charger is currently available and authorized continuing on battery
(then approximately 98%). Temporary battery awake mode will expire after two
hours or release at 30% battery; normal unattended mode remains AC-powered.
User selected Telegram and authorized use/configuration of their bot. Managed
connection `trading-alerts` was paired and its actual setup alert verified on
November 17. Power state is observed continuously: the resumed November 17
baseline reports battery power and an expired awake grant; earlier AC verification
does not establish current continuous uptime.

- Task 1: complete — commit `22f9fe508ca7de4a73089f68516b753f7e5e0a2c`.
  CI run `43924181965` passed all jobs; its actual canonical artifact was
  authenticated, downloaded, digest-verified and published. The daemon's persisted
  readiness report accepted the release requirement. Private acceptance receipt:
  `.local/improvements/task1-acceptance.json`. Full suite: 5,231 passed, one
  credentialed skip; selected safety-module coverage: 93.62%.
  - Shared canonical validator now protects the publisher and readiness consumer;
    a bare pass flag, partial or mismatched result cannot enable trading.
  - CI checks out the exact head and retains only its canonical verifier report.
  - The standalone operator script authenticates read-only GitHub metadata,
    validates repository/workflow/head/attempt/jobs and archive digest, then uses
    private atomic publication with immutable hash-bound provenance.
  - Runtime Composio integration and outbound restrictions remain unchanged;
    static import-boundary tests keep the operator script out of runtime imports.
  - Independent review found blocking FIFO reads and an unsafe second input read.
    Both were reproduced RED and fixed GREEN, along with managed output bounds,
    inherited Git controls, unsafe archives and changed checkout/CI attempts.
  - `PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin .venv/bin/python -m pytest
    -o addopts='' tests/test_release_evidence.py tests/test_release_actions_client.py
    tests/test_release_operator_boundary.py tests/test_autopilot_readiness.py
    tests/test_release_verifier.py` plus the two CI contract tests: 169 passed.
  - Static release gate passed. Full collection is pinned to 5,232 tests;
    the security stage includes all new boundary tests and is pinned to 730.
  - Real managed-auth transport probe downloaded a 194-byte public official
    Actions artifact and matched GitHub's digest. Real current-project run/job
    metadata also matched the expected commit and attempt. These probes do not
    substitute for importing the actual candidate report after successful CI.
  - Final handoff output will be saved privately outside tracked source, so
    recording acceptance does not itself invalidate the verified commit.
- Task 2: implementation and available runtime acceptance complete — commit
  `23c7d268f44c01b916a0ceeed28cc8e0ea5fdce5`; CI run `44006288335` passed
  verification and secret scanning, and the scheduled importer authenticated and
  published its actual canonical artifact. All eight verifier stages passed;
  full local suite: 5,270 passed, one credentialed skip. Private acceptance receipt:
  `.local/improvements/task2-acceptance.json`.
  - Private atomic state, deduplicated health/incident history, bounded maintenance
    handling and recovery guidance are installed. All eight runtime jobs loaded.
  - AC awake guard and explicitly bounded battery mode were tested. Real battery
    assertion was observed; its two-hour grant expired normally. On November 17, AC power/charging and
    both idle/system sleep assertions were observed. Future continuous uptime is
    measured by the campaign; configuration does not guarantee it.
  - Real exclusive scheduled backup paused app/daemon/watchdog, created a verified
    encrypted backup, and restored exactly those jobs. Doctor recognized planned
    maintenance and returned to fresh heartbeat/app/reconciliation afterward.
  - Filesystem device number changed while database/control inodes and timestamps
    stayed identical. Integrity/FK checks, fresh verified backup and exact-file
    reselection restored the database binding. Proven stale app control artifacts
    were quarantined privately. Identity checks and old backup receipts remained
    intact. Private receipt: `.local/improvements/device-rebind-20261116.json`.
  - The reviewed automatic `daemon:monitor` broker-outage stop was reset via the
    authenticated guarded API after current CI and fresh broker order/position
    reconciliation. `operator_global` generation advanced from 1 to 2. Persisted
    daemon readiness was READY with every requirement passing. Prior cycles and
    audit history were preserved; no forced/duplicate cycle was run.
  - Telegram managed account connected as TradeAviBot. Private human-chat nonce
    pairing and the actual setup test alert were verified on November 17, including
    the message visibly arriving in the existing Telegram browser session. Tokens
    remain managed; private recipient IDs do not enter tracked source or logs.
  - Independent review's Unicode pairing, update-pagination and unknown-maintenance
    findings were reproduced and fixed. Scoped review reported no remaining concrete
    Critical/Important findings. Notification health exposes missing pairing,
    unconfirmed tests, stale delivery jobs and uncertain sends explicitly.
  - Full/security collections at this milestone: 5,271/791 tests. Future changes
    require fresh current-commit release evidence before acceptance.
- Task 3: complete — release candidate `88397c71245bbace75a219ab651845a3b65a7a3d`;
  CI `44046754916` passed all jobs and its canonical artifact was published.
  Daemon readiness accepted the current report and was READY.
  Full suite: 5,290 passed, one credentialed skip.
  Implementation
  task commits `92bcee0`, `1f9c64b`, `f741a2c`.
  - Added reusable Decimal forward evaluation, explicit capital/currency, dated
    benchmark matching, raw/Modified Dietz calculations, independent dollar/percent
    drawdown, exposure, source/identity validation and evidence completeness.
  - Date windows now consistently constrain fills/turnover, snapshot coverage and
    fill quality while retaining pre-window FIFO basis. Untrusted fills do not
    enter slippage arithmetic; excluded and quote-matched coverage is explicit.
  - Existing daemon snapshot path supports bounded sanitized economic-activity
    retrieval, original quote time and broker currency. First record establishes
    a baseline. Immediate Alpaca history, including an empty response, remains
    provisional because late postings/settlement-only dates cannot establish
    independently complete historical cash flows. Verified adjusted returns stay
    unavailable without that evidence; fees already in equity are not subtracted
    twice. Holdout and trading settings remain intact.
  - Focused verification: 154 passed, two documented dependency deprecations.
    Independent review found identity-validation, missing-capital and windowed
    unreadable-count defects; RED/GREEN fixes and scoped re-review addressed all
    three with no remaining concrete Important/Critical findings.
  - Genuine private report was generated with mode0600, seven trusted fills and
    no closed trades. All 191 legacy snapshots lack the new required identity or
    original timing evidence and are counted as excluded, not relabelled as verified.
    New-schema runtime observation produced two usable USD snapshots and one
    genuinely captured provisional activity interval. Exact-commit CI/artifact
    handoff was accepted; the isolated local mock safety drill also passed.
    Report observation: `.local/improvements/task3-reviewed-report-observation.json`.
  - Collection manifests at this candidate: 5,291 full / 811 security tests.
- Task 4: implementation and available monitoring baseline accepted at
  `831b023775f761be5dcae42006b9fb93f89c4288`; CI `44691536401` passed both
  jobs and all mandatory checks. Its authenticated canonical report was imported.
  Actual acceptance is recorded in `.local/improvements/task4-acceptance.json`.
  The daemon-owned
  campaign observes exchange calendars, expected sessions and bounded typed
  receipts. Thirty future calendar days remain an ongoing observation requirement.
  - Review corrections prevent stale scans from certifying missed sessions or a
    whole month, preserve window boundaries and independent order transitions,
    reject contradictory cycle scope, and make missing current evidence unknown.
    Campaign state and fresh doctor publications have authenticated provenance.
    Broker observations use encrypted producer metadata; backup inspection reads
    bounded committed artifacts directly. Controlled restart proof stays pending
    until a supported before/after source exists.
  - The covering integration suite passed 536 tests; subsequent focused fixes
    passed 87 tests. Independent scoped review closed all Important findings.
    Controller verifier/boundary checks passed 69 tests. Full/security collections
    are pinned to 5,384/904 at that release. CI passed 5,383 full tests with one
    credentialed skip and 93.61% selected safety-module coverage.
  - Daemon, watchdog and doctor were placed in recorded development maintenance
    during source changes. All eight jobs were restored, doctor and campaign
    publications were authenticated with the actual field-key ring, and the old
    unsigned campaign was preserved byte-for-byte as historical material.
    The verified period runs from November 20 at 13:30:08 UTC through December 20
    at 13:30:08 UTC. Future sessions and the controlled restart remain pending.
  - The resumed real baseline has fresh local health and successful broker-sync
    receipts, but paper trading remains stopped by `broker_drift` and
    `operator_global`. Today's natural partial fills advanced between separate
    activity/status reads. An isolated fake-broker regression reproduces the
    non-atomic snapshot comparison. Corrective Task 4b remains the next
    implementation step. Its implementation and independent review must finish
    before guarded trading recovery. Existing fill history remains intact.
  - Telegram pairing and delivery were confirmed. Uncertain attempts remain
    recorded and are not retried automatically. AC sleep-prevention assertions
    were observed at baseline acceptance; later power state remains measured.
- Task 4b: complete and accepted at `b86f4de6e99ee7f3fd42407c31bb1cbe460182d7`.
  CI `44949778127` passed every job and mandatory check; the actual canonical
  report was imported. Full suite: 5,400 passed and one credentialed skip;
  selected safety-module coverage: 93.65%. One bounded additional
  exact-activity read handles advancing partial fills, preserves the ordinary
  account cursor, and durably latches refreshed orders until comparison.
  Persistent or contradictory evidence still blocks trading. The initial
  focused run passed 345 tests; the review-fix selection passed 155, including
  all 17 dedicated convergence cases. Mandatory safety/full collections now
  contain 159/5,401 tests. All eight managed jobs were restored. Fresh real order
  and position reconciliation passed without cancellations. Separate guarded
  resets advanced broker drift from generation 6 to 7 and the global stop from
  generation 3 to 4; live health has no active breakers. The daemon and authenticated
  API report Paper READY with the unchanged approval fingerprint. Orders/fills
  remain 7/15; database integrity and foreign-key checks pass. Signed doctor and
  campaign observations verified; AC power and both sleep assertions were observed.
  The private runtime receipt is `task4b-acceptance.json`. No new natural partial-fill
  crossing or complete future month is claimed; controlled restart remains pending.
- Task 5: source implementation and independent review complete at `5beb3bf`,
  based on `b86f4de` in the isolated development checkout. Retries require a
  confirmed transient rejection and a fresh atomic reservation, with at most one
  additional attempt per task and two retry reservations per provider per UTC day,
  inside the original global ceilings. Ambiguous acceptance stays unknown;
  cancellation prevents further attempts. Reports distinguish logical tasks from
  paid attempts and show completion, failure and validated-plan rates with explicit
  cohorts and unknown outcomes. Local instrumentation errors remain separate from
  upstream ambiguity. No fallback, approval or execution authority was added.
  - The locked development baseline passed 339 tests. The affected implementation
    run passed 778; review-fix verification passed 47. All three Important review
    findings were addressed with RED/GREEN regressions and scoped re-review.
    Release-boundary checks passed 69 and the static safety gate passed.
  - The initial release `fa34116` passed actual CI (5,432 passed, one credentialed
    skip; 93.65% selected coverage), deployed, restored all eight jobs and reported
    Paper READY. Historical unknown usage was conservatively charged and settled
    without a refund or cap change. Two bounded Gemini calls returned HTTP 200
    but invalid citations; both failures remain in the evidence.
  - Round two constrains each request to its validated citation context, with
    independent concurrent schemas. Fourteen new regressions and 355 affected
    tests passed; scoped review is clean. Full/security collections are pinned
    to 5,447/1,174 tests, including the model quarantine suite. New exact-commit
    CI, deployment and one bounded real-provider validation remain required.
    Actual acceptance stays false in `.local/improvements/task5-acceptance.json`.
  - The designated Paper runtime continues on its accepted release during CI.
    The historical holdout remains preserved. Synthetic integration fixtures do
    not establish actual provider billing or strategy performance.
- Task 6: pending.
