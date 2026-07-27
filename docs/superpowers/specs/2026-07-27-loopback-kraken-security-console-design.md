# Loopback Kraken-Inspired Security Console Design

**Date:** 2026-07-27
**Status:** Approved design
**Deployment boundary:** One local operator on loopback HTTPS
**Trading boundary:** Alpaca paper only; manual approval remains mandatory

## 1. Purpose

Rework the existing trading assistant into a dark, data-dense operator console
inspired by the publicly documented Kraken design language, while making the
application's cost, secret, API, model, and ingress controls enforceable rather
than cosmetic.

The completed release must make five facts immediately visible:

1. what Alpaca paper and the local database prove now;
2. why trading is allowed or blocked;
3. how much paid-model and provider capacity remains;
4. whether secrets, sensitive persistence, and local transport are protected;
5. which actions were proposed, approved, rejected, or refused.

This work does not promise profitability, enable live trading, reset a breaker,
or manufacture evidence to pass an operational gate.

## 2. Existing controls and gaps

The current application already has server-side sessions, CSRF checks, recent
reauthentication for privileged actions, a restrictive content security policy,
stable redacted errors, strict Pydantic configuration, SQLite WAL, idempotent
order flows, scoped persisted breakers, and endpoint-specific rate limits.

The implementation must preserve those controls while fixing these gaps:

- rate policies are scattered and several mutating routes have no explicit
  limiter;
- the limiter is process-local, resets on restart, and cannot coordinate the
  API and daemon;
- paid-call limits count outer HTTP requests, not internal provider attempts or
  fan-out;
- CORS origins are hard-coded to port 8000 while the operator may use another
  configured port;
- no trusted-host middleware rejects hostile `Host` headers;
- production secrets are loaded from environment files rather than an
  operating-system secret store;
- sensitive free text is stored as plaintext in SQLite;
- general chat can expose mutating proposal and rule tools to the same model
  that reads arbitrary text;
- there is no route-policy inventory that fails when a new endpoint is left
  unclassified;
- there is no webhook endpoint today, but there is also no explicit invariant
  preventing one from being exposed accidentally.

## 3. Approaches considered

### 3.1 Visual reskin plus targeted patches

Restyle the current pages and add missing checks inline. This is fast but leaves
security decisions distributed across handlers and makes future omissions
likely.

### 3.2 Central policy plane plus operations deck — selected

Introduce small, testable services for route policy, durable budgets, secrets,
encrypted fields, untrusted model input, and security posture. Rebuild the
static frontend around those contracts. This fits the current single-host,
SQLite-backed architecture and creates one place to verify every boundary.

### 3.3 Redis, Vault, and reverse-proxy production stack

Move rate limits to Redis, secrets to a remote vault, and ingress behind a
public proxy. Those systems are appropriate for multiple hosts or public
traffic, but add operational dependencies without improving this loopback-only
release.

## 4. Non-negotiable invariants

- Bind only to `127.0.0.1` or `::1`.
- Serve the operator runtime over locally trusted HTTPS.
- Reject wildcard, LAN, or public binding at startup.
- Permit only `localhost`, `127.0.0.1`, and `::1` host headers.
- Expose no inbound webhook route.
- Keep API documentation and debug endpoints disabled.
- Keep Alpaca paper mode hard-locked.
- Keep human execution approval, execution-time risk checks, idempotency,
  reconciliation, and persisted circuit breakers.
- Never reset a breaker or start the daemon as a side effect of installation,
  migration, page load, or security setup.
- Never place API credentials in SQLite, source control, logs, browser storage,
  screenshots, audit payloads, or model context.
- Never present unknown, stale, or partial evidence as healthy.

## 5. Component architecture

```text
Browser on loopback HTTPS
          |
          v
Trusted host + same-origin + request bounds
          |
          v
Route policy registry
  | auth / CSRF / recent auth
  | per-session + global durable rate budget
  | provider/token reservation
          |
          +------> read-only application services
          |
          +------> audited mutation services
          |
          +------> quarantined model boundary
                         |
                         +--> read-only research model
                         +--> structured candidate output
                                      |
                                      v
                             explicit operator action
                                      |
                                      v
                             deterministic risk path

macOS Keychain ----> runtime secrets + encryption keys
SQLite WAL --------> counters, reservations, encrypted sensitive fields
Alpaca HTTPS ------> paper broker truth and market data
```

The new units are:

