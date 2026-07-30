# Task 9 implementation report

## Status

DONE

## Recovery

- Preserved the uncommitted implementation based on `cb62d76`; no reset,
  checkout, or discarded path was used.
- Reviewed the existing Task 9 diff and retained the separate
  `cb62d76` fake-clock fix.

## RED

Command:

```text
uv run pytest tests/test_bootstrap.py tests/test_launch.py
tests/test_watchdog.py tests/test_ops.py tests/test_monitor.py
tests/test_external_accounts.py tests/test_llm_backends.py
tests/test_startup_schema.py tests/test_alpaca_broker.py
tests/test_migrations.py tests/test_mcp_tools.py tests/test_security.py -v
```

Result:

```text
1 failed, 422 passed, 2 warnings
```

Failure:

```text
tests/test_monitor.py::test_runtime_reconciliation_failure_trips_global_breaker_once
expected generation 1, observed generation 2
```

Root cause: `_core_cycle()` durably tripped the global breaker before rule
evaluation, then `run()` caught the same untyped failure and tripped it a
second time. The breaker service intentionally advances generation on every
trip.

## GREEN

Minimal regression command:

```text
uv run pytest
tests/test_monitor.py::test_runtime_reconciliation_failure_trips_switches_before_rules
tests/test_monitor.py::test_runtime_reconciliation_failure_trips_global_breaker_once
tests/test_monitor.py::test_daemon_does_not_retry_a_failed_mutating_cycle -v
```

Result:

```text
3 passed
```

Fix: raise a private `RuntimeError` subtype after the inner reconciliation
path has already latched safety; the outer monitor handler still logs and
exits, but does not repeat the breaker mutation. Other cycle failures continue
to trip the breaker exactly once.

Focused Task 9 command:

```text
uv run pytest tests/test_bootstrap.py tests/test_launch.py
tests/test_watchdog.py tests/test_ops.py tests/test_monitor.py
tests/test_external_accounts.py tests/test_llm_backends.py
tests/test_startup_schema.py tests/test_alpaca_broker.py
tests/test_migrations.py tests/test_mcp_tools.py tests/test_security.py -v
```

Result:

```text
423 passed, 2 warnings
```

## Full verification

Command:

```text
uv run pytest
```

Result:

```text
1251 passed, 1 skipped, 2 warnings in 106.97s
```

Additional successful checks:

```text
uv lock
uv lock --check
uv sync --all-extras
uv run python -m compileall -q src tests
bash -n scripts/start.sh scripts/stop.sh scripts/launchd/install.sh scripts/launchd/uninstall.sh
git diff --check
```

Integrity scans confirmed:

- Migrations `20260724_0001` through `20260724_0006` are byte-identical to
  `cb62d76`.
- Task 8 static assets are byte-identical to `cb62d76`.
- Paper mode remains selected.
- Auto-execution and automatic bracket preference remain false.
- Cross-provider fallback remains null.
- Production roots contain no `create_all()`.
- `robin_stocks`, `pyotp`, Robinhood runtime classes, and Robinhood credential
  fields are absent from runtime configuration, dependencies, and source.

## Concerns

- No functional blocker remains in automated verification.
- Two pre-existing third-party deprecation warnings remain:
  `websockets.legacy` and Starlette's current `httpx` TestClient integration.

## Independent-review fix round

### RED

The first review regression command covered injected-container identity,
injected-secret reuse, production runtime logs, coherent broker-contact health,
and post-commit HTTP/MCP audit failure behavior:

```text
uv run pytest
tests/test_bootstrap.py::test_create_app_builds_missing_agent_from_exact_injected_container
tests/test_bootstrap.py::test_automatic_planning_and_screen_use_exact_injected_secrets
tests/test_bootstrap.py::test_production_runtime_role_installs_private_bounded_log
tests/test_launch.py::test_operational_health_excludes_contact_committed_after_safety_snapshot
tests/test_launch.py::test_operational_health_never_clamps_future_contact_to_zero
tests/test_mcp_tools.py::test_mcp_proposal_success_survives_supplementary_audit_failure
tests/test_mcp_tools.py::test_mcp_rule_mutation_receipts_preserve_channel_actor_and_request
-q
```

Result: `8 failed, 1 passed`. The failures proved:

- an injected container with no agent called `build_default_stack`;
- automatic planning constructed fresh ambient `Secrets`;
- `prepare_database_runtime` had no production-role log contract;
- broker-contact evidence was read after the safety snapshot and future
  evidence was clamped to age zero;
