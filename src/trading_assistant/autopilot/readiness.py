"""The readiness gate for ``autopilot.mode: paper``.

The gate fails closed: every requirement must pass, and any missing, stale,
failed or mismatched evidence fails it. Evidence counts only when it was
produced under the current configuration *and* decision-code fingerprints
(``identity``), so changing the rule, the universe, sizing or risk limits
starts the observation period again, and the operator's approval
(``autopilot.readiness.approved_fingerprint``) is bound to the same pair.

Observation is measured in distinct market sessions with a clean cycle, and
separately in calendar days spanned, so a burst of cycles cannot stand in
for elapsed sessions. Passing the gate means the configured rule ran as
designed on genuine observations and a real-data backtest; it says nothing
about profitability. Live trading is not supported at all.

The daemon evaluates the gate every cycle and writes the report to
``.local/autopilot/readiness.json`` (0600). ``python -m
trading_assistant.autopilot readiness`` prints that file.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

from ..assets import AssetClass
from ..config import AppConfig, TradingMode
from .decisions import autopilot_config_problems, resolve_universe
from .evidence import CLEAN_RESULTS, EvidenceRecord, EvidenceStore
from .identity import (
    approval_fingerprint,
    code_identity,
    config_fingerprint,
    decision_code_fingerprint,
)

REPORT_RELATIVE = Path(".local") / "autopilot" / "readiness.json"


@dataclass(frozen=True)
class Requirement:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class ReadinessReport:
    generated_at: str
    mode: str
    ready: bool
    config_fingerprint: str
    code_fingerprint: str
    approval_fingerprint: str
    code_identity: Optional[str]
    observed_sessions: int
    observation_calendar_days: int
    requirements: list[Requirement] = field(default_factory=list)
    note: str = (
        "Readiness is evidence that the configured rule ran as designed on "
        "genuine observations; it is not evidence of profitability. Live "
        "trading is not supported."
    )

    def unmet(self) -> list[Requirement]:
        return [r for r in self.requirements if not r.passed]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _matching(records: list[EvidenceRecord], config_fp: str, code_fp: str) -> list[EvidenceRecord]:
    return [
        r
        for r in records
        if r.detail.get("config_fingerprint") == config_fp
        and r.detail.get("code_fingerprint") == code_fp
    ]


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _release_requirement(config: AppConfig, root: Path, commit: Optional[str]) -> Requirement:
    readiness = config.autopilot.readiness
    if not readiness.require_release_verification:
        return Requirement("release_verification", True, "not required by configuration")
    if commit is None:
        return Requirement("release_verification", False, "running commit is unknown")
    path = Path(readiness.release_evidence_path)
    path = path if path.is_absolute() else root / path
    try:
        info = os.lstat(path)
        if not (info.st_mode & 0o170000 == 0o100000) or info.st_mode & 0o077:
            return Requirement("release_verification", False, f"{path} is not a private regular file")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Requirement("release_verification", False, f"no release evidence at {path}")
    except (OSError, ValueError):
        return Requirement("release_verification", False, f"unreadable release evidence at {path}")
    if payload.get("passed") is not True:
        return Requirement("release_verification", False, "latest release verification did not pass")
    if payload.get("commit") != commit:
        return Requirement(
            "release_verification",
            False,
            f"release evidence is for {payload.get('commit')}, running {commit}",
        )
    return Requirement("release_verification", True, f"verified commit {commit}")


def _backtest_requirement(
    config: AppConfig,
    backtests: list[EvidenceRecord],
    code_fp: str,
    now: datetime,
) -> Requirement:
    readiness = config.autopilot.readiness
    universe = set(resolve_universe(config))
    candidates = [
        r
        for r in backtests
        if r.result_code == "succeeded"
        and r.detail.get("strategy") == config.autopilot.strategy
        and r.detail.get("code_fingerprint") == code_fp
        and r.detail.get("data_source") not in (None, "synthetic")
    ]
    if not candidates:
        return Requirement(
            "backtest_evidence",
            False,
            "no succeeded real-data backtest of this strategy and decision code",
        )
    latest = max(candidates, key=lambda r: _aware(r.created_at))
    detail = latest.detail
    age = now - _aware(latest.created_at)
    problems = []
    if age > timedelta(days=readiness.backtest_max_age_days):
        problems.append(f"{age.days} days old (max {readiness.backtest_max_age_days})")
    if int(detail.get("calendar_days", 0)) < readiness.min_backtest_calendar_days:
        problems.append(
            f"covers {detail.get('calendar_days', 0)} days "
            f"(min {readiness.min_backtest_calendar_days})"
        )
    missing = universe - set(detail.get("symbols", []))
    if missing:
        problems.append("missing symbols " + ",".join(sorted(missing)))
    if not detail.get("holdout_evaluated"):
        problems.append("no holdout evaluation")
    if problems:
        return Requirement("backtest_evidence", False, "; ".join(problems))
    return Requirement(
        "backtest_evidence",
        True,
        f"backtest of {detail.get('window_start')}..{detail.get('window_end')} "
        f"recorded {_aware(latest.created_at).date()}",
    )


def _safety_requirement(breakers) -> Requirement:
    if breakers is None:
        return Requirement("no_blocking_safety_latch", False, "breaker state unavailable")
    from ..risk.breakers import BreakerScope

    blocking = [
        BreakerScope.broker_drift(),
        BreakerScope.operator_global(),
        BreakerScope.loss(AssetClass.EQUITY),
        BreakerScope.drawdown(AssetClass.EQUITY),
    ]
    try:
        tripped = [
            scope.key
            for scope in blocking
            if (state := breakers.get(scope)) is not None and state.tripped
        ]
    except Exception:
        return Requirement("no_blocking_safety_latch", False, "breaker state unreadable")
    if tripped:
        return Requirement("no_blocking_safety_latch", False, "tripped: " + ", ".join(tripped))
    return Requirement("no_blocking_safety_latch", True, "no drift, loss, drawdown or global latch")


def evaluate_readiness(
    *,
    config: AppConfig,
    store: EvidenceStore,
    root: Path,
    now: datetime,
    tenure_guard=None,
    breakers=None,
    installation_check: Optional[Callable[[Path], object]] = None,
) -> ReadinessReport:
    readiness = config.autopilot.readiness
    config_fp = config_fingerprint(config)
    code_fp = decision_code_fingerprint(config.autopilot.strategy)
    approval_fp = approval_fingerprint(config_fp, code_fp)
    commit = code_identity(root)
    now = _aware(now)
    requirements: list[Requirement] = []

    requirements.append(
        Requirement(
            "paper_trading_only",
            config.trading.mode is TradingMode.PAPER,
            "trading.mode is paper"
            if config.trading.mode is TradingMode.PAPER
            else "live trading is not supported",
        )
    )

    if installation_check is None:
        from ..installation import require_designated as installation_check
    try:
        installation_check(root)
        requirements.append(Requirement("installation_designated", True, str(root)))
    except Exception as error:
        requirements.append(
            Requirement("installation_designated", False, getattr(error, "code", "invalid"))
        )

    if tenure_guard is None:
        requirements.append(
            Requirement(
                "runtime_ownership",
                False,
                "only the running daemon can prove runtime tenure",
            )
        )
    else:
        owned = not getattr(tenure_guard, "lost", False) and not getattr(tenure_guard, "closed", False)
        requirements.append(
            Requirement(
                "runtime_ownership",
                owned,
                "daemon holds runtime tenure" if owned else "runtime tenure lost",
            )
        )

    problems = autopilot_config_problems(config)
    requirements.append(
        Requirement(
            "configuration_valid",
            not problems,
            "; ".join(problems) if problems else "universe and sizing pass startup checks",
        )
    )

    requirements.append(_backtest_requirement(config, store.backtests(), code_fp, now))

    cycles = _matching(store.cycles(), config_fp, code_fp)
    clean = [r for r in cycles if r.result_code in CLEAN_RESULTS]
    sessions = sorted({str(r.detail.get("session")) for r in clean if r.detail.get("session")})
    span_days = 0
    if sessions:
        first = datetime.fromisoformat(sessions[0])
        last = datetime.fromisoformat(sessions[-1])
        span_days = (last - first).days
    requirements.append(
        Requirement(
            "observed_sessions",
            len(sessions) >= readiness.min_observed_sessions
            and span_days >= readiness.min_observation_calendar_days,
            f"{len(sessions)} clean sessions over {span_days} calendar days "
            f"(need {readiness.min_observed_sessions} over "
            f"{readiness.min_observation_calendar_days})",
        )
    )

    failed = sum(1 for r in cycles if r.result_code == "failed")
    requirements.append(
        Requirement(
            "observation_failures",
            failed <= readiness.max_failed_cycles,
            f"{failed} failed cycles (max {readiness.max_failed_cycles})",
        )
    )
    degraded_sessions = {
        str(r.detail.get("session")) for r in cycles if r.result_code == "degraded"
    }
    requirements.append(
        Requirement(
            "degraded_sessions",
            len(degraded_sessions) <= readiness.max_degraded_sessions,
            f"{len(degraded_sessions)} sessions with stale or unavailable data "
            f"(max {readiness.max_degraded_sessions})",
        )
    )

    if clean:
        latest = max(_aware(r.created_at) for r in clean)
        age_hours = (now - latest).total_seconds() / 3600
        requirements.append(
            Requirement(
                "evidence_recent",
                age_hours <= readiness.max_evidence_age_hours,
                f"latest clean observation {age_hours:.1f}h ago "
                f"(max {readiness.max_evidence_age_hours}h)",
            )
        )
    else:
        requirements.append(Requirement("evidence_recent", False, "no clean observation yet"))

    requirements.append(_release_requirement(config, root, commit))
    requirements.append(_safety_requirement(breakers))

    approved = readiness.approved_fingerprint == approval_fp
    requirements.append(
        Requirement(
            "operator_approval",
            approved,
            "approved for these fingerprints"
            if approved
            else f"set autopilot.readiness.approved_fingerprint: {approval_fp} "
            "only after reviewing this report",
        )
    )

    return ReadinessReport(
        generated_at=now.isoformat(),
        mode=config.autopilot.mode,
        ready=all(r.passed for r in requirements),
        config_fingerprint=config_fp,
        code_fingerprint=code_fp,
        approval_fingerprint=approval_fp,
        code_identity=commit,
        observed_sessions=len(sessions),
        observation_calendar_days=span_days,
        requirements=requirements,
    )


def write_report(report: ReadinessReport, root: Path) -> Path:
    path = root / REPORT_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(report.to_dict(), handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(staging, path)
    return path


def render_report(payload: dict[str, Any]) -> str:
    lines = [
        f"autopilot readiness: {'READY' if payload.get('ready') else 'NOT READY'}"
        f"  (mode {payload.get('mode')}, generated {payload.get('generated_at')})",
        f"  observed sessions: {payload.get('observed_sessions')} over "
        f"{payload.get('observation_calendar_days')} calendar days",
        f"  approval fingerprint: {payload.get('approval_fingerprint')}",
        f"  running commit: {payload.get('code_identity')}",
    ]
    for requirement in payload.get("requirements", []):
        mark = "PASS" if requirement.get("passed") else "FAIL"
        lines.append(f"  [{mark}] {requirement.get('name')}: {requirement.get('detail')}")
    lines.append(f"  {payload.get('note')}")
    return "\n".join(lines)