- `RoutePolicyRegistry`: the complete method/path policy inventory;
- `DurableRateLimiter`: atomic cross-process windows and concurrency leases;
- `ProviderBudgetService`: reserves and settles paid-call/token allowances;
- `SecretProvider`: obtains runtime secrets without persisting them;
- `SensitiveDataCipher`: authenticated, versioned field encryption;
- `UntrustedContentGateway`: normalizes and quarantines external text;
- `SecurityPostureService`: read-only status for the operator UI.

Each unit has one interface, no broker authority, and deterministic tests.

## 6. Route and resource policy

### 6.1 Complete route inventory

Every HTTP route must have one named policy. A test introspects FastAPI's route
table and fails if a method/path is missing, duplicated, or assigned a weaker
policy than its action requires.

Each policy specifies:

- anonymous, authenticated, CSRF-protected, or recently reauthenticated;
- request-body byte limit;
- per-principal and global request windows;
- concurrency limit;
- whether the route may read the broker;
- whether the route may invoke an LLM or another paid provider;
- whether an idempotency key and audit receipt are required.

Static assets and loopback liveness receive explicit policies rather than an
implicit exemption.

### 6.2 Default limits

All values live under strict `security` configuration with
`extra="forbid"`. Unknown keys fail startup.

| Policy | Principal limit | Global limit | Concurrency |
| --- | ---: | ---: | ---: |
| Login | 5 / 15 minutes per source | 20 / 15 minutes | 2 |
| Session/status read | 120 / minute | 240 / minute | 16 |
| Broker-backed read | 30 / minute | 60 / minute | 4 |
| Ordinary mutation | 20 / minute | 40 / minute | 4 |
| Approval/plan approval | 10 / 5 minutes | 20 / 5 minutes | 1 per target |
| Breaker reset/reconcile | 5 / 5 minutes | 10 / 5 minutes | 1 |
| Chat | 10 / 10 minutes | 20 / 10 minutes | 1 per session |
| Analysis/screen/propose | 5 / 10 minutes | 10 / 10 minutes | 1 |
| Backtest start | 2 / hour and 6 / day | 6 / day | 1 |

`/panic` is not placed behind a long cooldown that could deny the first
emergency action. The first valid, recently reauthenticated request acquires a
single account-scoped lease. Concurrent duplicates coalesce onto the same panic
receipt; subsequent requests receive that receipt rather than initiating more
broker calls.

The browser receives `429` with a stable error code, `Retry-After`, and reset
metadata. Limits never include secrets or raw request content in their keys.

### 6.3 Durable enforcement

SQLite WAL is the authority for rate windows, concurrency leases, and daily
provider budgets. Atomic compare-and-update statements prevent parallel
processes from overspending the same allowance. An in-memory check may reject
obvious bursts early but can never grant authority by itself.

Limiter database failure is fail-closed for paid calls, broker reads, and
mutations. Loopback liveness may continue reporting the degraded state.

## 7. Paid-provider budget

HTTP request counts are not sufficient because one request can fan out or
retry. Every provider attempt must acquire a durable reservation immediately
before network I/O.

Default aggregate LLM ceilings are:

- 100 provider attempts per day;
- 1,000,000 input tokens per day;
- 200,000 output tokens per day;
- the existing per-request output-token maximum;
- eight tool-loop turns for chat and two structured-plan attempts;
- no LLM use in ordinary backtests unless a separate backtest LLM budget is
  explicitly enabled.

The service reserves a conservative provider-specific input estimate plus the
worst permitted output before calling a provider and settles against returned
usage afterward. If the input estimator is unavailable, the request is denied
rather than sent without an input reservation. If acceptance or usage is
unknown, the reservation remains charged. Retries and repair attempts each
count. Expired reservations may be released only when the durable state proves
the provider call never started.

USD cost is displayed as an estimate using explicitly configured, versioned
price metadata. Tokens and call counts are the hard authority because provider
pricing can change. Provider-console spending caps remain an independent outer
control.

Backtests additionally enforce one active run, a 20-minute runtime limit, a
configured symbol/date ceiling, and cancellation on budget exhaustion.

## 8. Secret lifecycle and sensitive persistence

### 8.1 Runtime secrets

`SecretProvider` supports:

- `MacOSKeychainSecretProvider` for the real local runtime;
- `EnvironmentSecretProvider` for tests and explicitly selected development.

The production composition root, API, daemon, MCP process, and start script all
refuse a non-Keychain secret provider. A migration command prompts through
`getpass`, writes values directly to Keychain, verifies retrieval, and never
prints them. `.env` remains a one-time migration input, must be ignored by Git,
and must have mode `0600`.

