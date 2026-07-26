# Task 10 release-safety evidence

Date: 2026-07-26
Branch: `codex/safety-foundation`
Base: `c876448df37b7d71aecfa110447e7b8c404e46aa`

## TDD RED evidence

1. Initial acceptance:
   - Command: `uv run pytest tests/test_safety_drill.py -v`
   - Expected RED: collection stopped with
     `ModuleNotFoundError: No module named 'trading_assistant.ops.safety_drill'`.
2. Refusal/report/CLI behavior:
   - Incremental tests first failed on missing `SafetyDrillError` and missing
     `main`; implementations followed those failures.
3. Preflight split and read-only behavior:
   - Command:
     `uv run pytest tests/test_launch.py -k 'preflight_' tests/test_startup_schema.py::test_preflight_reports_outdated_schema_without_mutating_it -v`
   - RED: `5 failed, 2 passed`; required paper-only, dangerous-switch,
     app-secret, LLM-configuration, notification-configuration, and separate
     schema results did not exist.
4. Runtime `create_all` prohibition:
   - Command:
     `uv run pytest tests/test_startup_schema.py::test_runtime_package_never_calls_create_all -v`
   - RED: one offender, `src/trading_assistant/db/models.py`.
5. Executable static gate:
   - Command: `uv run pytest tests/test_release_static.py -v`
   - RED: `scripts/check_release_safety.py` did not exist.
6. Persisted GTC behavior:
   - Command:
     `uv run pytest tests/test_safety_drill.py::test_credentialed_mode_preserves_preexisting_manifest_and_cleans_tagged_order tests/test_alpaca_broker.py::test_submit_equity_limit_defaults_to_day_but_explicit_gtc_is_preserved -v`
   - RED: two collection errors because `OrderTimeInForce` did not exist.
7. Whole-quantity Alpaca GTC compatibility:
   - Command:
     `uv run pytest tests/test_safety_drill.py::test_credentialed_mode_preserves_preexisting_manifest_and_cleans_tagged_order -v`
   - RED: expected `qty == 1`, observed fractional `0.013021`.
8. No-overwrite publication race:
   - Command:
     `uv run pytest tests/test_safety_drill.py::test_copy_publish_refuses_a_racing_overwrite -v`
   - RED: the drill did not refuse a destination created between validation and
     publication.
9. Initial exact combined branch gate:
   - Result before added behavioral branch tests: `87.53%`, `2822` statements,
     `860` branches, `1310 passed, 1 skipped, 2 warnings`; the required 90% gate
     failed as expected.

## GREEN evidence

- `uv run pytest tests/test_safety_drill.py -v`
  - `19 passed, 1 warning in 2.89s`.
- Preflight targeted command from RED:
  - `7 passed, 29 deselected in 0.46s`.
- Runtime `create_all` test:
  - `1 passed in 0.10s`; test fixtures now own the only metadata creation.
- Static gate:
  - `1 passed in 0.30s`; executable result:
    `release static checks: PASS`.
- Persisted GTC/ordinary DAY targeted tests:
  - `2 passed`; credentialed request reconstructs GTC from persisted
    `submission_payload_json`, while ordinary equity LIMIT stays DAY.
- Whole-quantity credentialed request:
  - Targeted credentialed and deterministic mock tests: `2 passed`.
- Atomic no-overwrite publication:
  - `1 passed`; hard-link publication fails closed if the destination races into
    existence and does not overwrite it.

## Full deterministic tests and genuine branch gate

Command:

```bash
uv run pytest
```

Result: `1369 passed, 1 skipped, 1 warning in 199.68s`.

Exact required branch command:

```bash
uv run pytest --cov=trading_assistant.risk \
  --cov=trading_assistant.orders \
  --cov=trading_assistant.rules \
  --cov=trading_assistant.app.auth \
  --cov-branch --cov-fail-under=90
```

Result: `1369 passed, 1 skipped, 1 warning in 319.56s`.

- Statements: `2834`
- Missed statements: `218`
- Branches: `866`
- Partial branches: `115`
- Combined branch coverage: `90.41%`
- Gate: PASS at genuine `--cov-fail-under=90`
- No broad exclusions, coverage pragmas, or lowered threshold were added.

## Static, lock, compile, shell, and secret gates

