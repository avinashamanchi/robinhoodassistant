# Terminal Operator Workflow and Canonical Checkout Design

**Date:** 2026-07-30
**Status:** Approved design
**Repository boundary:** `/Users/avi/Desktop/robinhood/trading-assistant`
**Trading boundary:** Alpaca paper only; every broker submission remains human-gated

## 1. Purpose

Add a guided, plain-text terminal interface that lets the local operator run
the reviewed trading workflow without relying on the browser UI. One command
starts or verifies the secure loopback application and opens the menu.
Monitoring remains off until the operator explicitly starts it from the menu.

The same change set will make the normal Desktop checkout the canonical source
and runtime location while preserving the paper-trading database currently
used by the `codex/safety-foundation` worktree.

This work does not:

- enable live trading;
- promise or imply profitability;
- allow the terminal process to construct a broker client;
- allow an LLM, daemon, or menu action to approve an order automatically;
- bypass the application session, CSRF, reauthentication, idempotency, risk,
  breaker, reconciliation, or audit boundaries;
- commit Keychain values, TLS private keys, databases, logs, caches, or local
  backups to Git.

## 2. Existing state

The complete implementation is currently on
`codex/safety-foundation` in the Desktop repository's
`.worktrees/safety-foundation` checkout. GitHub PR #2 targets `main`; its
existing checks pass before this feature begins. The normal Desktop checkout
is clean but stale.

The application already exposes the required authenticated control-plane
operations for:

- health, security posture, account, positions, and logs;
- analyst proposal generation and immutable plan review;
- plan approval and cancellation;
- pending-order review, approval, rejection, and cancellation;
- synchronization, reconciliation, panic, and breaker reset.

The service also supports listing and canceling rules, but those operations are
not yet exposed through the authenticated HTTP control plane. The only existing
terminal trading command is a bounded paper drill. It is not a general operator
interface.

## 3. Approaches considered

### 3.1 Local HTTPS API client plus guided menu — selected

The terminal logs into the existing loopback HTTPS application and invokes the
same routes used by the browser UI. It keeps the session cookie and CSRF token
in process memory only. This preserves one application authority and one
submission path.

### 3.2 Direct in-process service client

The terminal could load Keychain secrets, construct `TradingService`, and call
the broker path directly. This is rejected because it creates another mutable
database and broker authority, complicates runtime tenure, and risks bypassing
HTTP policy, rate limits, recent authentication, and route-level audit
receipts.

### 3.3 Shell and `curl` wrapper

A shell menu could compose raw requests. This is rejected because cookie,
CSRF, TLS, JSON, idempotency, rate-limit, and stable-error handling would be
brittle, and credentials would be easier to leak through arguments, history,
or debug output.

## 4. Non-negotiable invariants

- The terminal connects only to the configured
  `https://localhost:8020` loopback origin.
- The terminal verifies the configured local CA and has no insecure TLS mode.
- The terminal never accepts a remote base URL, proxy override, or
  certificate-bypass flag.
- Operator secrets are entered with a no-echo prompt, retained only for the
  immediate login or reauthentication request, then have their references
  dropped. They are never persisted, printed, logged, placed in an argument,
  or copied into an environment variable. Python does not promise zeroization
  of immutable string storage, so the design does not claim it.
- Session cookies, CSRF tokens, review tokens, and idempotency keys remain in
  process memory only.
- The CLI has no `AlpacaBroker`, `TradingService`, database session, or direct
  order-submission dependency.
- Proposal generation never approves a plan, starts monitoring, queues an
  order, or submits to Alpaca.
- Plan approval creates paper-only monitored rules; it does not constitute
  order approval.
- A triggered rule creates a pending order proposal only.
- Order approval always refreshes confirmation evidence and passes the
  execution-time risk engine before the existing application can reach Alpaca
  paper submission.
- Monitoring starts only through an explicit menu action.
- Live mode, automatic preapproved-rule execution, automatic bracket
  submission, cross-provider fallback, inbound webhooks, and Composio remain
  disabled.
- Unknown, stale, partial, or conflicting evidence is shown as blocked or
  unknown, never as ready.

## 5. User entry point

The canonical command is:

```bash
cd /Users/avi/Desktop/robinhood/trading-assistant
./scripts/operator.sh
```

`scripts/operator.sh`:

1. resolves and verifies the canonical repository root;
2. refuses a symlinked root, missing virtual environment, or unexpected
   checkout;
3. verifies the local CA file and fixed loopback origin;
4. probes anonymous liveness with certificate verification;
5. invokes the existing strict `scripts/start.sh` only when the application is
   not live;