The application loads secrets once at composition-root startup, registers only
their values with the existing redactor, and passes the typed secret object to
provider factories. Secret values never enter error messages or status
responses.

The previously posted Composio credential is never used. Composio remains
disabled until the credential is revoked in an authenticated Composio account
and a new restricted credential is stored in Keychain. This repository cannot
truthfully assert provider-side revocation without that account receipt.

### 8.2 Database encryption

Credentials and session tokens are not reversibly encrypted:

- API credentials are never stored in the database;
- operator secrets remain outside the database;
- session and CSRF tokens remain one-way hashed.

Sensitive narrative fields use AES-256-GCM authenticated encryption:

- LLM prompt, tool-call input, and reasoning summary;
- proposal reasoning;
- approval and mutation reasons;
- detailed audit payloads;
- analysis and plan narrative blobs when they can include external text.

Queryable operational fields such as status, timestamps, symbols, quantities,
prices, breaker scope, and token counts remain plaintext so safety logic and
reconciliation do not depend on decryption.

Each encrypted envelope carries version and key ID. A unique nonce is generated
per field, and associated data binds ciphertext to table, row, column, and
schema version. The active key and retained rotation keys live in Keychain,
never beside the database.

Migration creates an encrypted backup before rewriting existing plaintext.
Startup refuses mixed plaintext/ciphertext state after the migration is marked
complete. Key rotation writes a new key, re-encrypts in bounded transactions,
verifies every row, and retires the old key only after verification.

FileVault and encrypted off-device backups remain required defense-in-depth;
field encryption does not replace host-disk protection.

## 9. Loopback HTTPS and endpoint perimeter

The normal operator URL becomes `https://localhost:{APP_PORT}`. A setup
command creates a locally trusted certificate for `localhost`, `127.0.0.1`, and
`::1`. Certificate files and private keys are untracked; private-key permissions
must be `0600`.

Startup checks:

- bind address is loopback;
- certificate and private key exist and have safe permissions;
- secure cookies are enabled;
- allowed hosts are exact loopback names;
- no proxy-header trust is configured;
- no webhook feature is enabled.

The request boundary rejects authenticated or state-changing traffic whose URL
scheme is not HTTPS. This prevents a direct manual Uvicorn launch from turning
plain loopback HTTP into an accidental production path even if the start script
is bypassed.

Because the browser and API are same-origin, the normal deployment removes
cross-origin access instead of maintaining a CORS allowlist. Requests carrying
an `Origin` that does not exactly match the configured local HTTPS origin are
rejected. `TrustedHostMiddleware` rejects malformed or unexpected host headers.

Request middleware also enforces maximum body size, bounded header count and
length, accepted content types, request IDs, and finite handler/provider
timeouts. OpenAPI, ReDoc, debug routes, directory listing, and arbitrary static
file paths remain disabled.

Outbound clients use fixed provider base URLs, HTTPS verification, finite
timeouts, bounded responses, and redacted errors. User input can select a
symbol or identifier but never a URL, hostname, redirect target, or file path.

## 10. Webhook and DNS posture

There is no inbound webhook in this release. Tests assert that no route starts
with `/webhook` or `/hooks`, and the startup configuration has no enabled
webhook receiver.

Alpaca truth continues through authenticated outbound REST and WebSocket
clients. DNS is not treated as authentication: TLS certificate validation and
provider credentials remain required. Provider hostnames are exact allowlisted
configuration values; redirects to a different host are rejected.

A future public webhook receiver requires a separate approved design and
service boundary with HTTPS, provider-specific HMAC/signature verification,
signed timestamp tolerance, replay IDs, idempotent ingestion, body limits,
event/action allowlists, queue isolation, and maintained provider IP allowlists.

## 11. Prompt-injection boundary

### 11.1 Separate untrusted reading from action

General chat defaults to research mode and exposes read-only tools only.
External news, filings, documents, search results, connector messages, and
pasted third-party text are represented as `UntrustedContent`.

A quarantined model may read that content but receives no proposal, rule,
notification, broker, account, file, connector, or execution tools. It returns
a bounded structured summary containing facts, source references,
uncertainties, and injection flags.

The privileged analyst receives deterministic `MarketFeatures` plus the
structured summary. It never receives raw external text. Its only model output
is a validated analysis or proposal candidate.

### 11.2 Explicit proposal action

