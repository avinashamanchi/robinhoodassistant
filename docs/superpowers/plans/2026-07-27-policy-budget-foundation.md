# Policy and Budget Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every FastAPI route and every paid/model or high-volume provider call a durable, centrally inventoried policy that survives restarts and coordinates the API and daemon.

**Architecture:** Strict configuration defines named route and provider budgets. SQLite WAL stores fixed-window counters, concurrency leases, daily provider usage, and call reservations. A route-policy registry is validated against FastAPI's complete route table at startup, while a budgeted LLM decorator reserves conservative input and maximum output before each provider attempt.

**Tech Stack:** Python 3.11, FastAPI/Starlette, Pydantic v2, SQLAlchemy 2, Alembic, SQLite WAL, pytest

## Global Constraints

- The governing specification is `docs/superpowers/specs/2026-07-27-loopback-kraken-security-console-design.md`.
- Keep Alpaca paper mode hard-locked; do not add a live-mode path.
- Do not reset a circuit breaker, start the daemon, submit an order, call an LLM, or send a notification during tests or migration.
- SQLite WAL is the authoritative shared store; an in-memory optimization may deny but may never grant.
- Unknown configuration keys must fail via `extra="forbid"`.
- Every provider retry and structured-output repair attempt is a separate charged call.
- A denied budget must result in zero provider or broker network calls.
- Existing endpoint authentication, CSRF, recent-authentication, idempotency, audit, and execution-time risk checks remain in place while central policy is added.
- Use UTC for persisted windows and provider-budget days.
- Run focused tests after each task and the full suite before completing this plan.

---

## File map

**Create**

- `src/trading_assistant/app/policy.py` — named route policies, route inventory, and policy enforcement.
- `src/trading_assistant/app/limits.py` — durable fixed-window counters and concurrency leases.
- `src/trading_assistant/llm/budget.py` — daily provider reservations and budgeted LLM decorator.
- `migrations/versions/20260727_0011_policy_budgets.py` — persistent policy tables.
- `tests/test_durable_limits.py` — cross-instance and concurrency tests.
- `tests/test_route_policy.py` — complete FastAPI route inventory tests.
- `tests/test_llm_budget.py` — reservation, settlement, and zero-call denial tests.

**Modify**

- `src/trading_assistant/config.py:92-107` — strict policy and budget configuration.
- `config.yaml:67-79` — explicit limits and LLM ceilings.
- `src/trading_assistant/db/models.py:420-570` — policy persistence models and durable panic receipts.
- `src/trading_assistant/bootstrap.py:41-64,153-232` — compose shared policy services.
- `src/trading_assistant/app/main.py:255-445,451-1135` — install registry and remove scattered limit checks.
- `src/trading_assistant/app/routers/auth.py:27-98` — use central login/session policies.
- `src/trading_assistant/app/security.py:71-210` — stable rate-limit response metadata.
- `src/trading_assistant/app/agent.py:151-258` — pass provider request identity.
- `src/trading_assistant/analyst/analyst.py:160-310` — pass provider request identity through analysis and repair attempts.
- `src/trading_assistant/analyst/planning.py` — forward the existing mutation request ID.
- `src/trading_assistant/llm/base.py` — document the normalized backend protocol.
- `src/trading_assistant/llm/factory.py:1-45` — wrap the selected backend once.
- `src/trading_assistant/llm/anthropic_backend.py:24-37` — accept explicit request identity.
- `src/trading_assistant/llm/gemini_backend.py:90-125` — accept explicit request identity.
- `src/trading_assistant/llm/groq_backend.py:83-105` — accept explicit request identity.
- `src/trading_assistant/backtest/llm_runner.py` — identify each backtest provider attempt.
- `src/trading_assistant/backtest/runner.py` — acquire one global backtest lease.
- `src/trading_assistant/operations/service.py:17-94` — coalesce panic calls with a durable lease.
- `src/trading_assistant/daemon/main.py` and `src/trading_assistant/bootstrap.py:194-204` — rate-limit scheduled provider reads.
- `tests/conftest.py:18-156` — injectable clock/config helpers for durable policies.
- `tests/test_config.py` — strict nested-key and default tests.
- `tests/test_api.py` and `tests/test_security.py` — replace injected in-memory limiter tests.
- `tests/test_bootstrap.py` — exact shared-service wiring.
- `tests/test_migrations.py` and `tests/test_startup_schema.py` — migration head coverage.
- `scripts/check_release_safety.py:274-286` — route-policy and provider-wrapper static checks.

**Delete after all callers migrate**

- `src/trading_assistant/app/ratelimit.py` — superseded process-local limiter.

---

### Task 1: Add strict policy and provider-budget configuration

**Files:**

- Modify: `src/trading_assistant/config.py:92-107`
- Modify: `config.yaml:67-79`
- Test: `tests/test_config.py`

**Interfaces:**

- Produces: `WindowLimitConfig`, `RateLimitsConfig`, `ProviderPriceConfig`,
  `ProviderBudgetConfig`, `BacktestLimitConfig`, and `RequestBoundsConfig`.
- Produces: `AppConfig.security.rate_limits`, `.provider_budget`, and `.request_bounds`.
- Consumes: existing `_Strict` Pydantic base and `SecurityConfig`.

- [ ] **Step 1: Write failing tests for exact defaults and typo rejection**

```python
def test_security_policy_defaults_are_explicit(app_config):
    security = app_config.security
    assert security.rate_limits.login == WindowLimitConfig(
        requests=5, global_requests=20, window_seconds=900, concurrency=2
    )
    assert security.rate_limits.backtest.daily_requests == 6
    assert security.rate_limits.backtest.global_daily_requests == 6
    assert security.provider_budget.daily_calls == 100
    assert security.provider_budget.daily_input_tokens == 1_000_000
    assert security.provider_budget.daily_output_tokens == 200_000
    assert security.provider_budget.max_chat_tool_turns == 8
    assert security.provider_budget.max_structured_attempts == 2
    price = security.provider_budget.prices["gemini:gemini-3.6-flash"]
    assert price.input_usd_per_million == Decimal("1.50")
    assert price.output_usd_per_million == Decimal("7.50")
    assert app_config.llm.gemini_model == "gemini-3.6-flash"
    assert security.backtest_limits.runtime_seconds == 1_200
    assert security.request_bounds.default_body_bytes == 16_384


def test_typo_in_nested_rate_limit_fails(tmp_path):
    raw = yaml.safe_load(Path("config.yaml").read_text())
    raw["security"]["rate_limits"]["chat"]["window_second"] = 600
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError, match="window_second"):
        load_config(path)
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```bash
uv run pytest tests/test_config.py::test_security_policy_defaults_are_explicit tests/test_config.py::test_typo_in_nested_rate_limit_fails -v
```

Expected: FAIL because the nested config models do not exist.

- [ ] **Step 3: Add the strict models**

Add these definitions before `SecurityConfig`:

Import `date` from `datetime`, `Decimal` from `decimal`, and `AnyUrl` from
Pydantic.

```python
class WindowLimitConfig(_Strict):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    requests: int = Field(gt=0)
    global_requests: int = Field(gt=0)
    window_seconds: int = Field(gt=0)
    concurrency: int = Field(default=1, gt=0)
    daily_requests: Optional[int] = Field(default=None, gt=0)
    global_daily_requests: Optional[int] = Field(default=None, gt=0)


