# Task 10 release-safety evidence

Date: 2026-07-26

Branch: `codex/safety-foundation`

Original Task 10 base: `c876448df37b7d71aecfa110447e7b8c404e46aa`

First correction review base:
`7a42dd2142ae891512c677432b24e25a2c7b61e2`

Second correction review base:
`d7bf50ad4878dc4f54219eb2a8d25f5ef71ac362`

## Final result

The deterministic release gate passes. The release remains Alpaca paper-only,
human-approved, autoexecute-off, bracket-preference-off, one-attempt, and
acceptance-unknown-no-retry. No credentialed provider pass is claimed.

## Second review correction

The second independent review found that credentialed evidence still trusted a
construction-time target, compensation could begin without confirmed terminal
truth for the original, and source fingerprinting was not descriptor-bound.
The correction is test-first and fail-closed:

- only the exact initialized `AlpacaBroker` plus exact SDK `TradingClient` can
  enter credentialed mode;
- the current SDK sandbox/base URL is dynamically re-derived at initial
  validation and immediately before every submit/cancel mutation;
- offline Alpaca-shaped doubles run only in mock mode and can never emit
  `alpaca_paper:passed`;
- compensation requires two stable identity-verified terminal observations of
  the original; failed or unconfirmed cancellation submits no opposite order;
- the source parent is securely traversed and held with directory descriptors,
  group/world-writable source parents and source symlinks are refused, and
  main/sidecar files are held with `O_NOFOLLOW` and `fstat`;
- the source URI is `mode=ro&nofollow=1`, pathname-to-held-inode identity is
  checked immediately before connect, immediately after connect, and after
  backup, and held-file fingerprints must remain stable;
- obvious periodic operator tokens now fail; successful preflight wording says
  the check is basic format/placeholder validation, not entropy proof.

The remaining threat boundary is explicit: no user-space sequence can prove
immunity to a malicious process with the same OS principal repeatedly swapping
paths between individual system calls. Release evidence requires no untrusted
concurrent same-user process.

## Strict TDD correction evidence

Every correction began with a behavior test and an observed failure.

### RED

1. SDK-derived paper identity:

   ```text
   uv run pytest tests/test_alpaca_broker.py::test_execution_target_is_derived_from_actual_sdk_client_and_immutable tests/test_safety_drill.py::test_credentialed_mode_refuses_unverified_execution_target_before_copy_or_access tests/test_safety_drill.py::test_credentialed_validation_refuses_uninitialized_alpaca_broker -v
   ```

   Result: `5 failed`. The broker exposed no immutable SDK-derived target and
   unsafe targets were not rejected at the required boundary.

2. Read-only/private/race-resistant online copy:

   ```text
   uv run pytest tests/test_safety_drill.py::test_source_backup_connection_is_read_only_and_preserves_main_wal_shm tests/test_safety_drill.py::test_online_copy_quotes_special_characters_in_read_only_source_uri tests/test_safety_drill.py::test_source_main_wal_shm_identity_is_unchanged_when_a_gate_fails tests/test_safety_drill.py::test_refuses_nested_existing_path_beneath_symlink_component tests/test_safety_drill.py::test_refuses_group_or_world_writable_destination_parent tests/test_safety_drill.py::test_copy_temp_is_private_regular_single_link_before_sqlite_connect tests/test_safety_drill.py::test_temp_symlink_swap_is_refused_without_touching_victim -v
   ```

   Result: `7 failed`. A follow-up active-WAL command initially had `2 failed,
   1 passed`; the corrected hot-journal and closed-WAL fail-before-connect
   fixtures also observed their required RED states.

3. Fill attribution, outer cleanup, real restart, OCO competition, and quote
   sanity:

   ```text
   uv run pytest tests/test_safety_drill.py::test_credentialed_mode_compensates_only_its_adverse_fill tests/test_safety_drill.py::test_credentialed_mode_refuses_unrelated_or_masked_position_drift_before_compensation tests/test_safety_drill.py::test_credentialed_mode_requires_exact_initial_fill_activity_before_compensation tests/test_safety_drill.py::test_cleanup_cancels_validated_tagged_remote_while_local_acceptance_is_stale tests/test_safety_drill.py::test_outer_cleanup_runs_for_base_exception_after_broker_mutation tests/test_safety_drill.py::test_credentialed_mode_refuses_quote_without_nonmarketable_sane_limit tests/test_safety_drill.py::test_crash_gate_disposes_and_reconstructs_before_identity_reconciliation tests/test_safety_drill.py::test_oco_gate_competes_two_independent_repositories_in_bounded_threads -v
   ```

   Result: `5 failed, 4 passed`.

4. Static escapes and preflight:

   ```text
   uv run pytest tests/test_launch.py::test_preflight_app_secret_quality_is_independent_of_provider_credentials tests/test_launch.py::test_preflight_needs_me_is_not_ready_and_nonzero tests/test_release_static.py::test_release_static_gate_rejects_negative_fixtures -v
   ```

   Result: `7 failed`.

