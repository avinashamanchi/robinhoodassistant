# Trading Assistant

An LLM-driven agentic trading assistant (Alpaca broker, Model Context Protocol).
**Human-gated, risk-enforced, paper-first.** The LLM proposes; a human approves;
a deterministic risk engine is the final authority on every order.

> ⚠️ Educational / paper-trading project. Live trading is OFF by default and
> requires a double-lock (config flag **and** an environment confirmation string).

## Status

Built in phases (see `docs/superpowers/specs/`):

- **Phase 1 ✅** — scaffold, config, DB models + order state machine, `BrokerClient`
  ABC + `MockBroker`, risk engine (pure) with FIFO P&L + persistent kill switch +
  injectable market clock. Full pytest coverage of every limit.
- **Phase 2** — `AlpacaBroker` (paper) + MCP server.
- **Phase 3** — FastAPI host + agentic loop + approval UI.
- **Phase 4** — monitoring daemon + conditional rules + Telegram.
- **Phase 5** — hardening.

## Quickstart (Phase 1)

```bash
uv venv --python 3.11
uv pip install -e '.' pytest pytest-cov pyyaml
cp .env.example .env      # fill in when you reach Phase 2
uv run pytest             # run the suite
```

## Safety model

1. Live trading requires BOTH `config.yaml` `trading.mode: live` AND
   `LIVE_TRADING_CONFIRM=I_UNDERSTAND_LIVE_TRADING`.
2. The LLM only ever produces `PROPOSED` orders. Execution needs human approval
   (or an explicitly pre-approved rule).
3. The risk engine runs on every order and cannot be bypassed.
4. Everything dangerous defaults OFF.

Configuration lives in `config.yaml` (risk limits, non-secret) and `.env`
(secrets, gitignored — see `.env.example`).
