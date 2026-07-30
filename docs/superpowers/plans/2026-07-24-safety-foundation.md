# Safety Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Alpaca paper-trading control plane fail closed and recover correctly from crashes, ambiguous broker acceptance, duplicate actions, concurrent OCO triggers, stale risk inputs, and schema drift.

**Architecture:** Keep the existing `TradingService` as a temporary compatibility facade while extracting migration, authentication, order submission, reconciliation, rule leasing, and breaker services behind focused interfaces. Persist intent before broker I/O, keep all network calls outside SQLite write transactions, and require authenticated, CSRF-protected operator sessions for every non-liveness route.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2, Alembic, Pydantic 2, SQLite WAL, pytest, Alpaca paper API, vanilla browser modules for the temporary security-safe UI.

## Global Constraints

- Alpaca is the only execution broker.
- Trading mode stays `paper`.
- Human approval is required for every order.
- `features.auto_execute_preapproved_rules` stays `false`.
- `execution.prefer_bracket_orders` stays `false`.
- Robinhood, Composio, live trading, and autonomous execution are out of scope.
- External text and connector payloads have no instruction authority.
- Every new behavior is test-driven: failing test, minimal implementation, passing focused test, regression test, commit.
- Never run a broker network call while a SQLite write transaction is open.
- Never automatically retry an order whose broker acceptance is unknown.
- Preserve the current operator database only through an explicit, backed-up migration command.

## File Structure

### Database and migrations

- Create `alembic.ini` — repository-local Alembic configuration.
- Create `migrations/env.py` — loads SQLAlchemy metadata and runtime database URL.
- Create `migrations/script.py.mako` — revision template.
- Create `migrations/versions/20260724_0001_baseline.py` — exact current schema.
- Create `migrations/versions/20260724_0002_order_outbox.py` — order state and audit fields.
- Create `migrations/versions/20260724_0003_reconciliation.py` — reconciliation cursors.
- Create `migrations/versions/20260724_0004_rule_leases.py` — typed rule groups and leases.
- Create `migrations/versions/20260724_0005_breakers.py` — persisted scoped breakers.
- Create `migrations/versions/20260724_0006_auth_sessions.py` — server-side sessions.
- Create `migrations/versions/20260724_0007_runtime_health.py` — heartbeat uniqueness and health state.
- Create `src/trading_assistant/db/schema.py` — revision inspection and startup gate.
- Create `src/trading_assistant/db/migrate.py` — explicit backup, adopt, upgrade, and status CLI.
- Modify `src/trading_assistant/db/models.py` — new persistence fields and tables.
- Test in `tests/test_migrations.py`.

### Order execution

- Create `src/trading_assistant/orders/__init__.py`.
- Create `src/trading_assistant/orders/repository.py` — compare-and-set persistence only.
- Create `src/trading_assistant/orders/application.py` — proposal expiry and approval recording.
- Create `src/trading_assistant/orders/snapshot.py` — complete execution-time portfolio snapshots.
- Create `src/trading_assistant/orders/submission.py` — risk recheck, durable claim, broker I/O, acceptance result.
- Create `src/trading_assistant/orders/reconciliation.py` — unknown acceptance, statuses, fills, drift, panic.
- Modify `src/trading_assistant/broker/models.py`, `broker/base.py`, `broker/mock.py`, and `broker/alpaca.py`.
- Modify `src/trading_assistant/service.py` to delegate while preserving its public API.
- Test in `tests/test_order_application.py`, `tests/test_order_submission.py`, and `tests/test_reconciliation_service.py`.

### Rules and risk

- Create `src/trading_assistant/rules/__init__.py`.
- Create `src/trading_assistant/rules/models.py` — typed commands and persisted enums.
- Create `src/trading_assistant/rules/repository.py` — rule-group leases and terminal claims.
- Create `src/trading_assistant/rules/application.py` — atomic proposal and terminal-group transition.
- Create `src/trading_assistant/rules/worker.py` — one group execution at a time.
- Create `src/trading_assistant/risk/breakers.py` — scoped persisted breakers.
- Modify `src/trading_assistant/broker/models.py`, `risk/engine.py`, `daemon/monitor.py`, and `service.py`.
- Test in `tests/test_rule_models.py`, `tests/test_rule_leases.py`, `tests/test_breakers.py`, and `tests/test_execution_risk_snapshot.py`.

### Authentication and application composition

- Create `src/trading_assistant/app/auth.py` — session issuance, validation, CSRF, reauthentication.
- Create `src/trading_assistant/app/security.py` — headers and no-store policy.
- Create `src/trading_assistant/app/routers/auth.py` and package initializer.
- Create `src/trading_assistant/bootstrap.py` — one application container.
- Create `src/trading_assistant/operations/audit.py` — shared mutation context and audit recorder.
- Create `src/trading_assistant/operations/health.py` and package initializer.
- Create `src/trading_assistant/operations/service.py` — panic, scoped reset, and operational health facade.
- Modify `src/trading_assistant/app/main.py`, `daemon/main.py`, `mcp_server/server.py`, `preflight.py`, and `ops/paper_drill.py`.
- Split inline UI assets into `app/static/css/console.css` and `app/static/js/{auth,index,plans,backtests}.js`.
- Test in `tests/test_auth.py`, `tests/test_security_headers.py`, `tests/test_bootstrap.py`, and browser-static assertions in `tests/test_security.py`.

---

### Task 1: Add versioned schema management and an explicit migration gate

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/20260724_0001_baseline.py`
- Create: `src/trading_assistant/db/schema.py`
- Create: `src/trading_assistant/db/migrate.py`
- Test: `tests/test_migrations.py`

**Interfaces:**
- Produces: `schema_status(engine: Engine) -> SchemaStatus`
- Produces: `require_current_schema(engine: Engine) -> None`
- Produces: CLI `python -m trading_assistant.db.migrate {status,adopt-existing,upgrade}`
- Consumes: `trading_assistant.db.models.Base.metadata`

- [ ] **Step 1: Add the failing migration tests**

```python
# tests/test_migrations.py
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from trading_assistant.db.migrate import adopt_existing, upgrade
from trading_assistant.db.schema import SchemaOutOfDate, require_current_schema
from trading_assistant.db.session import create_db_engine


def _url(path: Path) -> str:
    return f"sqlite:///{path}"


def _legacy_engine(path: Path):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _url(path))
    command.upgrade(cfg, "20260724_0001")
    engine = create_db_engine(_url(path))
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
    return engine


def test_fresh_database_upgrades_to_head(tmp_path):
    engine = create_db_engine(_url(tmp_path / "fresh.db"))
    upgrade(engine)
    require_current_schema(engine)
    assert "orders" in inspect(engine).get_table_names()
    assert "alembic_version" in inspect(engine).get_table_names()


def test_existing_unversioned_database_must_be_adopted(tmp_path):
    path = tmp_path / "legacy.db"
    engine = _legacy_engine(path)
    with pytest.raises(SchemaOutOfDate, match="adopt-existing"):
        require_current_schema(engine)
    backup = adopt_existing(engine)
    assert backup.exists()
    upgrade(engine)
    require_current_schema(engine)


def test_adoption_backup_contains_committed_wal_rows(tmp_path):
    path = tmp_path / "legacy.db"
    engine = _legacy_engine(path)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO orders "
                          "(idempotency_key,ticker,side,order_type,status,created_at,updated_at) "
                          "VALUES ('keep-me','AAPL','buy','market','proposed',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    backup = adopt_existing(engine)
    with create_db_engine(_url(backup)).connect() as conn:
        assert conn.scalar(
            text("SELECT count(*) FROM orders WHERE idempotency_key='keep-me'")
        ) == 1


def test_unknown_revision_is_rejected_at_startup(tmp_path):
    engine = create_db_engine(_url(tmp_path / "unknown.db"))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        conn.execute(text(
            "INSERT INTO alembic_version(version_num) VALUES ('unknown_revision')"
        ))
    with pytest.raises(SchemaOutOfDate, match="current='unknown_revision'"):
        require_current_schema(engine)
```

- [ ] **Step 2: Run the focused tests and confirm the missing-module failure**

Run:

```bash
uv run pytest tests/test_migrations.py -v
```

Expected: collection fails because `trading_assistant.db.migrate` and
`trading_assistant.db.schema` do not exist.

- [ ] **Step 3: Add Alembic and generate the exact baseline revision**

Update `pyproject.toml`:

```toml
dependencies = [
    "alembic>=1.13,<2",
    "pydantic>=2.6",
    "pydantic-settings>=2.2",
    "pyyaml>=6.0",
    "sqlalchemy>=2.0",
    "tzdata>=2024.1",
]
```

Run:

```bash
uv lock
uv sync --all-extras
uv run alembic revision --autogenerate \
  -m "baseline existing schema" \
  --rev-id 20260724_0001
```

Set `down_revision = None`. Inspect the generated revision and verify that its
table and index names match `Base.metadata.sorted_tables`; do not leave imports
from application model modules inside the revision.

- [ ] **Step 4: Implement schema inspection and the explicit migration CLI**

```python
# src/trading_assistant/db/schema.py
from dataclasses import dataclass

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect


class SchemaOutOfDate(RuntimeError):
    pass


@dataclass(frozen=True)
class SchemaStatus:
    current: str | None
    head: str
    versioned: bool

    @property
    def ready(self) -> bool:
        return self.versioned and self.current == self.head


def _config() -> Config:
    return Config("alembic.ini")


def schema_status(engine: Engine) -> SchemaStatus:
    head = ScriptDirectory.from_config(_config()).get_current_head()
    versioned = "alembic_version" in inspect(engine).get_table_names()
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision() if versioned else None
    return SchemaStatus(current=current, head=head, versioned=versioned)


def require_current_schema(engine: Engine) -> None:
    status = schema_status(engine)
    if status.ready:
        return
    action = "adopt-existing" if not status.versioned else "upgrade"
    raise SchemaOutOfDate(
        f"database schema is not current: current={status.current!r}, "
        f"head={status.head!r}; run `python -m trading_assistant.db.migrate {action}`"
    )
```

```python
# src/trading_assistant/db/migrate.py
import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect
from sqlalchemy.engine import make_url

from .schema import require_current_schema, schema_status

BASELINE = "20260724_0001"
LEGACY_TABLES = {
    "analysis_reports", "backtest_metric_rows", "backtest_runs", "fills",
    "graded_calls", "heartbeats", "holdout_access_log", "killswitch_state",
    "llm_decisions", "orders", "proposals", "risk_events", "rules",
    "shadow_calls", "trade_plans",
}


def _config(engine: Engine) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    return cfg


