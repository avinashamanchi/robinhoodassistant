"""FastMCP server — a thin wrapper over TradingService.

The tools the LLM sees. None of them execute a trade: ``propose_order`` creates a
PENDING proposal in the DB and returns it. Execution requires a separate,
human-gated approval step (Phase 3). This module deliberately holds no business
logic — it maps tool calls to :class:`TradingService` methods.
"""

from __future__ import annotations

from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from ..broker.factory import build_broker, build_clock
from ..config import Secrets, load_config
from ..db.models import create_all
from ..db.session import create_db_engine, make_session_factory
from ..service import TradingService

mcp = FastMCP("trading-assistant")

_service: Optional[TradingService] = None


def configure(service: TradingService) -> None:
    """Inject a service (used by tests and by custom hosts)."""
    global _service
    _service = service


def build_default_service() -> TradingService:
    from ..external_accounts.factory import build_external_source
    from ..logging import register_all_secrets

    config = load_config()
    secrets = Secrets()
    register_all_secrets(secrets)
    engine = create_db_engine(secrets.database_url)
    create_all(engine)
    session_factory = make_session_factory(engine)
    broker = build_broker(config, secrets)
    clock = build_clock(config, secrets)
    return TradingService(
        broker, session_factory, config, clock,
        external_source=build_external_source(config, secrets),
    )


def _svc() -> TradingService:
    global _service
    if _service is None:
        _service = build_default_service()
    return _service


# ── read-only tools ─────────────────────────────────────────────
@mcp.tool()
def get_market_data(ticker: str) -> dict[str, Any]:
    """Latest price, bid/ask, and day change for a ticker."""
    return _svc().get_market_data(ticker)


@mcp.tool()
def get_account_summary() -> dict[str, Any]:
    """Buying power, equity, cash, and current positions."""
    return _svc().get_account_summary()


@mcp.tool()
def get_open_orders() -> list[dict[str, Any]]:
    """All orders still live (proposed/approved/submitted/partially filled)."""
    return _svc().get_open_orders()


@mcp.tool()
def get_order_status(order_id: int) -> Optional[dict[str, Any]]:
    """Full record for one order by its local id."""
    return _svc().get_order_status(order_id)


# ── proposing (never executes) ──────────────────────────────────
@mcp.tool()
def propose_order(
    ticker: str,
    side: str,
    order_type: str,
    qty: Optional[str] = None,
    notional: Optional[str] = None,
    limit_price: Optional[str] = None,
) -> dict[str, Any]:
    """Propose an order for human approval. Does NOT execute.

    Provide EXACTLY ONE of ``qty`` (shares) or ``notional`` (USD). ``side`` is
    "buy" or "sell"; ``order_type`` is "market" or "limit" (limit requires
    ``limit_price``). The order is risk-checked and stored as PROPOSED (or
    REJECTED with a reason). A human must approve it before anything trades.
    """
    return _svc().propose_order(
        ticker=ticker,
        side=side,
        order_type=order_type,
        qty=qty,
        notional=notional,
        limit_price=limit_price,
    )


# ── conditional rules ───────────────────────────────────────────
@mcp.tool()
def create_conditional_rule(
    ticker: str, condition: dict[str, Any], action: dict[str, Any]
) -> dict[str, Any]:
    """Store a standing rule, e.g. condition {"price_below": 175} action
    {"side": "buy", "notional": "50"}. The daemon (Phase 4) evaluates it."""
    return _svc().create_conditional_rule(ticker, condition, action)


@mcp.tool()
def list_rules() -> list[dict[str, Any]]:
    """List all standing conditional rules."""
    return _svc().list_rules()


@mcp.tool()
def cancel_rule(rule_id: int) -> dict[str, Any]:
    """Cancel a standing conditional rule by id."""
    return _svc().cancel_rule(rule_id)


# ── external (read-only) account tools ──────────────────────────
@mcp.tool()
def get_external_positions() -> dict[str, Any]:
    """READ-ONLY holdings at external brokers (e.g. Robinhood): ticker, quantity,
    avg cost, current value, unrealized P&L, source. Informational only."""
    return _svc().get_external_positions()


@mcp.tool()
def get_external_account_summary() -> dict[str, Any]:
    """READ-ONLY external account: total equity, cash, buying power. Informational."""
    return _svc().get_external_account_summary()


@mcp.tool()
def get_external_order_history(days: int = 30) -> dict[str, Any]:
    """READ-ONLY external order history over the last N days."""
    return _svc().get_external_order_history(days)


@mcp.tool()
def get_external_dividends(days: int = 90) -> dict[str, Any]:
    """READ-ONLY external dividends over the last N days."""
    return _svc().get_external_dividends(days)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