class RateLimitsConfig(_Strict):
    login: WindowLimitConfig = WindowLimitConfig(
        requests=5, global_requests=20, window_seconds=900, concurrency=2
    )
    session_read: WindowLimitConfig = WindowLimitConfig(
        requests=120, global_requests=240, window_seconds=60, concurrency=16
    )
    broker_read: WindowLimitConfig = WindowLimitConfig(
        requests=30, global_requests=60, window_seconds=60, concurrency=4
    )
    mutation: WindowLimitConfig = WindowLimitConfig(
        requests=20, global_requests=40, window_seconds=60, concurrency=4
    )
    approval: WindowLimitConfig = WindowLimitConfig(
        requests=10, global_requests=20, window_seconds=300, concurrency=1
    )
    privileged: WindowLimitConfig = WindowLimitConfig(
        requests=5, global_requests=10, window_seconds=300, concurrency=1
    )
    chat: WindowLimitConfig = WindowLimitConfig(
        requests=10, global_requests=20, window_seconds=600, concurrency=1
    )
    analysis: WindowLimitConfig = WindowLimitConfig(
        requests=5, global_requests=10, window_seconds=600, concurrency=1
    )
    backtest: WindowLimitConfig = WindowLimitConfig(
        requests=2,
        global_requests=6,
        window_seconds=3600,
        concurrency=1,
        daily_requests=6,
        global_daily_requests=6,
    )
    provider_read: WindowLimitConfig = WindowLimitConfig(
        requests=180, global_requests=240, window_seconds=60, concurrency=8
    )
    panic: WindowLimitConfig = WindowLimitConfig(
        requests=60, global_requests=120, window_seconds=60, concurrency=1
    )


class ProviderPriceConfig(_Strict):
    model: str
    effective_date: date
    input_usd_per_million: Decimal = Field(ge=0)
    output_usd_per_million: Decimal = Field(ge=0)
    source_url: AnyUrl


class ProviderBudgetConfig(_Strict):
    daily_calls: int = Field(default=100, gt=0)
    daily_input_tokens: int = Field(default=1_000_000, gt=0)
    daily_output_tokens: int = Field(default=200_000, gt=0)
    reservation_ttl_seconds: int = Field(default=300, gt=0)
    max_chat_tool_turns: int = Field(default=8, gt=0, le=8)
    max_structured_attempts: int = Field(default=2, gt=0, le=2)
    backtest_llm_enabled: bool = False
    prices: dict[str, ProviderPriceConfig] = Field(default_factory=dict)


class BacktestLimitConfig(_Strict):
    runtime_seconds: int = Field(default=1_200, gt=0, le=1_200)
    max_symbols: int = Field(default=20, gt=0)
    max_calendar_days: int = Field(default=3_000, gt=0)


class RequestBoundsConfig(_Strict):
    default_body_bytes: int = Field(default=16_384, gt=0)
    chat_body_bytes: int = Field(default=32_768, gt=0)
    max_header_count: int = Field(default=64, gt=0)
    max_header_bytes: int = Field(default=16_384, gt=0)
```

Extend `SecurityConfig`:

```python
class SecurityConfig(_Strict):
    session_hours: int = Field(default=8, gt=0)
    reauthentication_minutes: int = Field(default=5, gt=0)
    cookie_secure: bool = False
    rate_limits: RateLimitsConfig = Field(default_factory=RateLimitsConfig)
    provider_budget: ProviderBudgetConfig = Field(
        default_factory=ProviderBudgetConfig
    )
    backtest_limits: BacktestLimitConfig = Field(
        default_factory=BacktestLimitConfig
    )
    request_bounds: RequestBoundsConfig = Field(
        default_factory=RequestBoundsConfig
    )
```

- [ ] **Step 4: Put every default into `config.yaml`**

Use the exact nested keys from the models. Do not rely on hidden defaults in
the committed production configuration. Pin `llm.gemini_model` to the stable
`gemini-3.6-flash` identifier instead of the moving `latest` alias. Add this
versioned price entry:

```yaml
prices:
  "gemini:gemini-3.6-flash":
    model: gemini-3.6-flash
    effective_date: 2026-07-09
    input_usd_per_million: 1.50
    output_usd_per_million: 7.50
    source_url: https://ai.google.dev/gemini-api/docs/latest-model
```

Startup must find exactly one price record for the selected provider/model.
Changing provider or model without updating reviewed price metadata is a
configuration error; the system never silently displays a zero-dollar
estimate.

- [ ] **Step 5: Run configuration tests**

Run:

```bash
uv run pytest tests/test_config.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/trading_assistant/config.py config.yaml tests/test_config.py
git commit -m "feat(security): define strict route and provider budgets"
```

---

### Task 2: Persist rate windows, leases, and provider reservations

**Files:**

- Modify: `src/trading_assistant/db/models.py:420-570`
- Create: `migrations/versions/20260727_0011_policy_budgets.py`
- Modify: `tests/test_migrations.py`
- Modify: `tests/test_startup_schema.py`

**Interfaces:**

- Produces ORM models `RateWindow`, `ConcurrencyLease`, `ProviderBudgetDay`,
  `ProviderReservation`, and `PanicReceipt`.
- Produces Alembic head `20260727_0011`.
- Consumes `UTCDateTime`, `utcnow`, and SQLite WAL.

- [ ] **Step 1: Write failing ORM and migration tests**

```python
def test_policy_rows_round_trip(session_factory):
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    with session_factory() as session:
        session.add(
            RateWindow(
                bucket_key="a" * 64,
                policy_name="chat",
                window_started_at=now,
                expires_at=now + timedelta(minutes=10),
                hits=1,
            )
        )
        session.add(
            ProviderBudgetDay(
                provider="gemini",
                budget_day=date(2026, 7, 27),
                calls_used=1,
                input_tokens_used=100,
                output_tokens_used=50,
            )
        )
        session.commit()
    with session_factory() as session:
        assert session.get(RateWindow, "a" * 64).hits == 1
        assert session.get(
            ProviderBudgetDay, ("gemini", date(2026, 7, 27))
        ).calls_used == 1