5. Delayed and nonterminal compensation:

   ```text
   uv run pytest tests/test_safety_drill.py::test_compensation_is_boundedly_reconciled_to_terminal_broker_truth -v
   uv run pytest tests/test_safety_drill.py::test_nonterminal_compensation_is_canceled_but_never_claimed_safe -v
   ```

   Results after correcting the delayed-fill fake: `1 failed` and `1 failed`.

6. Final review points—known-symbol cleanup, per-order provider isolation, and
   OCO worker lifetime:

   ```text
   uv run pytest tests/test_safety_drill.py::test_tagged_cleanup_validates_against_known_drill_symbol tests/test_safety_drill.py::test_tagged_cleanup_isolates_provider_failure_per_order tests/test_safety_drill.py::test_oco_workers_finish_before_later_gates_and_are_non_daemon -v
   ```

   Result: `4 failed`. A further missing-ticker case produced `1 failed,
   1 passed` before the explicit identity requirement was added.

### GREEN

- SDK target command: `5 passed`.
- Static/preflight correction command: `7 passed`.
- Delayed/nonterminal compensation plus injected reconciliation failures:
  all targeted tests passed.
- Final review-point command: `5 passed, 1 warning`.
- Complete safety drill:

  ```text
  uv run pytest tests/test_safety_drill.py -v
  ```

  Result: `49 passed, 1 warning in 4.34s`.

The retained original Task 10 TDD evidence at the review base included the
missing `ops.safety_drill` module, split-preflight behavior, runtime
`create_all`, missing static executable, persisted GTC, whole-share GTC, atomic
no-overwrite publication, and an initial genuine coverage failure at `87.53%`.

### Second correction RED

The second correction reused the independent review reproducer and added
focused behavior tests before completing production changes.

```text
uv run pytest -q tests/test_alpaca_broker.py tests/test_safety_drill.py tests/test_launch.py
```

Result: `13 failed`. Exact broker/SDK type enforcement correctly invalidated the
old offline fake's credentialed claims, while those tests still incorrectly
expected `alpaca_paper:passed`.

```text
uv run pytest -q tests/test_safety_drill.py -k 'source_beneath_symlink or final_source_symlink or writable_source_parent or main_replacement or sidecar_swap or held_main_identity' tests/test_launch.py::test_preflight_app_secret_quality_is_independent_of_provider_credentials
```

Result: four source-binding failures were observed: symlinked source traversal,
final source-symlink resolution, writable source parent, and held-main identity
mismatch were not yet refused. Main-path and WAL-sidecar replacement already
failed closed through the retained fingerprint check.

```text
uv run pytest -q tests/test_launch.py::test_preflight_app_secret_quality_is_independent_of_provider_credentials
```

Result: `1 failed`; `01234567` repeated four times incorrectly passed.

### Second correction GREEN

```text
uv run pytest -q tests/test_alpaca_broker.py tests/test_safety_drill.py tests/test_launch.py
```

Result: `212 passed, 1 warning`. The source race fixtures, dynamic target
mutation checks, zero-write submit/cancel refusals, mock-only offline broker,
stable-terminal compensation requirement, failed/unconfirmed-cancel cases,
delayed later fill, and periodic-token check all pass.

## Full deterministic suite and genuine branch gate

```text
uv run pytest
```

Result: `1421 passed, 1 skipped, 1 warning in 116.72s`.

```text
uv run pytest --cov=trading_assistant.risk --cov=trading_assistant.orders --cov=trading_assistant.rules --cov=trading_assistant.app.auth --cov-branch --cov-report=term-missing --cov-fail-under=90
```

Result: `1421 passed, 1 skipped, 1 warning in 182.55s`.

- Statements: `2834`
- Missed statements: `209`
- Branches: `866`
- Partial branches: `114`
- Combined branch coverage: `90.73%`
- Gate: PASS at the genuine `--cov-fail-under=90`
- No broad exclusions, coverage pragmas, or lowered threshold were added.

## Copy, crash, concurrency, fill, and cleanup evidence

- Credentialed mode requires the exact broker/SDK client types and dynamically
  validates the current `_sandbox` and `_base_url` before every mutation; only
  the exact official paper endpoint passes. Live, uninitialized, subclassed,
  sandbox-false, and URL-overridden targets fail before broker mutation.
- The primary and present sidecars are descriptor-held with `O_NOFOLLOW`,
  `fstat`, safe-parent checks, and repeated pathname identity verification. It
  opens via a quoted `file:` URI with `mode=ro&nofollow=1`, `uri=True`.
  Source writes through that connection fail. A normal active WAL database is
  supported when regular WAL and SHM sidecars already exist. Main/WAL
  inode/content and logical schema/state remain unchanged when no unrelated
  writer changes them; SHM identity/presence remains fixed while ephemeral read
  marks may change. A hot journal and a closed WAL-mode database lacking
  WAL/SHM fail before recovery or sidecar creation.
