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
breaker, and quote checks remain after that structural barrier. Daemon
freshness is observed separately after startup; preflight has no daemon-health
row.

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
3. `FIELD_ENCRYPTION` proves configured-key availability plus singleton
   migration metadata, current schema/key ID, and completed progress without
   decrypting rows. The startup guard owns the one full envelope scan.
4. `OUTBOUND_ORIGINS` proves exact configuration/manifest equality.
5. `INTEGRATIONS_DISABLED` proves webhook and Composio false.

Every check returns fixed detail text. Any failure prints all five structural
results and exits before broker, provider, notifier, reconciliation, quote, or
other operational checks. Tests inject fakes that raise if those later paths
are reached.

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

# Task 11 Fix Round 1 Addendum

## Outcome

The review findings were closed in implementation commit
`1f63080bc18894d025a20690dfbd6b4e7d6dd946`.

The release gate now requires one unconditional canonical definition for every
final authority and rejects rebinding, direct or aliased mutation, conditional
definitions, dynamic construction, and integration-class mutation. Route
analysis unions branch possibilities, rejects unresolved branch side effects,
and gates `routes` list mutation plus `__getattribute__` registration. Chat
dispatch helpers are traversed recursively and fail closed on lambda, external,
dynamic, or mutable paths.

Sensitive-write analysis now includes ORM `Query.update`,
`Model.__table__.update().values`, helper-returned model aliases, aliased
`execute`, raw update/text DML, and bulk mappings. Environment analysis includes
mapping copies/views/getitem and prevents the two development-only providers
from escaping their exact `load_role_secrets` call chain.

The direct-client gate covers module-qualified and aliased Anthropic/OpenAI,
requests/httpx/aiohttp, urllib, WebSocket, and socket construction/calls.
Network option provenance rejects unresolved or mutated `**kwargs` and
credential-bearing query mappings without treating unrelated `verify` or
`params` attributes as network settings. Cookie, CORS-regex, SSL verification,
hostname, and minimum-TLS settings are gated.

Git-tree proof now requires the resolved scanned root to equal the resolved Git
toplevel, rejects scanned symlinks and outside-root resolution, and emits only
value-free stable output for path or CLI failures. Private artifact matching
includes broad environment, database/sidecar, private key/certificate,
decrypted-backup, log, and raw-export names while public certificate names
remain accepted.

Runtime composition now checks each adapter/role against the exact outbound
manifest before client construction. The manifest includes the MCP Alpaca-data
role and excludes preflight LLM roles. Watchdog liveness uses an injected,
proxy-free, no-redirect HTTPX transport pinned to the exact loopback HTTPS URL
and canonical local certificate.

Preflight constructs at most one explicit
`MacOSKeychainSecretProvider`, loads through that same provider, and never
accepts `provider=None` as proof. Provider construction/load failure still
produces all five independent structural rows. `LOCAL_TLS` requires the exact
certificate/key paths, and `FIELD_ENCRYPTION` is metadata-only.

## Exact round-1 RED evidence

The review probes were run against the pre-fix implementation with credential
variables removed:

```text
env -u COMPOSIO_API_KEY -u ALPACA_API_KEY -u ALPACA_SECRET_KEY \
  -u ANTHROPIC_API_KEY -u GEMINI_API_KEY -u GROQ_API_KEY \
  -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHAT_ID \
  -u FIELD_ENCRYPTION_KEYS_JSON -u APP_API_TOKEN \
  -u CANDIDATE_SIGNING_KEY -u BACKUP_ENCRYPTION_KEY \
  uv run pytest tests/test_release_static.py tests/test_launch.py \
  tests/test_outbound_policy.py tests/test_watchdog.py -q --tb=no \
  -k 'final_authorities or route_branch_union or
  route_list_and_dunder or route_mutation_fixture or chat_reachable or
  sensitive_write_round_one or environment_mapping or
  development_environment_provider or module_qualified or
  network_option_provenance or non_network_verify or transport_round_one or
  secure_cookie_call or scanned_root or security_sensitive_symlinks or
  root_symlink_loop or unsafe_tracked_path or broad_private or
  public_certificate or executable_plan or historical_marketstack or
  never_accepts_missing_keychain or exact_canonical_tls_paths or
  provider_constructor_failure or keychain_load_failure or
  constructs_loads_and_passes_one or metadata_only_and_never_builds_cipher or
  require_origin_enforces or local_liveness_transport or
  uses_only_injected_local_liveness or builds_one_local_liveness'

RED_FAILED=72
```

The evidence count came from the command's 72 emitted `FAILED` records. Two
later strict authority probes independently failed `2/13`, and the value-free
CLI parse probe independently failed `1/1` before their focused fixes.

## Final focused and static verification