An LLM candidate does not mutate the database. The operator must use a separate
same-origin action to queue that exact candidate. The server then validates
symbol, side, type, size, price, freshness, allowlist, rate budget, and risk
inputs before creating a proposal.

Order execution remains a second, separate, recently reauthenticated approval
that repeats the full execution-time risk check.

Conditional-rule candidates use the same explicit queue step. General chat
cannot cancel rules or create mutable state.

### 11.3 Input and output controls

The untrusted gateway:

- enforces source-specific size and item limits;
- normalizes Unicode and rejects hidden control characters;
- removes active HTML, remote images, scripts, forms, and embedded data URLs;
- detects suspicious encoded instruction payloads for audit and quarantine;
- preserves source, publication time, receipt time, and content hash;
- never claims detection is a complete prompt-injection defense.

Model outputs must match Pydantic schemas with `extra="forbid"`. Deterministic
code rejects unknown tools, unexpected fields, invalid symbols, non-finite
numbers, excessive tool loops, unsafe URLs, and action drift.

The frontend renders all model and external text with `textContent`; it never
uses `innerHTML`. The existing self-only CSP remains and is tightened where
possible.

## 12. Visual system

The product is named and branded as the Trading Assistant, not Kraken. The
Kraken-inspired source is a visual reference only.

### 12.1 Tokens

- Canvas: `#0b0914`
- Primary surface: `#101114`
- Raised surface: `#171420`
- Interactive surface: `#201a2e`
- Border: `#302941`
- Primary purple: `#7132f5`
- Purple hover/border: `#5741d8`
- Purple deep: `#5b1ecf`
- Purple wash: `rgba(133, 91, 251, 0.16)`
- Primary text: `#f7f4ff`
- Secondary text: `#9497a9`
- Verified: `#2bc48a`
- Caution: `#f0b45d`
- Danger: `#ff647c`

Purple indicates navigation, focus, and neutral primary action. It never means
"safe to trade." Verified, caution, danger, blocked, stale, and unknown states
always include direct text and an icon or shape in addition to color.

The application uses local system fonts under the self-only CSP:

- display/UI: `Inter`, `SF Pro Display`, `Helvetica Neue`, sans-serif;
- numeric data: `SFMono-Regular`, `Cascadia Mono`, monospace.

Buttons use a maximum 12px radius. Panels use 12–16px radii, quiet borders, and
minimal shadow. There are no glossy gradients, oversized marketing heroes,
fake candlestick charts, or decorative market motion.

### 12.2 Main console layout

```text
┌─ product / environment / UTC clock / operator ─────────────────┐
├─ proof rail: PAPER | market | quotes | daemon | drift | breaker ┤
├────────────── account and exposure masthead ────────────────────┤
│ portfolio facts      │ cost + security posture │ quick actions │
├───────────────┬──────────────────────────┬───────────────────────┤
│ risk/breakers │ positions + pending      │ broker/daemon truth   │
│ sticky rail   │ decisions                │ and freshness         │
├───────────────┴──────────────────────────┴───────────────────────┤
│ research candidates / assistant / immutable activity ledger     │
└──────────────────────────────────────────────────────────────────┘
```

The proof rail is the strongest visual element after the account masthead.
Blocked evidence stays visible while the rest of the console remains usable.

Plans, backtests, and login pages inherit the same tokens and navigation.
Backtests retain the permanent simulated-performance warning. The interface
never displays a live-trading control.

### 12.3 Interaction and accessibility

- one restrained initial reveal; no continuous animation;
- `prefers-reduced-motion` disables transitions;
- visible keyboard focus and logical tab order;
- minimum 44px touch targets for consequential controls;
- accessible table headers and horizontal overflow at 360 CSS pixels;
- dialogs trap focus and restore it to their trigger;
- stale or failed refresh clears prior facts rather than leaving them current;
- destructive and approval controls include exact target, reason, and receipt.

## 13. Security posture API and UI

Add one authenticated, read-only posture endpoint that returns no secret
values. It reports:

- loopback bind and trusted-host status;
- HTTPS and secure-cookie status;
- secret-provider type and last successful load time;
- sensitive-field encryption version and migration state;
- remaining request/provider budgets and reset times;
- LLM calls and token usage for the current UTC day;
- webhook receiver disabled;
- untrusted-content quarantine counts;
- current breakers, daemon heartbeat, reconciliation age, and broker mode.

Every field has `status`, `observed_at`, and `detail_code`. The browser does not
infer readiness from partial fields. Missing posture evidence is unknown or
blocked, never green.

