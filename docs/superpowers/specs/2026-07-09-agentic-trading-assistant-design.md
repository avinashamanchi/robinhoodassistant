# Agentic Trading Assistant — Design Spec

**Date:** 2026-07-09
**Status:** Approved (with amendments 1–8)
**Broker:** Alpaca (paper first; live behind double-lock)

---

## 1. Overview

An LLM-driven trading assistant. Claude (Anthropic API, `claude-sonnet-4-6`, native tool
use) interprets natural-language commands and decides which tools to call. An MCP server
exposes broker tools. A FastAPI host runs the agentic loop and enforces a **human
confirmation gate** before any live order. A monitoring daemon watches prices and evaluates
standing conditional orders.

Everything runs against Alpaca **paper trading** first. Live trading is OFF by default and
requires a double-lock (config flag AND an env confirmation string).

### The one invariant

> The LLM **proposes** (`PROPOSED` rows in the DB). Execution happens **only** after human
> approval (or an explicitly pre-approved rule). **Every** execution re-runs the risk engine
> at execution time. No code path bypasses `risk/`. The risk engine is the final authority.

---

## 2. Tech Stack

- Python **3.11+**, managed with **uv** (`pyproject.toml` + `uv.lock`).
- Broker: **Alpaca** (`alpaca-py`), paper env. Swappable via `BrokerClient` ABC.
- MCP: official `mcp` Python SDK (server).
- LLM: Anthropic API, `claude-sonnet-4-6`, native tool use.
- Backend: **FastAPI** (host app + REST).
- Frontend: single-page HTML/JS, no build step.
- DB: **SQLite** via SQLAlchemy, **WAL mode** (app + daemon both write).
- Config/secrets: **pydantic-settings** from `.env`; risk limits in `config.yaml`.
- Testing: **pytest** with a `MockBroker` and `FakeClock`.
- Notifications: Telegram (optional module, config flag, default OFF).

---

## 3. Architecture (six isolated units)

| Unit | Responsibility | Depends on |
|------|----------------|------------|
| `broker/` | `BrokerClient` ABC + `AlpacaBroker` / `MockBroker`; `PortfolioSnapshot`, idempotency keys | Alpaca SDK (Alpaca impl only) |
| `risk/` | Deterministic, **pure** checks on every order; kill switch; realized-P&L; market clock consumer | config, snapshot (no I/O) |
| `db/` | SQLAlchemy models, order state machine, kill-switch state, reconciliation | SQLite (WAL) |
| `mcp_server/` | MCP tools; **proposes only, never executes** | `broker/`, `db/` |
| `app/` | FastAPI host + agentic loop + atomic approval endpoints + frontend | all |
| `daemon/` | Async monitor for conditional rules + websocket + notifications | `broker/`, `risk/`, `db/` |

---

## 4. Amendments (interface-shaping decisions)

### A1 — Risk engine is pure; caller assembles the snapshot
`risk/` cannot depend on live I/O. The interface is:

```python
RiskEngine.check(order: OrderRequest, snapshot: PortfolioSnapshot) -> RiskResult
```

`PortfolioSnapshot` (in `broker/models.py`) carries: `positions`, latest `quotes` for the
relevant tickers, `buying_power`, and `realized_pnl_today`. The **caller** (host app or
daemon) assembles the snapshot from the broker + DB; the risk engine performs no I/O and is
fully deterministic and unit-testable.

### A2 — Realized P&L (Phase 1)
`risk/pnl.py` computes realized P&L from the `fills` table using **FIFO lot tracking**.
"Daily" = since the most recent market open, evaluated in **America/New_York**. All
timestamps stored in **UTC**; convert only for market-day boundary math. Kill-switch tests
exercise this real computation, not a stub.

### A3 — Kill switch persists
Tripped state lives in the **database** (dedicated `killswitch_state` table), not process
memory. A restart returns tripped. Manual reset writes an audit row to `risk_events`.

### A4 — Explicit order state machine (Phase 1)
Statuses and legal transitions live in `db/models.py`:

```
PROPOSED ──▶ APPROVED ──▶ SUBMITTED ──▶ PARTIALLY_FILLED ──▶ FILLED
   │             │            │                │
   ▼             ▼            ▼                ▼
EXPIRED      (REJECTED)   CANCELED         CANCELED
REJECTED
CANCELED
```

Legal transitions (enforced by `OrderStateMachine.transition`):
- `PROPOSED → {APPROVED, REJECTED, EXPIRED, CANCELED}`
- `APPROVED → {SUBMITTED, REJECTED, CANCELED}` (execution-time risk re-check may REJECT)
- `SUBMITTED → {PARTIALLY_FILLED, FILLED, CANCELED, REJECTED}`
- `PARTIALLY_FILLED → {PARTIALLY_FILLED, FILLED, CANCELED}`
- Terminal: `FILLED`, `CANCELED`, `REJECTED`, `EXPIRED`

