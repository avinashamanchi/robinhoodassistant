# Task 11 Report — Trust-Boundary Release Gates

## Outcome

Task 11 was implemented in
`355f2cea904849aa202f39586b65aef2ae8a2876`.

The release checker now emits only immutable, value-free
`ReleaseViolation(code, path, line)` findings, sorted as
`CODE path:line` with a generic count. It fails closed over the effective route
graph, chat tool registry and reachable dispatch graph, root-local sensitive
field writes, production secret sources, exact outbound origins and clients,
transport settings, disabled integrations, and tracked private artifacts.

Preflight now evaluates `KEYCHAIN`, `LOCAL_TLS`, `FIELD_ENCRYPTION`,
`OUTBOUND_ORIGINS`, and `INTEGRATIONS_DISABLED` before constructing or calling
any broker, model provider, or notifier. Existing paper, reconciliation,
breaker, quote, and daemon checks remain after that structural barrier.

MarketStack was removed from runtime, configuration, secret models, tests, and
operator documentation. Historical equities use the pinned Alpaca data origin.
Injected tests use fakes and never receive production SDK transport mutation.

## Static finding contract

`ReleaseViolation` is a frozen, ordered, slotted dataclass. Its constructor
accepts only:

- an uppercase stable code;
- a safe root-relative path without URL/query/newline syntax; and
- a positive integer line.

The CLI never prints source text, matched values, URLs, query strings,
exception text, AST representations, model names, sensitive field names, or
secret material. Scanner exceptions become fixed fail-closed findings.

The exact trust-boundary codes covered by negative fixtures include:

- `WEBHOOK_ROUTE_PRESENT` and `ROUTE_REGISTRATION_UNPROVEN`;
- `DUPLICATE_EFFECTIVE_ROUTE`;
- `MUTABLE_CHAT_TOOL` and `CHAT_TOOL_REGISTRY_UNPROVEN`;
- `SENSITIVE_REGISTRY_INVALID` and `PLAINTEXT_SENSITIVE_WRITE`;
- `ENVIRONMENT_SECRETS_IN_PRODUCTION` and `COMPOSIO_ENABLED`;
- `OUTBOUND_ORIGIN_UNAPPROVED`, `OUTBOUND_CLIENT_UNAPPROVED`, and
  `QUERY_SECRET`;
- `CROSS_ORIGIN_REDIRECT_ENABLED`, `PROXY_HEADERS_TRUSTED`,
  `INSECURE_COOKIE`, `WILDCARD_HOST_ORIGIN`, and `TLS_DISABLED`;
- `TRACKED_ENV_FILE`, `TRACKED_SQLITE_DATABASE`, `TRACKED_SQLITE_WAL`,
  `TRACKED_SQLITE_SHM`, `TRACKED_TLS_PRIVATE_KEY`,
  `TRACKED_TLS_PRIVATE_CERTIFICATE`, `TRACKED_DECRYPTED_BACKUP`,
  `TRACKED_RUNTIME_LOG`, `TRACKED_RAW_EXPORT`, and `GIT_TREE_UNPROVEN`.

## Effective route and chat boundaries

The route scanner starts from the canonical application root and resolves:

- FastAPI and APIRouter aliases;
- decorator aliases and static `getattr`;
- router constructor prefixes;
- nested and imported `include_router` composition;
- imported app factories;
- `add_api_route`, `add_route`, and `mount`;
- mounted child applications; and
- direct route-list mutation as unproven.

Effective `/webhook*` and `/hooks*` paths fail. Dynamic paths, prefixes,
methods, imports, registrations, or composition fail closed. Normalized
method/path duplicates, including parameter-name aliases, retain Task 10's
effective-route protection. Comments and inert strings are ignored.

The chat scanner parses the final tuple assigned to `READ_ONLY_TOOL_SPECS`, the
literal `ToolRouter.dispatch` table, exact service receivers, the two local
candidate-draft constructors, and `Agent.chat`. Dynamic registries, imports,
`getattr`, wrong receivers, aliases to mutations, or reachable
execute/approve/submit/cancel/reset/notify-style methods fail.

## Sensitive writes and secret sources

The release checker parses `SENSITIVE_FIELDS` and ORM table declarations from
the scanned root with AST. It loads only the value-independent scanner
implementation by file, with explicit root-local model/table maps; it never
imports the active application's registry or ORM models for a fixture.

It traces ORM constructor kwargs, annotated and ordinary assignments,
`setattr`, SQLAlchemy insert/update/delete/value chains, execute parameters,
bulk mappings, raw SQL, model aliases, object aliases, and helper bypasses.
Only the reviewed sensitive-field store surface is accepted.

