# Evidence-First Trading Platform — Master Design

**Date:** 2026-07-24  
**Status:** Approved direction  
**Operating posture:** Alpaca paper trading, human approval required, all autonomous and live capabilities off

## 1. Purpose

Turn the existing trading assistant into a dependable, evidence-first operator
terminal. The system may research, measure, simulate, propose, and explain. It
must not imply that an LLM, technical indicator, or backtest guarantees profit.

The product succeeds when it can:

1. prove where every market fact came from and when it was known;
2. measure a strategy honestly against relevant benchmarks after costs;
3. survive retries, crashes, duplicate events, and conflicting broker state;
4. present every consequential action for informed human review; and
5. operate continuously in Alpaca paper mode without silently degrading.

This design does not promise a target account value. Profitability remains an
empirical question answered by out-of-sample evidence.

## 2. Hard constraints

- Alpaca remains the only execution broker during this program.
- Execution remains paper-only and requires human approval.
- The LLM cannot submit, cancel, replace, approve, or reset a kill switch.
- Robinhood integration uses the official MCP only, in a later isolated
  read-only gateway. The existing `robin_stocks` adapter is retired.
- Composio initially exposes only exact outbound notification or draft tools.
- The exposed Composio key is never used; a rotated key is required later.
- External text is untrusted data and cannot introduce instructions.
- All current strategy and analyst evidence is considered unvalidated.
- A backtest, shadow result, or model score can never enable live trading.

## 3. Product principles

### 3.1 Evidence before opinion

Every factual input carries source, source timestamp, receipt timestamp, fetch
timestamp, adjustment policy, freshness deadline, provider request ID, schema
version, and content hash. The UI distinguishes confirmed fact, deterministic
calculation, model interpretation, unresolved disagreement, and missing data.

### 3.2 Deterministic authority

Normal code owns prices, indicators, abnormal-return calculations, sizing,
portfolio constraints, risk checks, approval state, and broker submission. An
LLM may summarize evidence, identify uncertainty, compare cases, and abstain.

### 3.3 Fail closed

Missing authentication, stale prices, unknown broker acceptance, schema drift,
data disagreement, failed reconciliation, or an unhealthy risk dependency blocks
new risk. Read-only research may continue with explicit degraded-state labels.

### 3.4 One action, one receipt

Every approval displays the broker, mode, symbol, side, quantity/notional, order
type, prices, expiration, evidence age, resulting exposure, active limits, and
exact side effect. Approval is actor-attributed, auditable, and consumed once.

## 4. Program decomposition

The program is split into four independently planned and verified subprojects.
Each subproject receives its own implementation plan and completion review.

1. **Safety foundation**
   - authentication and browser security;
   - database migrations and typed persistence;
   - durable order submission and reconciliation;
   - OCO concurrency correctness;
   - risk and operational circuit breakers;
   - service decomposition and shared bootstrap.

2. **Point-in-time data and event intelligence**
   - immutable data snapshots and provenance;
   - Alpaca market/news streaming;
   - SEC and ALFRED ingestion;
   - durable event normalization and deduplication;
   - exposure graph and market-reaction measurement;
   - bounded Octen escalation.

3. **Research and evaluation**
   - corrected simulated broker and metrics;
   - multi-asset portfolio simulation;
   - rolling train/validation windows with purge and embargo;
   - technically protected final holdout;
   - factor, momentum, event, and deterministic benchmark models;
   - uncertainty, calibration, and multiple-testing controls.

4. **Operator UI and constrained connectors**
   - typed web client and authenticated session flow;
   - command center, approvals, portfolio/risk, research, backtests, operations;
   - evidence ledger and reproducible experiment reports;
   - official Robinhood read-only gateway;
   - exact Composio outbound tools with approval policy.

## 5. Target architecture

```text
Alpaca streams ─┐
SEC / ALFRED ───┼──> ingestion adapters ──> immutable observations
Octen on demand ┘                                  │
                                                   v
                                     normalize / deduplicate / map
                                                   │
                               ┌───────────────────┴───────────────────┐
                               v                                       v
                     deterministic features                    evidence ledger
                               │                                       │
                               └────────────> research cases <─────────┘
                                                   │
                                       proposal application service
                                                   │
                                         pure portfolio risk engine
                                                   │
                                         human approval receipt
                                                   │
                                       durable submission outbox
                                                   │
                                           Alpaca paper broker
                                                   │
                                    fills / reconciliation / projections
```

### 5.1 Runtime processes

- **API:** authenticated control plane and read models; no long-running jobs.
- **Core worker:** broker reconciliation, rule evaluation, event processing,
  shadow grading, and heartbeats.
- **Research worker:** backtests and model evaluations in cancellable jobs.
- **Provider gateways:** isolated adapters with provider-specific credentials.