- an MCP proposal committed, then raised when its supplementary audit failed.

The HTTP post-commit regression separately failed with `503` after the
authoritative approval had submitted exactly once.

The mandated provenance matrix initially produced `3 failed, 13 passed`:
failed MCP rule creation had no receipt, and rule failure results were not
distinguished from success. HTTP approval, rejection, cancellation, reset,
panic, and backtest success/failure rows already passed.

The final production-root RED command produced `5 failed`:

```text
uv run pytest
tests/test_bootstrap.py::test_app_daemon_and_mcp_default_roots_pass_distinct_runtime_roles
tests/test_launch.py::test_operations_domain_success_survives_supplementary_audit_failure
tests/test_launch.py::test_launchd_discards_unbounded_stream_files
-q
```

It proved daemon/MCP omitted their roles, operations panic/reset propagated a
supplementary receipt failure after committing, and launchd still wrote
unbounded app/daemon stream files.

### GREEN

The first corrected regression set passed:

```text
10 passed
```

The expanded composition, health, post-commit, and provenance set passed:

```text
26 passed
```

The production-root and launchd set passed:

```text
5 passed
```

The app/daemon/MCP role logs are separate bounded rotating files. Both checked-in
launchd plists and the installer-generated app/daemon plists send inherited
streams to `/dev/null`; `scripts/start.sh` does the same, so those streams cannot
grow around the rotating handlers. The files and directory remain owner-only.

Supplementary HTTP, MCP, and operations receipts are now best-effort after an
authoritative domain transaction. Their failure emits only a stable action and
request ID and never changes a committed mutation into a retryable transport
failure. Atomic domain audit rows remain unchanged and authoritative.

### Final verification

Focused Task 9 files:

```text
uv run pytest tests/test_bootstrap.py tests/test_launch.py
tests/test_mcp_tools.py tests/test_security.py tests/test_api.py
tests/test_watchdog.py tests/test_monitor.py tests/test_ops.py
tests/test_startup_schema.py -q
```

Result: all passed with the two known third-party warnings.

Final complete suite after the last installer change:

```text
uv run pytest
```

Result:

```text
1282 passed, 1 skipped, 2 warnings in 201.15s
```

Additional passing checks:

```text
uv lock --check
uv run python -m compileall -q src tests
bash -n scripts/start.sh scripts/stop.sh scripts/launchd/install.sh
scripts/launchd/uninstall.sh
plistlib parsing of every checked-in launchd plist
git diff --check
```

The residual warnings are the same upstream `websockets.legacy` and Starlette
TestClient deprecations recorded above; no new warning or safety concern was
introduced.

## Independent-review fix round 2

### RED

The round-two regression file was written before production changes:

```text
uv run pytest tests/test_task9_round2.py -q
```

Initial result:

```text
20 failed
```

Those failures proved:

- partial `create_app` injection still read ambient `Secrets` and could build a
  second stack;
- complete explicit injection could not opt out of ambient runtime secrets;
- MCP transport started without eagerly composing and validating its container;
- the seven production roles did not share a complete private rotating-log
  contract;
- preflight, paper drill, watchdog, and backup did not all pass their exact
  role and reuse one `Secrets` instance;
- generated periodic and daily launchd jobs still wrote unbounded inherited
  stream files;
- public production bootstrap accepted `BrokerKind.MOCK`;
- no explicit test-only broker/clock composition seam existed.

### GREEN

The unchanged initial round-two regression set passed after the minimal
production fixes:

```text
uv run pytest tests/test_task9_round2.py -q
20 passed
```

After strengthening exact-secret and utility-entrypoint identity coverage, the
same file passed:

```text
uv run pytest tests/test_task9_round2.py -q
24 passed
```

The first broader compatibility run correctly exposed 32 watchdog tests whose
zero-argument test doubles no longer represented the production seam:

```text
32 failed, 240 passed
```

The doubles were updated to accept the exact injected `Secrets` and
`runtime_role`; the identical 272-test command then passed. No production
fallback was added.

The focused Task 9 matrix before the final startup-boundary check was:

```text
uv run pytest tests/test_task9_round2.py tests/test_bootstrap.py
tests/test_launch.py tests/test_watchdog.py tests/test_ops.py
tests/test_monitor.py tests/test_external_accounts.py
tests/test_llm_backends.py tests/test_startup_schema.py
tests/test_alpaca_broker.py tests/test_migrations.py
tests/test_mcp_tools.py tests/test_security.py tests/test_api.py
tests/test_plans_api.py tests/test_backtests_api.py -q
```