```text
uv run pytest tests/test_release_static.py --tb=short
245 passed in 41.84s

uv run pytest tests/test_monitor.py \
  tests/test_task9_round2.py tests/test_watchdog.py tests/test_launch.py \
  -v --tb=short
228 passed, 1 warning in 6.66s

uv run pytest <25-file trust and affected matrix> --tb=short
1575 passed, 1 warning in 132.37s

uv run python scripts/check_release_safety.py
release static checks: PASS

uv run python -m compileall -q scripts/check_release_safety.py \
  src/trading_assistant tests/test_release_static.py \
  tests/test_monitor.py tests/test_task9_round2.py
PASS

git diff --check
PASS
```

The matrix is the Task 11 trust matrix plus affected provider, transport,
backtest, configuration, watchdog, bootstrap, model-trust, safety-drill,
startup-schema, authentication, and security-header files. The warning is the
existing third-party `websockets.legacy` deprecation warning.

## Sole round-1 full-suite run

Exactly one no-argument full suite was run after focused and static green:

```text
uv run pytest
3535 passed, 7 failed, 1 skipped, 1 warning in 289.19s
```

All seven failures were stale fake signatures at the newly explicit
boundaries: five daemon notifier fakes did not accept `runtime_role`, one
watchdog fake omitted the injected liveness transport/certificate config, and
one preflight compatibility test assumed the now-forbidden provider-less call.
Production behavior was not loosened. The legacy tests were updated to use the
explicit interfaces.

Per the exactly-one-full-suite constraint, the full suite was not rerun. The
exact failed set plus the paired utility parameter passed `8/8`; all complete
affected monitor, Task 9 compatibility, watchdog, and preflight files then
passed `228/228`; the final 1,575-test matrix and repository static gate passed.
This remains an explicit full-suite evidence caveat.

## Round-1 review package

- Base: `1f25c102c7886fb8425c88198dbaf1618ddb090a`
- Implementation: `1f63080bc18894d025a20690dfbd6b4e7d6dd946`
- Diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-1f25c10..1f63080.diff`
- Diff size: 5,487 lines / 199,838 bytes
- Review package:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/task-11-review-package-r1.md`

The bounded review checked all 18 requested finding groups, value leakage,
root isolation, runtime role propagation, preflight ordering, MarketStack
remnants, operator documentation, and Task 12/Plan 3 scope. No open code
finding remains at this implementation gate.

## Round-1 residual limits

- The sole full-suite artifact has seven pre-correction legacy-test failures;
  final-tree proof is the exact eight-test correction, complete 228-test
  affected set, 1,575-test trust/affected matrix, and static gate—not a second
  full run.
- Unsupported or dynamic trust-boundary constructs are deliberately rejected;
  adding one requires an explicit parser/model extension plus negative and
  positive fixtures.
- Provider-side revocation/rotation of the previously exposed integration
  credential remains external. Composio has no origin, route, toolkit, caller,
  or tool and stays disabled.
- Preflight has no daemon-health row. Daemon freshness is a separate
  post-start observation.
- This release remains paper-only, human-approved, webhook-free, and provides
  no profit guarantee or live-mode path.

# Task 11 Fix Round 2 Addendum

## Supersession and outcome

The round-1 claims that its bounded review had no open code findings and that
its evidence commit was evidence-only are superseded.

Fresh review found real gaps after round 1. In addition, commit
`7cc5c91d42b0349a7235ddf65cc21626250a0ccc` changed executable MarketStack
plan instructions while labeling itself an evidence commit. That provenance
claim was inaccurate. Fix-round-2 runtime, test, setup, operator-document, and
executable-plan changes are all contained in implementation commit
`d7c9576146ec205f454a8fd7b8db1425a2ce91d0`; the following commit is limited to
SDD evidence and plan completion records.

The implementation closes the requested final-authority, reachable-helper,
mapping-provenance, client, transport, artifact, watchdog-TLS, query-credential,
and preflight-composition findings. Unsupported or dynamic security-boundary
constructs remain deliberately fail-closed.

## Reviewer-claim verification

Every reviewer claim was probed against the round-1 tree before implementation.
Two exact subclaims were not gaps:

- `Anthropic(**{"base_url": ..., "api_key": ...})` already failed closed as
  `OUTBOUND_CLIENT_UNAPPROVED`. That hermetic fixture is retained.
- route-registrar list/subscript indirection already failed closed as
  `ROUTE_REGISTRATION_UNPROVEN`. That hermetic fixture is retained.

The broader named-mapping/network-option and route-expression boundaries were
still reviewed; real adjacent gaps were fixed. The mkcert claim was confirmed
without a socket: an in-memory TLS handshake rejected the localhost leaf as a
CA file with `SSLCertVerificationError`, while the generated root CA verified
the same leaf.