def _backup(engine: Engine) -> Path | None:
    url = make_url(str(engine.url))
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    source = Path(url.database).resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = source.with_name(f"{source.name}.{stamp}.pre-migration.bak")
    # SQLite's online backup API includes committed WAL pages. A raw file copy
    # can silently omit them.
    with sqlite3.connect(source) as source_db, sqlite3.connect(target) as backup_db:
        source_db.backup(backup_db)
    with sqlite3.connect(target) as backup_db:
        integrity = backup_db.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            target.unlink(missing_ok=True)
            raise RuntimeError(f"migration backup failed integrity check: {integrity!r}")
        backed_up_tables = {
            row[0]
            for row in backup_db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    source_tables = set(inspect(engine).get_table_names())
    if backed_up_tables != source_tables:
        target.unlink(missing_ok=True)
        raise RuntimeError("migration backup table manifest mismatch")
    target.chmod(0o600)
    return target


def adopt_existing(engine: Engine) -> Path:
    tables = set(inspect(engine).get_table_names())
    if tables != LEGACY_TABLES:
        raise RuntimeError(f"legacy schema mismatch: {sorted(tables ^ LEGACY_TABLES)}")
    backup = _backup(engine)
    assert backup is not None
    command.stamp(_config(engine), BASELINE)
    return backup


def upgrade(engine: Engine) -> Path | None:
    status = schema_status(engine)
    if not status.versioned and set(inspect(engine).get_table_names()):
        raise RuntimeError(
            "unversioned non-empty database; run `python -m "
            "trading_assistant.db.migrate adopt-existing` first"
        )
    backup = _backup(engine) if status.versioned and not status.ready else None
    command.upgrade(_config(engine), "head")
    require_current_schema(engine)
    return backup
```

Add the `argparse` entrypoint with `status`, `adopt-existing`, and `upgrade`. The
entrypoint constructs the engine from `Secrets().database_url`, prints the
revision and backup path, and exits nonzero on `SchemaOutOfDate`. `adopt-existing`
must run `adopt_existing(engine)` and then `upgrade(engine)` as separate,
operator-visible operations so the legacy database is never stamped without
immediately proving the upgrade path. Migration tests insert a committed row
while WAL is active and prove the backup contains it; a size-only backup test is
not acceptable.

- [ ] **Step 5: Run migration tests and the existing database tests**

Run:

```bash
uv run pytest tests/test_migrations.py tests/test_db_models.py tests/test_asset_class.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit the migration foundation**

```bash
git add pyproject.toml uv.lock alembic.ini migrations \
  src/trading_assistant/db/schema.py src/trading_assistant/db/migrate.py \
  tests/test_migrations.py
git commit -m "feat(db): add versioned schema migrations"
```

### Task 2: Persist approval identity and recoverable order states

**Files:**
- Modify: `src/trading_assistant/broker/models.py`
- Modify: `src/trading_assistant/db/models.py`
- Create: `migrations/versions/20260724_0002_order_outbox.py`
- Create: `src/trading_assistant/orders/__init__.py`
- Create: `src/trading_assistant/orders/repository.py`
- Create: `src/trading_assistant/orders/application.py`
- Test: `tests/test_order_state_machine.py`
- Test: `tests/test_order_application.py`
- Test: `tests/test_migrations.py`

**Interfaces:**
- Produces: `ApprovalCommand(order_id: int, actor: str, reason: str, now: datetime)`
- Produces: `OrderApplicationService.approve(command: ApprovalCommand) -> ApprovalResult`
- Produces: `OrderRepository.record_approval(...) -> bool`
- Produces: `OrderRepository.claim_submission(order_id: int, now: datetime) -> bool`

- [ ] **Step 1: Write failing state and approval tests**

```python
# tests/test_order_application.py
from datetime import datetime, timezone

import pytest

from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import AuditEvent, Order
from trading_assistant.orders.application import (
    ApprovalCommand,
    ApprovalConflict,
    OrderApplicationService,
)


def test_approval_records_actor_reason_and_audit(make_service):
    svc = make_service()
    order_id = svc.propose_order("AAPL", "buy", "market", notional="100")["order_id"]
    app = OrderApplicationService(svc.session_factory)
    result = app.approve(
        ApprovalCommand(order_id, "operator:avi", "reviewed receipt",
                        datetime.now(timezone.utc))
    )
    assert result.status is OrderStatus.APPROVAL_RECORDED
    with svc.session_factory() as session:
        row = session.get(Order, order_id)
        assert row.approval_actor == "operator:avi"
        assert row.approval_reason == "reviewed receipt"
        assert session.query(AuditEvent).filter_by(action="order.approve").count() == 1


def test_approval_compare_and_set_succeeds_once(make_service):
    svc = make_service()
    order_id = svc.propose_order("AAPL", "buy", "market", notional="100")["order_id"]
    app = OrderApplicationService(svc.session_factory)
    command = ApprovalCommand(order_id, "operator:avi", "reviewed",
                              datetime.now(timezone.utc))
    app.approve(command)
    with pytest.raises(ApprovalConflict):
        app.approve(command)
```

Extend `tests/test_order_state_machine.py` to assert:

```python
assert OrderStateMachine.can_transition(
    OrderStatus.PROPOSED, OrderStatus.APPROVAL_RECORDED
)
assert OrderStateMachine.can_transition(
    OrderStatus.APPROVAL_RECORDED, OrderStatus.SUBMITTING
)
assert OrderStateMachine.can_transition(
    OrderStatus.SUBMITTING, OrderStatus.ACCEPTANCE_UNKNOWN
)
assert OrderStateMachine.can_transition(
    OrderStatus.ACCEPTANCE_UNKNOWN, OrderStatus.SUBMITTED
)
```

Extend `tests/test_migrations.py` before creating the revision:

```python
def test_order_outbox_upgrade_preserves_and_maps_legacy_order(tmp_path):
    path = tmp_path / "legacy-order.db"
    engine = _legacy_engine(path)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO orders "
            "(idempotency_key,ticker,side,order_type,status,created_at,updated_at) "
            "VALUES ('legacy-approved','AAPL','buy','market','approved',"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        ))
    adopt_existing(engine)
    upgrade_backup = upgrade(engine)
    assert upgrade_backup is not None
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT status, approval_actor FROM orders "
            "WHERE idempotency_key='legacy-approved'"
        )).one()
    assert row.status == "approval_recorded"
    assert row.approval_actor is None
```

- [ ] **Step 2: Run focused tests and verify they fail**

Run:

```bash
uv run pytest tests/test_order_application.py tests/test_order_state_machine.py \
  tests/test_migrations.py -v
```

Expected: import or enum-member failures for the new states and service.

- [ ] **Step 3: Add statuses, columns, audit table, and legal transitions**

Add to `OrderStatus`:

```python
APPROVAL_RECORDED = "approval_recorded"
SUBMITTING = "submitting"
ACCEPTANCE_UNKNOWN = "acceptance_unknown"
```

Add to `Order`:

```python
approval_actor: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
approval_reason: Mapped[str] = mapped_column(Text, default="")
approved_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
submission_kind: Mapped[str] = mapped_column(String(16), default="simple")
submission_payload_json: Mapped[str] = mapped_column(Text, default="{}")
submission_attempt: Mapped[int] = mapped_column(default=0)
submission_started_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
acceptance_state: Mapped[str] = mapped_column(String(24), default="not_started")
last_reconciled_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
last_error_code: Mapped[str] = mapped_column(String(64), default="")
version: Mapped[int] = mapped_column(default=0)
```

Add:

```python
class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    result_code: Mapped[str] = mapped_column(String(64), default="")
    latency_ms: Mapped[int] = mapped_column(default=0)
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
```

Update legal transitions so `PROPOSED -> APPROVAL_RECORDED -> SUBMITTING`, and
allow
`SUBMITTING -> SUBMITTED|PARTIALLY_FILLED|FILLED|ACCEPTANCE_UNKNOWN|REJECTED`
plus
`ACCEPTANCE_UNKNOWN -> SUBMITTED|PARTIALLY_FILLED|FILLED|REJECTED|CANCELED`.
Also allow
`PROPOSED|APPROVAL_RECORDED -> EXPIRED`; approval never disables the proposal
TTL.

- [ ] **Step 4: Implement compare-and-set repository and approval service**

```python
# src/trading_assistant/orders/repository.py
import time
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import AuditEvent, Order


class OrderRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def record_approval(
        self, order_id: int, actor: str, reason: str, request_id: str, now: datetime
    ) -> bool:
        started = time.perf_counter()
        with self.session_factory() as session:
            idempotency_key = session.execute(
                update(Order)
                .where(Order.id == order_id, Order.status == OrderStatus.PROPOSED.value)
                .values(
                    status=OrderStatus.APPROVAL_RECORDED.value,
                    approval_actor=actor,
                    approval_reason=reason,
                    approved_at=now,
                    updated_at=now,
                    version=Order.version + 1,
                )
                .returning(Order.idempotency_key)
            ).scalar_one_or_none()
            if idempotency_key is None:
                session.rollback()
                return False
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            session.add(AuditEvent(
                actor=actor, action="order.approve", target_type="order",
                target_id=str(order_id), request_id=request_id, reason=reason,
                idempotency_key=idempotency_key,
                result_code="approval_recorded",
                latency_ms=elapsed_ms,
            ))
            session.commit()
            return True
```

```python
# src/trading_assistant/orders/application.py
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import Order

from .repository import OrderRepository


class ApprovalConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class ApprovalCommand:
    order_id: int
    actor: str
    reason: str
    now: datetime
    request_id: str = ""


@dataclass(frozen=True)
class ApprovalResult:
    order_id: int
    status: OrderStatus


class OrderApplicationService:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory
        self.repository = OrderRepository(session_factory)

    def approve(self, command: ApprovalCommand) -> ApprovalResult:
        with self.session_factory() as session:
            order = session.get(Order, command.order_id)
            if order is None:
                raise KeyError(f"order {command.order_id} not found")
            if order.proposal is not None and order.proposal.is_expired(command.now):
                order.status = OrderStatus.EXPIRED.value
                order.updated_at = command.now
                session.commit()
                return ApprovalResult(order.id, OrderStatus.EXPIRED)
        request_id = command.request_id or uuid4().hex
        if not self.repository.record_approval(
            command.order_id, command.actor, command.reason, request_id, command.now
        ):
            raise ApprovalConflict(f"order {command.order_id} approval already consumed")
        return ApprovalResult(command.order_id, OrderStatus.APPROVAL_RECORDED)
```

- [ ] **Step 5: Create and verify the immutable order-outbox migration**

Generate:

```bash
uv run alembic revision --autogenerate \
  -m "add order outbox state" \
  --rev-id 20260724_0002
```

Set `down_revision = "20260724_0001"`. Verify all added columns have server
defaults where existing rows need them, and add indexes for
`orders(status, broker_order_id)`, `orders(status, idempotency_key)`, and
`audit_events(request_id)`. Once committed, this revision is immutable; later
tasks add new revisions rather than editing it. Data-migrate any legacy
`APPROVED` row to `APPROVAL_RECORDED` while preserving its order, proposal, and
idempotency key. Keep `APPROVED` readable only for downgrade/legacy
deserialization; no new runtime transition may enter it.

Run:

```bash
uv run pytest tests/test_migrations.py tests/test_order_application.py \
  tests/test_order_state_machine.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit order state persistence**

```bash
git add src/trading_assistant/broker/models.py \
  src/trading_assistant/db/models.py src/trading_assistant/orders \
  migrations/versions/20260724_0002_order_outbox.py \
  tests/test_migrations.py tests/test_order_application.py \
  tests/test_order_state_machine.py
git commit -m "feat(orders): persist approval and submission intent"
```

### Task 3: Submit every order through a durable outbox

**Files:**
- Modify: `src/trading_assistant/broker/base.py`
- Modify: `src/trading_assistant/broker/mock.py`
- Modify: `src/trading_assistant/broker/alpaca.py`
- Modify: `src/trading_assistant/orders/repository.py`
- Create: `src/trading_assistant/orders/snapshot.py`
- Create: `src/trading_assistant/orders/submission.py`
- Modify: `src/trading_assistant/service.py`
- Test: `tests/test_mock_broker.py`
- Test: `tests/test_alpaca_broker.py`
- Test: `tests/test_order_submission.py`
- Test: `tests/test_execution.py`

**Interfaces:**
- Consumes: `OrderApplicationService.approve()`
- Produces: `BrokerClient.get_order_by_client_id(client_order_id: str) -> OrderResult | None`
- Produces: `OrderSubmissionService.submit(order_id: int) -> SubmissionResult`
- Produces: `OrderRepository.claim_submission(...)` and `record_submission_result(...)`

- [ ] **Step 1: Write crash-window and network-transaction tests**

```python
# tests/test_order_submission.py
from decimal import Decimal

from sqlalchemy import event

from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import Order
from trading_assistant.orders.submission import OrderSubmissionService


class AcceptThenDisconnectBroker(MockBroker):
    def submit_order(self, order):
        super().submit_order(order)
        raise ConnectionError("response lost after acceptance")


def test_accept_then_disconnect_becomes_unknown_without_duplicate(make_service):
    broker = AcceptThenDisconnectBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    order_id = svc.propose_order("AAPL", "buy", "market", notional="100")["order_id"]
    svc.order_application.approve(svc.approval_command(order_id, "operator:avi", "reviewed"))
    result = svc.order_submission.submit(order_id)
    assert result.status is OrderStatus.ACCEPTANCE_UNKNOWN
    assert len(broker._orders_by_key) == 1
    result2 = svc.order_submission.submit(order_id)
    assert result2.status is OrderStatus.ACCEPTANCE_UNKNOWN
    assert len(broker._orders_by_key) == 1


def test_broker_call_occurs_after_claim_transaction_commits(make_service):
    svc = make_service()
    order_id = svc.propose_order("AAPL", "buy", "market", notional="100")["order_id"]
    svc.order_application.approve(svc.approval_command(order_id, "operator:avi", "reviewed"))
    checked = {"inside_write_tx": None}
    original = svc.broker.submit_order

    def submit(order):
        with svc.session_factory() as session:
            checked["inside_write_tx"] = session.get(Order, order_id).status
        return original(order)

    svc.broker.submit_order = submit
    svc.order_submission.submit(order_id)
    assert checked["inside_write_tx"] == OrderStatus.SUBMITTING.value


def test_approved_proposal_that_expires_before_submit_never_calls_broker(
    make_service, fake_now
):
    svc = make_service(now=fake_now)
    order_id = approved_order_id(svc)
    fake_now.advance(minutes=16)
    result = svc.order_submission.submit(order_id)
    assert result.status is OrderStatus.EXPIRED
    assert svc.broker.submission_count == 0
```

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run:

```bash
uv run pytest tests/test_order_submission.py -v
```

Expected: failures because submission services and durable states are not wired.

- [ ] **Step 3: Add client-order lookup to every broker**

Add to `BrokerClient`:

```python
class BrokerSubmissionRejected(RuntimeError):
    """Broker definitively rejected the request and did not accept an order."""

    def __init__(self, stable_code: str, message: str = "") -> None:
        super().__init__(message or stable_code)
        self.stable_code = stable_code


class BrokerAcceptanceUnknown(RuntimeError):
    """The request was sent but acceptance cannot be determined."""


def get_order_by_client_id(self, client_order_id: str) -> OrderResult | None: ...
```

Implement in `MockBroker` as:

```python
def get_order_by_client_id(self, client_order_id: str) -> OrderResult | None:
    return self._orders_by_key.get(client_order_id)
```

Expose Alpaca’s existing `_find_by_client_id` safely:

```python
def get_order_by_client_id(self, client_order_id: str) -> OrderResult | None:
    found = self._find_by_client_id(client_order_id)
    return self._to_result(found) if found is not None else None
```

Add contract tests proving unknown IDs return `None` and accepted IDs return the
same broker order without submission. In `AlpacaBroker`, map a documented
validation/4xx rejection to `BrokerSubmissionRejected` only when the response
proves no order was accepted. Map timeout, connection reset, malformed response,
and any post-send uncertainty to `BrokerAcceptanceUnknown`.

- [ ] **Step 4: Add repository claim and result methods**

```python
def claim_submission(self, order_id: int, now: datetime) -> bool:
    with self.session_factory() as session:
        result = session.execute(
            update(Order)
            .where(
                Order.id == order_id,
                Order.status == OrderStatus.APPROVAL_RECORDED.value,
            )
            .values(
                status=OrderStatus.SUBMITTING.value,
                submission_attempt=Order.submission_attempt + 1,
                submission_started_at=now,
                acceptance_state="pending",
                version=Order.version + 1,
                updated_at=now,
            )
        )
        session.commit()
        return result.rowcount == 1


def record_submission_result(
    self, order_id: int, status: OrderStatus, broker_order_id: str | None,
    error_code: str, now: datetime
) -> None:
    if status not in {
        OrderStatus.SUBMITTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.ACCEPTANCE_UNKNOWN,
        OrderStatus.REJECTED,
    }:
        raise ValueError(f"invalid submission result {status.value}")
    with self.session_factory() as session:
        result = session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status == OrderStatus.SUBMITTING.value)
            .values(
                status=status.value,
                broker_order_id=broker_order_id,
                acceptance_state=status.value,
                last_error_code=error_code,
                updated_at=now,
                version=Order.version + 1,
            )
        )
        if result.rowcount != 1:
            raise RuntimeError(f"order {order_id} lost submission claim")
        session.commit()


