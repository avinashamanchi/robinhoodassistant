"""Analyst validation pass (C3/C4).

    python -m trading_assistant.validate_analyst [--max-calls N] [--symbols AAPL,MSFT]

Prints the pre-run LLM cost estimate, then runs the analyst in trigger-mode over
the HOLDOUT window on cached real bars, grades every call, and prints the accuracy
+ calibration report. Aborts (asks for confirmation) if the estimate exceeds $5.
Promotes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys

from .analyst.accuracy import analyst_accuracy
from .analyst.analyst import Analyst
from .backtest.data import DataSource, cache_path, load_parquet
from .backtest.holdout import HoldoutGuard
from .backtest.llm_runner import LLMRunConfig, estimate_llm_calls
from .config import Secrets, load_config
from .llm.factory import build_llm_backend

COST_CONFIRM_USD = 5.0


def run(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--max-calls", type=int, default=300)
    p.add_argument("--symbols", default="")
    p.add_argument("--cost-per-call", type=float, default=0.0)  # Gemini/Groq free tiers
    p.add_argument("--yes", action="store_true", help="skip the >$5 confirmation")
    args = p.parse_args(argv)

    config = load_config("config.yaml")
    secrets = Secrets()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or config.risk.ticker_allowlist

    try:
        frames = {s: load_parquet(cache_path(".cache/bars", s, "1Day")) for s in symbols + ["SPY"]}
    except FileNotFoundError:
        print("No cached bars — run the C1 download first (see runbook).")
        return 1
    source = DataSource(frames)
    guard = HoldoutGuard(source.timeline(symbols), holdout_months=12)
    dev, hold = guard.split(source.timeline(symbols))
    start, end = (hold[0], hold[-1]) if hold else (None, None)

    run_cfg = LLMRunConfig(max_llm_calls=args.max_calls, cost_per_call_usd=args.cost_per_call, horizon_bars=5)
    est = sum(estimate_llm_calls(source, s, run_cfg, start=start, end=end)["estimated_calls"] for s in symbols)
    est_cost = est * args.cost_per_call
    print(f"Pre-run estimate: ~{est} analyst calls, ~${est_cost:.2f} "
          f"(capped at {args.max_calls}/run per symbol).")
    if est_cost > COST_CONFIRM_USD and not args.yes:
        print(f"Estimate exceeds ${COST_CONFIRM_USD}. Re-run with --yes to proceed.")
        return 2

    analyst = Analyst(build_llm_backend(config, secrets), max_tokens=config.llm.max_tokens)
    print("Running analyst over the holdout (this makes real LLM calls)...")
    report = analyst_accuracy(source, symbols, analyst, run_cfg, start=start, end=end)
    print(json.dumps(report, indent=2))
    print("\n" + report["verdict"])
    return 0


if __name__ == "__main__":
    sys.exit(run())