- `git diff --check`: PASS.
- `uv lock --check`: PASS, `78` packages resolved.
- `uv run python -m compileall -q src tests scripts`: PASS.
- `find scripts -type f -name '*.sh' -print0 | xargs -0 -n1 bash -n`: PASS.
- `uv run python scripts/check_release_safety.py`: PASS.
- Machine checks cover:
  - paper + Alpaca production profile;
  - autoexecute false;
  - bracket preference false;
  - LLM fallback null;
  - no runtime `Base.metadata.create_all`;
  - approved broker-submission source paths only;
  - no browser `localStorage`, inline handlers, or API-key headers;
  - no unofficial Robinhood dependency/import;
  - no tracked secret-bearing filenames.
- Existing session/security tests cover every non-liveness route, mutation CSRF,
  recent reauthentication, and fail-closed sessions.
- Existing submission/reconciliation tests plus the Task 10 crash drill cover
  one-attempt submission, unknown-acceptance exposure reservation, and client-ID
  recovery.
- Existing rule tests plus the Task 10 OCO gate cover group leases and
  single-terminal behavior.
- Full-history gitleaks CI job remains unchanged with `fetch-depth: 0`.
  The local `gitleaks` binary was not installed, so no local gitleaks pass is
  claimed.

## Migration rehearsal and status

No Alembic files changed. Frozen migrations `0001` through `0007` remain
unchanged; no Task 10 schema migration was needed. Explicit GTC is persisted in
the existing validated `submission_payload_json` order field.

A fresh private temporary source database was used:

```text
upgrade: current='20260724_0007' head='20260724_0007' backup=none
status: current='20260724_0007' head='20260724_0007' backup=none
```

The ignored default worktree database reported `current=None` and was
intentionally not migrated or otherwise touched.

## Preflight result

Preflight ran against the fresh migrated temporary source with a non-production
local evidence token:

```text
PASS paper-only Alpaca configuration    mode=paper broker=alpaca
PASS dangerous switches OFF             all disabled
PASS database schema current
PASS DB WAL mode                        journal_mode=wal
PASS kill switches                      all clear
0 FAIL / 5 NEEDS-ME / 7 PASS / 1 SKIP
=> READY
```

The five `NEEDS-ME` results were the deliberately absent Alpaca reads,
broker/local reconciliation, and configured Gemini credential. No broker write,
LLM call, or external notification occurred.

## Offline mock drill JSON

```json
{"auth_fail_closed": true, "breakers_persisted": true, "crash_recovered_without_duplicate": true, "details": ["mode:mock", "schema:current", "auth:fail_closed", "crash:recovered_once", "oco:single_terminal", "breakers:persisted_scoped_reset", "reconciliation:clean"], "oco_single_terminal": true, "reconciliation_clean": true, "safe": true, "schema_current": true}
```

The success/failure tests compare primary bytes, SQLite schema, and persisted
state before/after. The primary remains unchanged. Destination refusals cover
relative, primary, alias, symlink, hardlink, existing, racing overwrite,
non-SQLite, in-memory, and invalid-SQLite cases.

## Alpaca compatibility and warning triage

- Starlette `1.3.1` now uses its supported `httpx2` test dependency. The final
  suite no longer emits the Starlette/plain-httpx deprecation warning.
- Lock remains on latest Alpaca-py `0.43.5`.
- Alpaca-py still imports `websockets.legacy`; exactly one upstream deprecation
  warning remains visible. No warning filter was added.
- Alpaca's official order matrix documents GTC support for whole-quantity equity
  LIMIT orders and DAY-only support for fractional equity LIMIT orders:
  [Orders at Alpaca](https://docs.alpaca.markets/us/docs/orders-at-alpaca) and
  [Fractional Trading](https://docs.alpaca.markets/us/v1.1/docs/fractional-trading).
  Therefore credentialed mode persists GTC and submits exactly one share; mock
  mode remains a small deterministic DAY order.

## Credentialed Alpaca paper result

Credential presence check:

```text
alpaca_credentials_present=False
```

Result: **NOT RUN — explicit credential-absence skip.**

Neither `tests/test_alpaca_paper_integration.py -v` nor the credentialed
`--alpaca-paper` drill was run separately because valid Alpaca paper credentials
were absent. No credentialed pass is claimed. Offline Alpaca-shaped tests verify
unsafe-config refusal, pre-existing manifest preservation, one whole-share GTC
request, tagged cancellation, and exact adverse-fill compensation, but these are
not represented as provider evidence.