def record_pre_submission_rejection(
    self, order_id: int, reasons: tuple[str, ...], now: datetime
) -> None:
    with self.session_factory() as session:
        result = session.execute(
            update(Order)
            .where(
                Order.id == order_id,
                Order.status == OrderStatus.APPROVAL_RECORDED.value,
            )
            .values(
                status=OrderStatus.REJECTED.value,
                last_error_code="risk_rejected",
                updated_at=now,
                version=Order.version + 1,
            )
        )
        if result.rowcount != 1:
            raise RuntimeError(f"order {order_id} changed during risk rejection")
        session.add(RiskEvent(
            order_id=order_id,
            accepted=False,
            reasons_json=json.dumps(list(reasons)),
            created_at=now,
        ))
        session.commit()


def expire_approved(self, order_id: int, now: datetime) -> None:
    with self.session_factory() as session:
        result = session.execute(
            update(Order)
            .where(
                Order.id == order_id,
                Order.status == OrderStatus.APPROVAL_RECORDED.value,
            )
            .values(
                status=OrderStatus.EXPIRED.value,
                updated_at=now,
                version=Order.version + 1,
            )
        )
        if result.rowcount != 1:
            raise RuntimeError(f"order {order_id} changed during expiry")
        session.commit()
```

- [ ] **Step 5: Implement submission orchestration**

```python
# src/trading_assistant/orders/submission.py
from dataclasses import dataclass
from datetime import datetime, timezone

from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import Order, RiskEvent


@dataclass(frozen=True)
class SubmissionResult:
    order_id: int
    status: OrderStatus
    broker_order_id: str | None = None
    risk_reasons: tuple[str, ...] = ()


class OrderSubmissionService:
    def __init__(self, repository, session_factory, broker, snapshot_service,
                 risk_for_symbol, clock_for_symbol, killswitch_for_symbol,
                 now=lambda: datetime.now(timezone.utc)) -> None:
        self.repository = repository
        self.session_factory = session_factory
        self.broker = broker
        self.snapshot_service = snapshot_service
        self.risk_for_symbol = risk_for_symbol
        self.clock_for_symbol = clock_for_symbol
        self.killswitch_for_symbol = killswitch_for_symbol
        self.now = now

    def submit(self, order_id: int) -> SubmissionResult:
        now = self.now()
        with self.session_factory() as session:
            order = session.get(Order, order_id)
            if order is None:
                raise KeyError(f"order {order_id} not found")
            current = OrderStatus(order.status)
            if current is OrderStatus.ACCEPTANCE_UNKNOWN:
                return SubmissionResult(order_id, current, order.broker_order_id)
            if current is not OrderStatus.APPROVAL_RECORDED:
                return SubmissionResult(order_id, current, order.broker_order_id)
            expired = order.proposal is not None and order.proposal.is_expired(now)
            request = order.to_order_request()
            submission_kind = order.submission_kind
            submission_payload = order.submission_payload()
        if expired:
            self.repository.expire_approved(order_id, now)
            return SubmissionResult(order_id, OrderStatus.EXPIRED)

        # Provider reads and deterministic risk checks happen before the durable
        # broker-call claim. Failure here leaves APPROVAL_RECORDED and is safe to
        # retry; no broker request has been made.
        snapshot = self.snapshot_service.assemble_for_execution(
            request.ticker, exclude_order_id=order_id
        )
        risk = self.risk_for_symbol(request.ticker).check(
            request,
            snapshot,
            killswitch_tripped=self.killswitch_for_symbol(request.ticker),
            market_open=self.clock_for_symbol(request.ticker).is_open(),
        )
        if risk.rejected:
            reasons = tuple(risk.reasons)
            self.repository.record_pre_submission_rejection(order_id, reasons, now)
            return SubmissionResult(order_id, OrderStatus.REJECTED,
                                    risk_reasons=reasons)

        # Exactly one caller can claim. The committed SUBMITTING row is the
        # durable intent immediately preceding broker I/O.
        if not self.repository.claim_submission(order_id, self.now()):
            with self.session_factory() as session:
                changed = session.get(Order, order_id)
                return SubmissionResult(
                    order_id, OrderStatus(changed.status), changed.broker_order_id
                )
        try:
            if submission_kind == "bracket":
                broker_result = self.broker.submit_bracket(
                    request,
                    submission_payload["take_profit"],
                    submission_payload["stop_loss"],
                )
            else:
                broker_result = self.broker.submit_order(request)
        except BrokerSubmissionRejected as exc:
            self.repository.record_submission_result(
                order_id, OrderStatus.REJECTED, None,
                exc.stable_code, self.now()
            )
            return SubmissionResult(order_id, OrderStatus.REJECTED)
        except Exception as exc:
            # Once broker I/O begins, every unproven outcome is unknown. Never
            # retry here.
            self.repository.record_submission_result(
                order_id, OrderStatus.ACCEPTANCE_UNKNOWN, None,
                type(exc).__name__, self.now()
            )
            return SubmissionResult(order_id, OrderStatus.ACCEPTANCE_UNKNOWN)
        accepted_status = (
            broker_result.status
            if broker_result.status in {
                OrderStatus.SUBMITTED,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.FILLED,
            }
            else OrderStatus.SUBMITTED
        )
        self.repository.record_submission_result(
            order_id, accepted_status, broker_result.broker_order_id, "", self.now()
        )
        return SubmissionResult(
            order_id, accepted_status, broker_result.broker_order_id
        )
```

Move order-to-request and bracket-payload parsing into validated methods or helper
functions; never evaluate arbitrary JSON. Add a provider-failure test proving a
snapshot error leaves `APPROVAL_RECORDED`, and a definitive rejection test
proving only a broker-confirmed no-acceptance response becomes `REJECTED`.

- [ ] **Step 6: Delegate current public execution paths**

Update `TradingService.approve_order` to:

1. build an `ApprovalCommand` with the authenticated actor/reason/request ID;
2. call `OrderApplicationService.approve`;
3. call `OrderSubmissionService.submit`;
4. map `ApprovalConflict` to the existing conflict response;
5. report `executed=True` only for `SUBMITTED|PARTIALLY_FILLED|FILLED`.

Until Task 6 moves breaker and market state into `PortfolioSnapshot`, inject
`clock_for_symbol` from the existing class clock router and inject
`killswitch_for_symbol` as a read-only helper that opens a short session, calls
`KillSwitch.is_tripped(session, AssetClass.for_symbol(symbol))`, and closes the
session before any broker call.

Update `submit_bracket_order` to persist a proposed internal order with
`submission_kind="bracket"` and a validated payload, record approval intent, and
call the same `OrderSubmissionService`. Remove its separate broker-I/O path.

- [ ] **Step 7: Run focused and execution regression tests**

Run:

```bash
uv run pytest tests/test_order_submission.py tests/test_execution.py \
  tests/test_alpaca_broker.py tests/test_mock_broker.py tests/test_atomic_approval.py -v
```

Expected: all tests pass and no assertion expects `APPROVED` as a durable
post-approval state.

- [ ] **Step 8: Commit the durable outbox**

```bash
git add src/trading_assistant/broker src/trading_assistant/orders \
  src/trading_assistant/service.py tests/test_order_submission.py \
  tests/test_execution.py tests/test_alpaca_broker.py tests/test_mock_broker.py \
  tests/test_atomic_approval.py