The first implementation remains single-host. SQLite WAL remains the operational
database while transactions are shortened and migrations are introduced.
Append-heavy bars, events, and experiment artifacts use immutable Parquet files
with database manifests. PostgreSQL becomes necessary only for multi-host or
high-concurrency deployment.

## 6. Core component boundaries

The current `TradingService` is replaced incrementally by:

- `OrderApplicationService`: proposal, approval intent, expiry, rejection.
- `OrderSubmissionService`: durable outbox and broker submission recovery.
- `PortfolioSnapshotService`: quotes, positions, pending exposure, buying power.
- `RiskDecisionService`: pure checks plus persisted circuit-breaker state.
- `ReconciliationService`: broker orders, activities, fills, positions, drift.
- `RuleApplicationService`: typed rule creation and cancellation.
- `RuleWorker`: group-level leases and one-time condition execution.
- `OperationsService`: health, panic, recovery, and operator actions.

All processes are created through one `bootstrap.py` composition root. Routers,
workers, MCP, preflight, and drills consume the same configured services.

## 7. Order and rule durability

### 7.1 Order lifecycle

```text
PROPOSED -> APPROVAL_RECORDED -> SUBMITTING -> SUBMITTED
                                      │             │
                                      v             v
                              ACCEPTANCE_UNKNOWN  PARTIALLY_FILLED -> FILLED
                                      │
                                      └──── reconcile by client order ID ────┘
```

`REJECTED`, `EXPIRED`, `CANCELED`, and `FAILED` are legal only from explicitly
defined states. Approval records intent in a short transaction. Broker network
I/O occurs after the durable `SUBMITTING` record commits. A crash after remote
acceptance is recovered by client order ID before any retry.

### 7.2 OCO and rules

Rules use typed condition/action models and enumerated states. A database lease
is acquired for the entire plan/OCO group, not an individual child rule. One
worker may transition a group into execution; sibling exits cannot concurrently
submit. Every lease expires and is recoverable after a crash.

## 8. Risk authority

The risk engine evaluates production-equivalent portfolio snapshots in both live
paper execution and simulation. It checks:

- order notional, resulting symbol exposure, portfolio exposure, buying power;
- pending orders, reserved exits, and total open-stop risk;
- asset-class and sector concentration plus rolling correlation;
- quote age, provider health, spread, halt state, and abnormal price movement;
- market state and proposal age;
- realized and unrealized daily loss;
- account drawdown from a persisted high-water mark;
- broker/local reconciliation drift;
- active data-integrity, liquidity, volatility, or operator kill switches.

Circuit breakers are scoped, persisted, reasoned, and independently reset:

- trading-loss breaker;
- drawdown breaker;
- stale/inconsistent-data breaker;
- liquidity/volatility breaker;
- broker-drift breaker;
- global operator halt.

Reset requires authenticated confirmation, actor identity, reason, and a current
health report. Panic never claims success while cancellations remain unconfirmed.

## 9. Data and event intelligence

### 9.1 Sources

- Alpaca trades, quotes, bars, order updates, and news streams.
- SEC submissions, filing documents, and XBRL company facts.
- FRED/ALFRED observations using historical vintages for simulation.
- Corporate-action and earnings calendars.
- Octen Web Search for targeted gaps and Broad Search for explicitly escalated
  investigations only.

### 9.2 Event record

An event stores publication, first-seen, receipt, and processing timestamps;
source quality; canonical URL and provider ID; affected symbols and factors;
confirmed facts; novelty and surprise components; model interpretation;
uncertainties; and price, volume, spread, market, and sector reactions.

Text is bounded and encoded before model use. Instructions in source text have no
authority. Claims from social or low-quality sources require corroboration.

### 9.3 Exposure graph

Versioned relationships connect each symbol to sector, peers, suppliers,
customers, countries, commodities, currencies, rates, indexes, and regulators.
Each edge carries direction, weight, effective dates, and evidence. Historical
regressions may update measured sensitivities, but never rewrite historical
versions used by a past experiment.

## 10. Research and model policy

### 10.1 Strategies

Technical strategies remain deterministic benchmarks. Research adds:

- market, sector, size, value, profitability, investment, and momentum exposure;
- time-series and cross-sectional momentum;
- volatility targeting and portfolio risk budgets;
- earnings and filing event studies;
- market/sector-adjusted abnormal returns;
- cash, SPY, equal-weight, and buy-and-hold benchmarks.

“Trading demographics” is not treated as a primary edge. Retail attention or
positioning may be an explicitly sourced secondary feature with latency and
coverage warnings.

### 10.2 Evaluation

- Point-in-time universes include delistings and historical constituents.
- Features, labels, parameters, and data snapshot hashes are immutable per run.
- Rolling train and validation windows use purge and embargo for overlapping
  labels.
- The final holdout is sealed, has a single release policy, and cannot be used by
  parameter sweeps or ordinary UI runs.
- Metrics are after fees, spread, slippage, impact, partial fills, and cash
  constraints.
