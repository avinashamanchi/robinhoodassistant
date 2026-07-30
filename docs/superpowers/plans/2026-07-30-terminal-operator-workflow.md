# Terminal Operator Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide one safe terminal command for reviewing analyst proposals, managing rules and pending orders, explicitly supervising the daemon, and reaching the existing Alpaca paper execution path only after fresh human approval.

**Architecture:** A standard-library terminal client talks exclusively to the existing authenticated loopback HTTPS application, preserving its session, CSRF, rate-limit, idempotency, audit, risk, and submission boundaries. A separate exact-child supervisor starts the daemon only on request. A one-use fail-closed consolidation utility moves the current runtime database into the canonical Desktop checkout only after both databases are durably backed up and all writers are proven absent.

**Tech Stack:** Python 3.11+, urllib/ssl/http.cookiejar, FastAPI, Pydantic, SQLAlchemy/SQLite WAL, pytest, Bash, Git/GitHub

## Global Constraints

- The governing specification is `docs/superpowers/specs/2026-07-30-terminal-operator-workflow-design.md`.
- The canonical repository and runtime root is exactly `/Users/avi/Desktop/robinhood/trading-assistant`.
- Alpaca paper is the only broker mode. Do not add live-mode or Robinhood execution support.
- Every order submission remains behind fresh human approval and the existing execution-time risk engine.
- Proposal generation cannot approve a plan, start monitoring, queue an order, or submit an order.
- A plan approval may create monitored rules; a triggered rule may create only a pending proposal.
- The terminal cannot import or construct broker, service, bootstrap, database, order-submission, or LLM objects.
- The client origin is exactly `https://localhost:8020`, verified with `.local/tls/rootCA.pem`; there is no URL or insecure-TLS override.
- Operator secrets, cookies, CSRF values, review tokens, and idempotency keys are never printed, logged, persisted, placed in arguments, or copied to environment variables.
- Monitoring is off at menu startup and starts only through an explicit menu action.
- No mutation is retried automatically after an ambiguous response.
- Live mode, automatic preapproved-rule execution, automatic bracket submission, cross-provider fallback, inbound webhooks, and Composio remain disabled.
- Unknown, stale, partial, conflicting, or unverifiable state is reported as blocked or unknown.
- Databases, sidecars, encrypted backups, TLS keys, logs, `.local`, caches, and runtime control files remain untracked.
- Verification never submits, approves, cancels, or replaces an Alpaca paper order.

---

## File map

### Create

- `src/trading_assistant/ops/operator_api.py` — fixed-origin HTTPS client, memory-only auth state, bounded JSON, stable failures.
- `src/trading_assistant/ops/operator_daemon.py` — exact-child daemon lifecycle and startup-posture interpretation.
- `src/trading_assistant/ops/operator_terminal.py` — line-oriented menu and human review gates.
- `src/trading_assistant/ops/runtime_consolidation.py` — one-use verified SQLite transfer between exact roots.
- `scripts/operator.sh` — canonical strict launcher.
- `tests/test_operator_api.py` — transport, auth, bounds, redaction, and mutation semantics.
- `tests/test_operator_daemon.py` — explicit start, posture, heartbeat, ownership, and stop semantics.
- `tests/test_operator_terminal.py` — menu flow and view-before-approve safety tests.
- `tests/test_operator_launcher.py` — shell launcher structural and behavioral tests.
- `tests/test_runtime_consolidation.py` — filesystem, SQLite, tenure, interruption, and receipt tests.

### Modify

- `src/trading_assistant/app/main.py` — authenticated rule list/cancel routes.
- `src/trading_assistant/app/policy.py` — rule route policies and target resource identity.
- `pyproject.toml` — `trading-operator` console entry point.
- `scripts/check_release_safety.py` — terminal import boundary and consolidation safety checks.
- `scripts/verify_loopback_release.py` — focused terminal/consolidation tests in the offline release sequence.
- `tests/test_auth.py` — rule routes require session/CSRF.
- `tests/test_api.py` — rule route behavior and idempotent cancellation.
- `tests/test_release_static.py` — negative terminal-authority and plaintext-copy fixtures.
- `tests/test_release_verifier.py` — verifier includes the new offline suites and no broker write.
- `tests/test_launch.py` — canonical operator launcher invariants.
- `README.md` — canonical terminal start and paper-only workflow.
- `docs/RUNBOOK.md` — login, proposals, approvals, daemon control, recovery, and consolidation evidence.

---

### Task 1: Expose rule reads and cancellation through the guarded HTTP authority

**Files:**

- Modify: `src/trading_assistant/app/main.py`
- Modify: `src/trading_assistant/app/policy.py`
- Modify: `tests/test_auth.py`
- Modify: `tests/test_api.py`

**Interfaces:**

- Consumes: `TradingService.list_rules() -> list[dict[str, Any]]`.
- Consumes: `TradingService.cancel_rule(rule_id: int, *, actor: str, reason: str, request_id: str) -> dict[str, Any]`.
- Produces: `GET /rules -> {"rules": [{"rule_id": 1, "state": "active"}]`
  for a representative active rule.
- Produces: `POST /rules/{rule_id}/cancel` with `ApprovalIn`, CSRF, `Idempotency-Key`, target-scoped lease, and audit mutation.

- [ ] **Step 1: Write route-policy and authentication tests**

```python
def test_rule_routes_require_expected_auth(client):
    assert client.get("/rules").status_code == 401
    assert client.post(
        "/rules/1/cancel",
        json={"reason": "operator requested cancellation"},
        headers={"Idempotency-Key": "rule-cancel-without-session"},
    ).status_code == 401


def test_rule_cancel_requires_csrf(authenticated_client):
    client, _csrf = authenticated_client
    response = client.post(
        "/rules/1/cancel",
        json={"reason": "operator requested cancellation"},
        headers={"Idempotency-Key": "rule-cancel-without-csrf"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_required"
```