## Static closure

The release scanner now:

- follows nested aliases of canonical authorities and fails closed on dynamic
  `globals()`/`vars()` rebinding;
- rejects assignment, annotated assignment, augmented assignment, deletion,
  and named-expression state effects in every reachable local chat helper;
- permits dynamic wrapper URL flow only for the narrowly modeled
  `NoRedirectSession.request` path where the same URL variable is checked by
  `OutboundPolicy.assert_url`;
- resolves shared mapping aliases, literal/named `**kwargs`, mutations,
  `update`, `setdefault`, and subscript writes, then validates effective
  network options and query keys;
- propagates sensitive ORM object, mutation-call, and `session.execute`
  aliases;
- detects module-qualified environment-provider aliases and copied environment
  mappings;
- gates `http.client` connection/request surfaces and unverified SSL contexts;
- recognizes actual middleware registration, aliased `set_cookie`, aliased SSL
  factories, and unpacked Uvicorn proxy settings;
- rejects tracked production SQL/backup payloads while accepting
  `docs/plaintext-format.md`; and
- limits `verify`/`params` interpretation to proven network call sites.

All findings remain immutable and value-free. No source snippet, URL/query,
matched value, exception text, or arbitrary AST representation is emitted.

## Runtime, TLS, and preflight closure

`OutboundPolicy.assert_url` rejects credential-like query names before either
requests or HTTPX transport is reached and exposes only a generic denial.

Watchdog liveness now trusts only the canonical public
`.local/tls/rootCA.pem`, with proxy/environment trust and redirects disabled
and exact loopback request/final URLs enforced. TLS preflight validates the
canonical CA/leaf/key paths, modes, current self-signed CA, leaf issuer and
signature, exact SANs, and leaf/private-key match. The setup script copies only
the public mkcert root certificate; it never copies the CA private key.

Operational preflight uses `build_preflight_service` with runtime role
`preflight`. It builds no LLM provider, agent, app, or notifier. The five local
structural rows retain their independent failure behavior. After all five pass,
the existing operator-controlled paper broker/reconciliation checks may repair
or cancel already-known paper-order state, but they do not submit a new order.

## Exact RED evidence

The first round-2 static node bundle produced:

```text
22 failed, 2 passed
```

The two passes were the retained inline-literal-provider and route-indirection
counterexamples above. The remaining authority, chat-effect, wrapper URL,
named mapping, proxy-option, sensitive alias, environment alias, stdlib
client, shared query mapping, transport alias, tracked SQL, and false-positive
fixtures were RED.

The runtime/TLS/preflight bundle produced:

```text
18 failed, 12 passed
```

The in-memory leaf-versus-root TLS characterization was already green because
it describes platform behavior rather than implementation behavior. A second
wrapper fixture then exposed the instance-client path:

```text
uv run pytest \
  tests/test_release_static.py::test_outbound_wrapper_rejects_unproven_dynamic_direct_request \
  --tb=short -q
.F
```

The dedicated composition API had its own RED:

```text
uv run pytest \
 tests/test_task9_round2.py::test_dedicated_preflight_builder_constructs_no_llm_capability \
  --tb=short -q
1 failed
AttributeError: bootstrap has no build_preflight_service
```

No exception value, credential, real Keychain value, runtime database content,
or network result was used as evidence.

## Focused and release-gate verification

```text
New static probes:
25 passed in 7.40s

Runtime-focused files:
326 passed, 1 warning in 16.72s

Affected trust matrix:
1680 passed, 1 warning in 261.01s

uv run python scripts/check_release_safety.py
release static checks: PASS

uv run python -m compileall -q src scripts tests
PASS

git diff --check
PASS
```

The warning is the existing third-party `websockets.legacy` deprecation
warning.

Exactly one no-argument full suite was run:

```text
uv run pytest
3582 passed, 1 skipped, 1 warning in 522.86s
```

During the later evidence-coherence pass, one documentation-only executable
plan sentence was corrected to stop calling reconciliation broker-write-free.
No production or test code changed after the full run. The repository static
gate and `git diff --check` were rerun on that final implementation tree and
passed; the full suite was not rerun under the one-run constraint.

## Round-2 review package

- Base: `7cc5c91d42b0349a7235ddf65cc21626250a0ccc`
- Implementation: `d7c9576146ec205f454a8fd7b8db1425a2ce91d0`
- Bounded diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-7cc5c91..d7c9576.diff`
- Diff size: 2,184 lines / 86,981 bytes
- Review package:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/task-11-review-package-r2.md`

The requested probes are closed at the implementation gate. This is not a
claim that static analysis can prove arbitrary Python semantics; unsupported
or dynamic trust-boundary constructs are intentionally rejected and any new
supported form requires explicit modeling plus negative and positive fixtures.