Any illegal transition raises `IllegalStateTransition`.

### A5 — Atomic approval
`POST /approve/{order_id}` transitions `PROPOSED → APPROVED` in a **single atomic DB
operation** that succeeds exactly once. A second concurrent approval returns **409**.
Implemented via a conditional UPDATE guarded on current status (compare-and-set); the
model/session layer supports this in Phase 1 even though the endpoint lands in Phase 3.
SQLite runs in **WAL mode**.

### A6 — Proposal TTL
New config key `risk.proposal_ttl_minutes: 15`. Proposals older than the TTL transition to
`EXPIRED` and cannot be approved. Conditional-rule proposals (Phase 4) may carry their own
TTL. Execution-time risk re-check is unchanged.

### A7 — Injectable market clock
Define a `MarketClock` **protocol** in Phase 1: `is_open()`, `next_open()`, `next_close()`.
`FakeClock` (controllable) drives tests. `market_hours.py` consumes the protocol only — no
hand-rolled holiday calendar. Phase 2 adds `AlpacaClock` backed by Alpaca's clock/calendar
API.

### A8 — Config fails fast
All config models use pydantic with `extra="forbid"`. Unknown/misspelled keys in
`config.yaml` raise at startup. A silently-ignored risk limit is the worst failure mode this
project has — a test asserts a typo'd risk key fails to load.

---

## 5. Data model (`db/models.py`)

- `orders` — full lifecycle; `status`, `idempotency_key` (unique), `client_order_id`,
  broker refs, timestamps (UTC).
- `proposals` — LLM-proposed order details, `created_at`, `ttl_minutes`, link to order.
- `rules` — standing conditional rules (ticker, condition, action, state).
- `llm_decisions` — prompt, tool calls, reasoning summary, model, tokens.
- `risk_events` — every rejection/kill-switch trip/reset, with reason.
- `fills` — per-fill qty/price/time (UTC); source for FIFO P&L.
- `killswitch_state` — singleton row: `tripped` (bool), `tripped_at`, `reason`.

---

## 6. Config schema (`config.yaml`, committed, non-secret)

```yaml
trading:
  mode: paper              # paper | live
  broker: alpaca           # alpaca | mock

risk:
  ticker_allowlist: [AAPL, MSFT, GOOGL, AMZN, NVDA]
  max_notional_per_order: 500
  max_position_per_ticker: 2000
  max_portfolio_exposure: 10000
  daily_realized_loss_limit: 500
  price_sanity_pct: 5.0
  reject_when_market_closed: true
  proposal_ttl_minutes: 15

features:
  auto_execute_preapproved_rules: false
  telegram_notifications: false

llm:
  model: claude-sonnet-4-6
  max_tokens: 4096

daemon:
  poll_interval_seconds: 15
  use_websocket: true
```

`.env` (secrets, gitignored; documented in `.env.example`): `ANTHROPIC_API_KEY`,
`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER_BASE_URL`, `LIVE_TRADING_CONFIRM`
(must equal `I_UNDERSTAND_LIVE_TRADING` for live), `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, `APP_HOST`, `APP_PORT`.

**Guardrail #1:** live mode requires `config.yaml` `mode: live` **AND**
`LIVE_TRADING_CONFIRM=I_UNDERSTAND_LIVE_TRADING`. Missing either → paper.

---

## 7. Security

- No secrets in code. `.env.example` documents every variable; `.env` gitignored.
- `logging.py` installs a redaction filter — API keys / secrets never logged.
- All external text (news/web, if ever added) is untrusted; actions always pass the risk
  engine.
- Rate limiting on cost-incurring / broker endpoints (host app, Phase 3).

---

## 8. Phases

- **Phase 1** — Scaffold, config (fail-fast), DB models + state machine + killswitch state,
  `BrokerClient` ABC + `MockBroker` + `PortfolioSnapshot`, risk engine (pure) + FIFO P&L +
  persistent kill switch + market-clock protocol/FakeClock. Full pytest coverage.
- **Phase 2** — `AlpacaBroker` (paper) + `AlpacaClock`, MCP server with all tools,
  integration test that proposes (does not execute).
- **Phase 3** — Host app + agentic loop + atomic approval endpoints + minimal frontend.
- **Phase 4** — Monitoring daemon, conditional rules, websocket feed, Telegram (flag).
- **Phase 5** — Hardening: idempotency, kill-switch drills, partial fills, cancel/replace,
  startup reconciliation, README.

---

## 9. Non-negotiable guardrails

1. Live trading requires BOTH config flag AND env confirmation string.
2. LLM proposes; only human approval (or pre-approved rule) executes.
3. Risk engine is final authority on every order; no bypass path.
4. Default state of everything dangerous is OFF.