6. rechecks exact process ownership and HTTPS liveness;
7. executes the terminal menu with the repository virtual-environment Python.

The launcher does not start the daemon, run analysis, reset a breaker,
reconcile, approve, reject, cancel, or submit an order as a side effect.

## 6. Terminal architecture

### 6.1 `OperatorApiClient`

A small standard-library HTTPS client owns:

- an `ssl.SSLContext` rooted only in `.local/tls/rootCA.pem`;
- a memory-only cookie jar;
- login, reauthentication, and logout;
- JSON request and stable error-envelope parsing;
- CSRF attachment for every mutation;
- fresh random idempotency keys for routes whose policy requires them;
- bounded connect/read timeouts and response-size limits;
- explicit handling for `401`, `403`, `409`, `422`, `429`, and `503`;
- redacted exceptions that never include headers, cookies, credentials, raw
  request bodies, or provider responses.

The base origin comes from strict application configuration and must normalize
to the fixed loopback HTTPS origin. Command-line URL overrides are forbidden.

### 6.2 `OperatorMenu`

The menu is line-oriented rather than curses-based. It uses only the Python
standard library so it remains readable in Terminal, SSH-disabled local
shells, CI transcripts with a fake client, and accessibility tools.

Input is parsed as menu choices and bounded typed fields. It is never evaluated
as Python, passed to a shell, interpolated into a URL path without validation,
or treated as authority because it came from model output.

The menu owns only presentation and short-lived review state. Every fact is
refetched from the application before a consequential action.

### 6.3 No direct broker authority

The terminal imports neither broker adapters nor mutable service composition.
All Alpaca reads and writes occur in the already guarded application. A static
test fails if the terminal module imports broker, database, bootstrap,
submission, or service modules.

## 7. Menu structure

The top-level menu is:

1. **System status**
   - application health;
   - security posture;
   - reconciliation state;
   - daemon heartbeat and locally supervised daemon status;
   - breaker state and evidence timestamps.
2. **Alpaca paper account**
   - account summary;
   - buying power and equity;
   - positions with source and observation time.
3. **Generate analyst proposals**
   - ask for proposal count and an operator reason;
   - show that paid LLM calls may occur;
   - require `GENERATE <count>` before the request;
   - display each result as unproven analysis.
4. **Plans**
   - list plans;
   - review one immutable plan and sizing payload;
   - approve only a plan reviewed in the current terminal session;
   - cancel a plan with a reason.
5. **Rules**
   - list active and inactive rules;
   - cancel one rule with a reason and idempotency key.
6. **Pending orders**
   - list pending proposals;
   - refresh and display approval-confirmation evidence;
   - approve, reject, or cancel with a reason.
7. **Monitoring**
   - start the daemon explicitly;
   - show supervised process status and application heartbeat;
   - stop only the exact child started by this menu.
8. **Operations**
   - synchronize open orders;
   - run reconciliation;
   - display redacted logs.
9. **Emergency safety**
   - panic;
   - breaker reset using a fresh generation and required reason.
0. **Log out and exit**

The browser UI remains available; terminal and browser actions share the same
server-side concurrency, rate, idempotency, and audit controls.

## 8. Review and confirmation gates

### 8.1 Proposal generation

Before `/propose`, the terminal states that the analyst is unproven and that
paid model calls may occur. The operator supplies the count and reason, then
types `GENERATE <count>`. Canceling or EOF performs no request.

### 8.2 Plan approval

The terminal must fetch and render `/plans/{plan_id}` in the current process
before approval. It caches the returned immutable review token only in memory.
Immediately before approval it refetches the plan and refuses if the token,
status, or authority digest changed. The operator supplies a reason, locally
reauthenticates, and types:

```text
APPROVE PAPER PLAN <plan_id>
```

The terminal then sends the server-provided review token to the existing plan
approval route with a fresh idempotency key.

### 8.3 Order approval

The terminal must fetch
`/pending/{order_id}/confirmation` immediately before approval and render the
symbol, side, size, order type, limit price, estimated exposure, quote
observation, expiry, breaker state, and reconciliation evidence. Missing or
unknown fields block the client before the server's independent checks.

The operator supplies a reason, locally reauthenticates, and types:

```text
APPROVE ALPACA PAPER ORDER <order_id>
```

The application then performs its atomic approval, proposal-TTL check,
execution-time snapshot, risk check, durable outbox transition, idempotent
submission, and broker reconciliation behavior. The terminal cannot weaken
any result.

### 8.4 Other consequential actions

Reject, cancel, sync, reconcile, panic, and breaker reset each require a
nonblank reason. Recent authentication is requested whenever the server policy
requires it. Panic and breaker reset use exact typed phrases and display the
scope and current generation. The terminal never retries an ambiguous broker
mutation automatically.