Result:

```text
591 passed, 2 warnings
```

The final review then identified two boundary cases hidden by discarded
inherited streams: app startup after container construction and MCP transport
startup. Their focused RED result was:

```text
2 failed
```

After wrapping those exact production boundaries with the already-tested role
logger, the unchanged pair passed:

```text
2 passed
```

The final-tree affected regression command covered the complete round-two file,
bootstrap, API, MCP tools, and schema startup:

```text
uv run pytest tests/test_task9_round2.py tests/test_bootstrap.py
tests/test_api.py tests/test_mcp_tools.py tests/test_startup_schema.py
-o addopts='' -q
```

Result:

```text
129 passed, 2 warnings in 9.42s
```

The fixes establish:

- production `create_app()` automatically composes only through one container;
  partial explicit injection fails before any ambient read, while complete
  explicit injection requires service, agent, token, and explicit planning
  dependencies;
- container planning, agent construction, feature providers, screen providers,
  authentication, audit, operations, and routes reuse exact container
  identities;
- MCP composes, schema-checks, and configures one exact container before
  `mcp.run()`;
- app, daemon, MCP, preflight, paper drill, watchdog, and backup each use a
  private bounded `logs/<role>.runtime.log`, with stable redacted startup-failure
  evidence;
- all four installer-generated launchd jobs and both checked-in jobs discard
  inherited stdout/stderr through `/dev/null`;
- public production bootstrap rejects every non-Alpaca broker, while tests may
  explicitly inject a fake broker and clock only with Alpaca still selected in
  configuration.

### Full verification

Repository-wide command:

```text
uv run pytest -o addopts='' -q
```

Result:

```text
1308 passed, 1 skipped, 2 warnings in 206.75s
```

Additional passing checks:

```text
uv lock
uv lock --check
uv run python -m compileall -q src tests
find scripts -type f -name '*.sh' -print0 | xargs -0 bash -n
plistlib parsing and /dev/null/Umask assertions for every checked-in plist
static paper/Alpaca/autoexecute/bracket/fallback/schema guard checks
git diff --check
```

The only warnings remain the pre-existing upstream `websockets.legacy` and
Starlette TestClient deprecations. Paper mode, human approval, auto-execution
off, automatic brackets off, null cross-provider fallback, one-write mutation
semantics, and the schema gate remain intact.

## Final Task 9 review fix

### RED

The final two review regressions were added before production or documentation
changes:

```text
uv run pytest
tests/test_task9_round2.py::test_daemon_main_logs_startup_reconciliation_failure_with_one_secret
tests/test_watchdog.py::test_launchd_and_start_scripts_wire_anonymous_liveness_only
-o addopts='' -q
```

Result:

```text
2 failed in 0.86s
```

The daemon regression proved that `daemon.runtime.log` was private and bounded
but empty after startup reconciliation raised: `build_monitor()` had already
left its `runtime_startup` context before `asyncio.run(monitor.run())`. The
documentation regression found the stale unbounded
`/tmp/trading-app.err` launchd path.

### GREEN

Production `main()` now creates exactly one `Secrets` instance and keeps one
`runtime_startup("daemon", secrets)` context around both `_build_monitor(...)`
and the complete `asyncio.run(monitor.run())`. The same regression verifies the
stable failure marker, secret redaction, owner-only mode, rotating handler, and
exact config/secrets identity.

The operations guide now names `scripts/launchd/install.sh` as canonical,
documents `/dev/null` inherited streams, explains decimal Umask 63 as octal
`077`, and lists the bounded private role logs. Its static regression permits
only `/dev/null` for documented launchd stream paths.

The unchanged focused pair passed:

```text
2 passed in 0.44s
```

The complete Task 9 focused matrix passed:

```text
594 passed, 2 warnings in 63.32s
```

### Full verification

Repository-wide command:

```text
uv run pytest -o addopts='' -q
```

Result:

```text
1309 passed, 1 skipped, 2 warnings in 193.79s
```

Additional passing checks:

```text
uv lock --check
uv run python -m compileall -q src tests
find scripts -type f -name '*.sh' -print0 | xargs -0 bash -n
plistlib parsing plus /dev/null and Umask assertions
static paper/Alpaca/autoexecute/bracket/fallback/schema checks
static rejection of /tmp/trading-app.err and non-/dev/null documented streams
git diff --check
```

The warnings are unchanged upstream deprecations from `websockets.legacy` and
Starlette TestClient. No trading guardrail was relaxed.
