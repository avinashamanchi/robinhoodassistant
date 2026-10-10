"""Stable identities: order intent keys, fingerprints, and code identity.

* ``action_key`` names one intended trading action (strategy, symbol, side,
  decision session). It is the order's idempotency key, which the broker
  adapter sends as ``client_order_id``, so a restart, a duplicate schedule or
  a retried cycle can only ever find the same order, never create a second.
* ``config_fingerprint`` covers every setting that changes what the autopilot
  would decide or how large an order may be.
* ``decision_code_fingerprint`` hashes the source of the rule, the feature
  pipeline and the shared session policy, so evidence produced by different
  decision code never counts toward readiness.
* ``approval_fingerprint`` binds the operator's approval to both of those and
  to the readiness thresholds (``gate_fingerprint``).
* ``code_identity`` is the git commit the process runs (for release
  evidence); it is read from ``.git`` without running git.
"""

from __future__ import annotations

from datetime import date
import hashlib
import inspect
import json
from pathlib import Path

from ..config import AppConfig
from .decisions import STRATEGIES, resolve_universe

_KEY_PREFIX = "ap-"


def action_key(strategy: str, symbol: str, side: str, session: date) -> str:
    """Idempotency key for one intended action (fits the 64-char column)."""
    material = f"autopilot|{strategy}|{symbol.upper()}|{side}|{session.isoformat()}"
    return _KEY_PREFIX + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def config_fingerprint(config: AppConfig) -> str:
    autopilot = config.autopilot
    risk = config.risk
    material = {
        "trading": {
            "mode": config.trading.mode.value,
            "broker": config.trading.broker.value,
        },
        "autopilot": {
            "strategy": autopilot.strategy,
            "universe": resolve_universe(config),
            "notional_per_trade": str(autopilot.notional_per_trade),
            "max_orders_per_day": autopilot.max_orders_per_day,
            "max_feature_age_hours": autopilot.max_feature_age_hours,
            "run_after_open_minutes": autopilot.run_after_open_minutes,
        },
        "risk": {
            "ticker_allowlist": sorted(s.upper() for s in risk.ticker_allowlist),
            "max_notional_per_order": risk.max_notional_per_order,
            "max_position_per_ticker": risk.max_position_per_ticker,
            "max_portfolio_exposure": risk.max_portfolio_exposure,
            "daily_realized_loss_limit": risk.daily_realized_loss_limit,
            "max_daily_total_loss": getattr(risk, "max_daily_total_loss", None),
            "max_account_drawdown_pct": getattr(risk, "max_account_drawdown_pct", None),
            "price_sanity_pct": risk.price_sanity_pct,
            "max_spread_pct": getattr(risk, "max_spread_pct", None),
            "max_quote_age_seconds": getattr(risk, "max_quote_age_seconds", None),
        },
    }
    return hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()


def _decision_sources(strategy: str) -> list[Path]:
    """Source files whose behavior determines what the autopilot decides."""
    from ..signals import events, features, indicators, regime, sessions, structure
    from ..strategies import base
    from . import decisions, engine

    modules = [
        inspect.getmodule(STRATEGIES[strategy]),
        base,
        features,
        indicators,
        regime,
        structure,
        events,
        sessions,
        decisions,
        engine,
    ]
    return [Path(inspect.getfile(module)) for module in modules]


def decision_code_fingerprint(strategy: str) -> str:
    digest = hashlib.sha256()
    for path in _decision_sources(strategy):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def evidence_fingerprint(config_fp: str, code_fp: str) -> str:
    """The configuration and decision-code pair that one cycle's evidence is for."""
    return hashlib.sha256(f"{config_fp}:{code_fp}".encode("utf-8")).hexdigest()


def gate_fingerprint(config: AppConfig) -> str:
    """The readiness thresholds an approval was given under.

    Excludes the approval itself and where release evidence is read from:
    neither loosens the gate.
    """
    thresholds = config.autopilot.readiness.model_dump(
        mode="json",
        exclude={"approved_fingerprint", "release_evidence_path"},
    )
    return hashlib.sha256(_canonical(thresholds).encode("utf-8")).hexdigest()


def approval_fingerprint(config_fp: str, code_fp: str, gate_fp: str) -> str:
    """What ``autopilot.readiness.approved_fingerprint`` must equal.

    Loosening any readiness threshold therefore voids an approval, while
    the evidence (keyed by ``evidence_fingerprint``) still counts.
    """
    return hashlib.sha256(
        f"{evidence_fingerprint(config_fp, code_fp)}:{gate_fp}".encode("utf-8")
    ).hexdigest()


def code_identity(root: Path) -> str | None:
    """The checked-out commit, read from .git (worktrees and packed refs)."""
    try:
        git = root / ".git"
        if git.is_file():
            pointer = git.read_text(encoding="utf-8").strip()
            if not pointer.startswith("gitdir: "):
                return None
            git = Path(pointer[len("gitdir: "):])
            common = git / "commondir"
            common_dir = (
                (git / common.read_text(encoding="utf-8").strip()).resolve()
                if common.is_file()
                else git
            )
        else:
            common_dir = git
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head if _is_sha(head) else None
        ref = head[len("ref: "):]
        for base in (git, common_dir):
            candidate = base / ref
            if candidate.is_file():
                value = candidate.read_text(encoding="utf-8").strip()
                return value if _is_sha(value) else None
        packed = common_dir / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref and _is_sha(parts[0]):
                    return parts[0]
    except OSError:
        return None
    return None


def _is_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value)
