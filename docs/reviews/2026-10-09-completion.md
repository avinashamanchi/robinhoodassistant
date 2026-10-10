# Completion pass — 2026-10-09

Stage three of the review of PR #3. It follows
[`2026-10-05-project-review.md`](2026-10-05-project-review.md) and
[`2026-10-06-review-followup.md`](2026-10-06-review-followup.md). This pass
finished the implementation those reviews left open, re-verified everything,
and records what still needs the operator's approval or genuine elapsed time.

Status vocabulary: **Fixed ✔** (fixed and verified) · **Implemented ◐**
(implemented; verification blocked) · **Approval** (awaiting explicit
approval) · **Time** (awaiting genuine observation time) · **Disproved** ·
**Open** (unresolved, with blocker and next action).

**Heads.** The final code commit is `f90332d`. The commit that adds this file
changes only documentation. Verification results below name the commit they
ran on.

## Consolidated issue register

### Original review (2026-10-05)

| # | Issue | Status | Evidence |
|---|---|---|---|
| 1 | Live bar cache never expired | **Fixed ✔** | Superseded by session-aware freshness (N19); `test_marketdata`, `test_sessions` |
| 2 | Moved checkout: venv, LaunchAgents and worktree point at the old path | **Approval** | Repository side fixed (N3, N20). Local repairs A1–A4b below. The installed jobs run from the worktree, so the runtime must be consolidated before reinstalling (N29) |
| 3 | Autopilot rule did not match the backtested rule | **Fixed ✔** | Strategies are the backtester's classes |
| 4 | Missing data read as an exit | **Fixed ✔** | HOLD on incomplete data |
| 5 | Sold positions other workflows opened | **Fixed ✔** | Trusted-fill ownership ledger |
| 6 | Unfilled orders resubmitted; loop never synced fills | **Fixed ✔** | In-flight guard, broker sync, intent keys (N21) |
| 7 | Autopilot competed for maintenance tenure | **Fixed ✔** | Daemon hosts it; standalone paths retired (N4) |
| 8 | Duplicated, unbounded, world-readable logs | **Fixed ✔** | Role log private, redacted, rotated (`test_autopilot_outages`) |
| 9 | One broker blip lost the day | **Fixed ✔** | Daemon retries per session (bounded); CLI startup retry kept |
| 10 | Hand-written autopilot plist | **Fixed ✔** | Scheduled job retired; installer refuses it and warns about leftovers |
| 11 | Breaker self-heal regardless of market | **Fixed ✔** | Open markets only; never drift/loss/drawdown/global |
| 12 | Docs contradicted config | **Fixed ✔** | README, RUNBOOK, launchd README and `docs/autopilot.md` rewritten |
| 13 | Plaintext DB copies in the repo root | **Approval** | Ignored, untracked, 0600, unreferenced; options in A5 |
| 14 | No evidence gate before autopilot orders | **Implemented ◐ / Time** | Gate built and tested; real evidence needs A6, A7 and elapsed sessions |
| 15 | Release evidence predated the autopilot | **Fixed ✔** | Fresh evidence below; July reports stay as dated history |
| 16 | Very large modules | **Fixed ✔ (focused)** | `autopilot.py` split into 8 modules; `service.py` −590 lines (cancellation mixin). See "Not split" below |
| 17 | Round-numbered test files | **Fixed ✔** | 11 renamed; 364 cases identical |
| 18 | Cache key ignores `years` | **Disproved (latent)** | Only live callers use the cache (2y); backtest evidence uses its own cache dir |
| 19 | Repo name says Robinhood | **Open (decision)** | Naming only; next action is the operator's choice |

### Follow-up (2026-10-06) and this pass

