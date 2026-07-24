"""Submit and cancel one tiny, non-marketable Alpaca paper limit order."""

from __future__ import annotations

import argparse
from decimal import ROUND_UP, Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ..broker.models import OrderStatus
from ..config import AppConfig, BrokerKind, Secrets, TradingMode, load_config

if TYPE_CHECKING:
    from ..service import TradingService


class PaperDrillError(RuntimeError):
    """The paper drill could not complete without weakening a safety guardrail."""


def build_paper_service(config: AppConfig, secrets: Secrets) -> "TradingService":
    from ..broker.factory import build_broker, build_clock
    from ..db.schema import require_current_schema
    from ..db.session import create_db_engine, make_session_factory
    from ..service import TradingService

    engine = create_db_engine(secrets.database_url)
    require_current_schema(engine)
    return TradingService(
        build_broker(config, secrets),
        make_session_factory(engine),
        config,
        build_clock(config, secrets),
    )


def run_paper_drill(
    config: AppConfig,
    service: "TradingService | None",
    *,
    symbol: str = "AAPL",
    test_notional: Decimal = Decimal("1.25"),
) -> dict[str, Any]:
    """Exercise propose -> risk re-check -> broker accept -> cancel end to end."""
    if (
        config.trading.mode is not TradingMode.PAPER
        or config.trading.broker is not BrokerKind.ALPACA
    ):
        raise PaperDrillError("paper drill requires trading.mode=paper and broker=alpaca")
    if service is None:
        raise PaperDrillError("paper TradingService is required")
    if test_notional <= 0:
        raise PaperDrillError("test_notional must be positive")

    quote = service.broker.get_quote(symbol)
    # Four percent below the current reference is inside the configured 5% price
    # sanity band but deliberately unlikely to execute before cancellation.
    limit_price = (quote.last * Decimal("0.96")).quantize(
        Decimal("0.01"), rounding=ROUND_UP
    )
    qty = (test_notional / limit_price).quantize(
        Decimal("0.000001"), rounding=ROUND_UP
    )
    request_id = uuid4().hex

    proposal = service.propose_order(
        symbol,
        "buy",
        "limit",
        qty=str(qty),
        limit_price=str(limit_price),
        actor="operator:paper-drill",
        reason="paper drill order proposal",
        request_id=request_id,
    )
    if proposal["status"] != OrderStatus.PROPOSED.value:
        raise PaperDrillError(
            f"risk engine rejected paper drill: {proposal.get('risk_reasons', [])}"
        )

    broker_order_id: str | None = None
    terminal = False
    try:
        approved = service.approve_order(
            proposal["order_id"],
            actor="operator:paper-drill",
            reason="paper drill execution",
            request_id=request_id,
        )
        if not approved.get("executed") or not approved.get("broker_order_id"):
            raise PaperDrillError(f"broker did not accept paper drill: {approved}")
        broker_order_id = approved["broker_order_id"]
        accepted = service.broker.get_order_status(broker_order_id)
        if accepted.status not in (
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
        ):
            raise PaperDrillError(
                f"unexpected broker acceptance status: {accepted.status.value}"
            )

        canceled = service.cancel_live_order(
            proposal["order_id"],
            actor="operator:paper-drill",
            reason="paper drill terminal cancellation",
            request_id=request_id,
        )
        broker_terminal = service.broker.get_order_status(broker_order_id)
        if (
            canceled.get("status") != OrderStatus.CANCELED.value
            or broker_terminal.status is not OrderStatus.CANCELED
        ):
            raise PaperDrillError(
                "paper order did not reach canceled state at both broker and local DB"
            )
        terminal = True
        return {
            "order_id": proposal["order_id"],
            "broker_order_id": broker_order_id,
            "symbol": symbol,
            "qty": str(qty),
            "limit_price": str(limit_price),
            "broker_accepted": True,
            "terminal_status": OrderStatus.CANCELED.value,
        }
    finally:
        if broker_order_id is not None and not terminal:
            try:
                service.cancel_live_order(
                    proposal["order_id"],
                    actor="operator:paper-drill",
                    reason="paper drill failure cleanup",
                    request_id=request_id,
                )
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="AAPL")
    parser.add_argument("--notional", type=Decimal, default=Decimal("1.25"))
    args = parser.parse_args(argv)
    config = load_config()
    secrets = Secrets()
    result = run_paper_drill(
        config,
        build_paper_service(config, secrets),
        symbol=args.symbol.upper(),
        test_notional=args.notional,
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