## Residual hard limits

- Provider-side revocation/rotation of the previously exposed integration
  credential remains external. Composio has no enabled route, origin, caller,
  toolkit, MCP surface, or chat tool.
- There is no webhook receiver, live-mode path, autonomous approval, or profit
  guarantee.
- General chat remains read-only; immutable drafts reach the signed queue only
  through an explicit action and require separate human approval.
- Preflight has no daemon-health row. Daemon freshness is observed separately
  after startup.
- Verification used temporary Git roots, generated certificates, in-memory TLS,
  and fakes only. No app/daemon/MCP server, real Keychain, credentials, runtime
  database, broker/provider/notifier/integration, or network transport was
  started or called; nothing was pushed.

# Task 11 Fix Round 3 Addendum

## Supersession and boundary

The round-2 statement that no requested reproducer remained is historical and
superseded. Round 3 was verified from base
`8de8bd96783750500baccbd51f27b7561b505194`. All production, test, setup,
operator, and executable-plan changes are isolated in implementation commit
`b51e8ee0d5ece8bcde3701e4dd4b9adf58089c5c`; this addendum and its companion
brief, plan completion record, progress entries, bounded diff, and review
package are evidence only.

No Plan 3 work, push, service process, real Keychain or credential, runtime
database, broker/provider/notifier/integration transport, or external network
call was used. TLS tests used generated files in temporary roots; database
tests used temporary SQLite only.

## Finding disposition

1. **Final authorities — confirmed.** Dynamic `globals`/`locals`/`vars`,
   `exec`/`eval`, subscript/container aliases, nested alias mutation, and
   noncanonical rebinding now invalidate the applicable final authority.
2. **Chat root effects — confirmed.** Assignment/deletion in
   `ToolRouter.dispatch` was a gap. Root and recursively reachable helper
   effects now fail closed; an additional direct mutable-call probe was
   already rejected, while an unknown direct effect exposed and closed the
   remaining unproven-call gap.
3. **Outbound wrapper dominance — confirmed.** A policy assertion after I/O
   passed before the fix. The only supported dynamic requests wrapper now has
   exactly the guard followed by the direct return transport with inline query
   validation; URL rebinding or additional control flow is unproven.
4. **Chained mappings — confirmed.** Every chained assignment target now joins
   the same mapping component, so query and provider-option mutation through
   any alias reaches the effective call.
5. **Environment unpacking — confirmed.** Mapping unpack, copies, views,
   subscripts, aliases, and provider escapes are rejected outside the exact
   authorized development chain.
6. **Sensitive writes — confirmed.** Keyword `statement`/`values`, execute and
   ORM query aliases, and unannotated local-helper model parameters are traced
   or rejected.
7. **Security call identity — confirmed.** Module, attribute, and collection
   indirection around CORS middleware, HTTP clients, cookies, and SSL factories
   fails closed.
8. **Tracked SQL/dumps — confirmed.** Conventional root and nested SQL/dump
   backup names are rejected without reading contents; benign plaintext-format
   documentation remains allowed.
9. **Network-option decoy — confirmed false-positive gap.** A local
   `get(..., verify=False)` is clean unless the call identity is proven
   network-shaped.
10. **TLS constraints — confirmed with nuance.** The prior manual checks
    accepted a CA lacking `keyCertSign` and a client-only leaf. Cryptography's
    verifier rejects those chains. It accepts an absent EKU as unconstrained,
    while the release requirement is an explicit leaf `serverAuth`; the local
    validator therefore requires that extension in addition to standards
    verification.
11. **Direct stdlib networking/query keys — confirmed.** Chained
    `build_opener().open`, `socket.socket().connect`, HTTP client aliases,
    unverified contexts, and computed credential-like query names are gated.
12. **Preflight capability — confirmed.** The old dedicated role still
    returned a full mutable `TradingService`. It now returns only
    `PreflightReconciliationProbe.inspect_reconciliation`, backed by broker
    reads and local database inspection with no trading-table DML. SQLite
    connection setup may still establish WAL and secure sidecar modes. No
    clock, cipher hooks, LLM, notifier, mutable trading method, or writer
    tenure is constructed.
13. **Watchdog capability — round-3 disposition, later superseded.** Alpaca
    and Telegram origins were excess and are removed. The requirement tuple
    was already exactly `("database_url",)`, but round 4 proved the generic
    provider load still fetched every Keychain account before projection.
14. **TLS setup runner — confirmed.** The executable setup now uses
    `uv run python`; tests inspect the script but never execute setup.

## Exact RED evidence

The initial round-3 static bundle produced:

```text
18 failed
```

The sensitive-write reproducer produced:

```text
3 failed
```

The initial runtime/TLS/preflight bundle produced:

```text
6 failed, 1 passed
```

The one pass was the already-working runtime computed-query rejection. The
watchdog required-secret counterexample was later split from the valid egress
probe and passed `2/2`.

Final conservative diff audit added three narrowly scoped RED checks:

```text
direct mutable dispatch effect plus URL rebinding: .F
unknown direct dispatch effect: 1 failed
missing explicit leaf serverAuth EKU: 1 failed
```

The direct mutable effect was the retained pass: the existing reachable-call
scan already rejected it. URL rebinding, unknown root effects, and absent EKU
were genuine remaining gaps and were implemented before the final focused
matrix or full suite.

No RED output included source snippets from the release gate, credentials,
URLs with query strings, exception text from production paths, or real
resource data.

## Final verification

```text
Focused release/static/TLS/round-3 set:
432 passed in 101.29s

Final affected trust matrix (30 files):
1754 passed, 1 warning in 269.73s

uv run python scripts/check_release_safety.py
release static checks: PASS

uv run python -m compileall -q src scripts tests
PASS

git diff --check
PASS

bash -n scripts/setup-local-tls.sh
PASS
```

The warning is the existing third-party `websockets.legacy` deprecation
warning.

Exactly one no-argument full suite was run:

```text
uv run pytest
3619 passed, 1 failed, 1 skipped, 1 warning in 527.43s
```

The sole failure was
`tests/test_migrations.py::test_sensitive_trust_downgrade_lock_closes_checked_after_race[dependent-insert]`.
Round 3 does not modify that test or any migration. The test patches the first
`alembic_op.drop_index` while downgrading from repository head; later migration
revisions now contain an earlier index drop than the sensitive-state revision,
so scheduling can let the dependent insert make the later pristine-state gate
correctly fail closed. The exact failed node immediately passed:

```text
1 passed in 3.51s
```

No second full suite was run and no out-of-scope migration behavior was
changed. The release evidence therefore retains, rather than hides, the sole
full-suite caveat.

## Review package

- Base: `8de8bd96783750500baccbd51f27b7561b505194`
- Implementation: `b51e8ee0d5ece8bcde3701e4dd4b9adf58089c5c`
- Bounded diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-8de8bd9..b51e8ee.diff`
- Diff size: 2,479 lines / 92,782 bytes
- Package:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/task-11-review-package-r3.md`

## Residual hard limits

- Static release analysis intentionally rejects unsupported or dynamic
  security-boundary Python. Supporting a new construct requires an explicit
  model plus negative and positive fixtures.
- The sole full-suite artifact is not fully green because of the recorded
  out-of-scope migration timing failure; the final Task 11 focused/matrix and
  all static/compile/diff/shell gates are green.
- Composio remains disabled pending provider-side revocation/rotation. It has
  no origin, route, caller, toolkit, MCP surface, or chat tool. No webhook or
  live-mode path exists.
- General chat remains read-only. Immutable drafts require an explicit signed
  queue action and separate human approval. Paper-only mode, manual approval,
  kill switches, and broker-truth checks remain unchanged; no profit guarantee
  is made.
- No app, daemon, MCP, real Keychain/credential/runtime database, external
  transport, trading action, notification, breaker reset, or push occurred.

# Task 11 Fix Round 4 Addendum

## Review boundary

- Base: `c12ce6f1c5df914f5f40e48d100bdfa0bf3fdb4c`
- Implementation:
  `2093b8049dc85e9d02b73fb424d4a648de8f3a1d`