- Destination traversal rejects every symlink component and unsafe parents.
  Staging is `O_CREAT|O_EXCL|O_NOFOLLOW`, regular, link-count one, mode `0600`;
  publication is directory-relative and no-overwrite with post-link identity
  checks.
- The crash gate raises a drill-only `BaseException` after broker acceptance,
  leaves local state `SUBMITTING`, disposes the first engine, reconstructs a new
  container from the copy, reconciles by client ID, and proves one broker
  submission.
- Two independent repositories compete in non-daemon threads for different OCO
  siblings. Writer acquisition and both joins are bounded; no later gate runs
  while either worker is alive.
- Compensation requires stable identity-verified terminal truth for the
  original, exact tagged `BrokerFill` aggregation equal to broker cumulative
  `filled_qty`, and exact full-manifest drift. Failed/unconfirmed cancellation
  submits no compensation. Any compensation uses the normal persisted
  human-gated path, is reconciled to terminal truth, and must be opposite/exact
  with tagged fills.
- Cleanup runs from the outer mutation `finally`, validates each tagged order
  against the known drill symbol, isolates provider failures per order, cancels
  stale-local but remotely validated tagged IDs, and never touches pre-existing
  IDs. Unavailable evidence always keeps `safe` false.

## Migration rehearsal

Migrations `0001` through `0007` are byte-history unchanged from both the
original Task 10 base and correction review base. No schema change was needed;
explicit GTC remains persisted in the existing validated submission payload.

Fresh private rehearsal:

```text
upgrade: current='20260724_0007' head='20260724_0007' backup=none
status: current='20260724_0007' head='20260724_0007' backup=none
```

## Supported active-WAL mock drill

The final release mock ran while a separate SQLite writer held the migrated
source open in WAL mode with an uncheckpointed committed sentinel and existing
regular WAL/SHM. No unrelated writes occurred during the copy.

```json
{"auth_fail_closed": true, "breakers_persisted": true, "crash_recovered_without_duplicate": true, "details": ["mode:mock", "schema:current", "auth:fail_closed", "crash:reconstructed_once", "oco:single_terminal", "breakers:persisted_scoped_reset", "reconciliation:clean"], "oco_single_terminal": true, "reconciliation_clean": true, "safe": true, "schema_current": true}
```

Result: `mock_exit=0`. CI uses a separately created private non-WAL source;
the active-WAL source path is exercised by focused tests and this release
rehearsal. A closed WAL-mode source without both sidecars is an explicit
operational refusal, not a supported drill source.

## Preflight

The intentional missing-credential run used a fresh migrated private database
and a diverse local evidence token. It made no broker write, LLM call, or
notification; it may perform local reconciliation/audit/breaker startup repair.

```text
0 FAIL · 5 NEEDS-ME · 7 PASS · 1 SKIP
=> NOT READY — fix FAIL/NEEDS-ME items
preflight_exit=1
```

Low-diversity, known-placeholder, and obvious periodic `APP_API_TOKEN` values
fail. Passing is explicitly described as a basic format/placeholder check, not
entropy proof. `READY` is impossible while Alpaca or the selected LLM
credential is missing.

## Static, dependency, shell, and secret checks

- `uv run python scripts/check_release_safety.py`: PASS.
- Negative fixture tests catch aliased/runtime `create_all`, direct/aliased/
  dynamic `getattr` broker submissions, and all inline `on*=` HTML/JS handlers.
- Legitimate adapter/backtest boundaries remain allowed.
- `uv lock --check`: PASS, `78` packages resolved.
- `uv run python -m compileall -q src tests scripts`: PASS.
- `find scripts -type f -name '*.sh' -print0 | xargs -0 -n1 bash -n`: PASS.
- `config.yaml` and `.github/workflows/ci.yml` YAML parse: PASS.
- `git diff --check`: PASS.
- Gitleaks `8.24.3` archive checksum: PASS.
- Redacted post-commit full-history scan: `124 commits`, no leaks.
- Redacted current-directory scan: no leaks.
- CI retains `fetch-depth: 0` and `gitleaks/gitleaks-action@v2`.

## Warning triage

- `starlette=1.3.1` with supported `httpx2=2.9.1`; the prior Starlette/plain-
  httpx deprecation warning is absent.
- `alpaca-py=0.43.5`, `websockets=16.0`; the upstream
  `websockets.legacy` deprecation warning remains visible by design. No warning
  filter was added.

## Credentialed Alpaca paper result

Boolean-only credential check:

```json
{"alpaca_credentials_present": false}
```

Result: **NOT RUN — explicit credential-absence skip.**

Neither the provider integration test nor credentialed `--alpaca-paper` drill
was run because valid paper credentials were absent. No credentialed pass is
claimed. Offline Alpaca-shaped tests are behavioral evidence only; they do not
replace provider evidence.
