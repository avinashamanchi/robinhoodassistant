"""Real-data backtest evidence for the readiness gate.

Runs the backtest harness on exactly the configured strategy class over
completed, final daily bars for the autopilot universe (plus SPY context),
with the configured cost model and a sacred holdout, and records an
``autopilot.backtest`` evidence event carrying provenance: strategy, data
source, data window, symbols, fingerprints, running commit, cost model and
per-window results against buy-and-hold.

Results are simulated and never authorize anything; the gate checks only that
genuine, matching, recent and complete evidence exists. Fetching real bars
needs the operator's Alpaca market-data credentials.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

import pandas as pd

from ..assets import AssetClass
from ..backtest.data import DataSource
from ..backtest.evaluate import walk_forward
from ..config import AppConfig
from ..signals.sessions import completed_bars, final_through
from .decisions import STRATEGIES, resolve_universe
from .evidence import EvidenceStore
from .identity import code_identity, config_fingerprint, decision_code_fingerprint


def record_backtest_evidence(
    config: AppConfig,
    frames: Mapping[str, pd.DataFrame],
    *,
    store: EvidenceStore,
    root: Path,
    now: datetime,
    data_source: str,
) -> dict:
    if data_source == "synthetic":
        raise ValueError("readiness backtest evidence requires real market data")
    universe = resolve_universe(config)
    missing = [s for s in universe + ["SPY"] if s not in frames]
    if missing:
        raise ValueError("missing bars for " + ",".join(missing))
    through = final_through(now, AssetClass.EQUITY)
    completed = {
        symbol: completed_bars(frame, asset_class=AssetClass.EQUITY, through=through)
        for symbol, frame in frames.items()
    }
    source = DataSource(completed)
    strategy = config.autopilot.strategy
    backtest = getattr(config, "backtest", None)
    report, _guard = walk_forward(
        source,
        universe,
        [STRATEGIES[strategy]],
        backtest_config=backtest,
        holdout_months=backtest.holdout_months if backtest is not None else 12,
        spy_symbol="SPY",
        label=f"autopilot readiness evidence: {strategy}",
    )
    timeline = source.timeline(universe)
    if not timeline:
        raise ValueError("no completed bars in the backtest window")
    window_start, window_end = timeline[0], timeline[-1]
    detail = {
        "strategy": strategy,
        "data_source": data_source,
        "symbols": universe,
        "window_start": window_start.date().isoformat(),
        "window_end": window_end.date().isoformat(),
        "calendar_days": (window_end - window_start).days,
        "holdout_start": report.holdout_start.isoformat() if report.holdout_start else None,
        "holdout_evaluated": any(row.window == "holdout" for row in report.rows),
        "config_fingerprint": config_fingerprint(config),
        "code_fingerprint": decision_code_fingerprint(strategy),
        "code_identity": code_identity(root),
        "cost_model": backtest.model_dump(mode="json") if backtest is not None else None,
        "recorded_at": now.isoformat(),
        "disclaimer": report.disclaimer,
        "rows": [row.to_dict() for row in report.rows],
    }
    store.record_backtest(result_code="succeeded", detail=detail)
    return detail


def fetch_alpaca_frames(
    symbols: list[str],
    secrets,
    *,
    years: int,
    cache_dir: str | Path = ".cache/bars",
    runtime_role: str = "paper-drill",
    client_factory: Callable | None = None,
) -> dict[str, pd.DataFrame]:
    """Download daily bars for the evidence run (fresh, not a stale cache)."""
    from ..backtest.data import download_alpaca_bars
    from ..security.secrets import secret_value

    frames: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        frames[symbol] = download_alpaca_bars(
            symbol,
            secret_value(secrets.alpaca_api_key),
            secret_value(secrets.alpaca_secret_key),
            timeframe="1Day",
            years=years,
            cache_dir=Path(cache_dir) / "readiness",
            runtime_role=runtime_role,
            client_factory=client_factory,
            max_cache_age_seconds=0,
        )
    return frames
