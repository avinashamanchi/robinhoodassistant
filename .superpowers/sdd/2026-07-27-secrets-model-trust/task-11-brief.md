### Task 11: Make trust-boundary regressions fail the release gate

**Files:**

- Modify: `scripts/check_release_safety.py`
- Modify: `tests/test_release_static.py`
- Modify: `src/trading_assistant/preflight.py`
- Modify: `tests/test_launch.py`
- Modify: `README.md`
- Modify: `docs/RUNBOOK.md`

**Interfaces:**

- Adds static checks for secret sources, webhook routes, mutable chat tools,
  plaintext field writes, unsafe URLs, proxy trust, and committed TLS/private
  state.
- Adds preflight checks `KEYCHAIN`, `LOCAL_TLS`, `FIELD_ENCRYPTION`,
  `OUTBOUND_ORIGINS`, `INTEGRATIONS_DISABLED`.

- [x] **Step 1: Add negative static fixtures**

Each fixture must fail with a stable code:

- `WEBHOOK_ROUTE_PRESENT`;
- `ENVIRONMENT_SECRETS_IN_PRODUCTION`;
- `COMPOSIO_ENABLED`;
- `MUTABLE_CHAT_TOOL`;
- `PLAINTEXT_SENSITIVE_WRITE`;
- `CROSS_ORIGIN_REDIRECT_ENABLED`;
- `PROXY_HEADERS_TRUSTED`;
- `INSECURE_COOKIE`;
- tracked `.env`, SQLite DB, TLS private key, or decrypted backup.

- [x] **Step 2: Run and verify missing checks**

```bash
uv run pytest tests/test_release_static.py tests/test_launch.py -v
```

Expected: FAIL because the new invariants are not gated.

- [x] **Step 3: Implement AST and Git-tree checks**

Parse FastAPI decorators and reject any path beginning `/webhook` or `/hooks`.
Parse `READ_ONLY_TOOL_SPECS` and forbid mutation names. Parse assignments to
the sensitive registry. Search runtime composition roots for
`EnvironmentSecretProvider`. Inspect `git ls-files` rather than only the
working tree for private artifacts.

Do not scan or print secret values. Pattern findings report path, line, and
stable rule only.

- [x] **Step 4: Extend preflight**

Normal readiness requires:

- Keychain provider and required fields present;
- local certificate valid and key mode `0600`;
- exact loopback bind/origin/hosts and secure cookies;
- encryption state complete and key ID available;
- no webhook/Composio integration;
- exact outbound HTTPS origins;
- existing paper-mode, reconciliation, breaker, quote-integrity, and daemon
  checks unchanged.

Preflight remains read-only and never resets a breaker, starts a daemon, or
submits/cancels an order.

- [x] **Step 5: Document operator commands and hard limits**

README/RUNBOOK must document:

- Keychain migration and audit;
- local TLS setup;
- encrypted field migration/verification/rotation;
- HTTPS app start and separate daemon start;
- Composio disabled pending provider-side rotation;
- no webhook;
- read-only chat → explicit queue → separate approval;
- backup recovery;
- no profit guarantee and no live-mode support.

- [x] **Step 6: Run the complete trust-boundary matrix**

```bash
uv run pytest tests/test_secret_provider.py tests/test_transport_boundary.py tests/test_outbound_policy.py tests/test_sensitive_crypto.py tests/test_sensitive_migration.py tests/test_untrusted_content.py tests/test_candidate_boundary.py tests/test_security_posture.py tests/test_release_static.py tests/test_launch.py -v
uv run python scripts/check_release_safety.py
```

Expected: all tests PASS and `release static checks: PASS`.

- [x] **Step 7: Run the full suite**

```bash
uv run pytest
```

Expected: PASS with only the repository's documented skip.

Actual sole run: `3437 passed, 1 failed, 1 skipped, 1 warning`. The failure
was the lazy default-Keychain-provider compatibility path; it was corrected,
then the exact failed test passed `2/2` and the complete affected preflight,
secret, posture, and Task 9 compatibility set passed `199/199`. The full suite
was not rerun under the explicit one-run constraint.