```

Add an assertion that a fresh migrated database contains:

```python
{
    "rate_windows",
    "concurrency_leases",
    "provider_budget_days",
    "provider_reservations",
    "panic_receipts",
}
```

- [ ] **Step 2: Run tests and verify missing-table failure**

Run:

```bash
uv run pytest tests/test_migrations.py tests/test_startup_schema.py -v
```

Expected: FAIL because revision `20260727_0011` and the models do not exist.

- [ ] **Step 3: Add the exact ORM shapes**

```python
class RateWindow(Base):
    __tablename__ = "rate_windows"

    bucket_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    policy_name: Mapped[str] = mapped_column(String(32), index=True)
    window_started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    hits: Mapped[int] = mapped_column(default=0)
    version: Mapped[int] = mapped_column(default=0)


class ConcurrencyLease(Base):
    __tablename__ = "concurrency_leases"

    resource_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), default="")
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    generation: Mapped[int] = mapped_column(default=0)


class ProviderBudgetDay(Base):
    __tablename__ = "provider_budget_days"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    budget_day: Mapped[date] = mapped_column(Date, primary_key=True)
    calls_used: Mapped[int] = mapped_column(default=0)
    input_tokens_used: Mapped[int] = mapped_column(default=0)
    output_tokens_used: Mapped[int] = mapped_column(default=0)
    reconciliation_required: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    reconciliation_code: Mapped[str] = mapped_column(String(32), default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ProviderReservation(Base):
    __tablename__ = "provider_reservations"

    reservation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    budget_day: Mapped[date] = mapped_column(Date, index=True)
    state: Mapped[str] = mapped_column(String(16), default="reserved", index=True)
    input_reserved: Mapped[int] = mapped_column()
    output_reserved: Mapped[int] = mapped_column()
    input_actual: Mapped[Optional[int]] = mapped_column(nullable=True)
    output_actual: Mapped[Optional[int]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    settled_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class PanicReceipt(Base):
    __tablename__ = "panic_receipts"

    account_scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(16), index=True)
    response_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
```

Import `Boolean`, `Date`, and `Text` from SQLAlchemy and `date` from
`datetime`.

- [ ] **Step 4: Create the Alembic revision**

The migration must create all five tables, the indexes represented above,
non-negative `CHECK` constraints for every counter, state constraints for
provider reservations and panic receipts, and foreign-key-free rows so policy
enforcement cannot be blocked by domain-row deletion. Its downgrade must refuse
if any provider reservation has `state IN ('started', 'unknown')` or any panic
receipt has `state = 'started'`.

- [ ] **Step 5: Run migration and model tests**

Run:

```bash
uv run pytest tests/test_migrations.py tests/test_startup_schema.py tests/test_db_models.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/trading_assistant/db/models.py migrations/versions/20260727_0011_policy_budgets.py tests/test_migrations.py tests/test_startup_schema.py tests/test_db_models.py
git commit -m "feat(security): persist rate and provider budget state"
```

---

### Task 3: Implement atomic durable windows and leases

**Files:**

- Create: `src/trading_assistant/app/limits.py`
- Create: `tests/test_durable_limits.py`

**Interfaces:**

- Produces `LimitSpec`, `LimitDecision`, `DurableRateLimiter.consume_pair()`.
- Produces `LeaseDecision`, `ConcurrencyLeaseService.acquire()`, `.release()`, and `.inspect()`.
- Produces bounded `.prune_expired(now, limit=500)` methods.
- Consumes `RateWindow`, `ConcurrencyLease`, and a SQLAlchemy session factory.

- [ ] **Step 1: Write fixed-window restart and parallel-consumer tests**

```python
def test_limit_survives_new_service_instance(session_factory):
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    spec = LimitSpec(
        "chat",
        principal_requests=2,
        global_requests=3,
        window_seconds=60,
    )
    first = DurableRateLimiter(session_factory)
    assert first.consume_pair(spec, principal="operator", now=now).allowed
    assert first.consume_pair(spec, principal="operator", now=now).allowed
    restarted = DurableRateLimiter(session_factory)
    denied = restarted.consume_pair(spec, principal="operator", now=now)
    assert denied.allowed is False
    assert denied.retry_after_seconds == 60


def test_parallel_consumers_cannot_overspend(engine, session_factory):
    limiter_a = DurableRateLimiter(session_factory)
    limiter_b = DurableRateLimiter(session_factory)
    spec = LimitSpec(
        "analysis",
        principal_requests=1,
        global_requests=1,
        window_seconds=60,
    )
    barrier = threading.Barrier(2)

    def consume(limiter):
        barrier.wait()
        return limiter.consume_pair(spec, principal="same").allowed

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, (limiter_a, limiter_b)))
    assert sorted(results) == [False, True]
```

Add lease tests proving:

- two different principals cannot exceed the global window;
- principal and global daily windows survive a restart;
- SQLite lock/busy failure produces store-unavailable and never grants through
  an in-memory fallback;
- the same owner may renew;
- a second owner is denied until expiry;
- release is owner-guarded;
- a new service instance sees the same lease.
- pruning deletes only expired rows, at most the requested limit, and never
  removes a live window/lease.

- [ ] **Step 2: Run tests and verify import failure**

Run:

```bash
uv run pytest tests/test_durable_limits.py -v
```

Expected: FAIL because `app.limits` does not exist.

- [ ] **Step 3: Implement immutable decisions and hashed bucket keys**

Import `Literal` from `typing`.

```python
@dataclass(frozen=True)
class LimitSpec:
    name: str
    principal_requests: int
    global_requests: int
    window_seconds: int
    principal_daily_requests: int | None = None
    global_daily_requests: int | None = None


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int
    reset_at: datetime


def _bucket_key(
    policy_name: str,
    bucket_kind: Literal["principal_window", "global_window",
                         "principal_day", "global_day"],
    principal: str,
) -> str:
    material = f"{policy_name}\0{bucket_kind}\0{principal}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()