## 9. Monitoring lifecycle

Monitoring remains off when the menu starts. Selecting **Start monitoring**:

1. refreshes system posture and refuses a blocked startup;
2. refuses when a different daemon heartbeat or runtime tenure is current;
3. launches the exact repository interpreter with
   `-m trading_assistant.daemon.main`;
4. retains the concrete child handle instead of searching by process name;
5. confirms a fresh daemon heartbeat before reporting running.

The daemon writes through its existing bounded runtime logger. The menu does
not capture an unbounded pipe.

Selecting **Stop monitoring** sends an interrupt only to the exact child handle
created by this menu and waits for normal `Monitor.run()` cleanup and runtime
tenure release. It never uses `pkill`, PID globs, or a repository-wide process
search. A timeout is reported as `stop_unconfirmed`; the menu does not escalate
to an unconditional kill.

On normal menu exit, a supervised daemon is stopped through the same path.
Therefore continuous monitoring requires leaving the operator menu running.
Launch-on-login or detached daemon ownership remains a separate, explicitly
reviewed operational workflow.

## 10. HTTP additions

Add only the missing rule operations:

- `GET /rules` — authenticated, bounded read;
- `POST /rules/{rule_id}/cancel` — CSRF-protected, idempotent, audited,
  target-scoped mutation.

The route-policy inventory must classify both routes. Cancellation calls the
existing `TradingService.cancel_rule()` method with the authenticated actor,
operator reason, request ID, and idempotency receipt. No new order-submission
route is added.

## 11. Failure behavior

- Application unavailable: show the liveness failure and launcher log path.
- TLS failure: stop; never offer an insecure mode.
- Login or reauthentication failure: immediately drop the entered secret
  reference and show the stable request ID.
- Rate limit: show bounded `Retry-After`; do not loop.
- Session expiry: discard local cookie/CSRF state and return to login.
- Proposal or plan conflict: refetch and require a new review.
- Broker acceptance unknown: display `acceptance_unknown`; prohibit retry and
  direct the operator to reconciliation.
- Risk or breaker denial: display server reasons without offering a bypass.
- Daemon startup reconciliation failure: show blocked and leave persisted
  breakers untouched.
- Ctrl-C or EOF: perform no partially entered mutation and clean up only a
  daemon owned by this menu.

## 12. Canonical checkout consolidation

### 12.1 Source-code publication

Implementation remains on `codex/safety-foundation` until verified. Commits are
pushed to GitHub PR #2. After every required local and hosted check passes, PR
#2 is merged into `main`, and the normal Desktop checkout is advanced with a
fast-forward-only update.

No history rewrite, force push, hard reset, or destructive checkout is used.

### 12.2 Runtime authority

The database used by the currently running safety worktree is the source
runtime authority for consolidation. The normal checkout's database, if
present, is preserved separately before replacement. The two databases are
never merged row-by-row.

Before migration:

1. stop the menu-supervised daemon, application, MCP process, validation
   writer, watchdog, and every other database writer;
2. prove that no runtime or maintenance tenure remains current;
3. create and verify an encrypted backup of the source database;
4. create and verify a separate encrypted backup of the destination database
   when it exists;
5. record only redacted hashes and verification receipts.

### 12.3 Database transfer

A one-purpose migration command performs the transfer. It:

- resolves source and destination beneath the two exact repository roots;
- rejects symlinks, hardlinks, aliases, in-memory URLs, non-SQLite sources,
  group/world-writable directories, and active writers;
- opens the source through SQLite and writes a new mode-`0600` staging database
  with SQLite's backup API;
- verifies source and staging `quick_check`, migration head, application ID,
  schema state, and a deterministic logical summary;
- fsyncs the staging file and destination directory;
- installs the staging file atomically only after the destination backup
  receipt is durable;
- removes only verified stale destination sidecars while writers remain
  stopped;
- leaves the original worktree database and encrypted artifacts intact as
  rollback evidence.

It never persists an additional decrypted archival copy. Failure before atomic
installation leaves the destination unchanged; failure after installation is
reported as migration-uncertain and blocks startup pending manual review.

### 12.4 Canonical restart

After transfer:

1. verify local TLS in the canonical checkout;
2. verify current schema and every registered encrypted field;
3. run static release checks and structural preflight;
4. start the application from the canonical checkout;
5. perform read-only startup reconciliation against Alpaca paper truth;
6. confirm exact process ownership, HTTPS health, database path, broker paper
   endpoint, and blocked/ready state;
7. launch the terminal menu from the canonical path.