Assert the route registry contains exactly one `GET /rules` and one
`POST /rules/{rule_id}/cancel`, with the cancel policy using target scope,
`rule_id`, idempotency, audit, and operation `rule_cancel`.

- [ ] **Step 2: Run the focused tests and prove the routes are absent**

```bash
uv run pytest tests/test_auth.py tests/test_api.py -k "rule_route or rule_cancel" -v
```

Expected: FAIL with `404` or a missing route-policy assertion.

- [ ] **Step 3: Add the two route policies**

```python
RoutePolicy("GET", "/rules", AuthLevel.SESSION, "session_read"),
RoutePolicy(
    "POST",
    "/rules/{rule_id}/cancel",
    AuthLevel.CSRF,
    "mutation",
    requires_idempotency=True,
    audit_mutation=True,
    concurrency_scope="target",
    target_param="rule_id",
    mutation_operation="rule_cancel",
),
```

Add `"/rules/{rule_id}/cancel"` to `_resource_material()` beside the order and
plan target-scoped paths so the lease key is derived from the validated
`rule_id`.

- [ ] **Step 4: Add the FastAPI handlers**

```python
@app.get("/rules")
def list_rules(
    principal: SessionPrincipal = Depends(current_principal),
):
    return {"rules": service.list_rules()}


@app.post("/rules/{rule_id}/cancel")
def cancel_rule(
    rule_id: int,
    body: ApprovalIn,
    request: Request,
    principal: SessionPrincipal = Depends(csrf_protected),
):
    context = _mutation(
        request,
        principal,
        body.reason,
        "http.rule_cancel",
        "rule",
        rule_id,
    )
    result = service.cancel_rule(
        rule_id,
        actor=context.actor,
        reason=context.reason,
        request_id=context.request_id,
    )
    if result.get("error") == "not found":
        raise ApiError("rule_not_found", 404, "Rule not found")
    if "error" in result:
        raise ApiError(
            "rule_conflict",
            409,
            "Rule cancellation is no longer current",
        )
    return result
```

- [ ] **Step 5: Test success, conflict, duplicate idempotency, and no submission**

Create one standalone rule through the service, cancel it through the route,
repeat the same idempotency key, and assert:

```python
assert first.status_code == 200
assert replay.status_code == 200
assert replay.json() == first.json()
assert service.broker.submit_calls == 0
```

Create a plan-owned rule and assert `409 rule_conflict`. Assert a missing rule
returns `404 rule_not_found`; blank reasons return `422`; a different
idempotency key after terminal cancellation returns `409`.

- [ ] **Step 6: Run the route and policy matrices**

```bash
uv run pytest tests/test_auth.py tests/test_api.py tests/test_route_policy.py -k "rule or route" -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/trading_assistant/app/main.py src/trading_assistant/app/policy.py tests/test_auth.py tests/test_api.py tests/test_route_policy.py
git commit -m "feat(api): expose guarded rule controls"
```

---

### Task 2: Build the fixed-origin, bounded terminal HTTPS client

**Files:**

- Create: `src/trading_assistant/ops/operator_api.py`
- Create: `tests/test_operator_api.py`

**Interfaces:**

- Produces: `OperatorApiError(status: int | None, code: str, message: str, request_id: str | None, retry_after: int | None)`.
- Produces: `OperatorSession(actor: str, csrf_token: str, expires_at: str | None)`.
- Produces: `OperatorApiClient(project_root: Path, *, opener=None, timeout_seconds: float = 10.0, max_response_bytes: int = 1_048_576)`.
- Produces methods `login`, `reauthenticate`, `logout`, `get`, and `mutate`.

- [ ] **Step 1: Write fixed-origin and TLS tests**

```python
def test_client_uses_only_loopback_https_and_local_ca(tmp_path):
    ca = tmp_path / ".local/tls/rootCA.pem"
    ca.parent.mkdir(parents=True)
    ca.write_text(TEST_CA, encoding="ascii")
    seen = []
    client = OperatorApiClient(
        tmp_path,
        opener=_RecordingOpener(seen, response={"alive": True}),
    )
    client.get("/health/live", authenticated=False)
    assert seen[0].full_url == "https://localhost:8020/health/live"
    assert "Proxy-Authorization" not in seen[0].headers


@pytest.mark.parametrize(
    "path",
    ["//evil.test", "https://evil.test/x", "/../x", "/x?secret=value"],
)
def test_client_rejects_noncanonical_paths(tmp_path, path):
    with pytest.raises(ValueError, match="operator_path_invalid"):
        client_for(tmp_path).get(path)
```

Also assert missing/symlinked/non-regular CA, an origin other than the exact
configured loopback value, and `ssl.CERT_NONE` construction fail before a
request.

- [ ] **Step 2: Run the focused test and prove the client is missing**

```bash
uv run pytest tests/test_operator_api.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement stable models and strict construction**

```python
@dataclass(frozen=True)
class OperatorSession:
    actor: str
    csrf_token: str
    expires_at: str | None