- Bounded diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-c12ce6f..2093b80.diff`
- Diff size: 1,370 lines / 49,857 bytes
- Scope: Task 11 production, tests, operator documentation, and the bounded
  Task 6 test-only process-fixture stabilization required to complete the
  owned full-suite release proof
- Excluded: Plan 3, push, service startup, ignored runtime database, real
  Keychain/credentials, broker/provider/notifier/integration/network calls,
  trading, reconciliation writes, notification, and breaker reset

The implementation commit contains every production, test, and operator-doc
change. This addendum, review package, bounded diff, brief, plan checkboxes,
and progress ledger form the separate evidence-only commit.

## Finding disposition

1. **Role-scoped watchdog secrets — confirmed.** The round-3
   `_required_fields` counterexample proved only post-load projection.
   `MacOSKeychainSecretProvider.load()` still requested all accounts. Round 4
   adds provider-level `load_for_role`; the watchdog fake records account
   access and proves that only `database_url` is requested or returned. Every
   other runtime role had an exact startup-required projection test; the final
   bounded correction below records why that was insufficient for optional
   branch-visible fields.
2. **Superseded fill tombstones — confirmed.** They are valid reconciliation
   exclusions and no longer count as drift. Quarantined or otherwise
   untrusted states still fail closed.
3. **Malformed broker truth — confirmed.** Remote IDs must be present and
   unique. Filled quantities must be finite, nonnegative, status-consistent,
   bounded by remote quantity, and consistent with the local snapshot.
4. **Mutation-built final-authority aliases — confirmed.** Insertion of a
   security authority into a mutable collection and later subscript use is
   rejected as unproven.
5. **Bound `Query.update` alias — confirmed.** Method provenance and keyword
   `values` survive aliasing, so registered plaintext writes are rejected.
6. **Nested keyword-only helper flow — confirmed.** Local call propagation
   now carries keyword-only model provenance; unsupported flows fail closed.
7. **Mutation-built security call identities — confirmed.** Collection
   indirection around CORS, HTTP clients, cookies, and SSL factories is
   rejected while a canonically local non-network `Client(verify=False)`
   remains a clean decoy.
8. **Migration test synchronization — confirmed test defect.** The old hook
   stopped at the first later `drop_index`, which now belongs to revision
   0014. The test now waits for the exact revision 0013
   `ux_untrusted_ingest_source_content` drop/lock point. No migration
   production code changed.

Operator wording now states the actual invariant: preflight performs no
trading-table DML. SQLite setup may configure WAL and sidecar permissions;
that maintenance is intentionally retained.

## Exact RED evidence

Before implementation:

```text
Runtime/provider/preflight/docs bundle:
22 failed, 2 passed

Exact static alias bundle:
8 failed

Exact migration synchronization command:
2 failed
```

The two runtime passes were already-closed subclaims: quarantined fills were
already rejected, and an empty remote broker ID was already rejected. The
first migration failure deterministically reported the revision 0014
`ix_runtime_tenures_expires_at` drop instead of the required revision 0013
`ux_untrusted_ingest_source_content` point. The second failure followed the
first aborted worker and reflected contaminated Alembic authority state; it
is not represented as a second production finding.

After the first full-suite process failure, two minimal test-infrastructure
regressions were added before the fix:

```text
uv run pytest -q \
  tests/test_task6_transaction_directory_hardening.py::test_crash_fixture_processes_use_fresh_interpreter_context \
  tests/test_task6_transaction_directory_hardening.py::test_timed_out_fixture_process_is_terminated_and_reaped

2 failed
```

The failures were exact: the fixture selected `fork`, and
`_join_fixture_process` did not yet exist.

## Process-hang diagnosis and correction

The first no-argument round-4 suite reported:

```text
uv run pytest
1 failed, 3651 passed, 1 skipped, 1 warning in 537.89s
```

The exact failed boundary was:

```text
tests/test_task6_transaction_directory_hardening.py::
test_recovery_preserves_unrecorded_old_sidecar_name[verification_opened]
```

`ForkProcess-107` (PID `56072`) remained alive after the parent test's
`process.join(timeout=10)`, leaving `exitcode is None`; the assertion then
failed without terminating or reaping the child. Pytest later waited during
multiprocessing shutdown. The stage hook itself was not a deterministic
production hang: the complete ten-parameter node group passed `10/10`, and
the pre-fix complete file passed `57/57` when run in isolation.

When asked to stop only parent PID `53460`, child PID `56072`, and resource
tracker PID `55923`, a read-only process check found that all three had
already exited. No signal was sent.

The test-only fix uses a fresh `spawn` interpreter and one bounded helper for
every process wait. On timeout the helper terminates, joins, escalates to kill
only if still alive, joins again, and then fails with stable fixture text.
Production backup and migration behavior is unchanged.

## Final verification

Initial round-4 gates before the first full run:

```text
Runtime/docs:
24 passed

Exact static probes:
8 passed

Exact migration race:
2 passed

Complete static fixture file:
304 passed

Secret provider, watchdog, and round-3:
187 passed

Sensitive migration subset:
17 passed

Preflight:
30 passed

31-file Task 11 matrix:
2036 passed, 1 warning in 308.48s

Repository static gate:
release static checks: PASS

Compileall, git diff --check, and shell syntax:
PASS
```

Hang-fix focused proof:

```text
New spawn/reap regressions:
2 passed

Exact previously failing node:
1 passed in 0.65s

Complete crash-fixture file:
59 passed

