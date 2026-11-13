"""Operator test proposals: an intentional, paper-only way to exercise the order lifecycle.

A test proposal is an ordinary ``PROPOSED`` order created through
``TradingService.propose_order``: the same risk pre-check, the same human
approval (which re-runs the risk engine), the same submission outbox,
reconciliation and audit trail as every other proposal. It differs only in
where it comes from and how it is labelled:

- it is created by the operator, never by a strategy, and its rationale starts
  with ``OPERATOR TEST (not a strategy signal)``;
- its intent key starts with ``optest-``, so reports can separate test trades
  from strategy trades;
- it is refused unless ``operator_test.enabled`` is true, the symbol is in
  ``operator_test.symbols``, and an entry's estimated notional is at most
  ``operator_test.max_notional_per_order``.

Scenarios (what each demonstrates):

- ``non_marketable``: a buy limit below the current bid, to submit and then
  cancel. It is *expected* to rest unfilled; it is not guaranteed to, because
  the price can fall to the limit before you cancel.
- ``entry``: a buy intended to fill (market, or a limit at or above the ask).
- ``exit``: a sell no larger than the position you can reduce, so it can never
  open a short.

Nothing here approves, submits or adjusts an order to force a fill.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from uuid import uuid4

from ..config import TradingMode

OPERATOR_TEST_PREFIX = "optest-"
SCENARIOS = ("non_marketable", "entry", "exit")
_LABEL = "OPERATOR TEST (not a strategy signal)"


class OperatorTestRefused(ValueError):
    """A stable, value-free reason the test proposal was not created."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class OperatorTestRequest:
    scenario: str
    symbol: str
    order_type: str
    qty: Optional[Decimal] = None
    notional: Optional[Decimal] = None
    limit_price: Optional[Decimal] = None


def is_operator_test_key(idempotency_key: str) -> bool:
    return idempotency_key.startswith(OPERATOR_TEST_PREFIX)


def _positive(value: Optional[Decimal]) -> bool:
    try:
        return value is not None and value.is_finite() and value > 0
    except (InvalidOperation, AttributeError):
        return False


def _price_precision_ok(price: Decimal) -> bool:
    # Alpaca accepts at most 2 decimals at or above $1 and 4 below it.
    places = -price.as_tuple().exponent if price.as_tuple().exponent < 0 else 0
    return places <= (2 if price >= 1 else 4)


def _validate_shape(request: OperatorTestRequest) -> None:
    if request.scenario not in SCENARIOS:
        raise OperatorTestRefused("scenario_invalid", f"scenario must be one of {', '.join(SCENARIOS)}")
    if request.order_type not in ("limit", "market"):
        raise OperatorTestRefused("order_type_invalid", "order type must be limit or market")
    if request.order_type == "limit":
        if not _positive(request.limit_price) or not _positive(request.qty) or request.notional is not None:
            raise OperatorTestRefused(
                "limit_order_invalid", "a limit order needs a positive quantity and limit price, and no notional"
            )
        if not _price_precision_ok(request.limit_price):
            raise OperatorTestRefused(
                "limit_price_precision", "use at most 2 decimals at or above $1 (4 below $1)"
            )
    else:
        if request.limit_price is not None or (_positive(request.qty) == _positive(request.notional)):
            raise OperatorTestRefused(
                "market_order_invalid", "a market order needs exactly one of quantity or notional, and no limit price"
            )
    if request.scenario == "non_marketable" and request.order_type != "limit":
        raise OperatorTestRefused("non_marketable_needs_limit", "the non-marketable scenario uses a limit order")
    if request.scenario == "exit" and not _positive(request.qty):
        raise OperatorTestRefused("exit_needs_quantity", "an exit names the share quantity to sell")


