"""Readiness gate, evidence store, backtest evidence and the readiness CLI.

Evidence rows here are fixtures written into disposable databases to drive
the gate's logic. They are not, and must never be presented as, genuine
observation or backtest evidence.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import update

from trading_assistant.autopilot.evidence import (
    BACKTEST_ACTION,
    CYCLE_ACTION,
    EvidenceStore,
    cycle_key,
)
from trading_assistant.autopilot.identity import (
    approval_fingerprint,
    config_fingerprint,
    decision_code_fingerprint,
    evidence_fingerprint,
    gate_fingerprint,
)
from trading_assistant.autopilot.readiness import (
    REPORT_RELATIVE,
    evaluate_readiness,
    render_report,
    write_report,
)
from trading_assistant.db.models import AuditEvent

NOW = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
COMMIT = "a" * 40


def _paper_config(app_config, **autopilot):
    readiness = app_config.autopilot.readiness
    base = dict(mode="paper", universe=["AAPL", "MSFT"], notional_per_trade=Decimal("100"))
    base.update(autopilot)
    return app_config.model_copy(
        update={"autopilot": app_config.autopilot.model_copy(update={**base, "readiness": readiness})}
    )


def _approved(config, fingerprint=None):
    fp = fingerprint or approval_fingerprint(
        config_fingerprint(config),
        decision_code_fingerprint(config.autopilot.strategy),
        gate_fingerprint(config),
    )
    return config.model_copy(
        update={
            "autopilot": config.autopilot.model_copy(
                update={
                    "readiness": config.autopilot.readiness.model_copy(
                        update={"approved_fingerprint": fp}
                    )
                }
            )
        }
    )


def _root(tmp_path, *, commit=COMMIT, passed=True, mode=0o600):
    root = tmp_path / "installation root"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text(f"{commit}\n", encoding="utf-8")
    evidence = root / ".local/verification/release-results.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(json.dumps({"passed": passed, "commit": COMMIT}), encoding="utf-8")
    evidence.chmod(mode)
    return root


def _set_created(store, action, created_at):
    with store.session_factory() as s:
        s.execute(
            update(AuditEvent)
            .where(AuditEvent.action == action, AuditEvent.created_at > created_at)
            .values(created_at=created_at)
        )
        s.commit()


def _seed_backtest(store, config, *, created_at=NOW - timedelta(days=5), **overrides):
    detail = {
        "strategy": config.autopilot.strategy,
        "data_source": "alpaca",
        "symbols": ["AAPL", "MSFT"],
        "window_start": "2023-10-02",
        "window_end": "2026-10-08",
        "calendar_days": 1102,
        "holdout_evaluated": True,
        "code_fingerprint": decision_code_fingerprint(config.autopilot.strategy),
        "config_fingerprint": config_fingerprint(config),
    }
    detail.update(overrides)
    store.record_backtest(result_code=overrides.pop("result_code", "succeeded"), detail=detail)
    _set_created(store, BACKTEST_ACTION, created_at)


def _seed_cycles(store, config, sessions, *, result="observed", last_created=NOW - timedelta(hours=20)):
    config_fp = config_fingerprint(config)
    code_fp = decision_code_fingerprint(config.autopilot.strategy)
    for session in sessions:
        store.record_cycle(
            key=cycle_key("observe", session, evidence_fingerprint(config_fp, code_fp)),
            session=session,
            result_code=result,
            detail={
                "session": session.isoformat(),
                "config_fingerprint": config_fp,
                "code_fingerprint": code_fp,
                "mode": "observe",
                "execution": "simulated",
            },
        )
    _set_created(store, CYCLE_ACTION, last_created)


def _weekdays(start, count):
    days, current = [], start
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


@pytest.fixture
def store(make_service):
    service = make_service()
    return EvidenceStore(service.session_factory, actor="autopilot:sma_trend"), service


@pytest.fixture
def baseline(app_config, store, tmp_path):
    evidence, service = store
    config = _approved(_paper_config(app_config))
    _seed_backtest(evidence, config)
    _seed_cycles(evidence, config, _weekdays(date(2026, 8, 31), 25))
    return SimpleNamespace(
        config=config,
        store=evidence,
        service=service,
        root=_root(tmp_path),
        guard=SimpleNamespace(lost=False, closed=False),
    )


def _evaluate(b, **overrides):
    arguments = dict(
        config=b.config,
        store=b.store,
        root=b.root,
        now=NOW,
        tenure_guard=b.guard,
        breakers=b.service.breakers,
        installation_check=lambda _root: None,
    )
    arguments.update(overrides)
    return evaluate_readiness(**arguments)


def _unmet(report):
    return {r.name for r in report.unmet()}


# ── the whole gate ────────────────────────────────────────────────────────────
def test_nothing_recorded_is_not_ready(app_config, store, tmp_path):
    evidence, service = store
    report = evaluate_readiness(
        config=_paper_config(app_config),
        store=evidence,
        root=tmp_path,
        now=NOW,
        breakers=service.breakers,
        installation_check=lambda _root: None,
    )
    assert report.ready is False
    assert {
        "runtime_ownership",
        "backtest_evidence",
        "observed_sessions",
        "evidence_recent",
        "release_verification",
        "operator_approval",
    } <= _unmet(report)
    assert report.observed_sessions == 0


def test_every_requirement_met_is_ready(baseline):
    report = _evaluate(baseline)
    assert report.ready is True, [(r.name, r.detail) for r in report.unmet()]
    assert report.observed_sessions == 25


# ── each requirement fails closed on its own ─────────────────────────────────
@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"data_source": "synthetic"}, "real-data backtest"),
        ({"calendar_days": 300}, "covers 300 days"),
        ({"symbols": ["AAPL"]}, "missing symbols MSFT"),
        ({"holdout_evaluated": False}, "no holdout"),
        ({"code_fingerprint": "f" * 64}, "real-data backtest"),
        ({"strategy": "sma_crossover"}, "real-data backtest"),
        ({"result_code": "failed"}, "real-data backtest"),
    ],
)
def test_backtest_evidence_must_match_and_be_complete(app_config, store, tmp_path, change, expected):
    evidence, service = store
    config = _approved(_paper_config(app_config))
    _seed_backtest(evidence, config, **change)
    _seed_cycles(evidence, config, _weekdays(date(2026, 8, 31), 25))
    b = SimpleNamespace(config=config, store=evidence, service=service,
                        root=_root(tmp_path), guard=SimpleNamespace(lost=False, closed=False))
    report = _evaluate(b)
    requirement = next(r for r in report.requirements if r.name == "backtest_evidence")
    assert requirement.passed is False and expected in requirement.detail
    assert report.ready is False


def test_stale_backtest_evidence_fails(app_config, store, tmp_path):
    evidence, service = store
    config = _approved(_paper_config(app_config))
    _seed_backtest(evidence, config, created_at=NOW - timedelta(days=200))
    _seed_cycles(evidence, config, _weekdays(date(2026, 8, 31), 25))
    report = _evaluate(SimpleNamespace(config=config, store=evidence, service=service,
                                       root=_root(tmp_path), guard=SimpleNamespace(lost=False, closed=False)))
    assert "backtest_evidence" in _unmet(report)


def test_too_few_sessions_fail(baseline):
    config = baseline.config.model_copy(
        update={"autopilot": baseline.config.autopilot.model_copy(update={
            "readiness": baseline.config.autopilot.readiness.model_copy(update={"min_observed_sessions": 30})
        })}
    )
    report = _evaluate(baseline, config=_approved(config))
    assert _unmet(report) == {"observed_sessions"}


def test_sessions_compressed_into_a_short_span_fail(app_config, store, tmp_path):
    evidence, service = store
    config = _approved(_paper_config(app_config))
    _seed_backtest(evidence, config)
    _seed_cycles(evidence, config, [date(2026, 9, 1) + timedelta(days=i) for i in range(20)])
    report = _evaluate(SimpleNamespace(config=config, store=evidence, service=service,
                                       root=_root(tmp_path), guard=SimpleNamespace(lost=False, closed=False)))
    requirement = next(r for r in report.requirements if r.name == "observed_sessions")
    assert requirement.passed is False and "over 19 calendar days" in requirement.detail


def test_cycles_are_counted_by_distinct_session(app_config, store, tmp_path):
    evidence, service = store
    config = _approved(_paper_config(app_config))
    sessions = _weekdays(date(2026, 9, 1), 5)
    for _ in range(8):
        _seed_cycles(evidence, config, sessions)
    report = _evaluate(SimpleNamespace(config=config, store=evidence, service=service,
                                       root=_root(tmp_path), guard=SimpleNamespace(lost=False, closed=False)))
    assert report.observed_sessions == 5


def test_a_failed_cycle_fails_the_gate(baseline):
    _seed_cycles(baseline.store, baseline.config, [date(2026, 10, 7)], result="failed")
    assert "observation_failures" in _unmet(_evaluate(baseline))


def test_too_many_degraded_sessions_fail(baseline):
    _seed_cycles(baseline.store, baseline.config, _weekdays(date(2026, 10, 5), 3), result="degraded")
    assert "degraded_sessions" in _unmet(_evaluate(baseline))


def test_old_evidence_fails_recency(app_config, store, tmp_path):
    evidence, service = store
    config = _approved(_paper_config(app_config))
    _seed_backtest(evidence, config)
    _seed_cycles(evidence, config, _weekdays(date(2026, 8, 3), 25), last_created=NOW - timedelta(days=10))
    report = _evaluate(SimpleNamespace(config=config, store=evidence, service=service,
                                       root=_root(tmp_path), guard=SimpleNamespace(lost=False, closed=False)))
    assert "evidence_recent" in _unmet(report)


@pytest.mark.parametrize(
    "root_kwargs",
    [{"passed": False}, {"commit": "b" * 40}, {"mode": 0o644}],
)
def test_release_verification_must_pass_for_the_running_commit(baseline, tmp_path, root_kwargs):
    root = _root(tmp_path / "variant", **root_kwargs)
    assert "release_verification" in _unmet(_evaluate(baseline, root=root))


def test_missing_release_evidence_fails(baseline):
    (baseline.root / ".local/verification/release-results.json").unlink()
    assert "release_verification" in _unmet(_evaluate(baseline))


def test_a_tripped_safety_latch_blocks(baseline):
    from uuid import uuid4

    from trading_assistant.risk.breakers import BreakerScope, trip_in_session

    with baseline.service.session_factory() as s:
        trip_in_session(s, BreakerScope.broker_drift(), "drift", "test", request_id=uuid4().hex)
        s.commit()
    assert _unmet(_evaluate(baseline)) == {"no_blocking_safety_latch"}


def test_installation_and_tenure_are_required(baseline):
    def not_designated(_root):
        raise RuntimeError("not designated")

    assert "installation_designated" in _unmet(_evaluate(baseline, installation_check=not_designated))
    lost = SimpleNamespace(lost=True, closed=False)
    assert "runtime_ownership" in _unmet(_evaluate(baseline, tenure_guard=lost))


def test_approval_is_required_and_exact(baseline):
    unapproved = baseline.config.model_copy(
        update={"autopilot": baseline.config.autopilot.model_copy(update={
            "readiness": baseline.config.autopilot.readiness.model_copy(update={"approved_fingerprint": None})
        })}
    )
    assert _unmet(_evaluate(baseline, config=unapproved)) == {"operator_approval"}
    wrong = _approved(baseline.config, fingerprint="0" * 64)
    assert _unmet(_evaluate(baseline, config=wrong)) == {"operator_approval"}

    # Loosening the gate after approval voids the approval, not the evidence.
    for loosened in (
        {"min_observed_sessions": 1},
        {"max_failed_cycles": 5},
        {"require_release_verification": False},
    ):
        weaker = baseline.config.model_copy(
            update={"autopilot": baseline.config.autopilot.model_copy(update={
                "readiness": baseline.config.autopilot.readiness.model_copy(update=loosened)
            })}
        )
        report = _evaluate(baseline, config=weaker)
        assert _unmet(report) == {"operator_approval"}, loosened
        assert report.observed_sessions == _evaluate(baseline).observed_sessions


# ── relevant changes void evidence and approval ──────────────────────────────
def test_configuration_change_voids_evidence_and_approval(baseline):
    changed = baseline.config.model_copy(
        update={"autopilot": baseline.config.autopilot.model_copy(update={"notional_per_trade": Decimal("150")})}
    )
    report = _evaluate(baseline, config=changed)
    assert report.observed_sessions == 0
    assert {"observed_sessions", "operator_approval", "evidence_recent"} <= _unmet(report)


def test_decision_code_change_voids_evidence(baseline, monkeypatch):
    from trading_assistant.autopilot import readiness as readiness_module

    monkeypatch.setattr(readiness_module, "decision_code_fingerprint", lambda _s: "e" * 64)
    report = _evaluate(baseline)
    assert report.observed_sessions == 0
    assert {"observed_sessions", "backtest_evidence", "operator_approval"} <= _unmet(report)


def test_live_trading_is_never_ready(baseline):
    from trading_assistant.config import TradingMode

    live = baseline.config.model_copy(
        update={"trading": baseline.config.trading.model_copy(update={"mode": TradingMode.LIVE})}
    )
    assert "paper_trading_only" in _unmet(_evaluate(baseline, config=live))


# ── report file and CLI ───────────────────────────────────────────────────────
def test_report_is_written_privately_and_rendered(baseline):
    path = write_report(_evaluate(baseline), baseline.root)
    assert path == baseline.root / REPORT_RELATIVE
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    text = render_report(json.loads(path.read_text(encoding="utf-8")))
    assert "READY" in text and "[PASS] backtest_evidence" in text
    assert "not evidence of profitability" in text


def test_readiness_cli_reports_and_signals_by_exit_code(baseline, monkeypatch, capsys):
    from trading_assistant import installation
    from trading_assistant.autopilot import cli

    monkeypatch.setattr(installation, "source_root", lambda: baseline.root)
    assert cli.main(["readiness"]) == 1
    assert "no readiness report yet" in capsys.readouterr().out

    write_report(_evaluate(baseline), baseline.root)
    assert cli.main(["readiness"]) == 0
    write_report(_evaluate(baseline, tenure_guard=None), baseline.root)
    assert cli.main(["readiness"]) == 1
    assert "[FAIL] runtime_ownership" in capsys.readouterr().out


@pytest.mark.parametrize("flag", [[], ["--once"], ["--startup-attempts", "3"]])
def test_retired_standalone_trading_entry_points_refuse(flag, capsys):
    from trading_assistant.autopilot import cli

    assert cli.main(flag) == 2
    assert "daemon hosts the autopilot" in capsys.readouterr().err


# ── backtest evidence ─────────────────────────────────────────────────────────
def _frames(symbols, days=800):
    index = pd.bdate_range(end="2026-10-08", periods=days, tz="UTC") + pd.Timedelta(hours=4)
    rng = np.random.default_rng(7)
    frames = {}
    for offset, symbol in enumerate(symbols):
        walk = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.015, days))) + offset
        frames[symbol] = pd.DataFrame(
            {"open": walk, "high": walk * 1.01, "low": walk * 0.99, "close": walk,
             "volume": np.full(days, 1e6)},
            index=index.rename("ts"),
        )
    return frames


def test_backtest_evidence_records_provenance_and_satisfies_the_gate(app_config, store, tmp_path):
    from trading_assistant.autopilot.backtest_evidence import record_backtest_evidence

    evidence, service = store
    config = _approved(_paper_config(app_config))
    root = _root(tmp_path)
    detail = record_backtest_evidence(
        config,
        _frames(["AAPL", "MSFT", "SPY"]),
        store=evidence,
        root=root,
        now=NOW,
        data_source="alpaca",  # test frames standing in for downloaded bars
    )
    assert detail["strategy"] == "sma_trend"
    assert detail["symbols"] == ["AAPL", "MSFT"]
    assert detail["calendar_days"] >= 730
    assert detail["holdout_evaluated"] is True
    assert detail["code_identity"] == COMMIT
    assert {row["window"] for row in detail["rows"]} >= {"development", "holdout"}
    assert "Simulated" in detail["disclaimer"]

    _set_created(evidence, BACKTEST_ACTION, NOW - timedelta(days=1))
    report = evaluate_readiness(
        config=config, store=evidence, root=root, now=NOW,
        tenure_guard=SimpleNamespace(lost=False, closed=False),
        breakers=service.breakers, installation_check=lambda _r: None,
    )
    assert next(r for r in report.requirements if r.name == "backtest_evidence").passed


@pytest.mark.parametrize(
    ("symbols", "source", "message"),
    [
        (["AAPL", "MSFT", "SPY"], "synthetic", "real market data"),
        (["AAPL", "SPY"], "alpaca", "missing bars for MSFT"),
    ],
)
def test_backtest_evidence_rejects_synthetic_or_incomplete_data(app_config, store, tmp_path, symbols, source, message):
    from trading_assistant.autopilot.backtest_evidence import record_backtest_evidence

    evidence, _service = store
    with pytest.raises(ValueError, match=message):
        record_backtest_evidence(
            _paper_config(app_config), _frames(symbols), store=evidence,
            root=_root(tmp_path), now=NOW, data_source=source,
        )
    assert evidence.backtests() == []
