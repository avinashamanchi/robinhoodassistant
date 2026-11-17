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
- Task 2: in progress — Mac power guard, private incident/health state, scheduled
  receipt sync and isolated Telegram incident delivery. The app job was restored
  using the canonical installer; app and daemon are reachable/running. An existing
  `operator_global` latch was set automatically by `daemon:monitor` at 01:07 on
  November 16 after three failed broker GETs. Reconciliation is currently clean;
  preserve the latch until guarded recovery checks succeed. Do not silently reset
  it. Telegram setup and physical long-term power remain external dependencies.
  - Added private bounded atomic operational state, AC caffeinate ownership and
    explicit expiring battery permission with low-battery/unknown-power release.
    Real battery assertion observed through `pmset`; no global sleep setting changed.
  - Read-only doctor persists deduplicated incidents, monitoring gaps and recovery
    transitions. It checks app/heartbeat/reconciliation/breakers, jobs, release and
    committed backup age. Active backup maintenance has a 15-minute bound; unknown
    maintenance cannot suppress health observation.
  - Separate managed-auth Telegram transport uses nonce-paired private human chat,
    fixed sanitized event text, durable attempt claims and no ambiguous resend.
    Real bot connection/pairing/test delivery remains pending user setup.
  - Independent review reproduced Unicode pairing, unadvanced update offset and
    suppressed unknown-maintenance bugs; regression tests now pass for all three.
    Reviewer reports no remaining concrete Critical/Important finding.
  - Live checks found filesystem device changed from 16777233 to 16777231 while
    DB inode remained identical. Runtime refused correctly. Integrity/FK checks,
    fresh verified encrypted backup and exact-file reselection restored binding.
    Old control socket had identical inode/timestamps, absent PID and no listener;
    it was quarantined privately. App liveness and clean broker reconciliation
    were re-observed. No historical receipt or identity guard was weakened.
    Private receipt: `.local/improvements/device-rebind-20261116.json`.
  - All eight authorized runtime jobs are installed/loaded. Live doctor reports
    app/database/heartbeat/reconciliation/jobs/recent-backup/monitor checks passing;
    battery mode and unpaired Telegram remain explicit warnings. Safety latch is
    retained and release proof is pending this candidate's CI.
  - Focused runtime/backup/installer suite: 64 passed; operator-boundary and verifier
    tests: 69 passed before the final manifest refresh. Static gate passed.
    Full/security collections are pinned to 5,271 and 791 tests respectively.
    Full local suite and final exact-commit CI acceptance remain in progress.
- Task 3: pending.
- Task 4: pending (month-long observation necessarily ongoing after tooling ships).
- Task 5: pending.
- Task 6: pending.
