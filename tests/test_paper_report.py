"""Paper evaluation report: FIFO P/L arithmetic, the proposal funnel, read-only access."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from trading_assistant.db.models import Fill, Order, Proposal, Rule, RuleGroup
from trading_assistant.ops import paper_report
from trading_assistant.ops.paper_report import (
    FillRecord,
    autopilot_summary,
    build_report,
    fifo_pnl,
    max_drawdown,
    read_only_session_factory,
)
from trading_assistant.security.sensitive_fields import persist_sensitive

T0 = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)


def _fill(side, qty, price, minutes, ticker="AAPL"):
    return FillRecord(ticker, side, Decimal(qty), Decimal(price), T0 + timedelta(minutes=minutes))


def test_fifo_pairs_lots_in_order_and_tracks_drawdown_turnover_and_exposure():
    pnl = fifo_pnl(
        [
            _fill("buy", "2", "100", 0),
            _fill("buy", "1", "110", 1),
            _fill("sell", "2", "120", 2),  # closes the two 100 lots: +40
            _fill("sell", "1", "105", 3),  # closes the 110 lot: -5
        ]
    )

    assert [(t.qty, t.entry_price, t.pnl) for t in pnl.trades] == [
        (Decimal("2"), Decimal("100"), Decimal("40")),
        (Decimal("1"), Decimal("110"), Decimal("-5")),
    ]
    assert [value for _at, value in pnl.curve] == [Decimal("40"), Decimal("35")]
    assert max_drawdown(pnl.curve) == Decimal("5")
    assert pnl.turnover == Decimal("655")
    assert pnl.peak_gross_exposure == Decimal("310")
    assert pnl.open_lots == {}


def test_fills_before_the_window_build_basis_but_are_not_reported():
    pnl = fifo_pnl(
        [_fill("buy", "1", "100", 0), _fill("sell", "1", "90", 60 * 24)],
        since=T0 + timedelta(hours=12),
    )

    (trade,) = pnl.trades
    assert trade.pnl == Decimal("-10") and trade.entry_price == Decimal("100")
    assert pnl.turnover == Decimal("90")


def test_an_open_position_and_a_short_are_kept_as_lots():
    pnl = fifo_pnl([_fill("buy", "3", "100", 0), _fill("sell", "1", "50", 1, ticker="MSFT")])

    assert {ticker: [lot.qty for lot in lots] for ticker, lots in pnl.open_lots.items()} == {
        "AAPL": [Decimal("3")],
        "MSFT": [Decimal("-1")],
    }


def _seed(session_factory):
    rows = [
        # key, status, approved, submitted, source_rule_id
        ("ap-auto-filled", "filled", True, True, None),
        ("ap-auto-expired", "expired", False, False, None),
        ("human-rejected", "rejected", False, False, None),
        ("rule-awaiting", "proposed", False, False, 7),
        ("human-broker-rejected", "rejected", True, True, None),
    ]
    with session_factory() as session:
        group = RuleGroup(group_key="fixture-group")
        session.add(group)
        session.flush()
        rule = Rule(group_id=group.id, ticker="AAPL", condition_json="{}", action_json="{}")
        session.add(rule)
        session.flush()
        for index, (key, status, approved, submitted, rule_id) in enumerate(rows):
            rule_id = rule.id if rule_id is not None else None
            order = Order(
                idempotency_key=key,
                ticker="AAPL",
                side="buy",
                order_type="market",
                status=status,
                qty=Decimal("1"),
                approved_at=T0 if approved else None,
                submission_started_at=T0 if submitted else None,
                created_at=T0 + timedelta(minutes=index),
                updated_at=T0 + timedelta(minutes=index),
            )
            persist_sensitive(session, order, {"approval_reason": "fixture"})
            proposal = Proposal(
                order_id=order.id,
                source_rule_id=rule_id,
                ttl_minutes=15,
                created_at=T0,
                expires_at=T0 + timedelta(minutes=15),
            )
            persist_sensitive(session, proposal, {"reasoning": f"why {key}"})
        filled = session.query(Order).filter_by(idempotency_key="ap-auto-filled").one()
        for side, price, minutes in (("buy", "100", 5), ("sell", "103", 90)):
            session.add(
                Fill(
                    order_id=filled.id,
                    ticker="AAPL",
                    side=side,
                    qty=Decimal("1"),
                    price=Decimal(price),
                    broker_fill_id=f"fill-{side}",
                    reconciliation_state="trusted",
                    filled_at=T0 + timedelta(minutes=minutes),
                )
            )
        session.commit()


def test_report_reads_the_funnel_and_pnl_through_a_read_only_connection(session_factory, tmp_path):
    _seed(session_factory)
    factory = read_only_session_factory(tmp_path / "test.db")

    with factory() as session:
        report = build_report(session, marks={"AAPL": Decimal("110")})
        session.add(Order(idempotency_key="must-not-write", ticker="AAPL", side="buy",
                          order_type="market", status="proposed", approval_reason=""))
        with pytest.raises(OperationalError, match="readonly"):
            session.flush()

    funnel = report["funnel"]
    assert funnel["proposals"] == 5
    assert funnel["by_source"] == {"autopilot": 2, "human_or_chat": 2, "rule": 1}
    assert (funnel["approved"], funnel["submitted"]) == (2, 2)
    assert funnel["outcomes"] == {
        "awaiting_approval": 1,
        "expired_unapproved": 1,
        "filled": 1,
        "rejected_before_submission": 1,
        "rejected_by_broker": 1,
    }
    pnl = report["pnl"]
    assert (pnl["realized"], pnl["wins"], pnl["losses"]) == ("3.00", 1, 0)
    assert report["open_positions"] == []
    assert "Alpaca paper fills are simulated" in report["caveats"][0]
    assert "PAPER EVALUATION REPORT" in paper_report.render_text(report)


def test_untrusted_fills_are_excluded_and_counted(session_factory, tmp_path):
    with session_factory() as session:
        session.add(Fill(ticker="AAPL", side="buy", qty=Decimal("1"), price=Decimal("100"),
                         reconciliation_state="quarantined", filled_at=T0))
        session.commit()

    with read_only_session_factory(tmp_path / "test.db")() as session:
        report = build_report(session)

    assert report["fills"] == {"trusted": 0, "excluded_untrusted": 1}


def test_autopilot_summary_lists_what_each_session_would_have_done():
    cycles = [
        SimpleNamespace(result_code="observed", detail={
            "session": "2026-10-06", "mode": "observe",
            "decisions": [{"reason": "enter_long"}, {"reason": "signal_hold"}],
            "intended_actions": [{"symbol": "AAPL", "side": "buy", "notional": "100"}],
            "orders": [],
        }),
        SimpleNamespace(result_code="degraded", detail={
            "session": "2026-10-07", "mode": "observe",
            "decisions": [{"reason": "features_stale"}], "intended_actions": [], "orders": [],
        }),
    ]

    summary = autopilot_summary(cycles)

    assert summary["results"] == {"degraded": 1, "observed": 1}
    assert summary["decision_reasons"] == {"enter_long": 1, "features_stale": 1, "signal_hold": 1}
    assert summary["sessions"][0]["intended"] == [{"symbol": "AAPL", "side": "buy", "notional": "100"}]


def test_report_refuses_a_database_that_was_not_chosen(monkeypatch, tmp_path, capsys):
    from trading_assistant.security import secrets as secrets_module
    from trading_assistant.security.secrets import RuntimeSecrets

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    database = tmp_path / "trading_assistant.db"
    database.write_bytes(b"")
    monkeypatch.setattr(
        secrets_module,
        "load_role_secrets",
        lambda role, *, config: RuntimeSecrets(database_url=f"sqlite:///{database}"),
    )

    assert paper_report.main([]) == 1
    assert "paper report refused: database_not_chosen" in capsys.readouterr().err


def test_autopilot_evidence_decrypts_through_the_read_only_connection(
    session_factory, sensitive_cipher, tmp_path
):
    from datetime import date

    from trading_assistant.autopilot.evidence import EvidenceStore
    from trading_assistant.security.sensitive_fields import bind_sensitive_cipher

    EvidenceStore(session_factory, actor="autopilot:sma_trend").record_cycle(
        key="ap-cycle-fixture",
        session=date(2026, 10, 6),
        result_code="observed",
        detail={"session": "2026-10-06", "mode": "observe", "decisions": [], "orders": []},
    )
    factory = read_only_session_factory(tmp_path / "test.db")
    bind_sensitive_cipher(factory, sensitive_cipher)

    summary = autopilot_summary(EvidenceStore(factory, actor="paper-report").cycles())

    assert summary["cycles"] == 1 and summary["results"] == {"observed": 1}


def test_json_export_is_private_from_creation_and_never_overwrites(tmp_path):
    target = tmp_path / "report.json"
    old_umask = os.umask(0)
    try:
        paper_report.write_private_json(target, {"pnl": {"realized": "1.00"}})
    finally:
        os.umask(old_umask)

    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        paper_report.write_private_json(target, {})
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "elsewhere.json")
    with pytest.raises(FileExistsError):
        paper_report.write_private_json(link, {})
    assert not (tmp_path / "elsewhere.json").exists()