git commit -m "feat(execution): submit orders through durable outbox"
```

### Task 4: Reconcile unknown acceptance and make panic truthful

**Files:**
- Modify: `src/trading_assistant/broker/base.py`
- Modify: `src/trading_assistant/broker/mock.py`
- Modify: `src/trading_assistant/broker/alpaca.py`
- Create: `src/trading_assistant/orders/reconciliation.py`
- Modify: `src/trading_assistant/db/models.py`
- Create: `migrations/versions/20260724_0003_reconciliation.py`
- Modify: `src/trading_assistant/service.py`
- Modify: `src/trading_assistant/daemon/monitor.py`
- Test: `tests/test_reconciliation_service.py`
- Test: `tests/test_hardening.py`
- Test: `tests/test_launch.py`

**Interfaces:**
- Consumes: `BrokerClient.get_order_by_client_id`
- Produces: `BrokerClient.get_open_orders() -> list[OrderResult]`
- Produces: `ReconciliationService.reconcile() -> ReconciliationReport`
- Produces: `ReconciliationService.panic(actor: str, reason: str) -> PanicReport`

- [ ] **Step 1: Write failing unknown-acceptance and panic tests**

```python
def test_reconcile_unknown_finds_remote_acceptance(make_service):
    svc = make_service(broker=AcceptThenDisconnectBroker())
    order_id = approved_order_id(svc)
    svc.order_submission.submit(order_id)
    report = svc.reconciliation.reconcile()
    assert report.resolved_unknown == 1
    with svc.session_factory() as session:
        row = session.get(Order, order_id)
        assert row.status == OrderStatus.SUBMITTED.value
        assert row.broker_order_id is not None


def test_panic_reports_unconfirmed_cancel_as_not_safe(make_service):
    broker = CancelFailsBroker()
    svc = make_service(broker=broker)
    order_id = submitted_order_id(svc)
    report = svc.reconciliation.panic("operator:avi", "manual drill")
    assert report.safe is False
    assert report.unconfirmed_order_ids == (order_id,)
    assert report.message != "everything halted"
```

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```bash
uv run pytest tests/test_reconciliation_service.py tests/test_hardening.py -v
```

Expected: missing reconciliation service or report fields.

- [ ] **Step 3: Add broker open-order enumeration**

Add:

```python
def get_open_orders(self) -> list[OrderResult]: ...
```

`MockBroker` returns values with `SUBMITTED` or `PARTIALLY_FILLED`. `AlpacaBroker`
uses the SDK open-order request and maps each result through `_to_result`.

- [ ] **Step 4: Implement reconciliation reports and unknown lookup**

```python
@dataclass(frozen=True)
class ReconciliationReport:
    resolved_unknown: int
    unresolved_unknown: tuple[int, ...]
    synced_orders: int
    inserted_fills: int
    broker_drift: tuple[str, ...]


def reconcile_unknown(self) -> tuple[int, tuple[int, ...]]:
    resolved = 0
    unresolved = []
    with self.session_factory() as session:
        rows = session.scalars(
            select(Order).where(Order.status.in_([
                OrderStatus.SUBMITTING.value,
                OrderStatus.ACCEPTANCE_UNKNOWN.value,
            ]))
        ).all()
    for row in rows:
        remote = self.broker.get_order_by_client_id(row.idempotency_key)
        if remote is None:
            unresolved.append(row.id)
            continue
        self.repository.resolve_acceptance(
            row.id, remote.broker_order_id, remote.status,
            datetime.now(timezone.utc)
        )
        resolved += 1
    return resolved, tuple(unresolved)
```

Reuse existing exact fill-activity reconciliation, but query incrementally by
persisted cursor instead of scanning every fill. Store the cursor only after all
activities in the batch commit. Add a `ReconciliationCursor` row keyed by
`broker` and `stream`, with the last broker activity ID/time and optimistic
version. Create revision `20260724_0003_reconciliation.py` with
`down_revision = "20260724_0002"`; never modify the committed outbox revision.

- [ ] **Step 5: Implement panic over broker truth and local truth**

Panic must:

1. persist the global operator breaker first;
2. disable active rule groups;
3. reconcile unknown acceptance;
4. enumerate broker open orders;
5. cancel the union of broker and local open orders;
6. query each cancellation result;
7. return `safe=True` only when no open or unknown order remains.

Use:

```python
@dataclass(frozen=True)
class PanicReport:
    safe: bool
    confirmed_canceled: tuple[str, ...]
    unconfirmed_order_ids: tuple[int, ...]
    remote_open_order_ids: tuple[str, ...]
    message: str
```

- [ ] **Step 6: Delegate monitor reconciliation and service compatibility methods**

`Monitor.reconcile()` calls `service.reconciliation.reconcile()`.
`TradingService.sync_open_orders()` serializes `ReconciliationReport`.
`TradingService.panic()` requires actor and reason internally and serializes
`PanicReport`; the API supplies those values.

- [ ] **Step 7: Run reconciliation, launch, and hardening tests**

Run:

```bash
uv run pytest tests/test_reconciliation_service.py tests/test_launch.py \
  tests/test_hardening.py tests/test_monitor.py -v
```

Expected: all pass, including duplicate-fill and restart tests.

- [ ] **Step 8: Commit reconciliation and truthful panic**

```bash
git add src/trading_assistant/broker src/trading_assistant/orders/reconciliation.py \
  src/trading_assistant/db/models.py \
  migrations/versions/20260724_0003_reconciliation.py \
  src/trading_assistant/service.py src/trading_assistant/daemon/monitor.py \
  tests/test_reconciliation_service.py tests/test_launch.py \
  tests/test_hardening.py tests/test_monitor.py
git commit -m "feat(ops): reconcile unknown orders and report panic truthfully"
```

### Task 5: Replace schemaless rules with typed commands and group leases

**Files:**
- Create: `src/trading_assistant/rules/__init__.py`
- Create: `src/trading_assistant/rules/models.py`
- Create: `src/trading_assistant/rules/repository.py`
- Create: `src/trading_assistant/rules/application.py`
- Create: `src/trading_assistant/rules/worker.py`
- Modify: `src/trading_assistant/db/models.py`
- Modify: `src/trading_assistant/daemon/monitor.py`
- Modify: `src/trading_assistant/service.py`
- Test: `tests/test_rule_models.py`
- Test: `tests/test_rule_leases.py`
- Test: `tests/test_plan_rules.py`
- Test: `tests/test_monitor.py`

**Interfaces:**
- Produces: `RuleCommand`, `RuleCondition`, `RuleAction`, `RuleKind`, `RuleState`
- Produces: `RuleRepository.lease_group(...) -> RuleGroupLease | None`
- Produces: `RuleRepository.claim_terminal(...) -> bool`
- Produces: `RuleApplicationService.propose_from_lease(...) -> RuleOutcome`
- Produces: `RuleWorker.tick() -> list[RuleOutcome]`

- [ ] **Step 1: Write failing validation and concurrent-lease tests**

```python
def test_unknown_rule_condition_is_rejected():
    with pytest.raises(ValidationError):
        RuleCommand.model_validate({
            "ticker": "AAPL",
            "kind": "price",
            "condition": {"type": "mystery", "value": 1},
            "action": {"side": "buy", "notional": "100", "order_type": "market"},
        })


def test_rule_action_requires_exactly_qty_or_notional():
    with pytest.raises(ValidationError):
        RuleAction(side="buy", order_type="market", qty="1", notional="100")


def test_two_workers_cannot_lease_sibling_rules(session_factory, seeded_oco_group):
    repo_a = RuleRepository(session_factory, owner="worker-a")
    repo_b = RuleRepository(session_factory, owner="worker-b")
    assert repo_a.lease_group(seeded_oco_group, now=NOW) is not None
    assert repo_b.lease_group(seeded_oco_group, now=NOW) is None
```

- [ ] **Step 2: Run focused tests and confirm failure**

Run:

```bash
uv run pytest tests/test_rule_models.py tests/test_rule_leases.py -v
```

Expected: imports fail because the typed rules package does not exist.

- [ ] **Step 3: Implement typed commands**

```python
class RuleKind(str, Enum):
    PRICE = "price"
    ENTRY = "entry"
    TARGET = "target"
    STOP = "stop"
    TRAILING = "trailing"
    TIME = "time"


class RuleState(str, Enum):
    ACTIVE = "active"
    PROCESSING = "processing"
    TRIGGERED = "triggered"
    CANCELED = "canceled"
    FAILED = "failed"


class PriceCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["price"]
    direction: Literal["below", "above"]
    price: Decimal = Field(gt=0)


class TrailingCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["trailing"]
    percent: Decimal = Field(gt=0, le=100)


class TimeCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["time"]
    deadline: datetime


RuleCondition = Annotated[
    PriceCondition | TrailingCondition | TimeCondition,
    Field(discriminator="type"),
]


class RuleAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"]
    qty: Decimal | None = Field(default=None, gt=0)
    notional: Decimal | None = Field(default=None, gt=0)
    limit_price: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_shape(self):
        if (self.qty is None) == (self.notional is None):
            raise ValueError("exactly one of qty or notional is required")
        if (self.order_type == "limit") != (self.limit_price is not None):
            raise ValueError("limit_price must be present only for limit orders")
        return self
```

`RuleCommand` includes ticker, kind, condition, action, group key, pre-approved
flag, fraction, and high-water mark. It rejects pre-approval while the global
auto-execute feature remains disabled.

- [ ] **Step 4: Add rule-group persistence and lease CAS**

Add `RuleGroup` with unique `group_key`, state, lease owner/expiry, terminal rule,
version, `reconciliation_required`, and timestamps. Add `group_id` and
`payload_version` to `Rule`, plus nullable `source_rule_group_id` on `Proposal`
so execution and reconciliation can trace an order back to the group that
created it.

Implement lease:

```python
def lease_group(self, group_id: int, now: datetime,
                ttl: timedelta = timedelta(seconds=30)) -> RuleGroupLease | None:
    with self.session_factory() as session:
        result = session.execute(
            update(RuleGroup)
            .where(
                RuleGroup.id == group_id,
                RuleGroup.state == "active",
                RuleGroup.reconciliation_required.is_(False),
                or_(RuleGroup.lease_expires_at.is_(None),
                    RuleGroup.lease_expires_at <= now),
            )
            .values(
                lease_owner=self.owner,
                lease_expires_at=now + ttl,
                version=RuleGroup.version + 1,
            )
        )
        session.commit()
        return RuleGroupLease(group_id, self.owner, now + ttl) if result.rowcount == 1 else None
```

`claim_terminal` transitions the group and winning rule, cancels siblings, and
clears the lease in one transaction guarded by owner and version. Any group
linked to a `SUBMITTING` or `ACCEPTANCE_UNKNOWN` order sets
`reconciliation_required=true`; only `ReconciliationService` clears it after
client-ID lookup proves the group has no unresolved broker acceptance. Add a
restart test proving an expired lease with that flag cannot be reclaimed.

- [ ] **Step 5: Create the rule-lease revision and migrate existing JSON**

Create `migrations/versions/20260724_0004_rule_leases.py` with
`down_revision = "20260724_0003"`. Its data migration maps:

- `{"price_below": X}` to `{"type":"price","direction":"below","price":X}`;
- `{"price_above": X}` to `{"type":"price","direction":"above","price":X}`;
- `{"trailing_stop_pct": X}` to `{"type":"trailing","percent":X}`;
- time rules to `{"type":"time","deadline":<existing deadline>}`.

Abort the migration on unknown keys rather than carrying malformed active rules.
Create one group per existing `plan_id`; standalone rules receive a stable
`legacy-rule-{id}` group key.

- [ ] **Step 6: Replace monitor claims with `RuleWorker`**

`RuleWorker.tick()`:

1. loads active groups;
2. leases one group;
3. obtains one fresh quote per ticker;
4. validates staleness;
5. evaluates typed conditions;
6. calls `RuleApplicationService.propose_from_lease`, which creates the bounded
   proposal, marks the winning rule terminal, and cancels siblings in one
   transaction guarded by lease owner and group version;
7. never auto-approves in this subproject;
8. releases the lease without a proposal when no condition fires.

`Monitor.tick()` delegates to `RuleWorker.tick()`. Remove per-rule
`active -> processing` claims and sibling cancellation from `Monitor`. Inject a
crash immediately before and after the proposal transaction and prove restart
creates at most one proposal for the group.

- [ ] **Step 7: Run rule and concurrency regressions**

Run:

```bash
uv run pytest tests/test_rule_models.py tests/test_rule_leases.py \
  tests/test_rules_engine.py tests/test_plan_rules.py tests/test_monitor.py -v