class OperatorApiError(RuntimeError):
    def __init__(
        self,
        *,
        status: int | None,
        code: str,
        message: str,
        request_id: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.request_id = request_id
        self.retry_after = retry_after
        super().__init__(message)
```

Resolve `project_root` strictly, reject symlinks, load config, require:

```python
config.server.origin == "https://localhost:8020"
config.server.bind_host == "127.0.0.1"
Path(config.server.tls_ca_path) == Path(".local/tls/rootCA.pem")
```

Create `ssl.create_default_context(cafile=str(ca_path))`, set
`check_hostname=True`, keep `verify_mode=ssl.CERT_REQUIRED`, use
`ProxyHandler({})`, `HTTPCookieProcessor(CookieJar())`, and
`HTTPSHandler(context=context)`.

- [ ] **Step 4: Implement bounded JSON and stable failures**

Read at most `max_response_bytes + 1`, reject excess as
`operator_response_too_large`, require a JSON object, and accept only UTF-8.
For HTTP failures, parse only:

```json
{"error":{"code":"stable_code","message":"Stable message","request_id":"id"}}
```

Clamp numeric `Retry-After` to `0..3600`. Never include URL headers, cookies,
body, TLS exception text, or provider text in `OperatorApiError.__str__`.

- [ ] **Step 5: Implement memory-only auth and mutation headers**

```python
def login(self, secret: str) -> OperatorSession:
    payload = self._request(
        "POST",
        "/auth/login",
        {"secret": secret},
        authenticated=False,
    )
    self._csrf_token = require_text(payload, "csrf_token")
    return OperatorSession(
        actor=require_text(payload, "actor"),
        csrf_token=self._csrf_token,
        expires_at=optional_text(payload, "expires_at"),
    )


def mutate(
    self,
    path: str,
    payload: dict[str, object],
    *,
    idempotent: bool,
) -> dict[str, object]:
    headers = {"X-CSRF-Token": self._require_csrf()}
    if idempotent:
        headers["Idempotency-Key"] = secrets.token_urlsafe(32)
    return self._request("POST", path, payload, headers=headers)
```

`reauthenticate` posts the supplied secret and current CSRF. `logout` attempts
one CSRF request and always clears the local cookie jar and CSRF reference in
`finally`. Any `401` also clears local auth state. The client never retries a
mutation.

- [ ] **Step 6: Test redaction, cookies, CSRF, idempotency, bounds, and errors**

Assert:

```python
assert secret not in caplog.text
assert csrf not in caplog.text
assert cookie not in repr(error)
assert first_idempotency_key != second_idempotency_key
assert opener.mutation_attempts == 1
```

Cover `401`, `403`, `409`, `422`, `429`, `503`, malformed envelopes, invalid
UTF-8, response overflow, timeout, TLS failure, missing CSRF, logout failure,
and unknown network failure.

- [ ] **Step 7: Run the client suite**

```bash
uv run pytest tests/test_operator_api.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/trading_assistant/ops/operator_api.py tests/test_operator_api.py
git commit -m "feat(ops): add bounded loopback API client"
```

---

### Task 3: Implement terminal rendering and proposal, plan, rule, and order gates

**Files:**

- Create: `src/trading_assistant/ops/operator_terminal.py`
- Create: `tests/test_operator_terminal.py`

**Interfaces:**

- Consumes: `OperatorApiClient`.
- Produces: `OperatorMenu(api, daemon, *, input_fn=input, secret_fn=getpass.getpass, output=print)`.
- Produces: `OperatorMenu.run() -> int`.
- Produces: `main(argv: list[str] | None = None) -> int`.

- [ ] **Step 1: Write cancel/EOF and import-boundary tests**

```python
def test_eof_before_confirmation_performs_no_mutation():
    api = FakeApi()
    menu = menu_with_inputs(api, ["3", "2", "research batch"])
    assert menu.run() == 0
    assert api.mutations == []


def test_terminal_has_no_direct_authority_imports():
    tree = ast.parse(Path(TERMINAL_MODULE).read_text(encoding="utf-8"))
    forbidden = {
        "trading_assistant.bootstrap",
        "trading_assistant.broker",
        "trading_assistant.db",
        "trading_assistant.orders",
        "trading_assistant.service",
        "trading_assistant.llm",
    }
    imported = imported_modules(tree)
    assert not any(
        name == prefix or name.startswith(f"{prefix}.")
        for name in imported
        for prefix in forbidden
    )
```

- [ ] **Step 2: Run the tests and prove the menu is missing**

```bash
uv run pytest tests/test_operator_terminal.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement bounded input and deterministic output helpers**

Use `parse_positive_int(value, maximum=20)`, `parse_identifier(value)`,
`require_reason(value, maximum=2_000)`, `confirm_exact(expected)`, and
`render_json_summary`. Reject control characters, paths, signs, floats,
overlong input, and non-ASCII confirmation phrases. Display values through
fixed labels and `json.dumps(value, ensure_ascii=True, sort_keys=True)` only
after recursively replacing sensitive-key fields:

```python
SENSITIVE_KEYS = {
    "secret", "token", "csrf", "cookie", "authorization",
    "api_key", "key", "credential",
}
```

- [ ] **Step 4: Implement login and top-level dispatch**

The menu starts with a no-echo operator-secret prompt. It drops the local
reference in `finally`, then displays exactly:

```text
ALPACA PAPER OPERATOR
No action is automatic. Every order requires fresh human approval.
```

Map choices `"1"` through `"9"` and `"0"` to dedicated methods. Unknown input
prints `Invalid choice`; EOF and Ctrl-C call owned-daemon cleanup and logout,
then return nonzero only when cleanup is unconfirmed.

- [ ] **Step 5: Implement proposal generation**

Flow:

```python
count = parse_positive_int(self._input("Proposal count (1-20): "), maximum=20)
reason = require_reason(self._input("Reason: "))
self._write("UNPROVEN ANALYST: this may use paid model calls.")
if not self._confirm_exact(f"GENERATE {count}"):
    return
payload = self.api.mutate(
    "/propose",
    {"n": count, "reason": reason},
    idempotent=True,
)
```

Render every result with `UNPROVEN` and do not call any other route.

- [ ] **Step 6: Implement plan review, approval, and cancellation**

`review_plan(plan_id)` fetches `/plans/{id}`, renders it, and stores only:

```python
ReviewedPlan(
    plan_id=plan_id,
    review_token=plan["review_token"],
    authority_digest=plan["authority_digest"],
    status=plan["status"],
)
```

Approval requires a current-process review. It refetches the plan and compares
all four fields, requires a reason, performs `api.reauthenticate(secret)`,
drops the local secret reference, requires
`APPROVE PAPER PLAN <id>`, then posts:

```python
self.api.mutate(
    f"/plans/{plan_id}/approve",
    {"reason": reason, "review_token": reviewed.review_token},
    idempotent=True,
)
```

Any mismatch deletes the cached review and prints `plan_review_stale`.
Cancellation requires a reason and posts only to `/plans/{id}/cancel`.

- [ ] **Step 7: Implement rules and pending-order flows**

Rules list through `/rules`; cancellation uses `/rules/{id}/cancel` with a
reason and idempotency.

Order approval fetches `/pending/{id}/confirmation` immediately before
rendering and requires non-null:

```python
ORDER_CONFIRMATION_FIELDS = (
    "order_id", "ticker", "side", "qty", "order_type",
    "estimated_exposure", "quote_observed_at", "expires_at",
    "breaker_state", "reconciliation",
)
```

`limit_price` may be null only for a market order. Missing, malformed, unknown,
stale, or mismatched `order_id` blocks locally. Then require reason,
reauthentication, and `APPROVE ALPACA PAPER ORDER <id>` before one idempotent
post to `/approve/{id}`. Reject posts to `/reject/{id}`. Cancel posts to
`/orders/{id}/cancel`. The client does not retry `acceptance_unknown`.

- [ ] **Step 8: Test every review gate**

Cover:

- generate exact phrase and no side effects on mismatch;
- plan approval without review;
- changed review token, digest, status, or ID;
- successful review → reauth → exact phrase → one approval;
- pending confirmation missing every required field in turn;
- market versus limit price validation;
- changed/expired pending proposal;
- `acceptance_unknown` shown once with no retry;
- reject/cancel reason validation;
- `401`, `403`, `409`, `429`, and `503` remain distinct;
- no output contains fake secret, cookie, CSRF, review token, provider body, or
  request JSON.

- [ ] **Step 9: Run the menu suite**

```bash
uv run pytest tests/test_operator_terminal.py tests/test_operator_api.py -v
```

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/trading_assistant/ops/operator_terminal.py tests/test_operator_terminal.py
git commit -m "feat(ops): add human-gated terminal trading menu"
```

---

### Task 4: Add read-only operations and explicit emergency actions

**Files:**

- Modify: `src/trading_assistant/ops/operator_terminal.py`
- Modify: `tests/test_operator_terminal.py`

**Interfaces:**

- Extends `OperatorMenu` with system, account, operations, panic, and breaker
  reset handlers.
- Consumes existing `/health`, `/security/posture`, `/account`, `/positions`,
  `/log`, `/sync`, `/reconcile`, `/panic`, and `/killswitch/reset`.

- [ ] **Step 1: Write read-only and emergency confirmation tests**

```python
def test_status_and_account_are_read_only():
    api = FakeApi()
    run_choices(api, ["1", "2", "0"])
    assert api.get_paths == [
        "/health",
        "/security/posture",
        "/account",
        "/positions",
    ]
    assert api.mutations == []


def test_panic_requires_reauth_reason_and_exact_phrase():
    api = FakeApi()
    run_choices(
        api,
        ["9", "1", "operator emergency", "wrong phrase", "0"],
    )
    assert not any(path == "/panic" for path, _ in api.mutations)
```

- [ ] **Step 2: Run and verify the handlers are missing**

```bash
uv run pytest tests/test_operator_terminal.py -k "status or account or panic or breaker or reconcile or sync" -v
```

Expected: FAIL because the choices are not implemented.

- [ ] **Step 3: Implement bounded read-only displays**

System status fetches `/health` and `/security/posture`; account fetches
`/account` and `/positions`; logs fetch `/log`. Display observed timestamps,
paper mode, broker, reconciliation, breaker checks, and daemon heartbeat.
Never transform absent evidence into a healthy label.

- [ ] **Step 4: Implement sync and reconciliation**

Both require a nonblank reason. `/sync` uses current CSRF. `/reconcile`
requires reauthentication before one idempotent mutation. A `409` causes a
fresh status display and no retry.

- [ ] **Step 5: Implement panic and breaker reset**

Panic requires reauthentication and exact phrase:

```text
PANIC ALPACA PAPER
```

Breaker reset begins with a fresh `/security/posture`, selects only a concrete
tripped scope with integer `generation > 0`, requires reauthentication and:

```text
RESET BREAKER <scope> GENERATION <generation>
```

Post:

```python
{
    "scope": scope,
    "expected_generation": generation,
    "reason": reason,
}
```

Never infer a generation and never offer reset for unknown state.

- [ ] **Step 6: Test failures and no automatic recovery**

Assert every action performs at most one mutation; `409 breaker_conflict`
refetches but does not retry; `panic_incomplete` remains a failure; logs and
posture redact sensitive fields; EOF during reason, secret, or phrase performs
no mutation.

- [ ] **Step 7: Run the complete menu suite**

```bash
uv run pytest tests/test_operator_terminal.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/trading_assistant/ops/operator_terminal.py tests/test_operator_terminal.py
git commit -m "feat(ops): add terminal safety operations"
```

---

### Task 5: Supervise the daemon as an exact, menu-owned child

**Files:**

- Create: `src/trading_assistant/ops/operator_daemon.py`
- Modify: `src/trading_assistant/ops/operator_terminal.py`
- Create: `tests/test_operator_daemon.py`
- Modify: `tests/test_operator_terminal.py`

**Interfaces:**

- Produces: `DaemonStatus(state: Literal["off", "starting", "running", "exited", "start_blocked", "stop_unconfirmed"], pid: int | None, detail_code: str)`.
- Produces: `DaemonSupervisor(project_root: Path, *, process_factory=subprocess.Popen, monotonic=time.monotonic, sleep=time.sleep)`.
- Produces: `start(*, posture: dict[str, object], heartbeat_loader: Callable[[], dict[str, object]]) -> DaemonStatus`.
- Produces: `stop(timeout_seconds: float = 15.0) -> DaemonStatus`.

- [ ] **Step 1: Write explicit-start and exact-child tests**

```python
def test_supervisor_is_off_and_spawns_nothing_at_construction(tmp_path):
    factory = RecordingProcessFactory()
    supervisor = DaemonSupervisor(tmp_path, process_factory=factory)
    assert supervisor.status().state == "off"
    assert factory.calls == []


def test_stop_signals_only_owned_child(tmp_path):
    child = FakeChild(pid=4242)
    supervisor = started_supervisor(tmp_path, child)
    result = supervisor.stop(timeout_seconds=1)
    assert child.signals == [signal.SIGINT]
    assert child.wait_calls == [1]
    assert result.state == "off"
```

Assert no source contains `pkill`, `killall`, `os.kill`, PID globbing, or
process-name search.

- [ ] **Step 2: Run and prove the supervisor is missing**

```bash
uv run pytest tests/test_operator_daemon.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement startup-posture validation**

Require a valid report object with `checks`. Block when:

- broker mode is not exactly paper;
- loopback/TLS/database/encryption/startup-reconciliation evidence is unknown,
  failed, stale, or absent;
- any circuit breaker is tripped or unknown;
- any unsafe order/fill/rule, uncertain interlock, quarantine failure, or
  conflicting runtime tenure is present;
- another fresh daemon heartbeat exists.

Only the expected pre-start daemon heartbeat state
`daemon_heartbeat_missing` or stale heartbeat with no current daemon tenure is
allowed. Return a stable `start_blocked` code; do not spawn.

- [ ] **Step 4: Implement exact child start**

Open `logs/daemon.operator.log` with mode `0600` and pass its descriptor for
stdout/stderr without a pipe. Spawn:

```python
[
    str(project_root / ".venv/bin/python"),
    "-m",
    "trading_assistant.daemon.main",
]
```

with `cwd=project_root`, `stdin=subprocess.DEVNULL`, `start_new_session=False`,
and no shell. Retain the returned child object. Poll boundedly until the child
exits or `/security/posture` reports a fresh daemon heartbeat whose evidence
time is later than the pre-start observation. Timeout sends one `SIGINT`,
waits normally, and returns `start_blocked` or `stop_unconfirmed`.

- [ ] **Step 5: Implement exact child stop and cleanup**

If there is no owned child, return `off` without signaling. If `poll()` shows
exit, close the log handle and return `exited`. Otherwise send `SIGINT` through
the child object and call `wait(timeout=timeout_seconds)`. On timeout, retain the child and
return `stop_unconfirmed`; never call `kill`, `terminate`, `os.kill`, or a
broad process tool.

- [ ] **Step 6: Add monitoring menu actions**

Start fetches posture first, states that monitoring may create pending
proposals but never approves them, requires:

```text
START PAPER MONITORING
```

Stop requires:

```text
STOP PAPER MONITORING
```

Menu exit invokes `stop()` only when `supervisor.owns_child` is true.

- [ ] **Step 7: Test startup failures and cleanup**

Cover blocked posture fields, current heartbeat, child exit before heartbeat,
heartbeat timeout, startup reconciliation failure, normal stop, timeout,
double start, double stop, Ctrl-C, EOF, log permissions, exact argv/cwd, no
pipe, and no unowned-process signal.

- [ ] **Step 8: Run daemon, menu, and existing monitor matrices**

```bash
uv run pytest tests/test_operator_daemon.py tests/test_operator_terminal.py tests/test_monitor.py tests/test_cooperative_control.py -v
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/trading_assistant/ops/operator_daemon.py src/trading_assistant/ops/operator_terminal.py tests/test_operator_daemon.py tests/test_operator_terminal.py
git commit -m "feat(ops): supervise explicit paper monitor"
```

---

### Task 6: Add the canonical one-command launcher and console entry point

**Files:**

- Create: `scripts/operator.sh`
- Modify: `pyproject.toml`
- Create: `tests/test_operator_launcher.py`
- Modify: `tests/test_launch.py`

**Interfaces:**

- Produces console script `trading-operator = trading_assistant.ops.operator_terminal:main`.
- Produces `./scripts/operator.sh`.

- [ ] **Step 1: Write launcher structure tests**

```python
def test_operator_launcher_is_canonical_and_does_not_start_daemon():
    source = Path("scripts/operator.sh").read_text(encoding="utf-8")
    assert "/Users/avi/Desktop/robinhood/trading-assistant" in source
    assert "scripts/start.sh" in source
    assert "trading_assistant.ops.operator_terminal" in source
    assert "trading_assistant.daemon.main" not in source
    assert "curl -k" not in source
    assert "--insecure" not in source
    assert "pkill" not in source
    assert "killall" not in source
```

Behavioral tests use a temporary canonical-path override available only through
an injected shell test harness, not a production CLI flag. Assert wrong cwd,
symlink root, missing venv, missing CA, failed liveness, failed controlled
start, and wrong process control all stop before menu launch.

- [ ] **Step 2: Run and prove the launcher is missing**

```bash
uv run pytest tests/test_operator_launcher.py tests/test_launch.py -v
```

Expected: FAIL because `scripts/operator.sh` does not exist.

- [ ] **Step 3: Add the console script**

```toml
[project.scripts]
trading-operator = "trading_assistant.ops.operator_terminal:main"
```

- [ ] **Step 4: Implement the shell launcher**

The script uses `set -euo pipefail`, `umask 077`, resolves its root with
`pwd -P`, requires exact canonical root in production, rejects symlink
components, checks `.venv/bin/python` and `.local/tls/rootCA.pem`, and probes:

```bash
/usr/bin/curl --fail --silent --show-error \
  --cacert "$TLS_CA" \
  "https://localhost:8020/health/live"
```

Pipe only into the repository interpreter to validate the exact liveness JSON.
When absent, invoke `./scripts/start.sh` once and revalidate both the
cooperative process metadata and HTTPS liveness. Finally use `exec`:

```bash
exec "$PY" -m trading_assistant.ops.operator_terminal
```

The script has no secret prompt, mutation, daemon launch, reconciliation,
breaker reset, proposal generation, or order action.

- [ ] **Step 5: Run shell syntax and launcher tests**

```bash
/bin/bash -n scripts/operator.sh
uv run pytest tests/test_operator_launcher.py tests/test_launch.py tests/test_launch_features.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/operator.sh pyproject.toml tests/test_operator_launcher.py tests/test_launch.py
git commit -m "feat(ops): add canonical terminal launcher"
```

---

### Task 7: Implement fail-closed runtime database consolidation

**Files:**

- Create: `src/trading_assistant/ops/runtime_consolidation.py`
- Create: `tests/test_runtime_consolidation.py`
- Modify: `scripts/check_release_safety.py`
- Modify: `tests/test_release_static.py`

**Interfaces:**

- Produces: `ConsolidationError(stable_code: str)`.
- Produces: `LogicalSummary(schema_head: str, table_counts: Sequence[tuple[str, int]], digest: str)`.
- Produces: `ConsolidationReceipt(source_hash: str, destination_hash: str, source_backup_hash: str, destination_backup_hash: str | None, summary_digest: str, installed: bool, status: str)`.
- Produces: `consolidate_runtime(source_root: Path, destination_root: Path, *, backup_key: bytes, backup_key_id: str, process_identity: ProcessIdentity, process_inspector: ProcessInspector) -> ConsolidationReceipt`.
- Produces CLI `python -m trading_assistant.ops.runtime_consolidation --source-root /Users/avi/Desktop/robinhood/trading-assistant/.worktrees/safety-foundation --destination-root /Users/avi/Desktop/robinhood/trading-assistant`.

- [ ] **Step 1: Write path, writer, and database identity tests**

Create two exact test roots with mode `0700`, current migrated SQLite databases,
and deterministic rows. Parametrize rejection of:

- source/destination root symlink;
- database symlink, hardlink, FIFO, directory, missing source, same inode, and
  path alias;
- group/world-writable root or database;
- source or destination `-wal`/`-shm` with an active writer;
- non-SQLite bytes, in-memory URL, stale schema, bad `quick_check`, and
  application/schema identity mismatch;
- current app, daemon, MCP, maintenance, migration, backup, or validation
  tenure.

Every failure asserts the destination inode/hash and logical rows are
unchanged.

- [ ] **Step 2: Write backup and interruption tests**

Inject backup and filesystem seams. Assert:

```python
assert events[:2] == ["source_backup_verified", "destination_backup_verified"]
assert events.index("destination_backup_verified") < events.index("install")
assert source.exists()
assert source.read_bytes() == original_source
```

Fail at source backup, destination backup, SQLite copy, source check, staging
check, summary compare, file fsync, directory fsync before install, install,
sidecar cleanup, and directory fsync after install. Before install, destination
must be byte-for-byte unchanged. A post-install failure returns or raises only
`migration_uncertain`, leaves an uncertainty marker mode `0600`, and blocks
normal startup.

- [ ] **Step 3: Run and prove the utility is absent**

```bash
uv run pytest tests/test_runtime_consolidation.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 4: Implement exact-root and regular-file validation**

Resolve both roots strictly and require:

```python
source_root.name == "safety-foundation"
source_root.parent.name == ".worktrees"
destination_root == Path(
    "/Users/avi/Desktop/robinhood/trading-assistant"
).resolve(strict=True)
```

The core function accepts alternate exact roots only through injected
`ConsolidationRoots` in tests; the production CLI does not. Validate each path
with `lstat` plus opened-descriptor `fstat`, owner UID, link count `1`, regular
file type, and modes no broader than `0600` for databases and `0700` for
directories. Use `O_NOFOLLOW` where available.

- [ ] **Step 5: Prove all writers and tenures are absent**

Reuse `RuntimeTenureService` inspection and cooperative app-control absence.
Open both databases read-only for inspection and reject any unexpired or
uncertain runtime/maintenance tenure. Require source app shutdown through
cooperative control and prove ports/control artifacts absent. Acquire a
consolidation maintenance lease on each database before backup/copy and retain
both guards through installation.

- [ ] **Step 6: Create and verify both encrypted backups**

Call the existing `backup_database()` separately for source and destination,
with the configured backup directory inside each root and the same validated
backup key. Require `receipt.verified is True`, matching `backup_key_id`, and
nonempty `path_hash` and `source_sha256`. Do not print artifact paths.

Because `backup_database()` owns a maintenance lease, use a serialized
preflight/backup/copy protocol that never nests incompatible lease owners:
prove absence → source backup → prove absence → destination backup → prove
absence → acquire consolidation leases → copy/install. Any changed database
identity or logical digest across these boundaries aborts.

- [ ] **Step 7: Implement SQLite backup, verification, and atomic install**

Create a private staging directory under destination `.local` with mode
`0700`, open a new mode-`0600` staging file with `O_CREAT|O_EXCL|O_NOFOLLOW`,
and use SQLite connections bound to validated descriptor paths. Run:

```sql
PRAGMA quick_check
PRAGMA foreign_key_check
```

Require `quick_check == [("ok",)]` and no foreign-key rows. Use
`require_current_schema()` on source and staging. Build a logical summary from
sorted application table names and `COUNT(*)`, plus a SHA-256 digest over
canonical JSON. Exclude SQLite internal tables only. Compare source/staging
schema head, counts, and digest.

Fsync staging, close all SQLite handles, revalidate descriptor/path identity,
atomically rename staging to the absent destination name only after moving the
old destination to a same-directory protected replacement name whose identity
matches the verified backup source. Fsync the directory after each authority
transition. Remove only descriptor-validated destination `-wal` and `-shm`
sidecars while both maintenance guards remain held.

- [ ] **Step 8: Add a static-gate rule for the one authorized transfer**

The static checker rejects `sqlite3.Connection.backup`, database-copy naming,
or plaintext SQLite staging everywhere except the exact
`runtime_consolidation.py` function. For that function require, through AST
and literal checks:

- exact-root validation;
- no network, broker, LLM, shell, subprocess, or glob imports;
- encrypted `backup_database` calls for both identities;
- `quick_check`, `foreign_key_check`, current-schema checks, logical summary;
- `0600`/`0700`, no-follow/exclusive creation, fsync, atomic rename;
- no deletion of source or encrypted artifacts;
- stable uncertainty marker and no plaintext archive retention.

Negative fixtures remove each required guard and must fail with
`PLAINTEXT_RUNTIME_TRANSFER_UNPROVEN`.

- [ ] **Step 9: Run consolidation and static suites**

```bash
uv run pytest tests/test_runtime_consolidation.py tests/test_release_static.py -v
uv run python scripts/check_release_safety.py
```

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/trading_assistant/ops/runtime_consolidation.py tests/test_runtime_consolidation.py scripts/check_release_safety.py tests/test_release_static.py
git commit -m "feat(ops): add verified runtime consolidation"
```

---

### Task 8: Integrate offline release verification and operator documentation

**Files:**

- Modify: `scripts/verify_loopback_release.py`
- Modify: `tests/test_release_verifier.py`
- Modify: `README.md`
- Modify: `docs/RUNBOOK.md`

**Interfaces:**

- Offline verifier runs terminal, launcher, rule-route, daemon-supervision, and
  consolidation tests without credentials or network.
- Documentation gives one canonical command and truthful blocked/ready states.

- [ ] **Step 1: Write verifier inclusion and exclusion tests**

```python
def test_release_verifier_includes_terminal_operator_safety_suites():
    flat = "\n".join(
        " ".join(command.argv)
        for command in ReleaseVerifier.default_commands()
    )
    for name in (
        "test_operator_api.py",
        "test_operator_terminal.py",
        "test_operator_daemon.py",
        "test_operator_launcher.py",
        "test_runtime_consolidation.py",
    ):
        assert name in flat
    assert "paper_drill" not in flat
    assert "alpaca_paper_integration" not in flat
    assert "daemon.main" not in flat
```

- [ ] **Step 2: Run and prove verifier coverage is incomplete**

```bash
uv run pytest tests/test_release_verifier.py -v
```

Expected: FAIL because the new suites are absent from the command inventory.

- [ ] **Step 3: Add one bounded offline operator step**

Add a finite command using `uv run --no-sync pytest` for all five new test
files plus the focused route-policy tests. Preserve the sanitized environment,
no-network guard, output cap, timeout, and fail-fast evidence.

- [ ] **Step 4: Document exact operator workflow**

README and RUNBOOK must include:

```bash
cd /Users/avi/Desktop/robinhood/trading-assistant
./scripts/operator.sh
```

Document:

- Keychain-backed operator secret retrieval without printing it;
- local CA setup and why browser certificate warnings must not be bypassed;
- analyst proposal cost warning and unproven label;
- plan review/approval versus order review/approval;
- explicit daemon start/stop and the requirement to leave the menu open;
- status, account, rules, sync, reconciliation, panic, and breaker reset;
- `acceptance_unknown`, stale evidence, and stop-unconfirmed recovery;
- paper-only boundary and no profit claim;
- consolidation backup receipts and rollback evidence;
- no database, secret, TLS key, log, or `.local` artifact belongs in Git.

- [ ] **Step 5: Run docs, verifier, and static tests**

```bash
uv run pytest tests/test_release_verifier.py tests/test_release_static.py tests/test_launch.py -v
uv run python scripts/check_release_safety.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/verify_loopback_release.py tests/test_release_verifier.py README.md docs/RUNBOOK.md
git commit -m "docs(ops): document terminal paper workflow"
```

---

### Task 9: Run the full local release gate on the exact candidate

**Files:**

- No feature source changes unless a failing test identifies a defect.
- Generated local evidence remains under ignored `.local/verification`.

**Interfaces:**

- Produces one clean candidate commit with deterministic local evidence.

- [ ] **Step 1: Run formatting and compile checks**

```bash
git diff --check
uv run --no-sync python -m compileall -q src
/bin/bash -n scripts/operator.sh scripts/start.sh scripts/stop.sh
```

Expected: all exit zero.

- [ ] **Step 2: Run focused safety matrices**

```bash
uv run --no-sync pytest \
  tests/test_operator_api.py \
  tests/test_operator_terminal.py \
  tests/test_operator_daemon.py \
  tests/test_operator_launcher.py \
  tests/test_runtime_consolidation.py \
  tests/test_auth.py \
  tests/test_api.py \
  tests/test_route_policy.py \
  tests/test_monitor.py \
  tests/test_cooperative_control.py \
  tests/test_launch.py \
  tests/test_launch_features.py \
  tests/test_release_static.py \
  tests/test_release_verifier.py -v
```

Expected: PASS.

- [ ] **Step 3: Run the deterministic static release gate**

```bash
uv run --no-sync python scripts/check_release_safety.py
```

Expected: exactly `release static checks: PASS`.

- [ ] **Step 4: Run the full suite with the existing coverage floor**

```bash
uv run --no-sync pytest
```

Expected: PASS with no skipped required safety suite and no credentialed paper
integration.

- [ ] **Step 5: Run the offline loopback verifier**

```bash
uv run --no-sync python scripts/verify_loopback_release.py
```

Expected: PASS and a redacted ignored evidence receipt.

- [ ] **Step 6: Verify the candidate is clean and private artifacts are untracked**

```bash
git status --short
git ls-files | rg '(^|/)(\.env|\.local|logs|runtime)(/|$)|\.(db|sqlite|sqlite3|pem|key)$'
```

Expected: clean status and no matches from the tracked-private-artifact query.

---

### Task 10: Publish, consolidate runtime authority, and hand off the canonical checkout

**Files:**

- Git refs and ignored local runtime state only.
- Do not delete the safety worktree.

**Interfaces:**

- Produces matching candidate SHA on `codex/safety-foundation`, PR #2, merged
  `origin/main`, and the canonical Desktop checkout.
- Produces a verified canonical paper runtime and terminal command.

- [ ] **Step 1: Push the exact candidate and verify hosted checks**

```bash
git push origin codex/safety-foundation
gh pr checks 2 --watch
```

Expected: every required check succeeds on the exact local HEAD SHA.

- [ ] **Step 2: Review the PR diff and merge without rewriting history**

```bash
gh pr diff 2 --name-only
gh pr merge 2 --merge --delete-branch=false
```

Confirm the merged commit contains the candidate and required hosted checks.
Do not force-push, squash away evidence, or delete the branch.

- [ ] **Step 3: Stop the current runtime cooperatively**

From the safety worktree:

```bash
./scripts/stop.sh
```

Prove the app, daemon, MCP, watchdog, validation writer, backup, migration, and
maintenance tenures are absent. If any state is unknown, stop and report it;
do not signal broadly.

- [ ] **Step 4: Fast-forward the canonical checkout**

```bash
cd /Users/avi/Desktop/robinhood/trading-assistant
git pull --ff-only origin main
```

Expected: no local change overwritten and HEAD matches `origin/main`.

- [ ] **Step 5: Run the consolidation command once**

Load the backup key through the existing role-secret provider without printing
it, then invoke:

```bash
.venv/bin/python -m trading_assistant.ops.runtime_consolidation \
  --source-root /Users/avi/Desktop/robinhood/trading-assistant/.worktrees/safety-foundation \
  --destination-root /Users/avi/Desktop/robinhood/trading-assistant
```

Expected output contains only stable status and hashes:

```json
{"installed":true,"status":"verified"}
```

plus bounded digest fields. It contains no path, secret, row value, account
data, or decrypted narrative.

- [ ] **Step 6: Verify canonical local TLS, schema, and structural preflight**

```bash
./scripts/setup-local-tls.sh
uv run --no-sync python -m trading_assistant.db.migrate current
uv run --no-sync python scripts/check_release_safety.py
```

Expected: trusted local certificate, current schema, and static PASS.

- [ ] **Step 7: Start only the canonical HTTPS application**

```bash
./scripts/start.sh
```

Verify exact cooperative process ownership and:

```bash
/usr/bin/curl --fail --silent --show-error \
  --cacert .local/tls/rootCA.pem \
  https://localhost:8020/health/live
```

Expected: `{"alive":true,"database_reachable":true}`. Do not start the daemon
or submit an order.

- [ ] **Step 8: Perform authenticated read-only truth checks**

Use the terminal client to inspect `/health`, `/security/posture`, `/account`,
and `/positions`. Confirm:

- mode is paper and broker is Alpaca;
- endpoint identity is the configured Alpaca paper origin;
- database path belongs to the canonical root;
- schema/encryption state is current;
- account and positions are read from broker truth;
- reconciliation and breakers are displayed exactly as ready, blocked, or
  unknown.

Do not reset a breaker automatically. If reconciliation requires an operator
mutation, leave it for explicit terminal action.

- [ ] **Step 9: Launch the guided terminal**

```bash
cd /Users/avi/Desktop/robinhood/trading-assistant
./scripts/operator.sh
```

Verify login and read-only menu sections. Do not generate a paid proposal,
start monitoring, approve a plan, approve an order, reject, cancel, panic,
reset, or reconcile as release verification.

- [ ] **Step 10: Record final evidence**

Report:

- canonical absolute path;
- local HEAD, `origin/main`, and PR merge SHA;
- hosted check conclusion;
- local full-suite and verifier conclusions;
- application liveness and exact process ownership;
- broker mode and current blocked/ready posture;
- whether runtime consolidation installed successfully;
- that the old worktree and encrypted backup evidence remain;
- the exact start command;
- that no paper trade was submitted by verification and profits are not
  guaranteed.

Do not claim the system is trading continuously unless the operator later
starts monitoring and a fresh daemon heartbeat proves it.

---

## Self-review checklist

- Every design section 1–16 maps to Tasks 1–10.
- Rule HTTP authority is added before the terminal consumes it.
- The terminal has no direct broker, database, service, bootstrap, submission,
  or LLM authority.
- Proposal, plan, and order confirmations are distinct and exact.
- Recent authentication is explicit for approvals, reconciliation, panic, and
  breaker reset.
- Daemon start is explicit and stop targets only an owned child.
- Runtime transfer backs up both identities, preserves the source, and fails
  closed around the atomic install.
- Local and hosted release evidence applies to the exact pushed commit.
- No verification action invokes an LLM, daemon, notification, broker write,
  order approval, cancellation, or paper drill.