```

`DurableRateLimiter.consume_pair()` must open `BEGIN IMMEDIATE`, consume the
principal and global fixed-window buckets, then the configured UTC-day buckets,
and commit only if every bucket grants. Each bucket uses SQLite
`INSERT ... ON CONFLICT DO UPDATE ... WHERE ... RETURNING`; an expired row
resets to one hit and a live row increments only below its ceiling. If any
bucket denies, roll back so a global denial does not spend the principal
allowance. Read only reset metadata after rollback. Never perform a
read-then-unconditional-write sequence.

Use the repository's finite SQLite busy timeout. Convert lock, disk, and
transaction exceptions to `LimitStoreUnavailable`; route and provider
policies consume that as a fail-closed denial according to the approved
failure matrix.

- [ ] **Step 4: Implement lease compare-and-set**

`ConcurrencyLeaseService.acquire()` uses one upsert whose conflict update is permitted only when the stored lease is expired or owned by the requesting owner. `release()` updates only `WHERE resource_key = :key AND owner = :owner`.

Use signatures:

```python
def acquire(
    self,
    resource_key: str,
    *,
    owner: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> LeaseDecision: ...

def release(self, resource_key: str, *, owner: str) -> bool: ...
```

Both services expose bounded pruning implemented as a primary-key subquery plus
delete inside one transaction. App startup runs one 500-row pass after
structural validation. The daemon runs one pass in its existing daily
maintenance task. Cleanup failure is reported in posture and does not convert a
denied lease/window into permission.

- [ ] **Step 5: Run concurrency tests repeatedly**

Run:

```bash
uv run pytest tests/test_durable_limits.py -v --count=20
```

If `pytest-repeat` is not installed, run:

```bash
for attempt in 1 2 3 4 5; do uv run pytest tests/test_durable_limits.py -q || exit 1; done
```

Expected: every run passes with exactly one allowed parallel consumer.

- [ ] **Step 6: Commit**

```bash
git add src/trading_assistant/app/limits.py tests/test_durable_limits.py
git commit -m "feat(security): add durable limits and leases"
```

---

### Task 4: Reserve and settle every LLM provider attempt

**Files:**

- Create: `src/trading_assistant/llm/budget.py`
- Create: `tests/test_llm_budget.py`
- Modify: `src/trading_assistant/llm/base.py`
- Modify: `src/trading_assistant/llm/anthropic_backend.py:24-37`
- Modify: `src/trading_assistant/llm/gemini_backend.py:90-125`
- Modify: `src/trading_assistant/llm/groq_backend.py:83-105`

**Interfaces:**

- Produces `BudgetLimits`, `BudgetReservation`, `BudgetStatus`,
  `ProviderInputEstimator`, and `ProviderBudgetService`.
- Produces `BudgetedLLMBackend`.
- Changes every backend `create()` to accept `request_id: str = ""`.
- Consumes normalized `response.usage.input_tokens` and `.output_tokens`.

- [ ] **Step 1: Write denial, settlement, and unknown-usage tests**

```python
def test_denied_budget_never_calls_delegate(session_factory):
    delegate = ScriptedBackend([])
    service = ProviderBudgetService(
        session_factory,
        BudgetLimits(calls=0, input_tokens=1, output_tokens=1),
    )
    backend = BudgetedLLMBackend(
        delegate,
        service,
        provider="anthropic",
        category="chat",
        max_output_tokens=10,
    )
    with pytest.raises(ProviderBudgetExceeded):
        backend.create(
            system="s",
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            request_id="request-1",
        )
    assert delegate.calls == []


def test_settlement_refunds_unused_output(session_factory):
    response = LLMResponse(
        content=[TextBlock(text="ok")],
        usage=Usage(input_tokens=3, output_tokens=2),
    )
    backend = budgeted_backend(session_factory, response, max_output_tokens=100)
    backend.create(
        system="s",
        messages=[{"role": "user", "content": "x"}],
        tools=[],
        request_id="request-2",
    )
    usage = ProviderBudgetService(session_factory, LIMITS).status("test")
    assert usage.calls_used == 1
    assert usage.input_tokens_used == 3
    assert usage.output_tokens_used == 2
```

Add tests proving:

- each supported provider has an explicit estimator whose result is at least
  the complete serialized UTF-8 payload byte length;
- a missing estimator denies before provider construction;
- `mark_started()` happens before delegate invocation;
- delegate exception leaves the reservation charged and `state="unknown"`;
- missing usage leaves the reserved amounts charged;
- actual input or output above its reservation charges the actual amount,
  marks the UTC budget day `reconciliation_required`, and blocks the next call;
- expired `reserved` rows can be released, while `started` and `unknown` rows
  can never be released automatically;
- two parallel reservations cannot cross any daily ceiling;
- the UTC date boundary creates a new `ProviderBudgetDay`;
- `BudgetStatus.estimated_usd` uses only configured model/effective-date price
  metadata while calls and tokens remain the hard authority.

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/test_llm_budget.py -v
```

Expected: FAIL because the budget service and decorator do not exist.

- [ ] **Step 3: Implement reservation types**

```python
@dataclass(frozen=True)
class BudgetLimits:
    calls: int
    input_tokens: int
    output_tokens: int
    reservation_ttl_seconds: int = 300


@dataclass(frozen=True)
class BudgetReservation:
    reservation_id: str
    provider: str
    category: str
    request_id: str
    input_reserved: int
    output_reserved: int


class ProviderInputEstimator(Protocol):
    def estimate_upper_bound(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ) -> int: ...
```

`ProviderBudgetService.reserve()` must open a SQLite `BEGIN IMMEDIATE`
transaction, load or create the UTC-day row, verify all three resulting totals,
increment the row, and insert the reservation in the same transaction.

`mark_started()` changes only `reserved -> started`.

`settle()` changes only `started -> settled` and stores actual usage. Normal
settlement refunds `reserved - actual`. If either actual count exceeds its
reservation, settlement charges the positive delta even when that takes the
day above its ceiling, sets `reconciliation_required=True` with
`reconciliation_code="provider_usage_over_reservation"`, and exposes that
state in `BudgetStatus`. Every later `reserve()` denies before network I/O while
reconciliation is required. No automatic reset or counter edit is provided.

`mark_unknown()` changes `started -> unknown` and refunds nothing.

`release_expired_unstarted(now)` opens `BEGIN IMMEDIATE`, selects only
`state="reserved"` rows whose `expires_at <= now`, subtracts their reservations
without going negative, and marks them `released`. It never releases
`started`, `unknown`, settled, or overrun usage. Run this cleanup inside
`reserve()` before evaluating a new request so it has the same transaction
authority.

Register an explicit `Utf8ByteUpperBoundEstimator` for each supported provider
name in `llm/factory.py`. It serializes the complete provider-bound system
prompt, message history, and tool schema and returns the UTF-8 byte count. A
provider without an explicit registration raises `ProviderBudgetUnavailable`;
there is no unmetered fallback.

- [ ] **Step 4: Implement the LLM decorator**

```python
class BudgetedLLMBackend:
    def create(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        tool_choice: str | None = None,
        request_id: str = "",
    ):
        if not request_id.strip():
            raise ValueError("budgeted LLM calls require request_id")
        input_reservation = self.estimator.estimate_upper_bound(
            system=system,
            messages=messages,
            tools=tools,
        )
        reservation = self.budgets.reserve(
            provider=self.provider,
            category=self.category,
            request_id=request_id,
            input_tokens=input_reservation,
            output_tokens=self.max_output_tokens,
        )
        self.budgets.mark_started(reservation.reservation_id)
        try:
            response = self.delegate.create(
                system=system,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                request_id=request_id,
            )
        except Exception:
            self.budgets.mark_unknown(reservation.reservation_id)
            raise
        usage = getattr(response, "usage", None)
        if usage is None:
            self.budgets.mark_unknown(reservation.reservation_id)
        else:
            self.budgets.settle(
                reservation.reservation_id,
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            )
        return response
```

- [ ] **Step 5: Add the optional `request_id` keyword to raw providers**

Raw provider adapters accept and ignore `request_id`; only the decorator
requires a non-empty value. Preserve every existing SDK request field.

The agent reads `provider_budget.max_chat_tool_turns` as a hard loop ceiling.
Analyst and planning repair paths read
`provider_budget.max_structured_attempts`; every attempt invokes the decorated
backend separately and therefore reserves separately.

- [ ] **Step 6: Run budget and backend tests**

Run:

```bash
uv run pytest tests/test_llm_budget.py tests/test_llm_backends.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/trading_assistant/llm/budget.py src/trading_assistant/llm/base.py src/trading_assistant/llm/anthropic_backend.py src/trading_assistant/llm/gemini_backend.py src/trading_assistant/llm/groq_backend.py tests/test_llm_budget.py tests/test_llm_backends.py
git commit -m "feat(llm): reserve durable provider budgets"
```

---

### Task 5: Compose one limiter, lease service, and provider budget

**Files:**

- Modify: `src/trading_assistant/bootstrap.py:41-64,153-232`
- Modify: `src/trading_assistant/llm/factory.py:1-45`
- Modify: `src/trading_assistant/app/main.py:230-375`
- Modify: `tests/test_bootstrap.py`
- Modify: `tests/test_factory.py`

**Interfaces:**

- Adds `ApplicationContainer.rate_limiter`, `.leases`, and `.provider_budget`.
- Changes `build_llm_backend(config, secrets, *, provider_budget, category)`.
- Consumes plan Tasks 1–4.

- [ ] **Step 1: Write exact-container wiring tests**

```python
def test_container_shares_policy_services_across_roles(app_config, test_secrets, mock_broker):
    container = build_test_container(
        app_config,
        test_secrets,
        broker=mock_broker,
        clock=FakeClock(open=True),
    )
    assert container.rate_limiter.session_factory is container.session_factory
    assert container.leases.session_factory is container.session_factory
    assert container.provider_budget.session_factory is container.session_factory


def test_factory_wraps_selected_provider_once(app_config, test_secrets, session_factory):
    budgets = ProviderBudgetService(session_factory, limits_from(app_config))
    backend = build_llm_backend(
        app_config,
        test_secrets,
        provider_budget=budgets,
        category="analysis",
    )
    assert isinstance(backend, BudgetedLLMBackend)
    assert backend.category == "analysis"
```

- [ ] **Step 2: Run tests and verify missing attributes**

Run:

```bash
uv run pytest tests/test_bootstrap.py tests/test_factory.py -v
```

Expected: FAIL because the container and factory do not expose these services.

- [ ] **Step 3: Extend `ApplicationContainer` and `_build_container()`**

Construct all three services immediately after `session_factory`. Pass the same
instances to the app, daemon rule worker, operations service, and LLM factory.
Do not construct a second limiter or provider budget in `app.main`.

- [ ] **Step 4: Wrap each LLM purpose with an exact category**

Use:

- `chat` for `Agent`;
- `analysis` for `Analyst` and planning;
- `untrusted` for the quarantine model added in the next plan;
- `backtest` for `LLMBacktestRunner`.

If `category == "backtest"` and
`config.security.provider_budget.backtest_llm_enabled` is false, construct a
backend that raises `ProviderBudgetExceeded` before delegate construction.

- [ ] **Step 5: Run bootstrap/factory tests**

Run:

```bash
uv run pytest tests/test_bootstrap.py tests/test_factory.py tests/test_llm_backends.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/trading_assistant/bootstrap.py src/trading_assistant/llm/factory.py src/trading_assistant/app/main.py tests/test_bootstrap.py tests/test_factory.py
git commit -m "refactor(runtime): share policy budget services"
```

---

### Task 6: Define and validate the complete route-policy registry

**Files:**

- Create: `src/trading_assistant/app/policy.py`
- Create: `tests/test_route_policy.py`
- Modify: `src/trading_assistant/app/security.py:71-210`

**Interfaces:**

- Produces `AuthLevel`, `RoutePolicy`, `ROUTE_POLICIES`, `RoutePolicyRegistry`.
- Produces `install_route_policy(app)` and `validate_route_inventory(app)`.
- Consumes `DurableRateLimiter`, `ConcurrencyLeaseService`, and `SessionAuth`.

- [ ] **Step 1: Write a failing inventory test**

```python
def test_every_api_route_has_exact_policy(make_service):
    app = create_app(
        service=make_service(),
        agent=StubAgent(),
        api_token="route-policy-secret",
        planning=None,
    )
    registry = app.state.route_policy_registry
    assert registry.unclassified(app) == []
    assert registry.duplicates() == []
```

Add explicit assertions:

```python
assert registry.get("POST", "/approve/{order_id}").auth is AuthLevel.RECENT
assert registry.get("POST", "/chat").limit_name == "chat"
assert registry.get("POST", "/backtests/run").limit_name == "backtest"
assert registry.get("GET", "/positions").limit_name == "broker_read"
assert registry.get("GET", "/positions").broker_read is True
assert registry.get("POST", "/analyze").provider_category == "analysis"
assert registry.get("GET", "/health/live").auth is AuthLevel.PUBLIC
```

- [ ] **Step 2: Run and verify missing registry failure**

Run:

```bash
uv run pytest tests/test_route_policy.py -v
```

Expected: FAIL because the registry is absent.

- [ ] **Step 3: Define policy types**

Import `Literal` from `typing`.

```python
class AuthLevel(str, Enum):
    PUBLIC = "public"
    SESSION = "session"
    CSRF = "csrf"
    RECENT = "recent"


@dataclass(frozen=True)
class RoutePolicy:
    method: str
    path: str
    auth: AuthLevel
    limit_name: str
    body_limit_name: str = "default"
    requires_idempotency: bool = False
    audit_mutation: bool = False
    broker_read: bool = False
    provider_category: str | None = None
    concurrency_scope: str = "principal"
    concurrency_behavior: Literal["reject", "coalesce_panic"] = "reject"
    target_param: str | None = None
```

- [ ] **Step 4: Enter the complete current route table**

The registry must contain all routes below plus the `/static` mount:

```python
ROUTE_POLICIES = (
    RoutePolicy("GET", "/health/live", AuthLevel.PUBLIC, "session_read"),
    RoutePolicy("GET", "/login", AuthLevel.PUBLIC, "session_read"),
    RoutePolicy("POST", "/auth/login", AuthLevel.PUBLIC, "login"),
    RoutePolicy("GET", "/auth/session", AuthLevel.SESSION, "session_read"),
    RoutePolicy("POST", "/auth/reauth", AuthLevel.CSRF, "privileged"),
    RoutePolicy("POST", "/auth/logout", AuthLevel.CSRF, "mutation"),
    RoutePolicy("GET", "/", AuthLevel.SESSION, "session_read"),
    RoutePolicy(
        "POST", "/chat", AuthLevel.CSRF, "chat", "chat",
        audit_mutation=True, provider_category="chat",
    ),
    RoutePolicy("GET", "/health", AuthLevel.SESSION, "session_read"),
    RoutePolicy("GET", "/pending", AuthLevel.SESSION, "session_read"),
    RoutePolicy("GET", "/pending/{order_id}/confirmation", AuthLevel.SESSION, "session_read"),
    RoutePolicy(
        "POST", "/approve/{order_id}", AuthLevel.RECENT, "approval",
        requires_idempotency=True, audit_mutation=True,
        broker_read=True, concurrency_scope="target", target_param="order_id",
    ),
    RoutePolicy(
        "POST", "/reject/{order_id}", AuthLevel.CSRF, "mutation",
        requires_idempotency=True, audit_mutation=True,
        concurrency_scope="target", target_param="order_id",
    ),
    RoutePolicy("GET", "/positions", AuthLevel.SESSION, "broker_read", broker_read=True),
    RoutePolicy("GET", "/account", AuthLevel.SESSION, "broker_read", broker_read=True),
    RoutePolicy("GET", "/log", AuthLevel.SESSION, "session_read"),
    RoutePolicy(
        "POST", "/killswitch/reset", AuthLevel.RECENT, "privileged",
        requires_idempotency=True, audit_mutation=True,
        concurrency_scope="account",
    ),
    RoutePolicy(
        "POST", "/orders/{order_id}/cancel", AuthLevel.CSRF, "mutation",
        requires_idempotency=True, audit_mutation=True,
        concurrency_scope="target", target_param="order_id",
    ),
    RoutePolicy(
        "POST", "/reconcile", AuthLevel.RECENT, "privileged",
        requires_idempotency=True, audit_mutation=True, broker_read=True,
    ),
    RoutePolicy(
        "POST", "/sync", AuthLevel.CSRF, "mutation",
        requires_idempotency=True, audit_mutation=True, broker_read=True,
    ),
    RoutePolicy(
        "POST", "/panic", AuthLevel.RECENT, "panic",
        requires_idempotency=True, audit_mutation=True,
        broker_read=True, concurrency_scope="account",
        concurrency_behavior="coalesce_panic",
    ),
    RoutePolicy("GET", "/analyst/scorecard", AuthLevel.SESSION, "session_read"),
    RoutePolicy(
        "POST", "/analyze", AuthLevel.CSRF, "analysis",
        requires_idempotency=True, audit_mutation=True,
        broker_read=True, provider_category="analysis",
    ),
    RoutePolicy("GET", "/plans", AuthLevel.SESSION, "session_read"),
    RoutePolicy("GET", "/plans/ui", AuthLevel.SESSION, "session_read"),
    RoutePolicy("GET", "/plans/{plan_id}", AuthLevel.SESSION, "session_read"),
    RoutePolicy(
        "POST", "/plans/{plan_id}/approve", AuthLevel.RECENT, "approval",
        requires_idempotency=True, audit_mutation=True,
        broker_read=True, concurrency_scope="target", target_param="plan_id",
    ),
    RoutePolicy(
        "POST", "/plans/{plan_id}/cancel", AuthLevel.CSRF, "mutation",
        requires_idempotency=True, audit_mutation=True,
        concurrency_scope="target", target_param="plan_id",
    ),
    RoutePolicy(
        "POST", "/screen", AuthLevel.CSRF, "analysis",
        provider_category="analysis", broker_read=True,
    ),
    RoutePolicy(
        "POST", "/propose", AuthLevel.CSRF, "analysis",
        requires_idempotency=True, audit_mutation=True,
        provider_category="analysis", broker_read=True,
    ),
    RoutePolicy("GET", "/holdings", AuthLevel.SESSION, "broker_read", broker_read=True),
    RoutePolicy("GET", "/external/positions", AuthLevel.SESSION, "session_read"),
    RoutePolicy("GET", "/external/summary", AuthLevel.SESSION, "session_read"),
    RoutePolicy("GET", "/backtests", AuthLevel.SESSION, "session_read"),
    RoutePolicy(
        "POST", "/backtests/run", AuthLevel.CSRF, "backtest",
        requires_idempotency=True, audit_mutation=True,
        provider_category="backtest",
    ),
    RoutePolicy("GET", "/backtests/{run_id}/report", AuthLevel.SESSION, "session_read"),
    RoutePolicy("GET", "/backtests/ui", AuthLevel.SESSION, "session_read"),
)
```

Represent `/static/{path:path}` as an explicit public immutable-asset mount
policy and reject directory listing/path traversal through the existing
`_AssetOnlyStaticFiles`.

- [ ] **Step 5: Add route matching, authentication, and stable denials**

Policy enforcement must:

1. resolve exactly one method/path template;
2. authenticate at the configured level using `SessionAuth`;
3. derive the limit principal from session ID or request source;
4. call `DurableRateLimiter.consume_pair()` and fail closed on store errors for
   all non-liveness routes;
5. acquire the configured principal, target, or account concurrency lease and
   release it in `finally`; ordinary contention returns a stable busy response,
   while `coalesce_panic` waits for and returns the exact durable
   `PanicReceipt` created by the lease owner;
6. reject missing/invalid idempotency keys before a domain mutation;
7. attach `X-RateLimit-Limit`, `X-RateLimit-Remaining`,
   `X-RateLimit-Reset`, and `Retry-After` on denial;
8. never include the raw session token or request body in a bucket key.

Preserve endpoint dependencies as defense-in-depth until Task 7 proves parity.

- [ ] **Step 6: Run route-policy and security tests**

Run:

```bash
uv run pytest tests/test_route_policy.py tests/test_security.py tests/test_security_headers.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/trading_assistant/app/policy.py src/trading_assistant/app/security.py tests/test_route_policy.py tests/test_security.py tests/test_security_headers.py
git commit -m "feat(api): inventory and enforce every route policy"
```

---

### Task 7: Replace scattered endpoint limiters with the durable registry

**Files:**

- Modify: `src/trading_assistant/app/main.py:255-445,451-1135`
- Modify: `src/trading_assistant/app/routers/auth.py:27-98`
- Modify: `src/trading_assistant/app/static/js/auth.js:45-170`
- Modify: `tests/test_api.py`
- Modify: `tests/test_security.py`
- Delete: `src/trading_assistant/app/ratelimit.py`

**Interfaces:**

- Consumes `RoutePolicyRegistry` and `DurableRateLimiter`.
- Removes constructor arguments `chat_rate`, `approve_rate`, `analysis_rate`, `backtest_rate`, `account_rate`, and `login_rate`.
- Preserves every existing endpoint response and domain call.

- [ ] **Step 1: Rewrite injected-limiter tests against configured durable state**

Create a helper that overrides one nested limit:

```python
def with_limit(app_config, name: str, *, requests: int, window_seconds: int):
    limits = app_config.security.rate_limits.model_copy(
        update={
            name: WindowLimitConfig(
                requests=requests,
                window_seconds=window_seconds,
                concurrency=1,
            )
        }
    )
    security = app_config.security.model_copy(update={"rate_limits": limits})
    return app_config.model_copy(update={"security": security})
```

Update tests to assert a new app instance using the same database remains
limited. Assert the denied request causes zero service/provider calls.

Add a frontend static test asserting `jsonPost()` creates one
`Idempotency-Key` with `crypto.randomUUID()` when the options object is built
and that the same options object is reused after recent-authentication retry.

- [ ] **Step 2: Run the focused API tests and verify old constructor dependence**

Run:

```bash
uv run pytest tests/test_api.py -k "rate_limit or account_reads" -v
```

Expected: FAIL until the tests and app use the durable registry.

- [ ] **Step 3: Remove all inline `.allow()` checks**

Delete the six optional limiter arguments, their default construction, the
`enforce_broker_read_rate()` closure, and all route-local limiter conditionals.
Install the central registry once using the instances from
`ApplicationContainer`.

For explicit test/service injection without a container, construct one durable
limiter from `service.session_factory`; never fall back to the old in-memory
class.

- [ ] **Step 4: Move login limiting out of the router**

Remove `request.app.state.login_rate` and the route-local source check. The
central login policy must reject before `SessionAuth.login()` performs a secret
comparison.

- [ ] **Step 5: Delete the obsolete limiter**

Before deletion, change `jsonPost()` to include:

```javascript
headers: {
  "Content-Type": "application/json",
  "Idempotency-Key": crypto.randomUUID(),
},
```

The key is generated once per operator action, not inside `request()`, so an
automatic recent-authentication retry uses the same key. Login and
reauthentication continue using their dedicated request code and do not need an
idempotency key.

Confirm:

```bash
rg -n "RateLimiter|chat_rate|approve_rate|analysis_rate|backtest_rate|account_rate|login_rate" src tests
```

Expected: no references to `app.ratelimit` or its constructor remain.

- [ ] **Step 6: Run API and auth suites**

Run:

```bash
uv run pytest tests/test_api.py tests/test_auth.py tests/test_security.py tests/test_launch.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/trading_assistant/app/main.py src/trading_assistant/app/routers/auth.py src/trading_assistant/app/static/js/auth.js tests/test_api.py tests/test_security.py tests/test_auth.py tests/test_launch.py
git rm src/trading_assistant/app/ratelimit.py
git commit -m "refactor(api): replace process-local endpoint limits"
```

---

### Task 8: Propagate request identity through all LLM call sites

**Files:**

- Modify: `src/trading_assistant/app/agent.py:151-258`
- Modify: `src/trading_assistant/analyst/analyst.py:160-310`
- Modify: `src/trading_assistant/analyst/planning.py`
- Modify: `src/trading_assistant/backtest/llm_runner.py`
- Modify: `tests/test_agent.py`
- Modify: `tests/test_analyst.py`
- Modify: `tests/test_planning.py`
- Modify: `tests/test_llm_runner.py`

**Interfaces:**

- Adds required `request_id` to `Analyst.analyze()` and `.analyze_plan()`.
- Passes the existing HTTP/audit request ID into every backend call.
- Uses deterministic `backtest:{run_id}:{symbol}:{timestamp}` IDs in backtests.

- [ ] **Step 1: Add capture tests**

```python
def test_agent_passes_request_id_to_every_provider_turn(make_agent):
    backend = CaptureBackend(two_turn_tool_script())
    agent = make_agent(backend)
    agent.chat(
        "price of AAPL",
        actor="operator:test",
        reason="research",
        request_id="http-request-123",
    )
    assert [call["request_id"] for call in backend.calls] == [
        "http-request-123",
        "http-request-123",
    ]
```

Add a plan-repair test asserting both attempts use the same parent request ID
and still consume two budget reservations.

- [ ] **Step 2: Run focused tests and verify keyword failure**

Run:

```bash
uv run pytest tests/test_agent.py tests/test_analyst.py tests/test_planning.py tests/test_llm_runner.py -v
```

Expected: FAIL because call sites do not pass `request_id`.

- [ ] **Step 3: Thread the existing request ID without generating replacements**

`Agent.chat()` already receives `request_id`; pass it on every
`backend.create()`.

`PlanningService.analyze()` already receives `request_id`; pass it to
`Analyst.analyze_plan()`.

Background and backtest callers construct a stable, non-secret ID from their
own persisted run/call identity.

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run pytest tests/test_agent.py tests/test_analyst.py tests/test_planning.py tests/test_llm_runner.py tests/test_llm_budget.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_assistant/app/agent.py src/trading_assistant/analyst/analyst.py src/trading_assistant/analyst/planning.py src/trading_assistant/backtest/llm_runner.py tests/test_agent.py tests/test_analyst.py tests/test_planning.py tests/test_llm_runner.py
git commit -m "refactor(llm): identify every provider attempt"
```

---

### Task 9: Gate backtests, panic, and daemon provider reads

**Files:**

- Modify: `src/trading_assistant/backtest/runner.py`
- Modify: `src/trading_assistant/app/main.py:1080-1113`
- Modify: `src/trading_assistant/operations/service.py:17-94`
- Modify: `src/trading_assistant/bootstrap.py:194-204`
- Modify: `src/trading_assistant/daemon/main.py`
- Modify: `tests/test_backtests_api.py`
- Modify: `tests/test_ops.py`
- Modify: `tests/test_monitor.py`

**Interfaces:**

- Uses lease key `backtest:global` with a 1,500-second TTL.
- Uses lease key `panic:alpaca-paper` with a 90-second TTL.
- Uses rate principal `provider:alpaca:market-data` for scheduled quote reads.
- Stores the exact coalesced emergency result in `PanicReceipt`.
- Enforces 1,200-second runtime, 20-symbol, and 3,000-calendar-day backtest
  ceilings.

- [ ] **Step 1: Write concurrency and zero-duplicate tests**

Add tests proving:

- a second backtest receives `409 backtest_busy` and starts no runner;
- a crashed/expired backtest lease can be reclaimed;
- an over-symbol or over-date request receives `422 backtest_bounds_exceeded`
  and starts no runner/provider;
- runtime expiry cooperatively cancels replay, persists `timed_out`, releases
  the lease, and makes no later provider call;
- two concurrent panic calls invoke `service.panic()` once and return the same
  persisted or in-process receipt;
- a denied scheduled quote allowance performs zero `broker.get_quote()` calls;
- execution-time broker reads used by risk/submission are not silently retried
  or converted into permission.

- [ ] **Step 2: Run focused tests and verify duplicate behavior**

Run:

```bash
uv run pytest tests/test_backtests_api.py tests/test_ops.py tests/test_monitor.py -v
```

Expected: FAIL because no shared leases/provider-read gate exist.

- [ ] **Step 3: Acquire and release the backtest lease**

Acquire before constructing the runner. Release in `finally`. If the process
dies, TTL provides recovery. A denied lease returns a stable API error before
data loading.

Validate symbol count and inclusive calendar span before acquiring the lease or
loading cached data. Pass a monotonic deadline and `threading.Event` into
`BacktestRunner`; the replay loop checks both before every bar, strategy call,
and optional LLM trigger. On expiry it sets the event, records
`status="timed_out"`, and unwinds before lease release. There is no detached
worker that can continue after the API reports timeout.

- [ ] **Step 4: Coalesce panic without weakening first-call availability**

Use an account-scoped lease. The lease owner records the resulting receipt in a
bounded 90-second `PanicReceipt` row created in Task 2. The owner atomically
sets `state="started"` before broker I/O and then stores the stable JSON result
with `state="completed"`. Followers poll that exact row for at most the request
timeout. They never invoke `service.panic()` themselves. An owner exception
sets `state="failed"` with no exception text in `response_json`; followers
receive `503 panic_incomplete`, never a fabricated success. An expired receipt
can be replaced only by the next lease owner.

- [ ] **Step 5: Rate-limit scheduled provider reads**

Wrap only scheduled/read-only market-data calls. Do not place broker order
submission behind a retrying generic limiter. A denied provider read is treated
as unavailable data and flows into the existing stale-data breaker behavior.

- [ ] **Step 6: Run focused and stress tests**

Run:

```bash
uv run pytest tests/test_backtests_api.py tests/test_ops.py tests/test_monitor.py tests/test_submission_barrier.py tests/stress/test_stress_scenarios.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/trading_assistant/backtest/runner.py src/trading_assistant/app/main.py src/trading_assistant/operations/service.py src/trading_assistant/bootstrap.py src/trading_assistant/daemon/main.py tests/test_backtests_api.py tests/test_ops.py tests/test_monitor.py
git commit -m "feat(runtime): bound backtests panic and provider reads"
```

---

### Task 10: Make policy omissions a release-gate failure

**Files:**

- Modify: `scripts/check_release_safety.py:274-286`
- Modify: `tests/test_release_static.py`
- Modify: `docs/RUNBOOK.md:210-234`

**Interfaces:**

- Adds static check `_check_route_policy_inventory`.
- Adds static check `_check_llm_construction_paths`.
- Documents stable `429` and provider-budget behavior.

- [ ] **Step 1: Add negative-fixture tests**

Create fixtures where:

- a FastAPI route exists but is absent from `ROUTE_POLICIES`;
- `AnthropicBackend`, `GeminiBackend`, or `GroqBackend` is constructed outside
  `llm/factory.py` or tests;
- a runtime file imports the deleted `RateLimiter`.

Each fixture must make `scripts/check_release_safety.py` return 1 with a stable
message.

- [ ] **Step 2: Run static-gate tests and verify failure**

Run:

```bash
uv run pytest tests/test_release_static.py -v
```

Expected: FAIL because the checks are not implemented.

- [ ] **Step 3: Implement AST-based static checks**

Follow the existing AST patterns in `check_release_safety.py`. Allow raw backend
construction only in:

```python
{
    "src/trading_assistant/llm/factory.py",
    "src/trading_assistant/llm/anthropic_backend.py",
    "src/trading_assistant/llm/gemini_backend.py",
    "src/trading_assistant/llm/groq_backend.py",
}
```

The route inventory check imports no application and calls no provider. Parse
decorators from `app/main.py` and `app/routers/*.py`, normalize method/path, and
compare with the literal registry.

- [ ] **Step 4: Update the runbook**

Document:

- durable windows survive process restart;
- `Retry-After` meaning;
- UTC daily model budgets;
- unknown provider acceptance remains charged;
- how to inspect budget status without changing it;
- why manual database counter edits are prohibited.

- [ ] **Step 5: Run release and policy suites**

Run:

```bash
uv run pytest tests/test_release_static.py tests/test_route_policy.py tests/test_durable_limits.py tests/test_llm_budget.py -v
uv run python scripts/check_release_safety.py
```

Expected: all tests PASS and the script prints `release static checks: PASS`.

- [ ] **Step 6: Run the full suite**

Run:

```bash
uv run pytest
```

Expected: PASS with only the repository's already documented skip.

- [ ] **Step 7: Commit**

```bash
git add scripts/check_release_safety.py tests/test_release_static.py docs/RUNBOOK.md
git commit -m "chore(security): gate route and provider policy coverage"
```

---

## Plan 1 completion checkpoint

Run:

```bash
git status --short
uv run pytest
uv run python scripts/check_release_safety.py
```

Required result:

- clean working tree;
- complete pytest pass;
- static release gate pass;
- no provider or broker network calls made by verification;
- no daemon start and no breaker reset;
- central route inventory and durable budgets available for Plan 2.