```

Expected: all tests pass, and a two-thread sibling-trigger test records one
proposal and one terminal rule.

- [ ] **Step 8: Commit typed rules and leases**

```bash
git add src/trading_assistant/rules src/trading_assistant/db/models.py \
  src/trading_assistant/daemon/monitor.py src/trading_assistant/service.py \
  migrations/versions/20260724_0004_rule_leases.py \
  tests/test_rule_models.py tests/test_rule_leases.py tests/test_rules_engine.py \
  tests/test_plan_rules.py tests/test_monitor.py
git commit -m "feat(rules): validate rules and lease OCO groups"
```

### Task 6: Enforce complete execution snapshots and scoped circuit breakers

**Files:**
- Modify: `src/trading_assistant/broker/models.py`
- Modify: `src/trading_assistant/config.py`
- Modify: `config.yaml`
- Create: `src/trading_assistant/risk/breakers.py`
- Modify: `src/trading_assistant/orders/snapshot.py`
- Modify: `src/trading_assistant/orders/submission.py`
- Modify: `src/trading_assistant/db/models.py`
- Create: `migrations/versions/20260724_0005_breakers.py`
- Modify: `src/trading_assistant/risk/engine.py`
- Modify: `src/trading_assistant/service.py`
- Test: `tests/test_breakers.py`
- Test: `tests/test_execution_risk_snapshot.py`
- Test: `tests/test_risk_engine.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `BreakerScope`, `BreakerState`, `BreakerService`
- Extends: `PortfolioSnapshot` with freshness, cash, P&L, drawdown, spread, and drift
- Consumes: complete snapshot in `RiskEngine.check`
- Produces: pure `RiskEngine.check(order: OrderRequest, snapshot: PortfolioSnapshot) -> RiskResult`

- [ ] **Step 1: Write failing pure-risk and persistence tests**

```python
def test_execution_rejects_stale_quote(risk_config, make_snapshot):
    snapshot = make_snapshot(prices={"AAPL": Decimal("100")})
    snapshot = replace(snapshot, quote_fresh=False)
    result = RiskEngine(risk_config).check(order("AAPL", "100"), snapshot)
    assert "quote is stale" in result.reasons


def test_execution_rejects_insufficient_buying_power(risk_config, make_snapshot):
    snapshot = make_snapshot(
        prices={"AAPL": Decimal("100")}, buying_power=Decimal("50")
    )
    result = RiskEngine(risk_config).check(order("AAPL", "100"), snapshot)
    assert "insufficient buying power" in result.reasons


def test_scoped_breakers_persist_and_reset_independently(session_factory):
    service = BreakerService(session_factory)
    data_scope = BreakerScope.data(AssetClass.EQUITY)
    drift_scope = BreakerScope.broker_drift()
    service.trip(data_scope, "feed disagreement", "daemon")
    service.trip(drift_scope, "position mismatch", "daemon")
    service.reset(
        data_scope,
        actor="operator:avi",
        reason="feed healthy",
        prior_health={"provider": "healthy", "age_seconds": 1},
    )
    assert service.is_tripped(data_scope) is False
    assert service.is_tripped(drift_scope) is True
```

- [ ] **Step 2: Run tests and confirm missing fields/services**

Run:

```bash
uv run pytest tests/test_breakers.py tests/test_execution_risk_snapshot.py -v
```

Expected: missing `BreakerService` and snapshot fields.

- [ ] **Step 3: Extend configuration and immutable snapshot**

Add risk keys:

```python
max_quote_age_seconds: float = Field(default=60.0, gt=0)
max_spread_pct: float = Field(default=1.0, gt=0)
max_daily_total_loss: float = Field(default=500.0, gt=0)
max_account_drawdown_pct: float = Field(default=10.0, gt=0, le=100)
require_broker_reconciled: bool = True
```

Set `ExecutionConfig.prefer_bracket_orders` default to `False` as well as keeping
the deployed YAML value false. Add strict-config tests proving neither
auto-execution nor automatic bracket execution can become enabled unnoticed.

Add snapshot fields:

```python
cash: Decimal = Decimal(0)
unrealized_pnl_today: Decimal = Decimal(0)
daily_pnl_complete: bool = True
account_high_water_mark: Decimal = Decimal(0)
account_equity: Decimal = Decimal(0)
quote_fresh: bool = True
market_open: bool = True
spread_pct_by_ticker: dict[str, Decimal] = field(default_factory=dict)
pending_buy_notional_by_ticker: dict[str, Decimal] = field(default_factory=dict)
reserved_sell_qty_by_ticker: dict[str, Decimal] = field(default_factory=dict)
broker_reconciled: bool = True
active_breakers: frozenset[str] = frozenset()
```

Extend `Position` with `unrealized_intraday_pnl: Decimal | None`. Alpaca maps the
broker-provided intraday P&L field; `MockBroker` computes it from the controlled
session-open price. If any held position lacks a trustworthy intraday value,
snapshot assembly marks daily P&L incomplete and the execution check rejects
new risk instead of substituting lifetime unrealized P&L.

Tests and builders must set explicit values rather than relying on production
defaults when exercising execution.

- [ ] **Step 4: Implement persisted breaker service**

Generalize kill-switch persistence into typed, targetable scopes:

```python
class BreakerKind(str, Enum):
    LOSS = "loss"
    DRAWDOWN = "drawdown"
    DATA = "data"
    LIQUIDITY = "liquidity"
    BROKER_DRIFT = "broker_drift"
    OPERATOR_GLOBAL = "operator_global"


@dataclass(frozen=True)
class BreakerScope:
    kind: BreakerKind
    target: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.target}" if self.target else self.kind.value

    @classmethod
    def data(cls, asset_class: AssetClass) -> "BreakerScope":
        return cls(BreakerKind.DATA, asset_class.value)

    @classmethod
    def loss(cls, asset_class: AssetClass) -> "BreakerScope":
        return cls(BreakerKind.LOSS, asset_class.value)

    @classmethod
    def drawdown(cls, asset_class: AssetClass) -> "BreakerScope":
        return cls(BreakerKind.DRAWDOWN, asset_class.value)

    @classmethod
    def liquidity(cls, target: str) -> "BreakerScope":
        return cls(BreakerKind.LIQUIDITY, target.upper())

    @classmethod
    def broker_drift(cls) -> "BreakerScope":
        return cls(BreakerKind.BROKER_DRIFT)

    @classmethod
    def operator_global(cls) -> "BreakerScope":
        return cls(BreakerKind.OPERATOR_GLOBAL)


@dataclass(frozen=True)
class BreakerState:
    scope: BreakerScope
    tripped: bool
    reason: str
    actor: str
    updated_at: datetime
```

Persist those values in `CircuitBreakerState`, keyed by unique `scope_key`.
`BreakerService` exposes these exact methods:

```python
def trip(
    self, scope: BreakerScope, reason: str, actor: str,
    *, now: datetime | None = None
) -> BreakerState: ...

def is_tripped(self, scope: BreakerScope) -> bool: ...

def active_for_symbol(self, symbol: str) -> tuple[BreakerState, ...]: ...

def reset(
    self, scope: BreakerScope, actor: str, reason: str,
    prior_health: Mapping[str, object], *, now: datetime | None = None
) -> BreakerState: ...
```

Each mutation uses one compare-and-set/upsert transaction and writes an
`AuditEvent`; reset refuses an empty reason or empty health report. Keep
`KillSwitch` as a compatibility facade over loss scopes until all callers
migrate. Create
`20260724_0005_breakers.py` with `down_revision = "20260724_0004"` and migrate
each existing `killswitch_state` row into the matching `loss:{asset_class}`
scope without clearing any tripped state. The same revision adds
`AccountRiskState(asset_class, high_water_mark, last_equity, updated_at)` so
drawdown state survives restarts.

- [ ] **Step 5: Add synchronous risk checks**

Restore the pure two-argument interface and append checks in this order:

```python
def check(self, order: OrderRequest, snapshot: PortfolioSnapshot) -> RiskResult:
    symbol = order.ticker.upper()
    quote = snapshot.quotes.get(symbol)
    reasons: list[str] = []
    base_checks = [
        rules.check_allowlist(order, self.config),
        rules.check_pending_exposure_known(snapshot),
        rules.check_market_hours(order, self.config, snapshot.market_open),
        rules.check_max_notional(order, snapshot, self.config),
        rules.check_max_position(order, snapshot, self.config),
        rules.check_portfolio_exposure(order, snapshot, self.config),
        rules.check_price_sanity(order, snapshot, self.config),
    ]
    reasons.extend(reason for reason in base_checks if reason is not None)

    if not snapshot.quote_fresh:
        reasons.append("quote is stale")
    if snapshot.active_breakers:
        scopes = ",".join(sorted(snapshot.active_breakers))
        reasons.append(f"active circuit breaker: {scopes}")
    if self.config.require_broker_reconciled and not snapshot.broker_reconciled:
        reasons.append("broker reconciliation is not current")
    if not snapshot.daily_pnl_complete:
        reasons.append("daily P&L snapshot is incomplete")

    if quote is not None:
        estimated = order.estimated_notional(quote.last)
        if order.side is OrderSide.BUY and estimated > snapshot.buying_power:
            reasons.append("insufficient buying power")
        if order.side is OrderSide.SELL:
            position = snapshot.positions.get(symbol)
            held = max(position.qty, Decimal(0)) if position else Decimal(0)
            reserved = snapshot.reserved_sell_qty_by_ticker.get(
                symbol, Decimal(0)
            )
            requested = (
                order.qty
                if order.qty is not None
                else order.notional / quote.last
            )
            if requested > held - reserved:
                reasons.append("sell quantity exceeds unreserved position")

    daily_total = snapshot.realized_pnl_today + snapshot.unrealized_pnl_today
    if daily_total <= -Decimal(str(self.config.max_daily_total_loss)):
        reasons.append("daily total-loss limit reached")
    if snapshot.account_high_water_mark > 0:
        drawdown = (
            snapshot.account_high_water_mark - snapshot.account_equity
        ) / snapshot.account_high_water_mark * Decimal(100)
        if drawdown >= Decimal(str(self.config.max_account_drawdown_pct)):
            reasons.append("account drawdown limit reached")
    spread = snapshot.spread_pct_by_ticker.get(symbol)
    if (
        spread is not None
        and spread > Decimal(str(self.config.max_spread_pct))
    ):
        reasons.append("spread exceeds configured maximum")

    warnings: list[str] = []
    if self.config.warn_on_cross_broker_concentration:
        warning = rules.check_cross_broker_concentration(
            order, snapshot, self.config
        )
        if warning is not None:
            warnings.append(warning)
    return RiskResult(approved=not reasons, reasons=reasons, warnings=warnings)
```

Update `OrderSubmissionService` in the same step so the completed snapshot is
the only runtime risk input:

```python
class OrderSubmissionService:
    def __init__(
        self, repository, session_factory, broker, snapshot_service,
        risk_for_symbol, now=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.repository = repository
        self.session_factory = session_factory
        self.broker = broker
        self.snapshot_service = snapshot_service
        self.risk_for_symbol = risk_for_symbol
        self.now = now

    def _risk_check(self, request: OrderRequest, order_id: int) -> RiskResult:
        snapshot = self.snapshot_service.assemble_for_execution(
            request.ticker, exclude_order_id=order_id
        )
        return self.risk_for_symbol(request.ticker).check(request, snapshot)
```

Inside `submit()`, replace the temporary Task 3 call with
`risk = self._risk_check(request, order_id)`.

- [ ] **Step 6: Assemble all fields at execution time**

`PortfolioSnapshotService.assemble_for_execution` obtains account, positions, and
quotes before opening the read-only database session used for pending exposure
and P&L. It computes quote age, spread, current reconciliation status, account
high-water mark, and active breaker scopes. It returns
`pending_exposure_complete=False` on any incomplete local ledger query, which the
risk engine already rejects. Pending exposure includes
`APPROVAL_RECORDED`, `SUBMITTING`, `ACCEPTANCE_UNKNOWN`, `SUBMITTED`, and
`PARTIALLY_FILLED`; buys reserve notional and sells reserve remaining quantity
without netting opposite sides. Tests prove an unknown buy consumes exposure and
an unknown sell prevents a second exit from overselling.

