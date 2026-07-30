"""FastMCP server — a thin wrapper over TradingService.

The tools the LLM sees. None of them execute a trade: ``propose_order`` creates a
PENDING proposal in the DB and returns it. Execution requires a separate,
human-gated approval step (Phase 3). This module deliberately holds no business
logic — it maps tool calls to :class:`TradingService` methods.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
from typing import Any, Optional
from uuid import uuid4

from mcp.server.fastmcp import FastMCP

from ..config import load_config
from ..security.secrets import load_role_secrets
from ..service import TradingService
from ..operations import AuditRecorder, MutationContext
from ..ops.tenure import TenureLost, TenureUncertain

mcp = FastMCP("trading-assistant")
_LOG = logging.getLogger(__name__)

_service: Optional[TradingService] = None
_audit: Optional[AuditRecorder] = None


def configure(
    service: TradingService,
    *,
    audit: AuditRecorder | None = None,
) -> None:
    """Inject a service (used by tests and by custom hosts)."""
    global _service, _audit
    _service = service
    _audit = audit or AuditRecorder(service.session_factory)


def build_default_container():
    from .. import bootstrap
    from ..logging import runtime_startup

    config = load_config()
    secrets = load_role_secrets("mcp", config=config)
    with runtime_startup("mcp", secrets):
        return bootstrap.build_container(
            config,
            secrets,
            runtime_role="mcp",
        )


def _svc() -> TradingService:
    global _service
    if _service is None:
        raise RuntimeError("mcp_service_unconfigured")
    return _service


def _record(
    context: MutationContext,
    action: str,
    target_type: str,
    target_id: object,
    result: dict[str, Any],
) -> None:
    global _audit
    if _audit is None:
        _audit = AuditRecorder(_svc().session_factory)
    try:
        result_code = (
            "failed"
            if result.get("error")
            else str(
                result.get("status")
                or result.get("state")
                or "completed"
            )
        )
        _audit.record(
            context,
            action,
            target_type,
            str(target_id),
            result_code,
        )
    except Exception:
        _LOG.disabled = False
        _LOG.error(
            "boundary_audit_unavailable action=%s request_id=%s",
            action,
            context.request_id,
        )


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
    reason: str,
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
    context = MutationContext(
        actor="assistant:mcp",
        reason=reason,
        request_id=uuid4().hex,
    )
    result = _svc().propose_order(
        ticker=ticker,
        side=side,
        order_type=order_type,
        qty=qty,
        notional=notional,
        limit_price=limit_price,
        actor=context.actor,
        reason=context.reason,
        request_id=context.request_id,
    )
    _record(context, "mcp.propose_order", "order", result.get("order_id", ""), result)
    return result


# ── conditional rules ───────────────────────────────────────────
@mcp.tool()
def create_conditional_rule(
    ticker: str,
    condition: dict[str, Any],
    action: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Store a standing rule, e.g. condition {"price_below": 175} action
    {"side": "buy", "notional": "50"}. The daemon (Phase 4) evaluates it."""
    context = MutationContext(
        actor="assistant:mcp",
        reason=reason,
        request_id=uuid4().hex,
    )
    try:
        result = _svc().create_conditional_rule(
            ticker,
            condition,
            action,
            actor=context.actor,
            reason=context.reason,
            request_id=context.request_id,
        )
    except Exception:
        _record(
            context,
            "mcp.rule_create",
            "rule_group",
            ticker.upper(),
            {"error": "failed"},
        )
        raise
    _record(context, "mcp.rule_create", "rule_group", result.get("group_id", ""), result)
    return result


@mcp.tool()
def list_rules() -> list[dict[str, Any]]:
    """List all standing conditional rules."""
    return _svc().list_rules()


@mcp.tool()
def cancel_rule(rule_id: int, reason: str) -> dict[str, Any]:
    """Cancel a standing conditional rule by id."""
    context = MutationContext(
        actor="assistant:mcp",
        reason=reason,
        request_id=uuid4().hex,
    )
    result = _svc().cancel_rule(
        rule_id,
        actor=context.actor,
        reason=context.reason,
        request_id=context.request_id,
    )
    _record(context, "mcp.rule_cancel", "rule", rule_id, result)
    return result


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


async def _run_owned_server(container) -> None:
    """Run stdio until it exits or durable MCP ownership is lost."""
    guard = getattr(container, "runtime_tenure_guard", None)
    if guard is None:
        raise TenureUncertain()
    loop = asyncio.get_running_loop()
    lost = asyncio.Event()
    guard.set_on_lost(
        lambda: loop.call_soon_threadsafe(lost.set)
    )
    server_task = asyncio.create_task(mcp.run_stdio_async())
    loss_task = asyncio.create_task(lost.wait())
    done, _pending = await asyncio.wait(
        {server_task, loss_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if loss_task in done and guard.lost:
        server_task.cancel()
        with suppress(asyncio.CancelledError):
            await server_task
        raise TenureLost()
    loss_task.cancel()
    with suppress(asyncio.CancelledError):
        await loss_task
    await server_task


def main() -> None:
    container = build_default_container()
    from ..logging import runtime_startup

    with runtime_startup("mcp", container.secrets):
        guard = getattr(container, "runtime_tenure_guard", None)
        primary_failure = False
        try:
            configure(container.service, audit=container.audit)
            asyncio.run(_run_owned_server(container))
        except BaseException:
            primary_failure = True
            raise
        finally:
            if guard is not None:
                try:
                    released = guard.close()
                except BaseException:
                    if not primary_failure:
                        raise TenureUncertain() from None
                else:
                    if not released and not primary_failure:
                        raise TenureUncertain()


if __name__ == "__main__":
    main()