- [x] **Step 8: Commit**

```bash
git add scripts/check_release_safety.py tests/test_release_static.py src/trading_assistant/preflight.py tests/test_launch.py README.md docs/RUNBOOK.md
git commit -m "chore(security): gate trust-boundary invariants"
```

---

## Plan 2 completion checkpoint

Run:

```bash
git status --short
uv run pytest
uv run python scripts/check_release_safety.py
```

Required result:

- clean working tree;
- complete pytest and static-gate pass;
- production roles require macOS Keychain;
- loopback HTTPS and exact-origin policy are enforced;
- registered sensitive fields are encrypted with migration state complete;
- general chat cannot mutate state;
- signed queue endpoints create proposals/rules but never execute;
- no webhook or Composio integration is enabled;
- no broker/provider calls, daemon start, breaker reset, or order submission
  occurred during verification.

## Governing preflight-review amendments

These amendments are part of Task 11 and override narrower or repeated
instructions above.

### Ownership and execution boundary

- Own Task 11 production, tests, documentation, and SDD evidence only.
- Do not start Plan 3, push, start the app/daemon/MCP server, access a real
  Keychain/credential/runtime database, or make broker/provider/notifier/network
  calls.
- Use temp fixtures and fakes only. Never read, use, store, or echo the
  compromised Composio credential.
- Use TDD with exact negative-fixture RED evidence, focused GREEN, exactly one
  no-argument `uv run pytest` after focused GREEN, then the static gate.
- Commit implementation/tests/docs first. Commit the brief, report, bounded
  review diff, plan checkboxes, and progress ledger separately as evidence.

### Static finding contract

- Introduce a strict immutable `ReleaseViolation(code, path, line)` whose
  fields contain only a stable code, root-relative path, and positive line.
- Never retain or print source snippets, matched values, URLs/query strings,
  secret-like text, exception text, or arbitrary AST representations.
- CLI violations are deterministically sorted and formatted exactly
  `CODE path:line`, followed by a generic stable failure count.

### Effective route graph

- Resolve effective FastAPI/Starlette routes, including decorator aliases,
  `getattr`, `APIRouter` prefixes, nested `include_router` prefixes, imported
  routers and app factories, imperative `add_api_route`, `add_route`, and
  `mount`, from canonical composition roots.
- Any effective `/webhook*` or `/hooks*` route emits
  `WEBHOOK_ROUTE_PRESENT`.
- Dynamic or unresolved route path/prefix/registration/import composition
  emits `ROUTE_REGISTRATION_UNPROVEN`.
- Preserve Task 10 duplicate/effective-route protection. Cover aliases,
  nested includes, factories, dynamic `getattr`/prefix/path, imperative
  mounts, and comment/string decoys.

### Chat tool boundary

- Gate the effective final `READ_ONLY_TOOL_SPECS` plus the Agent/ToolRouter
  dispatch registry and reachable call graph.
- Permit only the exact current read-tool allowlist and immutable local draft
  constructors.
- Mutation names, aliases to mutation, dynamic registry construction,
  `getattr`, dynamic imports, and execute/approve/submit/cancel/reset/notify
  paths emit `MUTABLE_CHAT_TOOL` or `CHAT_TOOL_REGISTRY_UNPROVEN`.
- Do not implement this boundary as text search.

### Sensitive-field writes

- Parse the root-local sensitive registry from the scanned fixture root with
  AST only; never import the active checkout to test a fixture.
- Trace ORM attribute assignment, constructor kwargs, `update(values)`, bulk
  operations, aliases, and helper bypasses for registered fields.
- Invalid or dynamic registry emits `SENSITIVE_REGISTRY_INVALID`; unapproved
  writes emit `PLAINTEXT_SENSITIVE_WRITE`. Findings remain value-free.

### Secret sources and disabled integrations

