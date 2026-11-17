# Progress — operational improvements, 2026-11-15

Plan: `docs/superpowers/plans/2026-11-15-operational-improvements.md`
Starting commit: `3257456efa76189506b483b0e4b4ca69a5ee2f74`

Authorization: user requested all six recommendations, one by one, with accurate
completion. Existing strategy, sizing, approval and risk settings remain intact.

Ruling: implement in the designated feature checkout, not a second trading
worktree — installation and broker ownership are tied to this source tree.

Ruling: continue within the approved six-item scope without another design
approval round — user explicitly requested implementation of that roadmap.

Host selected by user: existing Mac, no hosting cost. On November 16 the user
confirmed no charger is currently available and authorized continuing on battery
(then approximately 98%). Temporary battery awake mode will expire after two
hours or release at 30% battery; normal unattended mode remains AC-powered.
User selected Telegram and authorized use/configuration of their bot. Managed
connection `trading-alerts` is pending; no token or recipient has been assumed.

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
- Task 3: implementation/review complete; release acceptance in progress —
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
    New-schema runtime observation and exact-commit CI acceptance remain pending.
    Report observation: `.local/improvements/task3-reviewed-report-observation.json`.
  - Collection manifests at this candidate: 5,291 full / 811 security tests.
- Task 4: pending (month-long observation necessarily ongoing after tooling ships).
- Task 5: pending.
- Task 6: pending.
