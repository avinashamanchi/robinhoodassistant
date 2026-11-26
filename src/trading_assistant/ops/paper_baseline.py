"""Start a fresh database from a flat Alpaca paper account, without its history.

``ops.paper_history`` imports earlier history when one legacy database holds
exactly the broker's fills. When the history is split across databases, or
includes orders placed outside this app, nothing can be imported exactly. A
fresh database then reads every historical fill activity on its first
reconciliation, finds no local order for any of them, and reports drift on
every start.

This one-time, stopped-writer tool records a *baseline* instead. It refuses
unless the account holds no position and no open order (checked twice, with
the fill activities unchanged in between), the database holds no trading
history and no reconciliation cursor, and broker drift is already latched
(the failed first start that showed the history). It then writes the fills
cursor at the observation time, strictly after every historical fill, so
reconciliation reads only fills that happen afterwards, and records an
audit event with the number and digest of the fills it set aside.

It never imports a fill, never changes broker state and never clears the
drift latch: after reviewing the receipt the operator resets the latch
through the normal breaker reset. Realized P/L before the baseline is not
in this database; the archived databases and the Alpaca dashboard keep it.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sys
from typing import Callable
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from ..config import BrokerKind, TradingMode, load_config
from ..db.models import (
    AuditEvent,
    Base,
    CircuitBreakerState,
    ReconciliationCursor,
    RiskEvent,
    RuntimeTenure,
    SensitiveMigrationState,
    StartupReconciliationState,
)
from ..security.sensitive_fields import persist_sensitive
from .paper_history import (
    PaperHistoryAdoptionError,
    _account_is_flat,
    _activity_tuple,
    _broker_drift_breaker_generation,
    _canonical_digest,
    _decimal_text,
    _timestamp_text,
)

FILLS_STREAM = "fills"
# Rows a failed first start writes; anything else is trading history.
_BOOKKEEPING = {
    AuditEvent,
    CircuitBreakerState,
    RiskEvent,
    RuntimeTenure,
    SensitiveMigrationState,
    StartupReconciliationState,
}


class PaperBaselineError(RuntimeError):
    def __init__(self, stable_code: str) -> None:
        super().__init__(stable_code)
        self.stable_code = stable_code


@dataclass(frozen=True)
class PaperBaselineReceipt:
    baseline_at: str
    fills_set_aside: int
    activity_digest: str
    breaker_generation: int

    def payload(self) -> dict[str, object]:
        return {
            "activity_digest": self.activity_digest,
            "baseline_at": self.baseline_at,
            "breaker_generation": self.breaker_generation,
            "fills_set_aside": self.fills_set_aside,
            "next_step": "review, then reset the broker_drift breaker",
        }


def _drift_latch_generation(session: Session, expected: int | None = None) -> int:
    try:
        return _broker_drift_breaker_generation(session, expected_generation=expected)
    except PaperHistoryAdoptionError:
        code = (
            "paper_baseline_broker_drift_breaker_required"
            if expected is None
            else "paper_baseline_broker_drift_breaker_changed"
        )
        raise PaperBaselineError(code) from None


def _has_no_trading_history(session: Session) -> bool:
    for mapper in Base.registry.mappers:
        model = mapper.class_
        if model in _BOOKKEEPING:
            continue
        if session.scalar(select(func.count()).select_from(model)) != 0:
            return False
    return True


def _activity_digest(activities) -> str:
    return _canonical_digest(
        [
            {
                "fill": a.broker_fill_id,
                "order": a.broker_order_id,
                "ticker": a.ticker.upper(),
                "side": a.side,
                "qty": _decimal_text(a.qty),
                "price": _decimal_text(a.price),
                "at": _timestamp_text(a.filled_at),
            }
            for a in activities
        ]
    )


def establish_flat_baseline(
    session_factory: sessionmaker[Session],
    broker,
    *,
    actor: str,
    reason: str,
    request_id: str,
    maintenance_guard,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> PaperBaselineReceipt:
    actor, reason, request_id = actor.strip(), reason.strip(), request_id.strip()
    if not actor or not reason or not request_id:
        raise PaperBaselineError("paper_baseline_context_invalid")

    with session_factory() as session:
        if not _has_no_trading_history(session):
            raise PaperBaselineError("paper_baseline_target_has_history")
        breaker_generation = _drift_latch_generation(session)

    try:
        if not _account_is_flat(broker):
            raise PaperBaselineError("paper_baseline_account_not_flat")
        first = _activity_tuple(broker.get_fill_activities(after=None))
        second = _activity_tuple(broker.get_fill_activities(after=None))
        if not _account_is_flat(broker):
            raise PaperBaselineError("paper_baseline_account_not_flat")
    except PaperBaselineError:
        raise
    except Exception:
        raise PaperBaselineError("paper_baseline_broker_unavailable") from None
    if first != second:
        raise PaperBaselineError("paper_baseline_broker_changed")

    observed = now().astimezone(timezone.utc)
    latest = max((a.filled_at.astimezone(timezone.utc) for a in first), default=None)
    if latest is not None and latest >= observed:
        raise PaperBaselineError("paper_baseline_clock_behind_broker")
    receipt = PaperBaselineReceipt(
        baseline_at=_timestamp_text(observed),
        fills_set_aside=len(first),
        activity_digest=_activity_digest(first),
        breaker_generation=breaker_generation,
    )

    with maintenance_guard.exclusive_transaction_renewal():
        with session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            maintenance_guard.renew_in_transaction(session.connection())
            if not _has_no_trading_history(session):
                raise PaperBaselineError("paper_baseline_target_has_history")
            _drift_latch_generation(session, breaker_generation)
            session.add(
                ReconciliationCursor(
                    broker=str(broker.reconciliation_key),
                    stream=FILLS_STREAM,
                    last_activity_id=None,
                    last_activity_at=observed,
                    version=1,
                )
            )
            persist_sensitive(
                session,
                AuditEvent(
                    actor=actor,
                    action="paper_baseline.establish",
                    target_type="broker",
                    target_id=str(broker.reconciliation_key),
                    request_id=request_id,
                    result_code="baseline_recorded",
                ),
                {
                    "reason": reason,
                    "detail_json": json.dumps(receipt.payload(), sort_keys=True),
                },
                session_factory=session_factory,
            )
            session.commit()
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    guard = None
    try:
        from ..bootstrap import acquire_maintenance_guard, prepare_database_runtime
        from ..broker.alpaca import AlpacaBroker
        from ..logging import runtime_startup
        from ..security.crypto import build_sensitive_data_cipher
        from ..security.secrets import (
            MacOSKeychainSecretProvider,
            load_role_secrets,
            secret_value,
        )
        from ..security.sensitive_fields import bind_sensitive_cipher

        config = load_config()
        if (
            config.trading.mode is not TradingMode.PAPER
            or config.trading.broker is not BrokerKind.ALPACA
        ):
            raise PaperBaselineError("paper_baseline_paper_configuration_required")
        secrets = load_role_secrets(
            "paper-drill", config=config, provider=MacOSKeychainSecretProvider()
        )
        with runtime_startup("paper-drill", secrets):
            runtime = prepare_database_runtime(secrets)
            bind_sensitive_cipher(
                runtime.session_factory,
                build_sensitive_data_cipher(config.encryption, secrets),
            )
            guard = acquire_maintenance_guard(runtime)
            broker = AlpacaBroker.from_credentials(
                secret_value(secrets.alpaca_api_key),
                secret_value(secrets.alpaca_secret_key),
                paper=True,
                runtime_role="paper-drill",
            )
            receipt = establish_flat_baseline(
                runtime.session_factory,
                broker,
                actor="operator:paper-baseline",
                reason="operator-authorized baseline of a flat Alpaca paper account",
                request_id=uuid4().hex,
                maintenance_guard=guard,
            )
        print(json.dumps(receipt.payload(), sort_keys=True))
        return 0
    except PaperBaselineError as exc:
        print(exc.stable_code, file=sys.stderr)
        return 1
    except Exception as exc:
        code = getattr(exc, "stable_code", None) or "paper_baseline_failed"
        print(code, file=sys.stderr)
        return 1
    finally:
        if guard is not None:
            try:
                guard.close()
            except Exception:
                print("paper_baseline_tenure_release_uncertain", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