- Production composition roots cannot instantiate/use
  `EnvironmentSecretProvider` or raw `os.environ`/`getenv` for registered
  secrets.
- Allow `EnvironmentSecretProvider` only in explicit development CLI branches
  in `db/migrate.py` and `ops/safety_drill.py`, with exact mapping and explicit
  `allow_environment`; otherwise emit
  `ENVIRONMENT_SECRETS_IN_PRODUCTION`.
- Resolve aliases, `getattr`, and dynamic-import escapes; unresolved
  composition fails closed. macOS Keychain remains the production source.
- Check config defaults, env examples, runtime imports/callers/toolkits/MCP,
  URLs, and docs for explicit webhook/Composio disablement. Emit
  `COMPOSIO_ENABLED` or `WEBHOOK_ROUTE_PRESENT`; install or call no integration.

### Outbound manifest and MarketStack removal

- Define exact allowed HTTPS origins by adapter and role.
- Reject direct `requests`/`httpx`/`aiohttp`/OpenAI clients outside approved
  wrappers, redirects, proxies/`trust_env`, dynamic or unpinned base URLs,
  query-string credentials, HTTP, and unknown clients/origins.
- Emit `OUTBOUND_ORIGIN_UNAPPROVED`, `OUTBOUND_CLIENT_UNAPPROVED`, or
  `QUERY_SECRET`.
- Remove the MarketStack runtime/data path and use Alpaca historical data;
  update config, models, tests, and docs without network calls.
- Allow only feature/role-appropriate Alpaca, Anthropic, and Telegram origins.
  Composio has no origin or tool.

### Transport and tracked artifacts

- AST/config-check cross-origin redirects, trusted proxy/forwarded headers,
  insecure cookies, wildcard hosts/origins, and disabled TLS.
- Inspect tracked artifacts via `git ls-files -z` rooted at the scanned fixture.
  Emit separate stable codes for tracked `.env`, SQLite DB/WAL/SHM, TLS private
  key, private certificate, decrypted backup, logs, and raw exports.
- Never open or print private artifact contents. Git inspection failure emits
  `GIT_TREE_UNPROVEN`.

### Structural preflight

- Add checks named exactly `KEYCHAIN`, `LOCAL_TLS`, `FIELD_ENCRYPTION`,
  `OUTBOUND_ORIGINS`, and `INTEGRATIONS_DISABLED`.
- Run every local structural check before constructing or calling any
  broker/provider/notifier. Any structural failure exits before outbound
  construction.
- `KEYCHAIN` uses an injected provider and required role fields and returns
  only stable detail.
- `LOCAL_TLS` verifies exact loopback host/origin/allowed hosts, secure cookie,
  key mode, and certificate validity.
- `FIELD_ENCRYPTION` verifies completed migration and configured key with local
  safe checks.
- `OUTBOUND_ORIGINS` verifies the exact manifest/config.
- `INTEGRATIONS_DISABLED` verifies webhook and Composio are off.
- Existing paper-mode, reconciliation, breaker, quote, and daemon checks remain
  unchanged and run only after the structural gate.
- Tests use fakes that raise if outbound construction or calls occur.

### Documentation and verification

- Update `README.md`, `docs/RUNBOOK.md`, `docs/ops/README.md`, and
  `scripts/launchd/README.md` with safe Keychain migration/audit, local TLS,
  encrypted-field migration/verification/rotation, HTTPS app and separate
  daemon start, Composio disabled pending provider-side rotation, no webhook,
  read-only chat to explicit signed queue to separate approval, backup limits,
  no profit guarantee, and no live mode.
- Add exact negative fixtures for every requested stable code, one clean-root
  positive fixture, false-positive decoys, root-isolation coverage, and
  deterministic value-free output coverage.
- Run the plan trust matrix plus affected provider/backtest/config/preflight
  tests. The repository static gate must pass. Run exactly one full suite;
  reuse that result for the completion checkpoint rather than running it
  again.