Combined focused collection:
841 passed, 1 warning
```

After the focused fix, the unchanged 31-file Task 11 matrix plus the complete
crash-fixture file passed:

```text
2095 passed, 1 warning in 368.08s
```

The repository static gate again reported
`release static checks: PASS`; compileall, `git diff --check`, and syntax
checks for `setup-local-tls.sh`, `start.sh`, `stop.sh`, and both launchd
scripts exited zero.

The user expressly authorized one replacement no-argument suite after the
hang root cause was fixed and every focused/matrix/static prerequisite was
green:

```text
uv run pytest
3654 passed, 1 skipped, 1 warning in 615.81s
```

Pytest exited normally. The warning is the existing third-party
`websockets.legacy` deprecation warning.

## Residual hard limits

- Provider role projection is now enforced before Keychain retrieval. The
  watchdog cannot request or receive Alpaca, LLM, signing, encryption,
  Telegram, or app-token fields.
- Preflight remains capability-minimal and read-only with respect to trading
  tables. Superseded tombstones are excluded, while malformed or quarantined
  broker truth fails closed.
- Unsupported/dynamic security-boundary Python remains rejected by design.
- Composio remains disabled pending provider-side revocation/rotation. It has
  no origin, route, caller, toolkit, MCP surface, or chat tool. No webhook or
  live-mode path exists.
- General chat remains read-only. Immutable drafts require an explicit signed
  queue action and separate human approval. Paper mode, kill switches,
  broker-truth checks, and the no-profit-guarantee limit are unchanged.
- Verification used temporary Git roots, fakes, temporary SQLite databases,
  and hermetic process fixtures only. No app, daemon, MCP, ignored runtime
  database, real Keychain/credential, network, broker/provider/notifier call,
  trading action, reconciliation write, notification, breaker reset, or push
  occurred.

# Task 11 Final Bounded Role-Visibility Correction

## Supersession and verified scope

Round 4 correctly moved projection ahead of Keychain retrieval and correctly
reduced the watchdog to `database_url`. It overclaimed that the
startup-required-field map also represented every field a role could consume
in an optional branch. This correction supersedes only that claim.

The existing standalone watchdog fake-Keychain test was run before any
implementation change and passed. It already asserted exact account access,
exact returned database visibility, no encryption-key mapping, and empty
Alpaca, LLM, signing, backup, Telegram, app-token, and live-confirmation
fields. The reviewer subclaim that this proof was missing was therefore
invalid; the hermetic counterexample remains in the suite.

The audit confirmed three projection defects:

1. `safety-drill --alpaca-paper` consumes optional Alpaca paper credentials,
   but round 4 projected them away.
2. Preflight consumes `live_trading_confirm` when evaluating dangerous
   switches, but round 4 projected it away and could falsely report all
   switches disabled. App, daemon, and safety-drill also have branch-visible
   live-confirmation authority.
3. MCP, paper-drill, and safety-drill may consume the selected LLM credential
   when their news branch is enabled, but round 4 projected every LLM field
   away for those roles.

Backup, migration, validate-analyst, and watchdog had no additional optional
simple-secret branch omissions. Existing candidate-signing, backup-encryption,
and configured field-encryption key visibility remains because startup
key-material validation consumes it for every role except watchdog.

## Authority model and TDD evidence

The implementation now has two distinct immutable authorities:

- startup-required fields preserve the existing nonempty validation contract;
- role-visible fields enumerate the maximum exact field capability for each
  role before configuration narrows it.

The resolver exposes only the selected LLM provider, exposes news LLM
credentials only when news is enabled for an audited news role, exposes
Telegram credentials only when the feature is enabled for an authorized
role, and exposes no field outside that role's canonical tuple. It verifies
that every startup-required field is visible and otherwise fails closed with
`authority_mismatch`. There is no broad all-fields fallback.

The exact pre-implementation command selected the standalone watchdog proof,
the all-role account-access matrix, the safety-drill Alpaca branch, the
preflight consumer, and the three news roles:

```text
9 failed, 6 passed
```

The six passes included the already-safe standalone watchdog proof. The nine
failing cases were four role-matrix cases (`app`, `daemon`, `preflight`, and
`safety-drill`), the direct safety-drill consumer, the direct preflight
consumer, and three selected news-provider cases. After implementation the
same selection passed `15/15`. A final characterization also proves missing
Alpaca credentials do not become startup-required for safety-drill.

## Final verification

```text
Focused secret/preflight/safety-drill/watchdog:
353 passed, 1 warning

33-file affected trust matrix:
2182 passed, 1 warning in 385.43s

Repository static gate:
release static checks: PASS

Compileall:
PASS

git diff --check:
PASS

