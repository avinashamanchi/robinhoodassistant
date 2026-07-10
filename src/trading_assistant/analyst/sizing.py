"""Deterministic position sizing — code, NEVER the LLM.

Given a TradePlan (the LLM's thesis + levels) and a portfolio snapshot, compute
share counts per entry tranche from a fixed per-trade risk budget, then clamp to
every risk limit. Handles long (BUY) and short (SELL) entries. HOLD / NO_TRADE and
any binding-to-zero cap return 0 shares with a reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Optional

from ..broker.models import PortfolioSnapshot
from ..config import RiskConfig
from .models import PlanAction, TradePlan


def _floor(x: Decimal) -> Decimal:
    return x.to_integral_value(rounding=ROUND_DOWN)


def _allocate(total: Decimal, fractions: list[Decimal], caps: list[Decimal]) -> list[Decimal]:
    """Distribute ``total`` shares across tranches by fraction, floored, then hand
    out the remainder by largest fractional part — respecting per-tranche caps. The
    sum equals ``min(total, sum(caps))`` so the capped budget is fully used."""
    raw = [total * f for f in fractions]
    shares = [min(_floor(r), c) for r, c in zip(raw, caps)]
    remainder = int(total - sum(shares, Decimal(0)))
    # Order by descending fractional remainder, only tranches with headroom left.
    order = sorted(
        range(len(fractions)),
        key=lambda i: (raw[i] - _floor(raw[i])),
        reverse=True,
    )
    idx = 0
    while remainder > 0 and any(shares[i] < caps[i] for i in range(len(shares))):
        i = order[idx % len(order)]
        if shares[i] < caps[i]:
            shares[i] += Decimal(1)
            remainder -= 1
        idx += 1
    return shares


@dataclass
class SizedTranche:
    price_level: Decimal
    fraction: float
    shares: Decimal
    notional: Decimal


@dataclass
class SizedTradePlan:
    symbol: str
    direction: str                 # long | short | none
    total_shares: Decimal
    risk_budget: Decimal
    tranches: list[SizedTranche] = field(default_factory=list)
    zero_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "total_shares": str(self.total_shares),
            "risk_budget": str(self.risk_budget),
            "tranches": [
                {
                    "price_level": str(t.price_level),
                    "fraction": t.fraction,
                    "shares": str(t.shares),
                    "notional": str(t.notional),
                }
                for t in self.tranches
            ],
            "zero_reason": self.zero_reason,
        }


def _zero(symbol: str, reason: str, risk_budget: Decimal = Decimal(0)) -> SizedTradePlan:
    return SizedTradePlan(symbol, "none", Decimal(0), risk_budget, [], reason)


def size_trade(
    plan: TradePlan, snapshot: PortfolioSnapshot, risk_cfg: RiskConfig, equity: Decimal
) -> SizedTradePlan:
    symbol = plan.symbol.upper()
    if plan.action not in (PlanAction.BUY, PlanAction.SELL):
        return _zero(symbol, f"action is {plan.action.value}; no entry to size")

    ref = plan.reference_price
    stop = plan.exit_plan.stop
    weighted_entry = sum(
        (t.price_level * Decimal(str(t.fraction)) for t in plan.entry_plan.tranches),
        Decimal(0),
    )
    is_long = plan.action is PlanAction.BUY
    risk_per_share = (weighted_entry - stop) if is_long else (stop - weighted_entry)
    if risk_per_share <= 0:
        return _zero(symbol, "stop is not on the risk side of entry (risk/share <= 0)")

    risk_budget = (equity * Decimal(str(risk_cfg.per_trade_risk_pct)) / Decimal(100))
    total_desired = _floor(risk_budget / risk_per_share)
    if total_desired <= 0:
        return _zero(symbol, "risk budget too small for one share", risk_budget)

    # Per-tranche notional caps and whole-plan position/exposure caps.
    max_notional = Decimal(str(risk_cfg.max_notional_per_order))
    caps = [
        _floor(max_notional / t.price_level) if t.price_level > 0 else Decimal(0)
        for t in plan.entry_plan.tranches
    ]
    existing = snapshot.positions.get(symbol)
    existing_qty = abs(existing.qty) if existing else Decimal(0)
    max_pos = Decimal(str(risk_cfg.max_position_per_ticker))
    pos_cap = max((max_pos / ref) - existing_qty, Decimal(0)) if ref > 0 else Decimal(0)
    max_exp = Decimal(str(risk_cfg.max_portfolio_exposure))
    exp_cap = max((max_exp - snapshot.gross_exposure()) / ref, Decimal(0)) if ref > 0 else Decimal(0)
    max_added = _floor(min(pos_cap, exp_cap))

    target_total = min(total_desired, sum(caps, Decimal(0)), max_added)
    if target_total <= 0:
        return _zero(symbol, "position/exposure limits cap size to zero", risk_budget)

    fractions = [Decimal(str(t.fraction)) for t in plan.entry_plan.tranches]
    tranche_shares = _allocate(target_total, fractions, caps)
    total = sum(tranche_shares, Decimal(0))

    sized = [
        SizedTranche(
            price_level=t.price_level,
            fraction=t.fraction,
            shares=shares,
            notional=(shares * t.price_level).quantize(Decimal("0.01")),
        )
        for t, shares in zip(plan.entry_plan.tranches, tranche_shares)
    ]
    return SizedTradePlan(
        symbol=symbol,
        direction="long" if is_long else "short",
        total_shares=total,
        risk_budget=risk_budget.quantize(Decimal("0.01")),
        tranches=sized,
    )