- Reports include confidence intervals, effective sample size, calibration,
  benchmark-relative returns, factor alpha, drawdown, turnover, and Deflated
  Sharpe or equivalent multiple-testing control.

Promotion requires asset-class- and version-specific independent observations,
lower confidence bounds above the configured bar, positive net benchmark-relative
evidence, acceptable drawdown, execution reliability, and an explicit human
configuration change.

### 10.3 LLM routing

- deterministic filters handle routine classification and calculations;
- a lower-cost model summarizes ordinary eligible events;
- the strongest configured model handles material or ambiguous escalations;
- account-aware prompts never silently fall back to a new vendor;
- every model call has a purpose, input snapshot hash, cost budget, timeout,
  cache key, schema, and evaluation result;
- model output can create a research case or bounded proposal, never an action.

## 11. Operator interface

The visual direction is a dark **Risk Flight Deck** shell with a readable
**Research Ledger** inside evidence-heavy views.

Persistent navigation:

- Command Center
- Approval Inbox
- Portfolio & Risk
- Research
- Backtests
- Operations

A fixed risk rail shows broker/mode, market state, quote freshness, worker health,
asset-class kill switches, daily P&L budget, exposure, reconciliation age, and
autonomous-execution status. Red is reserved for blocked or dangerous state.

Approval receipts show current and proposed exposure, source age, risk verdict,
expiration, and exact action. Backtest reports show synchronized strategy and
benchmark curves, drawdowns, trade markers, regimes, costs, uncertainty, window
boundaries, and immutable configuration/data hashes.

The client is a small TypeScript application built to static assets and served by
FastAPI. It uses typed API contracts, accessible controls, responsive layouts,
per-panel stale/error states, and no third-party CDN runtime dependencies.

## 12. Authentication, secrets, and integrations

- Startup fails if the production/local-operator auth secret is missing.
- Login exchanges the secret for a short-lived, `HttpOnly`, `Secure` where
  applicable, `SameSite=Strict` session.
- State-changing requests require a CSRF token and recent reauthentication for
  approvals, resets, connector changes, and future live controls.
- All endpoints except minimal liveness require authentication.
- CSP denies framing and limits scripts, connections, images, and styles.
- Logs default to `0600`, rotate, minimize prompts, and redact registered secrets.

Robinhood MCP runs in an isolated read-only gateway with an exact tool/schema
allowlist and no Alpaca, app-approval, LLM, or Composio credentials.

Composio uses a rotated, restricted runtime key and direct-tools sessions. Sandbox,
remote shell, dynamic discovery, connection management, triggers, and raw proxy
are disabled. Initial tools can only send to fixed recipients or create drafts;
model-generated outbound messages require a separate notification policy.

## 13. Error handling and degraded modes

- Provider outages open a scoped circuit breaker and retain the last observation
  only for explicitly read-only display.
- Schema changes quarantine the payload and prevent affected calculations.
- Duplicate events and fills are idempotent by provider ID and content identity.
- Contradictory facts remain visible and reduce research-case confidence.
- Unknown broker acceptance blocks retries until reconciliation completes.
- Failed backtests preserve logs and immutable inputs but cannot produce a
  promotion-eligible report.
- The UI renders independent loading, stale, empty, and error states per panel.

## 14. Testing strategy

- State-machine property tests for orders, rules, leases, and breakers.
- Crash-point tests around every broker submission and database commit boundary.
- Concurrency tests for duplicate approvals, OCO groups, fills, and webhooks.
- Contract tests against recorded provider schemas and timestamp semantics.
- Prompt-injection tests using encoded adversarial source content.
- Point-in-time and lookahead property tests for every data source and feature.
- Golden accounting tests for fees, lots, partial fills, stops, and corporate
  actions.
- Statistical tests for window separation, holdout access, multiple-testing
  correction, and reproducibility.
- Browser tests for authentication, approvals, panic truthfulness, accessibility,
  responsive layouts, stale state, and failed requests.
- Credentialed Alpaca paper drills remain separate from deterministic CI.

## 15. Rollout gates

1. **Foundation gate:** no P0/P1 security or execution-durability finding open.
2. **Data gate:** freshness/provenance and point-in-time contracts pass.
3. **Simulation gate:** accounting and fill-model golden tests pass.
4. **Research gate:** preregistered baselines complete rolling validation.
5. **Shadow gate:** sufficient independent, fresh observations accumulate.
6. **Paper gate:** sustained paper execution matches simulated assumptions within
   tolerance and all reconciliation drills pass.
7. **Connector gate:** read-only Robinhood and outbound Composio threat reviews
   pass.
8. **Future live gate:** separate design review; never implied by prior gates.

## 16. Immediate next subproject

Implementation begins with the safety foundation. Data, research, UI, Robinhood,
Composio, and additional model providers do not expand until that gate passes.