## 14. Error handling and recovery

- Secret-provider, encryption-key, TLS, unsafe-bind, or migration failure stops
  the normal app before it accepts operator traffic.
- Rate-store failure denies paid and mutating routes.
- Provider-budget exhaustion returns a stable refusal before network I/O.
- Unknown provider acceptance remains charged and requires reconciliation.
- Model parsing or injection-gateway failure produces no candidate and no
  mutation.
- Security posture failure cannot weaken an order or breaker decision.
- UI fetch failures clear stale values and show a request ID.
- No recovery path resets a breaker, starts the daemon, or submits an order.

## 15. Verification contract

### 15.1 Route and resource tests

- every FastAPI route has exactly one policy;
- anonymous access is limited to explicit liveness, login page, login, and
  immutable static assets;
- every mutation requires CSRF, and privileged mutations require recent auth;
- rate windows survive a new limiter/app instance on the same database;
- parallel API/daemon consumers cannot exceed one shared allowance;
- denied requests perform zero broker or provider calls;
- internal LLM fan-out and retries consume exact reservations;
- `Retry-After` and reset metadata are correct;
- panic duplicates coalesce and never double-submit cancellation.

### 15.2 Secret and encryption tests

- committed-tree and history scanners find no real credential;
- `.env`, database, certificate key, logs, and backups remain ignored;
- unsafe secret/private-key file permissions fail preflight;
- Keychain provider is injectable and never logs values;
- AES-GCM round trip, unique nonce, associated-data binding, tamper rejection,
  key rotation, and mixed-state rejection;
- plaintext migration uses an encrypted backup and leaves no sensitive
  plaintext columns populated;
- redaction covers provider and database exception paths.

### 15.3 Endpoint and model tests

- non-loopback bind, untrusted host, mismatched origin, proxy headers,
  oversized body, and unsafe content type are rejected;
- no webhook route exists;
- outbound redirects cannot escape the provider allowlist;
- direct, indirect, Base64, Unicode-smuggling, HTML, Markdown-image, and
  tool-manipulation prompts cannot reach a mutable tool;
- general chat cannot create or cancel state;
- candidate queueing requires an explicit operator request and remains
  non-executing;
- all candidates and approvals still traverse deterministic validation and
  risk.

### 15.4 UI and release tests

- all pages use the approved tokens and contain no Kraken trademark assets;
- paper mode and active blockers are visible at desktop and mobile widths;
- no `innerHTML`, inline script, remote font, remote image, or unsafe CSP
  exception;
- keyboard, focus, contrast, reduced-motion, loading, empty, stale, blocked,
  error, and success states are exercised;
- existing focused safety suites, full pytest suite, release gate, local browser
  console inspection, and fresh Alpaca paper preflight all pass before handoff.

## 16. Implementation sequence

This design is implemented in four bounded workstreams:

1. **Policy and budget foundation**
   - strict configuration;
   - route registry;
   - durable rate/concurrency service;
   - paid-provider reservations;
   - complete endpoint policy tests.
2. **Secret and model trust boundaries**
   - Keychain provider and local TLS setup;
   - sensitive-field encryption and migration;
   - untrusted-content gateway;
   - read-only chat and explicit candidate queueing.
3. **Kraken-inspired operator console**
   - shared tokens and shell;
   - proof rail, account/risk grid, posture panel, decisions, and ledger;
   - consistent plans, backtests, and login pages.
4. **Operational verification and publication**
   - security and accessibility review;
   - full regression and release gate;
   - fresh broker/preflight evidence;
   - commit, push, and draft pull-request update.

Each workstream must end green before the next begins. A blocked Alpaca
preflight remains a blocked release even if every software test passes.

## 17. Completion criteria

The work is complete only when:

- the application runs on loopback HTTPS with exact trusted hosts;
- all routes are centrally classified and durably rate-limited;
- every paid provider attempt is budgeted before network I/O;
- runtime credentials come from Keychain and no real credential is committed;
- selected sensitive fields are authenticated-encrypted and migrated;
- untrusted external text cannot reach mutable tools;
- no webhook endpoint is exposed;
- the dark, data-dense console accurately presents broker, risk, cost, and
  security posture;
- the complete existing safety behavior remains unchanged or stricter;
- tests, release gate, browser checks, and fresh Alpaca paper evidence are
  recorded;
- no claim of profitability, live trading, or daemon operation is made without
  corresponding evidence.
