"""Deterministic, code-only baseline strategies — the benchmarks to beat.

Each implements Strategy.on_bar(features) -> Signal and runs through the SAME risk
engine and sizing as the (future) LLM path. Signals express desired position
intent: BUY = want to be long, SELL = want to be flat, HOLD = no change. The
engine translates intent into orders given the current position.
"""

from .base import Signal, SignalAction, Strategy  # noqa: F401