- [ ] **Step 7: Run risk, config, stress, and execution tests**

Run:

```bash
uv run pytest tests/test_breakers.py tests/test_execution_risk_snapshot.py \
  tests/test_risk_engine.py tests/test_config.py tests/stress/test_stress_scenarios.py -v
```

Expected: all pass, including restart persistence and asset-class independence.

- [ ] **Step 8: Commit complete risk enforcement**

```bash
git add config.yaml src/trading_assistant/config.py \
  src/trading_assistant/broker/models.py src/trading_assistant/db/models.py \
  migrations/versions/20260724_0005_breakers.py src/trading_assistant/risk \
  src/trading_assistant/orders/snapshot.py \
  src/trading_assistant/orders/submission.py src/trading_assistant/service.py \
  tests/test_breakers.py \
  tests/test_execution_risk_snapshot.py tests/test_risk_engine.py \
  tests/test_config.py tests/stress/test_stress_scenarios.py
git commit -m "feat(risk): enforce complete snapshots and scoped breakers"
```

### Task 7: Replace bearer-token access with fail-closed operator sessions

**Files:**
- Create: `src/trading_assistant/app/routers/__init__.py`
- Create: `src/trading_assistant/app/routers/auth.py`
- Create: `src/trading_assistant/app/auth.py`
- Create: `src/trading_assistant/app/errors.py`
- Create: `src/trading_assistant/app/security.py`
- Modify: `src/trading_assistant/app/main.py`
- Modify: `src/trading_assistant/db/models.py`
- Create: `migrations/versions/20260724_0006_auth_sessions.py`
- Modify: `src/trading_assistant/config.py`
- Modify: `config.yaml`
- Modify: `.env.example`
- Test: `tests/test_auth.py`
- Test: `tests/test_security_headers.py`
- Modify: `tests/test_api.py`
- Modify: `tests/test_security.py`

**Interfaces:**
- Produces: `SessionPrincipal(actor: str, session_id: int, authenticated_at: datetime)`
- Produces: `SessionAuth.login`, `authenticate`, `require_csrf`, `reauthenticate`, `logout`
- Produces: dependencies `current_principal`, `csrf_protected`, `recent_principal`
- Produces: stable `ApiError(code, status_code, message, request_id)`

- [ ] **Step 1: Write failing fail-closed, cookie, CSRF, and authorization tests**

```python
def test_missing_operator_secret_fails_startup(make_service):
    with pytest.raises(RuntimeError, match="APP_API_TOKEN"):
        create_app(service=make_service(), agent=_StubAgent(), api_token="")


def test_all_non_liveness_routes_require_session(client):
    assert client.get("/health/live").status_code == 200
    for path in ["/", "/pending", "/positions", "/log", "/plans", "/backtests"]:
        assert client.get(path).status_code == 401


def test_login_sets_http_only_same_site_cookie(client):
    response = client.post("/auth/login", json={"secret": TOKEN})
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Max-Age=" in cookie


def test_tls_cookie_uses_host_prefix_and_secure_flag(tls_client):
    response = tls_client.post("/auth/login", json={"secret": TOKEN})
    cookie = response.headers["set-cookie"]
    assert "__Host-trading_session=" in cookie
    assert "Secure" in cookie


def test_mutation_requires_csrf(authenticated_client):
    client, csrf = authenticated_client
    assert client.post("/killswitch/reset").status_code == 403
    assert client.post(
        "/killswitch/reset",
        headers={"X-CSRF-Token": csrf},
        json={"scope": "loss:equity", "reason": "drill complete"},
    ).status_code == 200


def test_expired_or_revoked_session_is_rejected(session_auth, fake_now):
    issued = session_auth.login(TOKEN, TOKEN)
    fake_now.advance(hours=9)
    with pytest.raises(SessionExpired):
        session_auth.authenticate(issued.token)
    issued = session_auth.login(TOKEN, TOKEN)
    session_auth.logout(issued.token)
    with pytest.raises(InvalidSession):
        session_auth.authenticate(issued.token)
```

- [ ] **Step 2: Run auth tests and confirm current fail-open behavior**

Run:

```bash
uv run pytest tests/test_auth.py tests/test_security_headers.py -v
```

Expected: failures showing blank-token startup succeeds and GET routes remain open.

- [ ] **Step 3: Add server-side session persistence**

Add `AuthSession`:

```python
class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(128), default="operator")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    authenticated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
```

Add strict YAML `SecurityConfig` with `session_hours: int = 8`,
`reauthentication_minutes: int = 5`, and `cookie_secure: bool = false`. Reject
`cookie_secure=false` whenever the configured bind is non-loopback. Add
`20260724_0006_auth_sessions.py` with `down_revision = "20260724_0005"`; the
migration creates `auth_sessions` and its unique/index constraints.

- [ ] **Step 4: Implement opaque sessions and CSRF**

```python
def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SessionAuth:
    def cookie_name(self) -> str:
        return "__Host-trading_session" if self.cookie_secure else "trading_session"

    def login(self, supplied_secret: str, expected_secret: str,
              actor: str = "operator:local") -> IssuedSession:
        if not expected_secret:
            raise RuntimeError("APP_API_TOKEN is required")
        if not hmac.compare_digest(supplied_secret, expected_secret):
            raise InvalidCredentials
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = self.now()
        with self.session_factory() as session:
            row = AuthSession(
                token_hash=_hash(token),
                csrf_hash=_hash(csrf),
                actor=actor,
                created_at=now,
                authenticated_at=now,
                expires_at=now + self.ttl,
            )
            session.add(row)
            session.commit()
        return IssuedSession(token=token, csrf=csrf, expires_at=row.expires_at)
```

`authenticate` hashes the cookie, rejects revoked/expired rows, and returns
`SessionPrincipal`. `require_csrf` compares the header hash in constant time.
`reauthenticate` verifies the operator secret and updates `authenticated_at`.
Inject a `now: Callable[[], datetime]` dependency (defaulting to UTC now) so
expiry and reauthentication windows are deterministic in tests.
The response sets `Secure` and the `__Host-` name only when TLS is configured;
loopback HTTP uses the unprefixed cookie because browsers reject an insecure
`__Host-` cookie. Both variants are `HttpOnly`, `SameSite=Strict`, and `Path=/`.

- [ ] **Step 5: Apply authentication dependencies to routes**

- `/health/live`, `/login`, and static login assets remain minimal and anonymous.
- `/auth/login` is anonymous and rate-limited by source address.
- `/auth/session`, `/auth/reauth`, and `/auth/logout` use session rules.
- Every other API and HTML route requires `current_principal`; static assets
  contain no data and may be fetched anonymously.
- Every state-changing route except `/auth/login` requires `csrf_protected`.
- Approval, breaker reset, plan approval, panic, and connector configuration use
  `recent_principal`.
- Rate limits key authenticated requests by session/principal, not only IP.
  Existing chat, analysis, approval, and backtest budgets remain, and login has a
  separate tighter budget. A limit response is `429` with a stable code and
  request ID.
- Route handlers pass `principal.actor`, operator reason, and request ID to
  application services.

Remove `_auth_dependency`, the `X-API-Key` request flow, and blank-token bypass.

Map domain failures through one response shape:

```python
@dataclass(frozen=True)
class ApiError(Exception):
    code: str
    status_code: int
    message: str


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    request_id = request.state.request_id
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "request_id": request_id,
            }
        },
    )
```

Use `401` for invalid sessions; `403` for CSRF, recent-auth, and policy denial;
`409` for consumed approvals, stale versions, leases, unknown acceptance, and
breaker conflicts; `422` for typed commands; `429` for budgets; and `503` for
required dependency health. Never return raw provider exception text.

- [ ] **Step 6: Add security response middleware**

```python
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}


@app.middleware("http")
async def secure_response(request, call_next):
    response = await call_next(request)
    for key, value in SECURITY_HEADERS.items():
        response.headers[key] = value
    if request.url.path != "/health/live":
        response.headers["Cache-Control"] = "no-store"
    return response
```

- [ ] **Step 7: Update fixtures and run API/security tests**

Create an `authenticated_client` fixture that logs in, requests `/auth/session`,
and returns the client plus CSRF token. Update API tests to use it; do not bypass
auth in `create_app`.

Run:

```bash
uv run pytest tests/test_auth.py tests/test_security_headers.py \
  tests/test_security.py tests/test_api.py tests/test_plans_api.py \
  tests/test_backtests_api.py -v
```

Expected: all pass; anonymous financial reads return 401.

- [ ] **Step 8: Commit fail-closed sessions**

```bash
git add src/trading_assistant/app src/trading_assistant/db/models.py \
  migrations/versions/20260724_0006_auth_sessions.py \
  src/trading_assistant/config.py config.yaml .env.example tests/test_auth.py \
  tests/test_security_headers.py tests/test_security.py tests/test_api.py \
  tests/test_plans_api.py tests/test_backtests_api.py
git commit -m "feat(security): require operator sessions and CSRF"
```

### Task 8: Make the temporary UI CSP-safe and action-truthful

**Files:**
- Create: `src/trading_assistant/app/static/css/console.css`
- Create: `src/trading_assistant/app/static/js/auth.js`
- Create: `src/trading_assistant/app/static/js/login.js`
- Create: `src/trading_assistant/app/static/js/index.js`
- Create: `src/trading_assistant/app/static/js/plans.js`
- Create: `src/trading_assistant/app/static/js/backtests.js`
- Create: `src/trading_assistant/app/static/login.html`
- Modify: `src/trading_assistant/app/static/index.html`
- Modify: `src/trading_assistant/app/static/plans.html`
- Modify: `src/trading_assistant/app/static/backtests.html`
- Modify: `src/trading_assistant/app/main.py`
- Test: `tests/test_security.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `/auth/login`, `/auth/session`, `/auth/reauth`, `/auth/logout`
- Produces: `api(path, options)` with in-memory CSRF only
- Produces: truthful panic, approval, and scoped-reset rendering

- [ ] **Step 1: Write failing static security assertions**

```python
from html.parser import HTMLParser


class _CspParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.errors = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and not attributes.get("src"):
            self.errors.append("inline script")
        if tag == "style":
            self.errors.append("inline style block")
        for name, _value in attrs:
            if name.lower().startswith("on"):
                self.errors.append(f"inline handler {name}")
            if name.lower() == "style":
                self.errors.append("inline style attribute")


@pytest.mark.parametrize("page", ["index.html", "plans.html", "backtests.html", "login.html"])
def test_pages_have_no_inline_script_or_handler(page):
    text = (_STATIC / page).read_text()
    parser = _CspParser()
    parser.feed(text)
    assert parser.errors == []
    assert "localStorage" not in text


def test_console_javascript_never_claims_everything_halted():
    text = (_STATIC / "js" / "index.js").read_text()
    assert "everything halted" not in text.lower()
    assert "unconfirmed_order_ids" in text
```

- [ ] **Step 2: Run static tests and confirm inline-script failures**

Run:

```bash
uv run pytest tests/test_security.py -v
```

Expected: failures for inline scripts, handlers, and `localStorage`.

- [ ] **Step 3: Extract shared CSS and authenticated fetch**

`auth.js` keeps CSRF only in a module variable:

```javascript
let csrfToken = null;

export async function loadSession() {
  const response = await fetch("/auth/session", { credentials: "same-origin" });
  if (response.status === 401) {
    window.location.assign("/login");
    throw new Error("authentication required");
  }
  const payload = await response.json();
  csrfToken = payload.csrf_token;
  return payload;
}