The old worktree is retained until the canonical runtime, GitHub branch, and
broker reconciliation evidence are all verified. Deleting it is outside this
feature and requires a separate operator decision.

## 13. Files

Expected additions:

- `src/trading_assistant/ops/operator_api.py`
- `src/trading_assistant/ops/operator_terminal.py`
- `src/trading_assistant/ops/runtime_consolidation.py`
- `scripts/operator.sh`
- `tests/test_operator_api.py`
- `tests/test_operator_terminal.py`
- `tests/test_operator_launcher.py`
- `tests/test_runtime_consolidation.py`
- this specification and an implementation plan

Expected modifications:

- `src/trading_assistant/app/main.py`
- `src/trading_assistant/app/policy.py`
- `pyproject.toml`
- `README.md`
- `docs/RUNBOOK.md`
- route-policy, security, launch, and release-verifier tests

The exact implementation may merge small modules when doing so preserves clear
interfaces and test isolation. It may not combine the API client with direct
broker or database authority.

## 14. Testing strategy

### 14.1 Terminal client

- exact loopback HTTPS origin and CA verification;
- no insecure or remote URL option;
- bounded timeouts and response sizes;
- memory-only cookie and CSRF handling;
- stable error parsing and redaction;
- no credential in arguments, environment, output, tracebacks, or logs;
- correct idempotency headers and no retry after ambiguous mutations.

### 14.2 Menu

- login and reauthentication do not retain secret references after the
  immediate request;
- proposal cost warning and exact confirmation;
- plan view-before-approve and stale-token rejection;
- order confirmation refresh and exact paper-order phrase;
- cancel/EOF cause no mutation;
- `401`, `403`, `409`, `429`, and `503` remain distinct;
- output labels all broker effects as Alpaca paper;
- terminal module has no direct broker, service, bootstrap, submission, or
  database import.

### 14.3 Monitoring

- daemon is off at menu startup;
- blocked posture prevents launch;
- existing current daemon prevents a second launch;
- the exact child is tracked;
- normal stop releases tenure;
- stop timeout remains unconfirmed and never escalates broadly;
- menu exit cleans up only its owned child.

### 14.4 HTTP and policy

- route inventory includes both rule routes exactly once;
- rule listing requires a session;
- rule cancellation requires CSRF, idempotency, reason, target-scoped
  concurrency, and audit receipt;
- rule cancellation cannot submit an order.

### 14.5 Runtime consolidation

- both source and destination require verified backups;
- active writer/tenure, symlink, hardlink, path swap, bad permissions,
  non-SQLite source, bad schema, failed `quick_check`, fsync failure, and
  destination collision all fail closed;
- interruption before install leaves destination unchanged;
- interruption after install is visibly uncertain;
- successful transfer preserves logical state and leaves the source intact.

### 14.6 Regression and release gates

Run, in order:

1. new focused red/green tests;
2. authentication, route-policy, service, daemon, launch, TLS, and migration
   matrices;
3. the deterministic static release gate;
4. the full test suite with the existing coverage floor;
5. the offline loopback release verifier;
6. hosted GitHub secret-scan and verification jobs;
7. canonical-path liveness, login, read-only account, database, and broker
   reconciliation checks.

No verification step submits an Alpaca order automatically. The first
terminal-originated paper order remains a separate, explicit operator approval.

## 15. Acceptance criteria

The feature is complete only when:

- `./scripts/operator.sh` from the normal Desktop checkout opens the guided
  menu over verified loopback HTTPS;
- startup never starts monitoring implicitly;
- the menu can generate and review analyst plans;
- plan and order approvals require fresh, explicit human confirmation;
- any resulting broker submission uses the existing Alpaca paper path and
  execution-time risk engine;
- every terminal and HTTP mutation is authenticated, rate-limited,
  idempotent where required, and audited;
- the menu can explicitly start and cleanly stop its owned monitoring daemon;
- current paper history is present in the canonical checkout after verified
  consolidation;
- local and hosted verification pass on the exact pushed commit;
- GitHub `main`, PR evidence, and the normal Desktop checkout identify the
  expected commit;
- no secret or local runtime artifact is tracked;
- documentation gives exact start, recovery, and terminal workflow commands.

## 16. Implementation sequence

The implementation plan separates the work into four reviewable stages:

1. loopback API client, rule routes, and guided menu;
2. strict launcher and menu-owned daemon supervision;
3. tested one-time runtime consolidation and canonical restart;
4. full release verification, GitHub merge, and normal-checkout handoff.

The canonical runtime cutover does not begin until the terminal feature and
consolidation utility pass their deterministic test matrices. Publication does
not begin until the exact candidate commit passes the full local release gate.
