# Review follow-up — 2026-10-06

> **Superseded in part by
> [`2026-10-09-completion.md`](2026-10-09-completion.md).** Three points here
> no longer hold:
>
> * **N13 (wrong).** The worktree is **not stale**. It moved with the
>   repository and holds the runtime database the installed jobs used. `git worktree prune` would have
>   dropped git's link to a real worktree; the correct operation is
>   `git worktree repair`.
> * **N3, C3 (superseded).** "Move the checkout back" is no longer the fix. The hard-coded
>   root was replaced by a per-user designated installation, so the checkout
>   can live anywhere once designated.
> * **N4 (superseded).** The tenure fix below was replaced: the standalone autopilot was
>   retired and the daemon now hosts it.

This is stage two of [`2026-10-05-project-review.md`](2026-10-05-project-review.md).
Stage one found and fixed the autopilot's frozen data, strategy-name mismatch,
and ownership problems, then opened draft PR #3. This stage did four things:

1. rechecked every stage-one finding against the current tree and machine;
2. inspected what stage one never traced: CI end to end, the release
   verifier, the canonical-root anchors, the autopilot's tenure lifecycle, and
   its first-ever live data refresh;
3. fixed what could be fixed without your decisions;
4. verified each fix and recorded what is still open.

Legend: **Fixed ✔** fixed and verified · **Fixed ◐** implemented, partly
verified · **Partial** · **Open** unresolved · **Decision** needs the
operator.

## Consolidated issue register

### Stage-one findings, rechecked

| # | Issue | Area | Sev. | Status now | Evidence / acceptance check |
|---|---|---|---|---|---|
| 1 | Live bar cache never expired | `backtest/data.py`, `coingecko.py`, `analyst/live_features.py` | Critical | **Fixed ✔** | Unit tests for expiry; new end-to-end test refreshes a 90-day-old cache through the `paper-drill` role and the durable limiter |
| 2 | Checkout moved; LaunchAgents and `.venv` point at the old path | operator machine | Critical | **Decision** | All five jobs now exit `78`; the 2026-10-06 07:00 autopilot run could not `chdir`. Corrected remedy: see N3 |
| 3 | Autopilot `sma_crossover` ≠ backtester `sma_crossover` | `autopilot.py`, `strategies/` | High | **Fixed ✔** | `test_autopilot_strategies_are_the_backtested_classes` |
| 4 | Missing data read as "flat" → sell | `autopilot.py` | High | **Fixed ✔** | `test_hold_signal_never_exits`, `test_decision_holds_on_incomplete_data` |
| 5 | Sold plan- or human-owned positions | `autopilot.py` | High | **Fixed ✔** | Ownership tests (`never_sells_a_position_it_did_not_buy`, `sells_only_the_autopilot_share`) |
| 6 | No in-flight check; loop never synced fills | `autopilot.py` | High | **Fixed ✔** | `test_in_flight_order_blocks_a_second_submission`, `test_order_sync_failure_skips_the_cycle` |
| 7 | Autopilot (`paper-drill`) needs exclusive maintenance tenure → cannot coexist with app/daemon/MCP | `autopilot.py`, `bootstrap.py`, `ops/tenure.py` | High | **Decision** | Unchanged. Topology change (host in daemon vs new role) |
| 8 | Duplicate/unbounded/0644 logs, no decision reasons | `autopilot.py`, plist | Medium | **Fixed ◐** | Decision log tested; duplicate-handler removal verified by code inspection only, not in a live launchd run |
| 9 | One transient broker failure lost the day; degraded runs exited 0 | `autopilot.py` | Medium | **Fixed ◐** | Retry and exit-code paths unit tested; not exercised against a real broker outage |
| 10 | Hand-written autopilot plist | `scripts/launchd/` | Medium | **Fixed ✔** | Installer tests. Stage-two regression in the reference copy fixed (N7) |
| 11 | Self-heal regardless of market state | `autopilot.py` | Medium | **Fixed ✔** | `test_self_heal_leaves_closed_market_breakers_alone` |
| 12 | Docs contradicted the config | `README.md` | Medium | **Fixed ✔** | Plus N11 found and fixed this stage |
| 13 | Plaintext DB copies in the repo root | repo root | Medium | **Partial** | Now git-ignored; the three files still exist (deleting user data is your call) |
| 14 | Autopilot enabled without the evidence gate the README requires | process | Medium | **Decision** | Scope addition; not implemented |
| 15 | Release evidence docs predate the autopilot | `docs/release/` | Medium | **Partial** | README now labels them point-in-time and points at CI; regenerating operational evidence needs a credentialed operator run |
| 16 | Very large modules | `service.py` etc. | Low | **Open** | Optional refactor, not attempted |
| 17 | Round-named test files | `tests/` | Low | **Open** | Optional; renaming also forces a manifest re-pin |
| 18 | Cache key ignores `years` | `backtest/data.py` | Low | **No longer active** | Every live caller uses `years=2`; no backtest path reads this cache. Latent only |
| 19 | Repo name says Robinhood | GitHub | Low | **Decision** | Naming |

### Corrections to stage one

| # | Stage-one claim | What is actually true |
|---|---|---|
| C1 | 46 local `test_release_verifier.py` failures came from user-owned Homebrew `uv` | They come from `~/.rote/bin/node` shadowing the trusted `/usr/local/bin/node`. With it removed from `PATH`, 62/62 pass |
| C2 | "Release gate and CI" listed as a strength | `main`'s CI has failed on every push since 2026-07-31 (N1, N2) |
| C3 | Fix the moved checkout with `uv sync` + re-install from the new path | The project pins a canonical root; moving back is the consistent fix (N3) |