export async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if ((options.method || "GET").toUpperCase() !== "GET") {
    headers.set("X-CSRF-Token", csrfToken || "");
  }
  const response = await fetch(path, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.detail?.message || body.detail || `HTTP ${response.status}`);
    error.status = response.status;
    error.body = body;
    throw error;
  }
  return body;
}
```

`login.html`, `index.html`, `plans.html`, and `backtests.html` load
`/static/js/login.js`, `/static/js/index.js`, `/static/js/plans.js`, and
`/static/js/backtests.js`, respectively; each authenticated module imports
`api`/`loadSession` from `/static/js/auth.js`. Move styles to
`/static/css/console.css`. Replace inline handlers with `addEventListener`.

- [ ] **Step 4: Add login, reauthentication, and accurate action receipts**

The login page posts the secret, clears the input immediately, and redirects.
Approval opens a confirmation dialog showing broker, paper mode, exact order,
expiry, and resulting exposure. A 403 `reauth_required` response invokes
`/auth/reauth` and retries once.

Panic renders:

- confirmed canceled IDs;
- unconfirmed local IDs;
- remaining remote IDs;
- a persistent critical banner when `safe` is false.

Breaker reset requires scope and reason; remove the global one-click reset from
the header.

- [ ] **Step 5: Mount static assets and keep HTML routes authenticated**

Use:

```python
app.mount("/static", StaticFiles(directory=_STATIC), name="static")
```

`/login` is anonymous. `/`, `/plans/ui`, and `/backtests/ui` require a valid
session. Static CSS/JS files contain no account data and may remain directly
fetchable.

- [ ] **Step 6: Run UI, CSP, API, and accessibility smoke assertions**

Run:

```bash
uv run pytest tests/test_security.py tests/test_api.py tests/test_plans_api.py \
  tests/test_backtests_api.py -v
```

Expected: all pass, CSP contains no `unsafe-inline`, and browser storage contains
no credential.

- [ ] **Step 7: Commit the security-safe temporary UI**

```bash
git add src/trading_assistant/app/static src/trading_assistant/app/main.py \
  tests/test_security.py tests/test_api.py tests/test_plans_api.py \
  tests/test_backtests_api.py
git commit -m "feat(ui): secure operator sessions and truthful actions"
```

### Task 9: Unify bootstrap, schema gates, logging, and heartbeats

**Files:**
- Create: `src/trading_assistant/bootstrap.py`
- Create: `src/trading_assistant/operations/__init__.py`
- Create: `src/trading_assistant/operations/audit.py`
- Create: `src/trading_assistant/operations/health.py`
- Create: `src/trading_assistant/operations/service.py`
- Modify: `src/trading_assistant/app/main.py`
- Modify: `src/trading_assistant/daemon/main.py`
- Modify: `src/trading_assistant/mcp_server/server.py`
- Modify: `src/trading_assistant/preflight.py`
- Modify: `src/trading_assistant/ops/paper_drill.py`
- Modify: `src/trading_assistant/ops/watchdog.py`
- Modify: `src/trading_assistant/service.py`
- Modify: `src/trading_assistant/logging.py`
- Modify: `src/trading_assistant/daemon/backoff.py`
- Modify: `src/trading_assistant/llm/factory.py`
- Modify: `src/trading_assistant/config.py`
- Modify: `config.yaml`
- Modify: `src/trading_assistant/db/models.py`
- Create: `migrations/versions/20260724_0007_runtime_health.py`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Test: `tests/test_bootstrap.py`
- Test: `tests/test_launch.py`
- Test: `tests/test_watchdog.py`
- Test: `tests/test_monitor.py`
- Test: `tests/test_external_accounts.py`

**Interfaces:**
- Produces: `ApplicationContainer`
- Produces: `build_container(config: AppConfig, secrets: Secrets) -> ApplicationContainer`
- Produces: `MutationContext` and `AuditRecorder.record(...)`
- Produces: `OperationsService.panic`, `reset_breaker`, and `health`
- Produces: `LivenessReport` and authenticated `OperationalHealthReport`

- [ ] **Step 1: Write failing composition and heartbeat tests**

```python
def test_every_runtime_uses_shared_container(monkeypatch):
    calls = []
    monkeypatch.setattr("trading_assistant.bootstrap.build_container",
                        lambda *a, **k: calls.append((a, k)) or fake_container())
    app_main.build_default_stack()
    daemon_main.build_monitor()
    assert len(calls) == 2


def test_bootstrap_rejects_outdated_schema(tmp_path, app_config):
    secrets = Secrets(database_url=f"sqlite:///{tmp_path}/old.db",
                      app_api_token="operator-secret")
    with pytest.raises(SchemaOutOfDate):
        build_container(app_config, secrets)


def test_heartbeat_upserts_one_row(make_service):
    svc = make_service()
    for _ in range(5):
        svc.write_heartbeat("daemon")
    with svc.session_factory() as session:
        assert session.query(Heartbeat).filter_by(source="daemon").count() == 1


def test_each_mutation_records_identity_result_and_latency(authenticated_client):
    client, csrf = authenticated_client
    response = client.post(
        "/reject/1",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "reject-once"},
        json={"reason": "operator review"},
    )
    assert response.status_code in {200, 404}
    event = latest_audit_event("http.reject")
    assert event.actor == "operator:local"
    assert event.request_id
    assert event.idempotency_key == "reject-once"
    assert event.result_code
    assert event.latency_ms >= 0
```

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```bash
uv run pytest tests/test_bootstrap.py tests/test_launch.py -v
```

Expected: missing `bootstrap.py` and heartbeat row count greater than one.

- [ ] **Step 3: Implement one application container**

```python
@dataclass(frozen=True)
class ApplicationContainer:
    config: AppConfig
    secrets: Secrets
    engine: Engine
    session_factory: sessionmaker[Session]
    broker: BrokerClient
    service: TradingService
    snapshot_service: PortfolioSnapshotService
    order_application: OrderApplicationService
    order_submission: OrderSubmissionService
    reconciliation: ReconciliationService
    breakers: BreakerService
    rule_worker: RuleWorker
    session_auth: SessionAuth
    audit: AuditRecorder
    operations: OperationsService


def build_container(config: AppConfig | None = None,
                    secrets: Secrets | None = None) -> ApplicationContainer:
    config = config or load_config()
    secrets = secrets or Secrets()
    if not secrets.app_api_token:
        raise RuntimeError("APP_API_TOKEN is required")
    if config.trading.mode is not TradingMode.PAPER:
        raise RuntimeError("live trading is locked out by the safety foundation")
    if config.features.auto_execute_preapproved_rules:
        raise RuntimeError("auto-execution must remain disabled")
    if config.execution.prefer_bracket_orders:
        raise RuntimeError("automatic bracket execution must remain disabled")
    if config.llm.fallback_provider is not None:
        raise RuntimeError("automatic cross-provider LLM fallback is disabled")
    register_all_secrets(secrets)
    configure_logging()
    engine = create_db_engine(secrets.database_url)
    require_current_schema(engine)
    session_factory = make_session_factory(engine)
    broker = build_broker(config, secrets)
    equity_clock = build_clock(config, secrets)
    clocks = {
        AssetClass.EQUITY: equity_clock,
        AssetClass.CRYPTO: CryptoClock(),
    }
    risks = {
        AssetClass.EQUITY: RiskEngine(config.risk),
        AssetClass.CRYPTO: RiskEngine(config.crypto_risk or config.risk),
    }

    def clock_for_symbol(symbol: str) -> MarketClock:
        return clocks[AssetClass.for_symbol(symbol)]

    def risk_for_symbol(symbol: str) -> RiskEngine:
        return risks[AssetClass.for_symbol(symbol)]

    breakers = BreakerService(session_factory)
    audit = AuditRecorder(session_factory)
    order_repository = OrderRepository(session_factory)
    snapshot_service = PortfolioSnapshotService(
        broker=broker,
        session_factory=session_factory,
        config=config,
        clock_for_symbol=clock_for_symbol,
        breakers=breakers,
    )
    order_application = OrderApplicationService(session_factory)
    order_submission = OrderSubmissionService(
        repository=order_repository,
        session_factory=session_factory,
        broker=broker,
        snapshot_service=snapshot_service,
        risk_for_symbol=risk_for_symbol,
    )
    rule_repository = RuleRepository(session_factory, owner=process_identity())
    rule_application = RuleApplicationService(session_factory, rule_repository)
    reconciliation = ReconciliationService(
        repository=order_repository,
        session_factory=session_factory,
        broker=broker,
        breakers=breakers,
        rules=rule_repository,
    )
    rule_worker = RuleWorker(
        repository=rule_repository,
        application=rule_application,
        broker=broker,
        max_quote_age_seconds=config.daemon.max_quote_age_seconds,
    )
    service = TradingService(
        broker=broker,
        session_factory=session_factory,
        config=config,
        clock=equity_clock,
        order_application=order_application,
        order_submission=order_submission,
        reconciliation=reconciliation,
        snapshot_service=snapshot_service,
        breakers=breakers,
    )
    session_auth = SessionAuth(
        session_factory=session_factory,
        ttl=timedelta(hours=config.security.session_hours),
        reauthentication_window=timedelta(
            minutes=config.security.reauthentication_minutes
        ),
        cookie_secure=config.security.cookie_secure,
    )
    operations = OperationsService(
        reconciliation=reconciliation,
        breakers=breakers,
        audit=audit,
        session_factory=session_factory,
        broker=broker,
    )
    return ApplicationContainer(
        config=config,
        secrets=secrets,
        engine=engine,
        session_factory=session_factory,
        broker=broker,
        service=service,
        snapshot_service=snapshot_service,
        order_application=order_application,
        order_submission=order_submission,
        reconciliation=reconciliation,
        breakers=breakers,
        rule_worker=rule_worker,
        session_auth=session_auth,
        audit=audit,
        operations=operations,
    )
```

Define the shown constructor signatures in Tasks 2–7 and do not call
`create_all`. Tests may inject a `MockBroker` through a dedicated test-only
factory fixture; production bootstrap always uses `build_broker`.

- [ ] **Step 4: Route all entrypoints through the container**

- `app.main` receives or builds `ApplicationContainer`.
- `daemon.main` builds one container and constructs `Monitor`.
- MCP, preflight, paper drill, and watchdog use the same schema/config/logging
  behavior.
- Planning remains optional only when its feature is disabled. A configured
  required subsystem logs a structured startup error and fails startup.

Use the following shared mutation context in API, MCP, daemon, and command
handlers:

```python
@dataclass(frozen=True)
class MutationContext:
    actor: str
    request_id: str
    reason: str = ""
    idempotency_key: str = ""
    started_at: float = field(default_factory=time.perf_counter)