Exactly one no-argument full suite:
3660 passed, 1 skipped, 1 warning in 609.41s
```

Pytest exited normally. The warning is the existing third-party
`websockets.legacy` deprecation warning. Implementation commit
`b6cee46bbddc3cae147c1cdaa9b3f970a96d6dbb` changes only
`src/trading_assistant/security/secrets.py` and
`tests/test_task11_round4.py`.

The correction used fake Keychain account access and local fixtures only. It
did not start services or access external systems. Paper-only, explicit
signed-queue, separate-human-approval, kill-switch, broker-truth, no-webhook,
Composio-disabled, and no-profit-guarantee boundaries are unchanged.

# Whole-Plan-2 Integration Closure

## Supersession and verified findings

This closure supersedes the prior claim that MCP, paper-drill, and safety-drill
consume optional news-provider credentials. Production call tracing confirmed
that their containers did not use the quarantine summarizer. Retaining those
credentials and constructing an app-role summarizer from a maintenance root
was excess authority, not a required capability.

All three requested integration findings were confirmed:

1. The public automatic app factory loaded ambient config and Keychain secrets
   and built a container without the startup/TLS receipt required by
   `ops.serve`.
2. Paper-drill borrowed the app role, safety-drill test containers defaulted
   to app, and the three non-news roots could construct or receive unused LLM
   capability.
3. `live_trading_enabled` still returned true for one legacy config/secret
   combination, and planning could persist non-paper-only authority.

## Implemented boundaries

The public `create_app` accepts only an exact config, exact role-scoped secrets,
and a one-shot startup receipt. `build_default_container` consumes that receipt
through the same private guarded-container path used by `ops.serve`. Missing,
mismatched, wrong-role, fabricated, or reused receipts fail before broker or
trading-service authority can be composed. Explicit injected stacks are
available only through the named `create_test_app` test boundary. No committed
module-level ASGI object or alternate Uvicorn factory invocation bypasses the
launcher.

Runtime role identity is now preserved:

- app and daemon are the only optional-news roots and pass their exact role to
  the selected LLM adapter;
- MCP remains `mcp` and has no news LLM secret or summarizer;
- paper-drill remains `paper-drill`, uses that role for origins and adapters,
  and holds mutually exclusive maintenance tenure rather than borrowing app
  tenure; and
- safety-drill passes `safety-drill` through its explicit fake-container path
  and its credentialed paper adapter boundaries. Production
  `build_container(..., runtime_role="safety-drill")` is rejected.

The role-visible secret authority no longer includes LLM credentials for MCP,
paper-drill, or safety-drill. Required-field validation, optional Alpaca paper
credentials for safety-drill, live-confirmation diagnostics, watchdog's exact
database-only projection, and key-material validation remain unchanged.

The affirmative live contract is removed. `TradingMode.LIVE` and
`live_trading_confirm` remain parseable only so configuration and preflight can
diagnose and reject them. `live_trading_enabled` cannot return true, production
bootstrap rejects every live configuration, broker/clock factories remain
paper-targeted, and planning always persists `paper_only=True`. Crypto remains
a supported paper-runtime asset class only.

## TDD and verification evidence

The exact pre-implementation selection, while production was untouched,
reported:

```text
26 failed, 5 passed
```

The failures covered ambient automatic app construction, missing receipt
plumbing and reuse protection, public unguarded injection, all nine
non-news-root adapter combinations, paper-drill app-role borrowing, all nine
non-news-root selected-secret combinations, safety-drill role loss, the exact
legacy live confirmation combination, and live planning promotion.

Post-implementation verification:

```text
Focused integration/config/role/asset proof:
78 passed, 1 warning

Final receipt/tenure composition file:
22 passed

Explicit 33-file affected matrix:
2022 passed, 1 warning

Repository static gate:
release static checks: PASS

Compileall, git diff --check, and shell syntax:
PASS

Exactly one no-argument full suite:
3700 passed, 1 skipped, 1 warning in 607.63s
```

Pytest exited normally. The warning is the existing third-party
`websockets.legacy` deprecation warning. No no-argument full suite ran before
the focused, affected, static, compile, diff, and shell prerequisites were
green, and no second full suite ran.

## Review package and residual limits

- Base: `b1e20161ae3e589338c96c1038ab147a298aa6b4`
- Implementation: `5d34837ef461b12ad4c5e7f8ea49f5e700cee2c1`
- Bounded diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-b1e2016..5d34837.diff`
- Review package:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/task-11-review-package-plan2-integration.md`

The public production factory cannot load ambient authority and direct Uvicorn
factory startup remains unsupported. Test-only injection is explicit and
requires supplied fakes. Non-news roles cannot receive or construct LLM
capability. Live inputs cannot enable live authority. General chat remains
read-only; immutable drafts still require an explicit signed queue action and
separate human approval. Composio remains disabled pending provider-side
revocation/rotation, there is no webhook, and no profitability claim is made.

Verification used temporary SQLite databases, fake Keychain/providers, fake
brokers, and hermetic local fixtures only. It did not start app/daemon/MCP,
access the ignored runtime database or real credentials/Keychain, make a
network/broker/provider/notifier/integration call, trade, reconcile, notify,
reset a breaker, or push.