### New findings

| # | Issue | Area | Sev. | Status | Evidence / acceptance check |
|---|---|---|---|---|---|
| N1 | CI red on `main` since 2026-07-31: migration `20260730_0018` landed but `EXPECTED_MIGRATION_HEAD` stayed `20260729_0017` | `scripts/verify_loopback_release.py:33` | Critical | **Fixed ◐** | Re-pinned. New `test_expected_migration_head_tracks_the_repository_head`. Authoritative check is the CI `verification` job on PR #3 |
| N2 | Collection pins stale: migration 182→186, full 4253→4843. 557 tests on `main` were never accepted by the verifier | same file | High | **Fixed ◐** | Recomputed with the verifier's algorithm. The method reproduces all five old pins exactly at `37972f0`. Versus `main`, only three intentionally replaced autopilot tests disappear |
| N3 | Canonical root `/Users/avi/Desktop/robinhood/trading-assistant` is hard-coded in `scripts/operator.sh:32`, `ops/operator_terminal.py:21`, `ops/runtime_consolidation.py:50,1375`, and the release gate; the checkout now lives elsewhere, so the operator launcher and consolidation refuse to run | operator tooling | High | **Decision** | Recommended: move the checkout back (fixes N3, #2, and the venv with zero code change). Alternative: deliberately re-anchor all four sites plus tests |
| N4 | Autopilot never checked tenure ownership; the loop's `except Exception` would swallow `TenureLost` | `autopilot.py` | High | **Fixed ✔** | Checks at cycle start and before every order; `run_loop` re-raises. Mutation-tested: removing either guard fails the new tests |
| N5 | Configs that fail every cycle were accepted: crypto symbols (role cannot read CoinGecko), symbols outside the allowlist, notional above the per-order cap | `autopilot.py` | Medium | **Fixed ✔** | `autopilot_config_problems`; checked-in profile passes |
| N6 | The autopilot's live refresh path had never run in production (cache always hit), so outbound permission and the limiter path were unproven | `live_features.py`, `security/outbound.py` | Medium | **Fixed ✔** | `paper-drill` is allowed `alpaca.historical`; end-to-end refresh test passes |
| N7 | Stage-one regression: regenerated autopilot reference plist used the moved path | `scripts/launchd/com.trading.autopilot.plist` | Low | **Fixed ✔** | Now matches the canonical root like the other reference plists; `plutil -lint` OK |
| N8 | Stage-two test flaw caught before commit: an unbounded loop test would hang, not fail, on regression | `tests/test_autopilot.py` | Low | **Fixed ✔** | Bounded with `max_cycles`; the mutation now fails in about one second |
| N9 | Full-suite runtime vs the verifier's 1800 s per-command timeout: the last green verifier took 44 min for all eight commands, and the suite has grown 14% | CI | Medium | **Open (risk)** | Estimated about 20 of 30 min for the full step. Watch the PR #3 run; split the suite or raise the timeout if it nears the cap |
| N10 | 77 process-artifact files tracked under git-ignored `.superpowers/` | repo | Low | **Decision** | `git rm --cached -r .superpowers` would untrack them; history keeps them |
| N11 | README "Status" claimed deterministic verification passes | `README.md` | Medium | **Fixed ✔** | Now points at the CI job and labels the July reports point-in-time |
| N12 | Suspected: live daily bars may include the forming session bar while backtests use completed bars, a parity gap for a 10:00 ET run | `live_features.py` | Medium | **Suspected** | Not confirmed; needs one look at real Alpaca output for today's bar |
| N13 | Stale worktree registration for `.worktrees/safety-foundation` at the old path | git metadata | Low | **Decision** | `git worktree prune` |
| N14 | Local toolchain: a version-manager `node` shim makes the local verifier unusable | developer setup | Low | **Fixed ✔** (docs) | RUNBOOK documents the trusted-toolchain requirement |

## Fixes implemented this stage

| Fix | Files |
|---|---|
| Re-pin verifier migration head and the migration/full manifests; fixture follows the constant; drift test | `scripts/verify_loopback_release.py`, `tests/test_release_verifier.py` |
| Tenure checks at cycle start and before each order; `run_loop` that stops on `TenureLost`; startup config validation | `src/trading_assistant/autopilot.py` |
| Tests: tenure loss (start, mid-cycle, per-order), loop behavior, config problems, end-to-end stale-cache refresh | `tests/test_autopilot.py` |
| Reference plist anchored at the canonical root | `scripts/launchd/com.trading.autopilot.plist` |
| Docs: re-pin procedure and local toolchain (RUNBOOK); README status, autopilot guards; launchd canonical root; stage-one corrections | `docs/RUNBOOK.md`, `README.md`, `scripts/launchd/README.md`, `docs/reviews/2026-10-05-project-review.md` |

## Verification

Recorded in the PR #3 description with exact commands and results. This file
lists only what each fix's acceptance check is; it does not claim a result
that was not run.

## Decisions needed

1. **Checkout location (N3, #2):** move it back to
   `/Users/avi/Desktop/robinhood/trading-assistant` (recommended), or re-anchor
   the four canonical-root sites.
2. **Autopilot runtime (#7):** host the cycle in the daemon (recommended) or
   add a dedicated runtime role.
3. **Evidence gate (#14):** require a stored backtest of `autopilot.strategy`
   and N weeks of dry-run logs before `enabled: true` takes effect?
4. **Housekeeping (#13, N10, N13):** delete or encrypt the plaintext DB
   copies; untrack `.superpowers/`; prune the stale worktree.
5. **Bar parity (N12):** confirm whether to drop the in-progress session bar
   for autopilot decisions.