class AuditRecorder:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def record(
        self,
        context: MutationContext,
        action: str,
        target_type: str,
        target_id: str,
        result_code: str,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        elapsed_ms = round((time.perf_counter() - context.started_at) * 1000)
        with self.session_factory() as session:
            session.add(AuditEvent(
                actor=context.actor,
                action=action,
                target_type=target_type,
                target_id=target_id,
                request_id=context.request_id,
                idempotency_key=context.idempotency_key,
                reason=context.reason,
                result_code=result_code,
                latency_ms=elapsed_ms,
                detail_json=json.dumps(detail or {}, sort_keys=True, default=str),
            ))
            session.commit()
```

FastAPI middleware creates a request ID when absent and returns it as
`X-Request-ID`. Every service mutation consumes `MutationContext`; daemon and MCP
use process/service actors. Add a parameterized test covering approval,
rejection, cancellation, breaker reset, panic, rule creation/cancellation, and
backtest launch.

`OperationsService` is the only API-facing operations facade:

```python
class OperationsService:
    def __init__(self, reconciliation, breakers, audit, session_factory, broker):
        self.reconciliation = reconciliation
        self.breakers = breakers
        self.audit = audit
        self.session_factory = session_factory
        self.broker = broker

    def panic(self, context: MutationContext) -> PanicReport:
        report = self.reconciliation.panic(context.actor, context.reason)
        self.audit.record(
            context, "operations.panic", "account", "alpaca-paper",
            "safe" if report.safe else "unconfirmed",
            {"unconfirmed_order_ids": report.unconfirmed_order_ids},
        )
        return report

    def reset_breaker(
        self,
        scope: BreakerScope,
        context: MutationContext,
        prior_health: Mapping[str, object],
    ) -> BreakerState:
        state = self.breakers.reset(
            scope=scope,
            actor=context.actor,
            reason=context.reason,
            prior_health=prior_health,
        )
        self.audit.record(
            context, "breaker.reset", "breaker", scope.key, "reset",
            {"prior_health": prior_health},
        )
        return state

    def health(self) -> OperationalHealthReport:
        return build_operational_health(
            self.session_factory, self.broker, self.breakers
        )
```

- [ ] **Step 5: Upsert heartbeats and add operational health**

Create `20260724_0007_runtime_health.py` with
`down_revision = "20260724_0006"`. Make `Heartbeat.source` unique after
deduplicating legacy rows by retaining the newest timestamp. Write:

```python
stmt = sqlite_insert(Heartbeat).values(source=source, at=utcnow())
stmt = stmt.on_conflict_do_update(
    index_elements=[Heartbeat.source],
    set_={"at": stmt.excluded.at},
)
session.execute(stmt)
session.commit()
```

Anonymous liveness contains only `alive` and `database_reachable`.
Authenticated operational health includes heartbeat age, breaker scopes,
reconciliation age, mode, broker, and last confirmed broker contact.

- [ ] **Step 6: Install redaction and private logging in every process**

`configure_logging()` installs the redaction filter once, creates runtime log
directories with `0700`, and opens files with `0600`. Update launch scripts to
set `umask 077` before process execution. Add rotation settings and prove
registered secret values do not appear in API, daemon, MCP, or drill logs.

Keep bounded retries limited to idempotent provider reads:

```python
@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    base_seconds: float = 1.0
    cap_seconds: float = 30.0
    jitter_fraction: float = 0.2


RETRIABLE_READ_ERRORS = (TimeoutError, ConnectionError)


async def retry_async_read(operation, policy: RetryPolicy, *, sleep=asyncio.sleep):
    for attempt in range(1, policy.attempts + 1):
        try:
            return await operation()
        except RETRIABLE_READ_ERRORS:
            if attempt == policy.attempts:
                raise
            delay = next_delay(
                attempt,
                base=policy.base_seconds,
                cap=policy.cap_seconds,
                jitter_frac=policy.jitter_fraction,
            )
            await sleep(delay)
```

Use this for daemon quote/news/reconciliation reads only. Submission, approval,
cancel, and breaker-reset writes never use this helper. Extend
`tests/test_monitor.py` with a deterministic injected sleeper proving two
failures produce bounded delays and one eventual read, while the order
submission test still proves one broker write attempt.

- [ ] **Step 7: Retire unofficial Robinhood runtime code**

Remove the `external` optional dependency and `robin_stocks`/`pyotp` lock entries.
Delete `external_accounts/robinhood.py`. Keep `ExternalAccountSource` and mock
interfaces only if current portfolio aggregation tests need them; otherwise
remove the disabled configuration fields and update `.env.example`.

No official Robinhood MCP code is added in this subproject.

Also set `llm.fallback_provider: null` in `config.yaml` and remove automatic
cross-provider fallback from `llm/factory.py`. A provider failure returns a
stable error and does not send the same financial context to a second vendor.
Provider changes require an explicit configuration edit and restart. Update
`tests/test_llm_backends.py` to assert one provider call on failure.

- [ ] **Step 8: Run composition, operations, and full deterministic tests**

Run:

```bash
uv lock
uv sync --all-extras
uv run pytest tests/test_bootstrap.py tests/test_launch.py tests/test_watchdog.py \
  tests/test_ops.py tests/test_monitor.py tests/test_external_accounts.py \
  tests/test_llm_backends.py -v
uv run pytest
```

Expected: focused tests pass; full result is at least the previous
`348 passed, 1 skipped`, adjusted only for deliberately removed
`robin_stocks`-specific tests and new tests.

- [ ] **Step 9: Commit unified runtime composition**

```bash
git add src/trading_assistant/bootstrap.py src/trading_assistant/operations \
  src/trading_assistant/app/main.py src/trading_assistant/daemon/main.py \
  src/trading_assistant/mcp_server/server.py src/trading_assistant/preflight.py \
  src/trading_assistant/ops src/trading_assistant/service.py \
  src/trading_assistant/logging.py src/trading_assistant/daemon/backoff.py \
  src/trading_assistant/db/models.py \
  src/trading_assistant/llm/factory.py src/trading_assistant/config.py config.yaml \
  migrations/versions/20260724_0007_runtime_health.py \
  src/trading_assistant/external_accounts \
  scripts pyproject.toml uv.lock README.md tests/test_bootstrap.py \
  tests/test_launch.py tests/test_watchdog.py tests/test_ops.py tests/test_monitor.py \
  tests/test_external_accounts.py tests/test_llm_backends.py
git commit -m "refactor(runtime): unify safe application bootstrap"
```

### Task 10: Rehearse migration, crash, concurrency, and Alpaca paper gates

**Files:**
- Create: `src/trading_assistant/ops/safety_drill.py`
- Create: `tests/test_safety_drill.py`
- Modify: `src/trading_assistant/preflight.py`
- Modify: `docs/RUNBOOK.md`
- Modify: `README.md`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: `python -m trading_assistant.ops.safety_drill --database-copy PATH`
- Produces: machine-readable `SafetyDrillReport`

- [ ] **Step 1: Write the failing drill acceptance test**

```python
def test_safety_drill_requires_every_gate(tmp_path, app_config):
    report = run_safety_drill(
        database_copy=tmp_path / "operator-copy.db",
        config=app_config,
        broker=MockBroker(),
    )
    assert report.schema_current
    assert report.auth_fail_closed
    assert report.crash_recovered_without_duplicate
    assert report.oco_single_terminal
    assert report.breakers_persisted
    assert report.reconciliation_clean
    assert report.safe
```

- [ ] **Step 2: Run the drill test and confirm the missing module**

Run:

```bash
uv run pytest tests/test_safety_drill.py -v
```

Expected: collection fails because `ops.safety_drill` does not exist.

- [ ] **Step 3: Implement the deterministic safety drill**

```python
@dataclass(frozen=True)
class SafetyDrillReport:
    schema_current: bool
    auth_fail_closed: bool
    crash_recovered_without_duplicate: bool
    oco_single_terminal: bool
    breakers_persisted: bool
    reconciliation_clean: bool
    safe: bool
    details: tuple[str, ...]
```

The drill operates only on an explicit database copy and an injected mock or
Alpaca paper broker. It refuses a live configuration, refuses the primary
database path, refuses to overwrite an existing destination, creates the copy
with SQLite's online backup API, runs each gate, and emits JSON. The credentialed
mode snapshots existing paper orders/positions, submits one tagged, small,
non-marketable limit order within the price-sanity bound, resolves its client ID,
and cancels it. If it fills, the drill flattens only the fill it created with a
tagged compensating paper order. It then proves there are no drill-tagged open
orders and no net position delta attributable to the drill; it never cancels or
alters pre-existing paper orders or positions.

- [ ] **Step 4: Add CI gates**

CI runs:

```bash
uv run pytest
uv run pytest --cov=trading_assistant.risk \
  --cov=trading_assistant.orders \
  --cov=trading_assistant.rules \
  --cov=trading_assistant.app.auth \
  --cov-branch --cov-fail-under=90
ci_safety_dir="$(mktemp -d)"
export DATABASE_URL="sqlite:///$ci_safety_dir/source.sqlite3"
export APP_API_TOKEN="ci-safety-only"
uv run python -m trading_assistant.db.migrate upgrade
uv run python -m trading_assistant.ops.safety_drill \
  --database-copy "$ci_safety_dir/drill.sqlite3" --mock
```

Keep the existing secret scan. Add a check that `config.yaml` has paper mode,
auto execution false, and bracket preference false.

- [ ] **Step 5: Run the complete local verification matrix**

Run:

```bash
git diff --check
uv run pytest
uv run pytest --cov=trading_assistant.risk \
  --cov=trading_assistant.orders \
  --cov=trading_assistant.rules \
  --cov=trading_assistant.app.auth \
  --cov-branch --cov-fail-under=90
uv run python -m trading_assistant.db.migrate status
uv run python -m trading_assistant.preflight
safety_tmp_dir="$(mktemp -d)"
uv run python -m trading_assistant.ops.safety_drill \
  --database-copy "$safety_tmp_dir/trading-assistant-safety-copy.sqlite3" --mock
```

Expected:

- no diff-check errors;
- every deterministic test passes;
- safety-module branch coverage is at least 90%;
- schema reports current;
- preflight reports paper mode and no enabled dangerous feature;
- mock safety drill reports `"safe": true`.

With Alpaca paper credentials configured, additionally run:

```bash
uv run pytest tests/test_alpaca_paper_integration.py -v
alpaca_tmp_dir="$(mktemp -d)"
uv run python -m trading_assistant.ops.safety_drill \
  --database-copy "$alpaca_tmp_dir/trading-assistant-alpaca-copy.sqlite3" \
  --alpaca-paper
```

Expected: credentialed test passes, every drill-tagged paper order is terminal,
and broker truth ends with no open order or net position delta created by the
drill. Pre-existing paper-account state is byte-for-byte unchanged in the
recorded before/after order-ID and position-quantity manifest; changing quotes
and market values are excluded from that equality check.

- [ ] **Step 6: Update operator documentation**

Document:

- backup and migration commands;
- login, session expiry, CSRF, and reauthentication behavior;
- interpretation and reset of every breaker scope;
- unknown-acceptance recovery;
- truthful panic states;
- migration rollback by restoring the verified copy;
- commands for deterministic and Alpaca paper safety drills;
- explicit statement that passing the gate does not prove profitability or
  authorize live trading.

- [ ] **Step 7: Commit the safety gate**

```bash
git add src/trading_assistant/ops/safety_drill.py \
  src/trading_assistant/preflight.py tests/test_safety_drill.py \
  docs/RUNBOOK.md README.md .github/workflows/ci.yml
git commit -m "test(safety): add crash and migration release gate"
```

## Final Plan Verification

After Task 10:

1. Confirm `git status --short` is empty.
2. Confirm no code path calls `Base.metadata.create_all()` outside test helpers.
3. Confirm no HTML or JavaScript contains `localStorage`, inline handlers, or
   `X-API-Key`.
4. Confirm all broker submissions originate in `OrderSubmissionService`.
5. Confirm all unknown submissions reserve exposure and reconcile by client ID.
6. Confirm all rule execution acquires a group lease.
7. Confirm every non-liveness route requires an operator session.
8. Confirm paper mode and both autonomous switches remain off.
9. Record deterministic test counts, coverage, migration rehearsal, and
   credentialed Alpaca paper results in the completion report.

## Post-review amendment: fill-activated plan protection

The original single-group plan decomposition was rejected during broad review:
one entry trigger could terminalize the group and cancel every remaining entry
and exit. The implemented amendment gives each entry tranche an independent
group and stores all exits in a pending protective group. Trusted reconciled
entry fills activate and resize exits; intermediate targets preserve the
remaining stop/trailing/time rules, while a terminal exit closes the remaining
plan groups. Migration `20260724_0009` persists these semantics and refuses a
lossy downgrade when specialized state exists.

The hostile follow-up review added four more invariants. Every plan proposal is
stamped with the current residual generation, and any later trusted fill makes
older intents stale regardless of event timestamps. Generic rule cancellation
cannot mutate plan-owned protection. Passive TTL expiration re-arms protective
rules without waiting for an approval request. Finally, a terminal plan that
receives a late trusted entry fill moves to `protection_required`, re-arms
downside rules, and durably trips broker-drift plus operator-global breakers.
Execution additionally proves that aggregate plan residuals do not exceed the
reconciled broker position.

The second hostile pass found that broker cancellation failure was not included
in startup/daemon failure state and that `last_error_code` could erase the only
retry marker. Migration `20260726_0010` adds an independent durable cancellation
state. Requested and indeterminate plan cancellations are retried on restart;
ordinary reconciliation reports them as failures, trips broker drift, and keeps
startup plus daemon rule evaluation blocked until terminal broker and exact fill
truth settle the intent.