| # | Issue | Status | Evidence |
|---|---|---|---|
| N1 | Migration head pin stale; CI red since 07-31 | **Fixed ✔** | CI migration-tests PASS on `df1e9f3` and later |
| N2 | Test-collection pins stale | **Fixed ✔** | Re-pinned with the verifier's own algorithm; every removed ID justified (see Verification) |
| N3 | Hard-coded checkout root | **Fixed ✔** | Per-user designation enforced by launcher, terminal, consolidation, installer and broker binding; gate forbids literal checkout paths |
| N4 | Autopilot ignored tenure loss | **Fixed ✔** | Daemon single owner; checks before cycle, intent, approval and evidence |
| N5 | Untradeable configs accepted | **Fixed ✔** | `autopilot_config_problems` at daemon start |
| N6 | Autopilot refresh path never exercised | **Fixed ✔** | End-to-end refresh test |
| N7 | Stage-one reference plist regression | **Fixed ✔ / superseded** | All machine-specific reference plists removed |
| N8 | Unbounded loop test could hang | **Fixed ✔ / superseded** | Loop retired |
| N9 | Full-suite runtime vs the 1800 s per-command cap | **Fixed ✔ (measured)** | Locally on `2fd5763`: full-tests 668 s, branch-coverage 1015 s. No timeout change needed |
| N10 | `.superpowers/` notes tracked | **Fixed ✔** | Untracked (83 local files kept); scan found no credentials |
| N11 | README claimed verification passed | **Fixed ✔** | Points to CI and fresh evidence |
| N12 | Live/backtest bar parity | **Fixed ✔** | Shared `signals/sessions.py`; equivalence tests |
| N13 | "Stale" worktree | **Disproved → Approval** | It moved with the repo, is clean, and holds the runtime database; `git worktree repair` (A2), never prune |
| N14 | Local node shim breaks the verifier | **Fixed ✔ (docs)** | RUNBOOK; clean-clone run uses trusted tools |
| N15 | macOS-only CA fixture broke Linux collection | **Fixed ✔** | Generated CA; CI full-tests PASS |
| N16 | ~450 operator tests never ran on Linux | **Fixed ✔** | Portable `stat` fake; CI full suite PASS |
| N17 | Screen sources frozen per process | **Fixed ✔** | `RefreshingScreenSource` (session-aware, locked, fail closed) |
| N18 | 32 known vulnerabilities in 6 locked packages | **Fixed ✔** | A8 approved; `50b4357`. `pip-audit 2.10.1`: no known vulnerabilities on CI (Linux) and locally (macOS). See Verification |
| N19 | Age-based cache let a 15:00 capture pass as the final bar after the close | **Fixed ✔** | Bar finality at 20:00 New York time; refresh when fetched before final |
| N20 | Designation scope overstated | **Fixed ✔** | Docs state the per-user limit; production broker binding enforces it |
| N21 | Duplicate-safe order intent | **Fixed ✔** | Intent keys equal `client_order_id`; replay, lost-response, rejected, cancelled and concurrent cases tested |
| N22 | Readiness gate and evidence | **Implemented ◐ / Time** | 34 gate tests; approval bound to the thresholds (N28). No genuine evidence exists yet |
| N23 | Credential-shaped test string failed the history gate | **Fixed ✔** | Caught by the gate before push; three unpushed local commits rewritten |
| N24 | Stale reference plists (app, daemon) | **Fixed ✔** | Removed; installer is the only source |
| N25 | Liveness test timed the limiter wait, not the probe: flaky under load (0.57 s vs 0.25 s) | **Fixed ✔** | `06fbdca` times only the probe and asserts login is still blocked; 25 sequential and 80 loaded runs passed |
| N26 | An oversize parametrized node ID made JUnit evidence malformed (verifier failed after 12 min) | **Fixed ✔** | `54429f6`: explicit ids, plus a collection guard mirroring the verifier's rule. Boundaries checked against the verifier itself (Verification) |
| N27 | Backup "inode replaced" test flaky on Linux: an unlink can hand back the same inode, leaving an identical state | **Fixed ✔** | `b366864`: the copy is created before the swap, and the test asserts the inode changed. Removing the inode binding from `ops/backup.py` still fails it |
| N28 | Approval not bound to the readiness thresholds: lowering `min_observed_sessions` or disabling release verification kept an approval valid | **Fixed ✔** | `500ba56`: `approval_fingerprint` covers `gate_fingerprint`; evidence keys unchanged. With the binding removed, the extended test fails |
| N29 | Repair plan A4 would have switched the runtime to another database: app, daemon, watchdog and backup run from `.worktrees/safety-foundation` | **Fixed ✔ (docs)** | RUNBOOK repair order now reads `status` first and consolidates before reinstalling; A4 split into A4a and A4b |
| N30 | Lease-renewal test flaky on CI: a 1 s lease left 0.9 s of tolerance for a process stall (200 instead of 409 on `500ba56`) | **Fixed ✔** | `f90332d`: a 3 s lease renewed every 0.1 s, follower at 3.5 s. An injected 1.1 s GIL stall fails the old timing 5/5 and passes the new 5/5; with renewal disabled the new timing fails 3/3 |