def propose_operator_test(
    service,
    request: OperatorTestRequest,
    *,
    actor: str,
    reason: str,
    request_id: str,
    idempotency_key: Optional[str] = None,
) -> dict[str, Any]:
    """Create one operator test proposal, or refuse with a stable code."""
    config = service.config
    if config.trading.mode is not TradingMode.PAPER:
        raise OperatorTestRefused("paper_only", "operator test proposals exist only in paper mode")
    settings = config.operator_test
    if not settings.enabled:
        raise OperatorTestRefused(
            "operator_test_disabled", "set operator_test.enabled: true in config.yaml to allow test proposals"
        )
    _validate_shape(request)
    symbol = request.symbol.strip().upper()
    if symbol not in {s.upper() for s in settings.symbols}:
        raise OperatorTestRefused(
            "symbol_not_approved_for_tests", f"{symbol} is not in operator_test.symbols"
        )
    if not service.market_is_open(symbol):
        raise OperatorTestRefused(
            "market_closed", "the market is closed; paper orders are rejected outside market hours"
        )
    quote = service.broker.get_quote(symbol)
    if not quote.is_valid or not (_positive(quote.bid) and _positive(quote.ask)):
        raise OperatorTestRefused("quote_unavailable", f"no valid bid and ask for {symbol}")

    side = "sell" if request.scenario == "exit" else "buy"
    held: Optional[Decimal] = None
    if request.scenario == "non_marketable" and request.limit_price >= quote.bid:
        raise OperatorTestRefused(
            "limit_would_be_marketable",
            f"limit {request.limit_price} is not below the bid {quote.bid}; it could fill at once",
        )
    if request.scenario == "entry" and request.order_type == "limit" and request.limit_price < quote.ask:
        raise OperatorTestRefused(
            "entry_limit_below_ask",
            f"limit {request.limit_price} is below the ask {quote.ask}; it is not expected to fill now",
        )
    if request.scenario == "exit":
        held = service.available_reduce_qty(symbol, "sell", reference_price=quote.bid)
        if held <= 0:
            raise OperatorTestRefused("no_position_to_reduce", f"there is no reducible {symbol} position")
        if request.qty > held:
            raise OperatorTestRefused(
                "exit_exceeds_position",
                f"sell {request.qty} exceeds the {held} shares that can be reduced; it would open a short",
            )

    reference = request.limit_price or (quote.ask if side == "buy" else quote.bid)
    estimated = request.notional if request.notional is not None else request.qty * reference
    cap = Decimal(str(settings.max_notional_per_order))
    if request.scenario != "exit" and estimated > cap:
        raise OperatorTestRefused(
            "test_notional_exceeds_cap",
            f"estimated ${estimated:.2f} exceeds operator_test.max_notional_per_order ${cap:.2f}",
        )

    size = f"${request.notional} notional" if request.notional is not None else f"{request.qty} shares"
    price = f" limit {request.limit_price}" if request.limit_price is not None else ""
    expectation = {
        "non_marketable": (
            "Expected to rest unfilled below the bid so it can be cancelled. Not guaranteed: "
            "if the price falls to the limit before cancellation it can fill, fully or partly."
        ),
        "entry": (
            "Intended to fill while the market is open. A market order fills near the ask; "
            "a limit can still go unfilled if the price moves away."
        ),
        "exit": (
            f"Reduces the position ({held} reducible shares) by {request.qty}. "
            "It cannot exceed what is held, so it cannot open a short."
        ),
    }[request.scenario]
    rationale = (
        f"{_LABEL}, scenario {request.scenario}: {side.upper()} {size} {symbol} "
        f"{request.order_type}{price}, estimated ${estimated:.2f}. "
        f"Quote at proposal: bid {quote.bid} ask {quote.ask} last {quote.last}. "
        f"{expectation} Operator note: {reason}"
    )
    key = idempotency_key or f"{OPERATOR_TEST_PREFIX}{uuid4().hex[:24]}"
    if not is_operator_test_key(key):
        raise OperatorTestRefused("key_invalid", "operator test keys start with optest-")
    result = service.propose_order(
        symbol,
        side,
        request.order_type,
        qty=None if request.qty is None else str(request.qty),
        notional=None if request.notional is None else str(request.notional),
        limit_price=None if request.limit_price is None else str(request.limit_price),
        idempotency_key=key,
        actor=actor,
        reason=rationale,
        request_id=request_id,
    )
    return {
        **result,
        "operator_test": True,
        "scenario": request.scenario,
        "idempotency_key": key,
        "estimated_notional": f"{estimated:.2f}",
        "reference_quote": {
            "bid": str(quote.bid),
            "ask": str(quote.ask),
            "last": str(quote.last),
        },
    }