The environment-secret scanner parses the scanned root's registered secret
names. Production modules cannot use `EnvironmentSecretProvider`,
`os.environ`, or `getenv` for those names. Only the explicit development
branches in `db/migrate.py` and `ops/safety_drill.py` are accepted, with the
exact role, `config.encryption`, `os.environ`, injected provider, and literal
`allow_environment=True`.

## Outbound, transport, and integration boundaries

`OUTBOUND_ORIGIN_MANIFEST` is immutable and exact by key, adapter, origin,
runtime role, and feature gate. Configuration must equal that manifest.
Adapter paths can use only their assigned origins.

The static gate rejects direct requests/httpx/aiohttp/OpenAI-style clients,
nested import aliases, unapproved provider SDK construction, unknown or
dynamic base URLs, HTTP, malformed origins, query credentials in literal,
formatted, or aliased mappings, redirect following, proxy or forwarded trust,
and disabled TLS. The approved outbound wrapper is itself checked for unsafe
option drift.

The committed server configuration is exact loopback HTTPS with canonical
hosts, secure cookies, canonical certificate paths, and no proxy trust.
Tracked artifacts are obtained only from `git ls-files -z`; private artifact
contents are never opened.

Webhook and Composio defaults are literal false. The gate checks runtime
imports, aliases, dynamic integration/MCP imports, toolkits, provider URLs,
environment examples, and all four operator documents. There is no Composio
origin or tool.

## Structural preflight

All five local checks run before the operational preflight:

1. `KEYCHAIN` proves the default or injected macOS Keychain provider and all
   fields required by the preflight role, including configured field keys.
2. `LOCAL_TLS` proves exact loopback configuration, secure cookies, transport
   policy, private-key mode, matching certificate, SANs, and validity.
3. `FIELD_ENCRYPTION` proves the configured key, singleton migration state,
   current schema/key ID, completed progress, and valid encrypted envelopes
   through local checks.
4. `OUTBOUND_ORIGINS` proves exact configuration/manifest equality.
5. `INTEGRATIONS_DISABLED` proves webhook and Composio false.

Every check returns fixed detail text. Any failure prints all five structural
results and exits before broker, provider, notifier, reconciliation, quote, or
daemon checks. Tests inject fakes that raise if those later paths are reached.

## Documentation

`README.md`, `docs/RUNBOOK.md`, `docs/ops/README.md`, and
`scripts/launchd/README.md` now document:

- private-file Keychain migration and value-free audit;
- canonical local TLS setup;
- stopped-writer field migration, verification, and rotation;
- the HTTPS app and separately started daemon;
- Composio disabled pending provider-side revocation and rotation;
- no webhook receiver;
- read-only chat to immutable draft to explicit signed queue to separate human
  approval;
- encrypted backup, verification, restore, and plaintext limits; and
- no profit guarantee or live-mode support.

No secret value, integration installation command, webhook enablement command,
or live-trading command was added.

## Exact RED evidence

The initial required RED:

```text
uv run pytest tests/test_release_static.py tests/test_launch.py -v
59 failed, 99 passed, 1 warning in 13.08s
```

The affected MarketStack/config/secret RED:

```text
uv run pytest tests/test_marketdata.py tests/test_config.py \
  tests/test_secret_provider.py -v
3 failed, 162 passed in 2.27s
```

Additional exact negative-fixture RED runs established missing handling for:

- 20 initial hardened trust fixtures;
- five missing-authority fixtures;
- six route/outbound alias and SDK fixtures;
- two exact chat-receiver fixtures (`2 failed, 7 passed`);
- three nested-client/query-secret fixtures (`3 failed, 20 passed`);
- TLS-path and operator-doc webhook rejection (`2 failed, 4 passed`);
- one direct route-list shadow (`1 failed, 11 passed`);
- two unsafe outbound-wrapper settings;
- one spoofed encryption-helper bypass (`1 failed, 6 passed`); and
- two dynamic MCP/integration import escapes (`2 failed, 3 passed`).

Each was rerun focused after implementation and passed.

## Focused verification

The final trust matrix plus affected provider, backtest, configuration,
preflight, bootstrap, daemon-monitor, and launch-feature tests:

```text
uv run pytest tests/test_secret_provider.py \
  tests/test_transport_boundary.py tests/test_outbound_policy.py \
  tests/test_sensitive_crypto.py tests/test_sensitive_migration.py \
  tests/test_untrusted_content.py tests/test_candidate_boundary.py \
  tests/test_security_posture.py tests/test_release_static.py \
  tests/test_launch.py tests/test_sensitive_write_sites.py \
  tests/test_marketdata.py tests/test_config.py \
  tests/test_alpaca_broker.py tests/test_news.py \
  tests/test_llm_backends.py tests/test_backtest_engine.py \
  tests/test_backtest_evaluate.py tests/test_backtests_api.py \
  tests/test_bootstrap.py tests/test_monitor.py \
  tests/test_launch_features.py -v

1264 passed, 1 warning in 97.39s
```

The warning is the existing third-party `websockets.legacy` deprecation
warning.

The final static and diff gates:

```text
uv run python scripts/check_release_safety.py
release static checks: PASS

git diff --check
PASS
```

## Sole full-suite run and post-fix proof

Exactly one no-argument full suite was run after focused green, with all
credential variables removed from its environment:

```text
uv run pytest
1 failed, 3437 passed, 1 skipped, 1 warning in 271.42s
```

The one failure was
`test_utility_main_reuses_one_secret_and_role_log[preflight-preflight]`.
`preflight.run()` eagerly constructed the default Keychain provider before the
test's injected secret loader could return its fake. The test autouse guard
stopped construction before any Keychain access.

The provider construction was restored to the lazy default-loader path.
Following the explicit exactly-one-full-suite constraint, the full suite was
not rerun. The exact failed parametrized test and every test file that directly
exercises preflight were then run:

```text
uv run pytest \
  tests/test_task9_round2.py::test_utility_main_reuses_one_secret_and_role_log \
  -q
2 passed

uv run pytest tests/test_launch.py tests/test_secret_provider.py \
  tests/test_security_posture.py tests/test_task9_round2.py -v
199 passed, 1 warning in 6.42s
```

The final-tree static gate and diff check passed after this correction. This is
an explicit verification caveat: there is no second clean full-suite artifact.

## No-I/O proof

All Task 11 tests used temporary fixture roots, temporary SQLite databases, or
injected fakes. Credential variables were removed from the focused and full
test environments. The credentialed Alpaca integration remained skipped.

No app, daemon, MCP server, real Keychain, real credential, runtime database,
broker, provider, notifier, integration, or network endpoint was accessed. No
real order was submitted/cancelled, no real breaker was reset, no external
reconciliation was initiated, and nothing was pushed.

## Implementation files

- `.env.example`
- `README.md`
- `config.yaml`
- `docs/RUNBOOK.md`
- `docs/ops/README.md`
- `pyproject.toml`
- `scripts/check_release_safety.py`
- `scripts/launchd/README.md`
- `src/trading_assistant/backtest/data.py`
- `src/trading_assistant/backtest/marketstack.py` (removed)
- `src/trading_assistant/config.py`
- `src/trading_assistant/logging.py`
- `src/trading_assistant/preflight.py`
- `src/trading_assistant/security/outbound.py`
- `src/trading_assistant/security/secrets.py`
- `src/trading_assistant/security/sensitive_write_scan.py`
- `tests/test_config.py`
- `tests/test_launch.py`
- `tests/test_marketdata.py`
- `tests/test_outbound_policy.py`
- `tests/test_release_static.py`
- `tests/test_secret_provider.py`

## Review package

- Base: `562e102d4c80f6a47dd1101e074fd0314d4e78cd`
- Implementation: `355f2cea904849aa202f39586b65aef2ae8a2876`
- Diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-562e102..355f2ce.diff`
- Diff size: 7,767 lines / 282,248 bytes

The bounded diff was reviewed for value leakage, unresolved route
composition, chat receiver aliases, sensitive-registry contamination, helper
bypasses, environment-provider escapes, query credentials, client/origin
pinning, proxy/redirect/TLS drift, tracked-artifact content reads, structural
preflight ordering, MarketStack remnants, Composio/webhook enablement, and
Task 12/Plan 3 scope. No open code-review finding remains.

## Residual limits

- The sole full-suite artifact has the documented one-test pre-fix failure;
  final-tree proof is the exact correction plus the complete affected
  199-test set, not a second full run.
- Provider-side revocation and rotation of the previously exposed integration
  credential is external to this repository. The integration remains disabled
  until that is completed and independently reviewed.
- The outbound manifest does not authorize arbitrary destinations. Existing
  Gemini, Groq, and CoinGecko adapters remain exact-origin and
  feature/role-scoped alongside Alpaca, Anthropic, and Telegram.
- This release remains paper-only, has no webhook receiver or live-mode path,
  and makes no profit or return guarantee.