### Not split, with reasons

- **`scripts/check_release_safety.py`** (9.1k lines) is a single-file gate,
  deliberately run as `-I -S` with no package imports; splitting it changes
  its trust model.
- **`rules/repository.py`** (2.6k lines) is transaction-heavy, and no seam
  could be separated without risking transaction semantics in this pass.
- **`app/policy.py` and `app/main.py`** are pinned by the release gate's
  route-registration proofs.

### Known limits (recorded, not defects in this PR)

- The commit-state file's mode is not part of its identity check; the backup
  directory is private, so only the inode and contents bind it.
- The node-ID guard's control-character branch is defense in depth: pytest
  already escapes control characters in parametrize ids, so they reach a node
  ID only through a file name.
- Route concurrency leases are wall-clock leases. A process pause longer than
  the TTL minus the renewal interval (30 s − 10 s in production) lets a second
  request in. The lost renewal is logged (`route_lease_renewal_uncertain`), and
  interlocked mutation routes are marked uncertain, so they fail closed.
- Some other tests still assert wall-clock timing. Three intermittent CI
  failures (N25, N27, N30) were each reproduced and fixed when they appeared;
  none was a product defect.

## Verification

### Authoritative release check (isolated clean clone)

`scripts/verify_loopback_release.py`, run as `python3.11 -I -S` under
`env -i` with `PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`. Tools
resolved inside trusted roots: git 2.39.5 (`/usr/bin`), uv 0.11.28 and
node 26.5.0 (Homebrew Cellar), Python 3.11.15 (Homebrew Cellar).

| Stage | `2fd5763` (10-10 01:51–02:22Z) | `500ba56` (06:42–07:16Z) | `f90332d` (11:51–12:21Z) |
|---|---|---|---|
| compile | PASS (0.1 s) | PASS (0.1 s) | PASS (0.2 s) |
| migration-tests | PASS, 186 tests (39 s) | PASS, 186 (39 s) | PASS, 186 (51 s) |
| security-tests | PASS, 660 (24 s) | PASS, 660 (28 s) | PASS, 660 (57 s) |
| safety-tests | PASS, 142 (56 s) | PASS, 142 (61 s) | PASS, 142 (116 s) |
| frontend-tests | PASS, 188 (7 s) | PASS, 188 (7 s) | PASS, 188 (8 s) |
| full-tests | PASS, 5017, 1 allowed skip (668 s) | PASS, 5017, 1 skip (757 s) | PASS, 5017, 1 skip (727 s) |
| branch-coverage (≥ 90 %) | PASS (1015 s) | PASS (1082 s) | PASS (828 s) |
| static-gate | PASS (29 s) | PASS (25 s) | PASS (25 s) |
| **result** | **PASS** | **PASS** | **PASS** |

Every run reported migration head `20260730_0018` and the commit it was
started on.

Each test stage passes only if its collected node IDs hash to the pinned
manifest and the JUnit evidence rebuilds the same IDs. The only skip allowed
is `tests/test_alpaca_paper_integration.py::test_paper_account_and_quote`
(needs real credentials). Coverage is measured over `risk`, `orders`, `rules`,
`app.auth` and `security`; CI measured 93.68 % on `54429f6`.

**Manifests.** Full suite 5017, `sha256:02b19449…77e4ca`; migration 186,
`sha256:8a6d2f81…5c46ff`; security 660, safety 142 and frontend 188 are
unchanged from `main`. `500ba56` and `f90332d` changed no node ID: the
recomputed full manifest is identical. Against the `df1e9f3` pin, 7 node IDs were removed,
each with replacement coverage:

