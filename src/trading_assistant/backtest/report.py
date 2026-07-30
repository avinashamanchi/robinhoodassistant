"""Backtest report structures. Every simulated result carries the mandatory
disclaimer (Phase 7 guardrail #3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import math
from typing import Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from ..config import BacktestConfig
from .metrics import Metrics

SIMULATED_LABEL = "Simulated — past performance does not predict future results."
BACKTEST_ARTIFACT_SCHEMA_VERSION = 2
MAX_REGIME_PNL_ROWS = 50
MAX_REGIME_NAME_LENGTH = 100


class BacktestMetricValues(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    total_return_pct: float = Field(allow_inf_nan=False)
    cagr_pct: float = Field(allow_inf_nan=False)
    sharpe: float = Field(allow_inf_nan=False)
    sortino: float = Field(allow_inf_nan=False)
    max_drawdown_pct: float = Field(le=0, allow_inf_nan=False)
    win_rate_pct: float = Field(ge=0, le=100, allow_inf_nan=False)
    profit_factor: float | None = Field(
        ge=0,
        allow_inf_nan=False,
    )
    avg_win: float = Field(ge=0, allow_inf_nan=False)
    avg_loss: float = Field(le=0, allow_inf_nan=False)
    exposure_pct: float = Field(ge=0, le=100, allow_inf_nan=False)
    turnover: float = Field(ge=0, allow_inf_nan=False)
    num_trades: int = Field(ge=0)
    pnl_by_regime: dict[str, float]

    @field_validator("pnl_by_regime")
    @classmethod
    def validate_regime_values(
        cls,
        value: dict[str, float],
    ) -> dict[str, float]:
        if len(value) > MAX_REGIME_PNL_ROWS:
            raise ValueError("regime attribution is oversized")
        if any(
            not isinstance(name, str)
            or not name
            or len(name) > MAX_REGIME_NAME_LENGTH
            or isinstance(amount, bool)
            or not isinstance(amount, float)
            or not math.isfinite(amount)
            for name, amount in value.items()
        ):
            raise ValueError("regime attribution is invalid")
        return value


class BacktestMetricRowPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    symbol: str = Field(
        min_length=1,
        max_length=20,
        pattern=r"^[A-Z0-9./-]+$",
    )
    strategy: str = Field(min_length=1, max_length=100)
    window: Literal["development", "holdout", "full"]
    metrics: BacktestMetricValues
    benchmark_buy_and_hold: BacktestMetricValues
    beat_buy_and_hold: bool

    @model_validator(mode="after")
    def beat_flag_matches_returns(self):
        expected = (
            self.metrics.total_return_pct
            > self.benchmark_buy_and_hold.total_return_pct
        )
        if self.beat_buy_and_hold is not expected:
            raise ValueError("benchmark comparison is inconsistent")
        return self


def validate_metric_row_payload(payload: object) -> dict:
    return BacktestMetricRowPayload.model_validate(
        payload,
        strict=True,
    ).model_dump(mode="json")


def canonical_metric_rows_digest(metric_rows: list[dict]) -> str:
    validated = [
        validate_metric_row_payload(payload)
        for payload in metric_rows
    ]
    encoded = json.dumps(
        validated,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class ReportRow:
    symbol: str
    strategy: str
    window: str                 # development | holdout | full
    metrics: Metrics
    benchmark: Metrics          # buy-and-hold on the same symbol/window
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    benchmark_equity_curve: list[tuple[datetime, float]] = field(
        default_factory=list
    )
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    total_fees: float = 0.0
    benchmark_total_fees: float = 0.0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "window": self.window,
            "metrics": self.metrics.to_dict(),
            "benchmark_buy_and_hold": self.benchmark.to_dict(),
            "beat_buy_and_hold": self.metrics.total_return_pct
            > self.benchmark.total_return_pct,
        }


@dataclass
class EvaluationReport:
    rows: list[ReportRow] = field(default_factory=list)
    holdout_start: Optional[datetime] = None
    label: str = ""
    disclaimer: str = SIMULATED_LABEL

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "holdout_start": self.holdout_start.isoformat() if self.holdout_start else None,
            "disclaimer": self.disclaimer,
            "rows": [r.to_dict() for r in self.rows],
        }

    def render_table(self) -> str:
        header = (
            f"{'symbol':8} {'strategy':16} {'window':12} "
            f"{'ret%':>8} {'B&H%':>8} {'sharpe':>7} {'maxDD%':>8} {'beat':>5}"
        )
        lines = [self.disclaimer, "", header, "-" * len(header)]
        for r in self.rows:
            beat = "yes" if r.metrics.total_return_pct > r.benchmark.total_return_pct else "no"
            lines.append(
                f"{r.symbol:8} {r.strategy:16} {r.window:12} "
                f"{r.metrics.total_return_pct:8.2f} {r.benchmark.total_return_pct:8.2f} "
                f"{r.metrics.sharpe:7.2f} {r.metrics.max_drawdown_pct:8.2f} {beat:>5}"
            )
        return "\n".join(lines)


def validate_persisted_artifacts(
    decoded: dict[str, object],
    metric_rows: list[dict],
    persisted_holdout_start: datetime | None,
) -> tuple[dict, list[dict]]:
    """Validate one complete encrypted artifact set and derive drawdowns."""
    expected_keys = {
        "manifest",
        *{
            f"series:{index:06d}"
            for index in range(len(metric_rows))
        },
    }
    if set(decoded) != expected_keys:
        raise ValueError("invalid artifact set")
    manifest, applied_config = _validate_manifest(
        decoded["manifest"],
        metric_rows,
        persisted_holdout_start,
    )
    series = [
        _series_with_drawdown(
            decoded[f"series:{index:06d}"],
            expected_row=metric_payload,
            expected_index=index,
            applied_config=applied_config,
        )
        for index, metric_payload in enumerate(metric_rows)
    ]
    if not series or not _artifact_bounds_match(manifest, series):
        raise ValueError("missing backtest series")
    return manifest, series


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("artifact timestamp must be text")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("artifact timestamp must be UTC")
    return parsed


def _nonnegative_finite(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("artifact number must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("artifact number must be finite and nonnegative")
    return number


def _validate_manifest(
    payload: object,
    metric_rows: list[dict],
    persisted_holdout_start: datetime | None,
) -> tuple[dict, dict]:
    expected_keys = {
        "schema_version",
        "data_source",
        "requested_range",
        "actual_range",
        "started_at",
        "completed_at",
        "duration_seconds",
        "backtest_config",
        "symbols",
        "strategies",
        "metric_rows_sha256",
        "holdout_start",
        "holdout_access_log",
        "validation",
        "episodes",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise ValueError("invalid artifact manifest")
    if payload["schema_version"] != BACKTEST_ARTIFACT_SCHEMA_VERSION:
        raise ValueError("invalid artifact schema")
    if payload["data_source"] != "synthetic":
        raise ValueError("invalid artifact source")
    if payload["validation"] != {
        "status": "unavailable",
        "reason": "not_run",
    } or payload["episodes"] != {"status": "not_run"}:
        raise ValueError("invalid artifact evidence status")

    requested = payload["requested_range"]
    actual = payload["actual_range"]
    if (
        not isinstance(requested, dict)
        or set(requested) != {"start", "end"}
        or not isinstance(actual, dict)
        or set(actual) != {"start", "end"}
    ):
        raise ValueError("invalid artifact bounds")
    requested_values = (requested["start"], requested["end"])
    if (requested_values[0] is None) != (requested_values[1] is None):
        raise ValueError("invalid requested bounds")
    if requested_values[0] is not None:
        requested_start = _parse_utc(requested_values[0])
        requested_end = _parse_utc(requested_values[1])
        if requested_end < requested_start:
            raise ValueError("invalid requested bounds")
    actual_start = _parse_utc(actual["start"])
    actual_end = _parse_utc(actual["end"])
    if actual_end < actual_start:
        raise ValueError("invalid actual bounds")
    started_at = _parse_utc(payload["started_at"])
    completed_at = _parse_utc(payload["completed_at"])
    if completed_at < started_at:
        raise ValueError("invalid run timestamps")
    _nonnegative_finite(payload["duration_seconds"])

    raw_config = payload["backtest_config"]
    if not isinstance(raw_config, dict):
        raise ValueError("invalid backtest config")
    applied = BacktestConfig.model_validate(
        raw_config,
        strict=True,
    ).model_dump(mode="json")
    if applied != raw_config:
        raise ValueError("noncanonical backtest config")

    symbols = payload["symbols"]
    strategies = payload["strategies"]
    if (
        not isinstance(symbols, list)
        or not symbols
        or not all(isinstance(value, str) and value for value in symbols)
        or not isinstance(strategies, list)
        or not strategies
        or not all(
            isinstance(value, str) and value for value in strategies
        )
    ):
        raise ValueError("invalid backtest identities")
    if any(
        row.get("symbol") not in symbols
        or row.get("strategy") not in strategies
        for row in metric_rows
    ):
        raise ValueError("manifest does not cover metric identities")
    metric_rows_sha256 = payload["metric_rows_sha256"]
    expected_metric_rows_sha256 = canonical_metric_rows_digest(
        metric_rows
    )
    if (
        not isinstance(metric_rows_sha256, str)
        or len(metric_rows_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in metric_rows_sha256
        )
        or not hmac.compare_digest(
            metric_rows_sha256,
            expected_metric_rows_sha256,
        )
    ):
        raise ValueError("metric row digest mismatch")

    holdout_start = _parse_utc(payload["holdout_start"])
    if (
        persisted_holdout_start is None
        or holdout_start != persisted_holdout_start
    ):
        raise ValueError("holdout evidence mismatch")
    access_log = payload["holdout_access_log"]
    if not isinstance(access_log, list) or not access_log:
        raise ValueError("missing holdout access log")
    for access in access_log:
        if (
            not isinstance(access, dict)
            or set(access) != {"at", "context", "blocked"}
            or not isinstance(access["context"], str)
            or not access["context"]
            or not isinstance(access["blocked"], bool)
        ):
            raise ValueError("invalid holdout access log")
        _parse_utc(access["at"])
    return payload, applied


def _series_with_drawdown(
    payload: object,
    *,
    expected_row: dict,
    expected_index: int,
    applied_config: dict,
) -> dict:
    expected_keys = {
        "schema_version",
        "row_index",
        "symbol",
        "strategy",
        "window",
        "window_bounds",
        "strategy_equity",
        "benchmark_equity",
        "actual_total_fees",
        "benchmark_actual_total_fees",
        "cost_assumptions",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise ValueError("invalid series artifact")
    if (
        payload["schema_version"] != BACKTEST_ARTIFACT_SCHEMA_VERSION
        or payload["row_index"] != expected_index
        or payload["symbol"] != expected_row.get("symbol")
        or payload["strategy"] != expected_row.get("strategy")
        or payload["window"] != expected_row.get("window")
        or payload["window"]
        not in {"development", "holdout", "full"}
    ):
        raise ValueError("series identity mismatch")
    bounds = payload["window_bounds"]
    if (
        not isinstance(bounds, dict)
        or set(bounds) != {"start", "end"}
    ):
        raise ValueError("invalid series bounds")
    window_start = _parse_utc(bounds["start"])
    window_end = _parse_utc(bounds["end"])
    if window_end < window_start:
        raise ValueError("invalid series bounds")

    def curve(name: str) -> tuple[list[dict], list[dict]]:
        points = payload[name]
        if not isinstance(points, list) or not points:
            raise ValueError("missing equity curve")
        peak = float("-inf")
        previous = None
        drawdown = []
        for point in points:
            if not isinstance(point, dict) or set(point) != {
                "at",
                "equity",
            }:
                raise ValueError("invalid equity point")
            at = point["at"]
            timestamp = _parse_utc(at)
            if previous is not None and timestamp <= previous:
                raise ValueError("unordered equity curve")
            previous = timestamp
            equity = float(point["equity"])
            if isinstance(point["equity"], bool) or not math.isfinite(equity):
                raise ValueError("invalid equity point")
            peak = max(peak, equity)
            value = 0.0 if peak <= 0 else (equity / peak - 1.0) * 100.0
            if not math.isfinite(value):
                raise ValueError("invalid drawdown point")
            drawdown.append({"at": at, "drawdown_pct": value})
        if (
            _parse_utc(points[0]["at"]) != window_start
            or _parse_utc(points[-1]["at"]) != window_end
        ):
            raise ValueError("curve does not match window bounds")
        return points, drawdown

    strategy, strategy_drawdown = curve("strategy_equity")
    benchmark, benchmark_drawdown = curve("benchmark_equity")
    for key in ("actual_total_fees", "benchmark_actual_total_fees"):
        _nonnegative_finite(payload[key])
    expected_costs = {
        "fills": applied_config["fills"],
        "slippage_bps": applied_config["slippage_bps"],
        "fees_bps": applied_config["fees_bps"],
    }
    if payload["cost_assumptions"] != expected_costs:
        raise ValueError("cost assumptions do not match manifest")
    return {
        **payload,
        "strategy_equity": strategy,
        "benchmark_equity": benchmark,
        "strategy_drawdown": strategy_drawdown,
        "benchmark_drawdown": benchmark_drawdown,
    }


def _artifact_bounds_match(manifest: dict, series: list[dict]) -> bool:
    points = [
        _parse_utc(point["at"])
        for payload in series
        for name in ("strategy_equity", "benchmark_equity")
        for point in payload[name]
    ]
    return (
        bool(points)
        and min(points)
        == _parse_utc(manifest["actual_range"]["start"])
        and max(points)
        == _parse_utc(manifest["actual_range"]["end"])
    )
