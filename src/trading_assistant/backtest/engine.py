"""Event-driven replay engine (single symbol) with strict no-lookahead.

At simulated time t the strategy sees only a DataView bounded at t. An order
decided at t is submitted after the decision and fills at t+1's open (via
SimBroker) — so no decision can use information it could not have had. Orders run
through the SAME RiskEngine as the live path; only the numeric limits differ
(a permissive backtest profile so capital can actually be deployed).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional

from ..assets import AssetClass
from ..broker.models import (
    OrderRequest,
    OrderSide,
    OrderType,
    PortfolioSnapshot,
)
from ..config import BacktestConfig, RiskConfig
from ..risk.engine import RiskEngine
from ..signals.features import build_features
from ..strategies.base import SignalAction, Strategy
from .data import DataSource
from .sim_broker import SimBroker, SimFill

TARGET_PCT = 0.95  # fraction of equity a full-size long deploys
# Feature indicators are causal and need at most ~252 bars (52-week window); a
# bounded lookback yields identical values while keeping the run O(n·window),
# not O(n^2). Still strictly <= t, so the no-lookahead guarantee is untouched.
FEATURE_LOOKBACK = 320


def permissive_risk_config(symbols: list[str], capital: float = 1e9) -> RiskConfig:
    """A RiskConfig that lets the backtest deploy real capital through the real
    engine. Allowlist is the tested symbols; limits are set to ``capital``."""
    return RiskConfig(
        ticker_allowlist=[s.upper() for s in symbols],
        max_notional_per_order=capital,
        max_position_per_ticker=capital,
        max_portfolio_exposure=capital,
        daily_realized_loss_limit=capital,
        price_sanity_pct=1e6,          # sanity check off for backtests
        reject_when_market_closed=False,
        proposal_ttl_minutes=1,
    )


@dataclass
class BacktestResult:
    symbol: str
    strategy: str
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    fills: list[SimFill] = field(default_factory=list)
    starting_equity: float = 0.0
    # Parallel per-bar series (same length/order as equity_curve).
    regimes: list = field(default_factory=list)      # Optional[Regime] per bar
    invested: list = field(default_factory=list)     # bool per bar (position held?)

    @property
    def ending_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else self.starting_equity

    @property
    def total_return_pct(self) -> float:
        if not self.starting_equity:
            return 0.0
        return (self.ending_equity / self.starting_equity - 1) * 100


def _target_notional(equity: float, size_hint: Optional[float]) -> float:
    return equity * TARGET_PCT * (size_hint if size_hint is not None else 1.0)


def run_backtest(
    strategy: Strategy,
    source: DataSource,
    symbol: str,
    *,
    backtest_config: BacktestConfig,
    risk_config: Optional[RiskConfig] = None,
    spy_symbol: Optional[str] = None,
    starting_cash: float = 100_000.0,
    warmup: int = 2,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> BacktestResult:
    """Replay ``symbol``. If ``start``/``end`` are given, only bars in that window
    are traded and scored, but feature views still see all prior history (so a
    sub-period run gets fair warmup — no cold-start bias)."""
    symbol = symbol.upper()
    ac = AssetClass.for_symbol(symbol)
    risk_config = risk_config or permissive_risk_config([symbol])
    engine_risk = RiskEngine(risk_config)
    broker = SimBroker(backtest_config, starting_cash)
    full = source.full(symbol)

    result = BacktestResult(symbol=symbol, strategy=strategy.name, starting_equity=starting_cash)

    for t in source.timeline([symbol]):
        if (start is not None and t < start) or (end is not None and t > end):
            continue
        # 1. Fill orders submitted at the previous bar, using THIS bar.
        if t in full.index:
            broker.process_bar(t, {symbol: full.loc[t]})

        view = source.view(t)
        hist = view.history(symbol, lookback=FEATURE_LOOKBACK)

        # 2. Decide on a fresh, bounded feature view (only data <= t).
        regime = None
        if len(hist) >= warmup:
            spy_hist = (
                view.history(spy_symbol, lookback=FEATURE_LOOKBACK) if spy_symbol else None
            )
            features = build_features(symbol, ac, hist, spy_df=spy_hist, as_of=t)
            regime = features.regime
            signal = strategy.on_bar(features)
            _act(engine_risk, broker, symbol, ac, signal.action, signal.size_hint)

        result.equity_curve.append((t, broker.equity()))
        result.regimes.append(regime)
        result.invested.append(_current_qty(broker, symbol) > 0)

    result.fills = broker.fills
    return result


def _current_qty(broker: SimBroker, symbol: str) -> float:
    pos = broker._pos.get(symbol.upper())
    return pos.qty if pos else 0.0


def _act(
    engine_risk: RiskEngine,
    broker: SimBroker,
    symbol: str,
    ac: AssetClass,
    action: SignalAction,
    size_hint: Optional[float],
) -> None:
    qty = _current_qty(broker, symbol)

    if action is SignalAction.BUY and qty <= 0:
        notional = _target_notional(broker.equity(), size_hint)
        order = OrderRequest(
            ticker=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            idempotency_key=uuid.uuid4().hex,
            notional=Decimal(str(round(notional, 2))),
        )
    elif action is SignalAction.SELL and qty > 0:
        order = OrderRequest(
            ticker=symbol,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            idempotency_key=uuid.uuid4().hex,
            qty=Decimal(str(qty)),
        )
    else:
        return  # HOLD, or intent already satisfied

    snapshot = _snapshot(broker, symbol)
    result = engine_risk.check(order, snapshot, killswitch_tripped=False, market_open=True)
    if result.approved:
        broker.submit_order(order)


def _snapshot(broker: SimBroker, symbol: str) -> PortfolioSnapshot:
    positions = {p.ticker: p for p in broker.get_positions()}
    quotes = {symbol.upper(): broker.get_quote(symbol)}
    for p in positions.values():
        quotes.setdefault(p.ticker, broker.get_quote(p.ticker))
    return PortfolioSnapshot(
        positions=positions,
        quotes=quotes,
        buying_power=broker.get_account().buying_power,
        realized_pnl_today=Decimal(0),
    )