- `test_autopilot::test_loop_survives_ordinary_failures_but_stops_on_tenure_loss`
  (the loop was retired; the daemon monitor tests cover tenure loss);
- three `test_launch::test_launchd_discards_unbounded_stream_files[...]` cases
  for the removed reference plists (now
  `test_installer_generates_bounded_jobs_for_the_designated_root`);
- `test_runtime_composition::test_launchd_installer_generates_only_bounded_stream_jobs`
  (moved to the installer harness);
- `test_runtime_composition::test_launchd_installer_schedules_autopilot_only_on_request`
  and `…rejects_malformed_autopilot_time` (the schedule was retired; now
  `test_installer_refuses_the_retired_autopilot_schedule` and
  `…warns_about_a_leftover_autopilot_job`).

Nine IDs were re-identified with explicit ids: seven
`test_installation::test_malformed_designations_are_untrusted[...]` and two
`test_release_static::test_release_static_gate_rejects_hardcoded_checkout_paths[...]`.
The eleven file renames are pure renames (364 cases identical).

### Linux CI (`.github/workflows/ci.yml`)

CI checks out the PR merge ref, so each run verifies the head merged into
`main` (`b92ec3f`, unchanged during this pass).

| Run | Head | Result | Notes |
|---|---|---|---|
| 37817984410 | `df1e9f3` | failure | Verifier PASS; `pip-audit` found the 32 vulnerabilities (N18) |
| 38007218286 | `c4a5518` | cancelled | Superseded by a push |
| 38008461832 | `54429f6` | failure | full-tests PASS; branch-coverage hit the inode flake (N27) |
| 38014672788 | `50b4357` | cancelled | Superseded by `b366864` |
| 38016155854 | `b366864` (merge `dfdcbb5`) | **success** | Verifier: all 8 stages PASS. `uv pip check`: 84 packages compatible. `pip-audit`: no known vulnerabilities. Migrations to `20260730_0018`, mock safety drill and gitleaks (27 commits, no leaks) PASS |
| 38031857762 | `500ba56` | failure | full-tests: `test_handler_longer_than_initial_lease_is_renewed_without_overlap` (N30); 5015 other tests passed. The same commit passed the local verifier |
| 38049849353 | `f90332d` (merge `ab1e5e1`) | **success** | Verifier: all 8 stages PASS. `uv pip check`: 84 packages compatible. `pip-audit`: no known vulnerabilities. Migrations to `20260730_0018`, mock safety drill and gitleaks (29 commits, no leaks) PASS |

### Verification environment

- **Clean clone.** A `git clone` of the local repository into a private
  scratch directory, checked out detached at the commit under test. No `.env`,
  credentials, databases or personal notes were copied. Its ignored files are
  outputs of the runs: two empty SQLite files (no tables) and mock-drill logs.
- **Virtual environment.** Copied from the operator's venv because the offline
  uv cache lacked the locked pyarrow 25 wheel. The stale editable install was
  removed and only the approved upgrades were synced. On `f90332d`:
  `uv lock --check` passes, `uv sync --frozen` with CI's verifier flags
  (`--no-install-project --no-build --no-config`) reports "Would make no
  changes", `uv pip check` passes, pyarrow 25.0.0 and cryptography 50.0.2
  import, and neither the project nor `httpx2-jsfetch` is installed (as in CI's
  verifier step).
- **Difference from a fresh install.** Console-script shebangs in the copied
  venv still name the old checkout path, which no longer exists. The verifier
  never runs a console script (every command is `uv run --no-sync python -m
  …`), so they are inert. CI is the fresh-install evidence.
- **Difference from Linux CI.** CI uses uv's managed Python 3.11.15 and
  root-owned copies of uv and node, and installs 83 packages. The three extra
  ones (`jeepney`, `secretstorage`, `greenlet`) are Linux- or x86-only.

### Dependencies

`main..f90332d` changes `cryptography>=48.0.1,<49` to `>=50.0.0,<51` and, in
`uv.lock`, exactly these packages (one commit, `50b4357`):

| Package | `main` | Now |
|---|---|---|
| anyio | 4.14.1 | 4.15.1 |
| cryptography | 48.0.1 | 50.0.2 |
| httpcore2 | 2.9.1 | 2.13.1 |
| httpx2 | 2.9.1 | 2.13.1 |
| pyjwt | 2.13.0 | 2.15.1 |
| urllib3 | 2.7.0 | 2.8.0 |
| httpx2-jsfetch | — | 1.0, only for `python ≥ 3.12 and sys_platform == 'emscripten'`; never installed here |

`pip-audit 2.10.1`, the tool and version CI pins:

- CI, Linux, on `b366864` (02:49Z) and `f90332d` (run 38049849353): no known
  vulnerabilities, 83 third-party packages; the project itself is not on PyPI.
- Local, macOS, on `500ba56` (06:42Z) and `f90332d` (16:42Z), same lock: no
  known vulnerabilities, 80 packages.
- Not covered by either: Windows-only `colorama`, `pywin32`,
  `pywin32-ctypes`, which neither platform installs.

### Guards rechecked

- **Node-ID guard vs the verifier** (driving the repository's conftest and the
  verifier's own `_junit_nodeid`): exactly 4096 characters passes and
  round-trips through JUnit; 4097 is rejected at collection. The limit
  applies to the escaped ID: one that is 4094 characters raw but 4097 escaped is
  rejected, and the escaped-4096 case is accepted.
- **Inode test (N27), approval binding (N28) and lease renewal (N30):**
  mutation-tested as above. The route-policy file (141 tests) passes, and the
  renewal test passed 10 of 10 under eight CPU-bound processes.
- **Freshness, ownership and idempotency:** `test_sessions`, `test_marketdata`,
  the five `test_autopilot*` files, `test_runtime_tenure`, `test_installation`,
  `test_launchd_installer` and the liveness test: 262 passed on `b366864`. The
  five autopilot files (105 tests) passed again on `500ba56`.
- **Release-gate tests:** 457 passed with a trusted `PATH`. With this
  workspace's blocking `node` shell function first on `PATH`, 46 fail, the
  known local artifact (C1), not a product failure.

## Approval requests

Nothing below has been done, except A8. Each needs a separate, explicit
approval. Repairing paths never enables order execution: the autopilot's mode
is configuration, and paper orders also need the readiness gate. Paths are
for the checkout at `/Users/avi/Desktop/project/robinhood/trading-assistant`
(`$R`). **Order:** stop jobs (A4b's backup and uninstall), A3, A2, A1, A4a,
then A4b's install.

| ID | Target | Action | Modifies | Risk | Rollback | Verified by |
|---|---|---|---|---|---|---|
| A1 Designate | `~/Library/Application Support/trading-assistant/installation-root` | `$R/.venv/bin/python -I $R/src/trading_assistant/installation.py designate` (runs as a file, so it works before the venv repair) | Creates a 0700 dir, a 0600 record and a lock file | Low: makes `$R` the only checkout that may bind the broker or install jobs | Delete the record file | `… installation.py status` shows `designated: $R` |
| A2 Worktree | `$R/.worktrees/safety-foundation` (clean; tip `51ec49b` is on origin) | Back up `$R/.git/worktrees/safety-foundation/gitdir` and `$R/.worktrees/safety-foundation/.git`, then `git -C $R worktree repair $R/.worktrees/safety-foundation` | Those two one-line files | Low: link files only; the worktree's databases are untouched | Copy both files back | `git worktree list` shows no `prunable` |
| A3 Venv | `$R/.venv` (editable install and shebangs point at the old path) | `mv $R/.venv $R/.venv.moved-<ts>` then `uv sync --frozen --all-extras --dev` (**downloads** missing wheels, e.g. pyarrow) | `$R/.venv` | Low: rebuilt from `uv.lock` | `rm -r $R/.venv && mv $R/.venv.moved-<ts> $R/.venv` | `installation.py check --project $R --venv-python $R/.venv/bin/python` exits 0 |
| A4a Consolidate | Runtime database `$R/.worktrees/safety-foundation/trading_assistant.db` → `$R/trading_assistant.db` | With every runtime stopped, after A1–A3: `uv run python -m trading_assistant.ops.runtime_consolidation --source-root $R/.worktrees/safety-foundation --destination-root $R` | Reads the backup key from Keychain; writes and verifies encrypted backups of both databases; replaces `$R/trading_assistant.db` | **Medium-high.** `$R`'s current database (last written 2026-10-05 by the retired autopilot job) then survives only in its encrypted backup. Any paper orders or positions that job recorded become unknown to the runtime, and startup reconciliation may fail closed until they are reviewed | No one-command rollback. Both encrypted backups are kept; restoring `$R`'s previous database needs the reviewed restore procedure the RUNBOOK describes under migration recovery (decrypt to private staging, verify, install under maintenance tenure). Do not keep a plaintext copy instead (#13) | Receipt `status`; app preflight; `installation.py status` |
| A4b launchd | 5 installed jobs, all exit 78: app, daemon, watchdog and backup (from the worktree) and the retired autopilot | Back up `~/Library/LaunchAgents/com.trading.*.plist`, `$R/scripts/launchd/uninstall.sh`; after A4a, preflight, then `$R/scripts/launchd/install.sh` | LaunchAgents: app, watchdog and backup reinstalled from `$R`; daemon and autopilot not | Medium: the app starts serving on localhost:8020 (human-gated) and backups resume | `uninstall.sh`, copy the plist backups back, `launchctl bootstrap` each | `launchctl list`, `installation.py status` shows no stale jobs, `/health/live` |
| A5 DB copies | `$R/trading_assistant.db.{legacy,tripped,breaker}-…`; also the worktree's `trading_runtime.db` (last written 2026-07-26, unreferenced by current code) and its four `.pre-migration.bak` files | Option (a) keep; (b) `mkdir -m 700 $R/.local/db-snapshots && mv` them there; (c) delete after confirming encrypted backups suffice | Those files | (b) none; (c) irreversible | (b) move back; (c) none | `ls`; the app only uses `trading_assistant.db` |
| A6 Backtest evidence | Alpaca market data (credentials) | Stop app and daemon, then `uv run python -m trading_assistant.autopilot backtest` | Adds one `autopilot.backtest` audit event | Low: market-data reads only, no orders | Evidence is append-only; a newer run supersedes it | `autopilot readiness` passes `backtest_evidence` |
| A7 Observation | Daemon in `observe` mode | Follow the RUNBOOK "Observation procedure" | Adds `autopilot.cycle` evidence each session; no orders | Low: the daemon uses broker credentials for reads and order sync | Stop the daemon | Readiness report counts sessions |
| A8 Dependencies (N18) | `pyproject.toml`, `uv.lock` | **Approved and done** (`50b4357`) | — | — | `git revert 50b4357` | CI and local `pip-audit` above |

## Readiness

| Area | State | What is missing |
|---|---|---|
| **Code and CI** | **Ready for review** | Final code commit `f90332d` passed the authoritative release check in a clean clone and Linux CI (verifier, dependency audit, migrations, mock drill, secret scan). CI on the documentation-only head is recorded in the PR. Merging is your decision |
| **Local installation** | **Not ready** | No designation (A1); the venv imports the old path (A3); worktree links point at the old path (A2); the runtime lives in the worktree and must be consolidated (A4a); all five LaunchAgents exit 78 (A4b) |
| **Dry-run observation** | **Not started** (time-dependent) | Needs the local installation repaired and the daemon started in `observe` mode (A7). Then ≥ 20 clean sessions spanning ≥ 28 calendar days must genuinely elapse. No observation evidence exists today: `autopilot readiness` reports no report yet |
| **Paper trading (autopilot)** | **Blocked by the readiness gate** | No backtest evidence (A6), no observations (A7 plus time), no release evidence configured for the running commit, no operator approval. Human-approved paper orders through the app are unaffected by the gate |
| **Live trading** | **Unsupported** | Rejected at startup and by the gate; there is no path to enable it |
