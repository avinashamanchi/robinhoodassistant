"""Paper-trading evaluation report: funnel, fills, P/L and what the autopilot did.

Read-only. It opens only the explicitly chosen runtime database, and opens it
with SQLite ``mode=ro``, so it can neither write nor convert it. Funnel and P/L
use plaintext columns only (no keys). ``--autopilot-evidence`` additionally
decrypts the autopilot's cycle evidence with the field keys from the Keychain
(the analysis role: database and keys, no broker credentials).

    python -m trading_assistant.ops.paper_report \\
        [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--mark SYMBOL=PRICE ...] \\
        [--autopilot-evidence] [--json PATH]

What the numbers are: orders and fills recorded from Alpaca **paper**
responses. Paper fills are simulated by Alpaca; they say nothing about live
execution quality, and a short sample says nothing about profitability.
Realized P/L pairs fills first-in, first-out per symbol; unrealized P/L is
computed only for symbols given a ``--mark`` price.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Iterable, Optional

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from ..db.models import Fill, Order, fill_has_trusted_identity

_SUBMITTED_OPEN = {"submitting", "submitted", "partially_filled", "acceptance_unknown"}
_ZERO = Decimal(0)


def read_only_session_factory(database: Path) -> sessionmaker[Session]:
    """Sessions over ``database`` opened read-only (no WAL conversion, no writes)."""
    uri = f"{Path(database).resolve(strict=True).as_uri()}?mode=ro"
    engine = create_engine(
        "sqlite://",
        creator=lambda: sqlite3.connect(uri, uri=True),
        poolclass=NullPool,
        future=True,
    )
    return sessionmaker(bind=engine, future=True)


@dataclass(frozen=True)
class FillRecord:
    ticker: str
    side: str
    qty: Decimal
    price: Decimal
    filled_at: datetime
    trusted: bool = True


@dataclass(frozen=True)
class ClosedTrade:
    ticker: str
    qty: Decimal
    entry_price: Decimal
    exit_price: Decimal
    pnl: Decimal
    opened_at: datetime
    closed_at: datetime


@dataclass
class _Lot:
    qty: Decimal  # signed: positive long, negative short
    price: Decimal
    opened_at: datetime


@dataclass
class PnL:
    trades: list[ClosedTrade] = field(default_factory=list)
    open_lots: dict[str, list[_Lot]] = field(default_factory=dict)
    curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    turnover: Decimal = _ZERO
    peak_gross_exposure: Decimal = _ZERO


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def fifo_pnl(fills: Iterable[FillRecord], *, since: Optional[datetime] = None) -> PnL:
    """Pair fills first-in, first-out per symbol.

    Every fill before ``since`` still builds cost basis; only trades closed at
    or after ``since`` (and fills at or after it, for turnover) are reported.
    """
    result = PnL()
    lots: dict[str, deque[_Lot]] = defaultdict(deque)
    realized = _ZERO
    for fill in sorted(fills, key=lambda f: _aware(f.filled_at)):
        at = _aware(fill.filled_at)
        in_window = since is None or at >= since
        signed = fill.qty if fill.side == "buy" else -fill.qty
        if in_window:
            result.turnover += abs(fill.qty * fill.price)
        book = lots[fill.ticker]
        remaining = signed
        closed_qty = _ZERO
        closed_pnl = _ZERO
        entry_cost = _ZERO
        opened_at = at
        while remaining != 0 and book and (book[0].qty > 0) != (remaining > 0):
            lot = book[0]
            matched = min(abs(lot.qty), abs(remaining))
            direction = Decimal(1) if lot.qty > 0 else Decimal(-1)
            closed_pnl += (fill.price - lot.price) * matched * direction
            entry_cost += lot.price * matched
            closed_qty += matched
            opened_at = min(opened_at, lot.opened_at)
            lot.qty -= matched * direction
            remaining += matched * direction
            if lot.qty == 0:
                book.popleft()
        if remaining != 0:
            book.append(_Lot(qty=remaining, price=fill.price, opened_at=at))
        if closed_qty and in_window:
            realized += closed_pnl
            result.trades.append(
                ClosedTrade(
                    ticker=fill.ticker,
                    qty=closed_qty,
                    entry_price=entry_cost / closed_qty,
                    exit_price=fill.price,
                    pnl=closed_pnl,
                    opened_at=opened_at,
                    closed_at=at,
                )
            )
            result.curve.append((at, realized))
        gross = sum(
            (abs(lot.qty) * lot.price for book in lots.values() for lot in book),
            _ZERO,
        )
        result.peak_gross_exposure = max(result.peak_gross_exposure, gross)
    result.open_lots = {ticker: list(book) for ticker, book in lots.items() if book}
    return result


def max_drawdown(curve: list[tuple[datetime, Decimal]]) -> Decimal:
    """Largest fall from a running peak of cumulative realized P/L (peak starts at 0)."""
    peak = _ZERO
    worst = _ZERO
    for _at, value in curve:
        peak = max(peak, value)
        worst = max(worst, peak - value)
    return worst


def _source(order: Order) -> str:
    if order.idempotency_key.startswith("ap-"):
        return "autopilot"
    proposal = order.proposal
    if proposal is not None and proposal.source_rule_id is not None:
        return "rule"
    return "human_or_chat"


def _funnel(orders: list[Order]) -> dict[str, object]:
    proposals = [order for order in orders if order.proposal is not None]
    outcomes: Counter[str] = Counter()
    for order in proposals:
        submitted = order.submission_started_at is not None
        if order.status == "proposed":
            outcomes["awaiting_approval"] += 1
        elif order.status == "expired" and not submitted:
            outcomes["expired_unapproved"] += 1
        elif order.status == "rejected" and not submitted:
            outcomes["rejected_before_submission"] += 1
        elif submitted and order.status == "filled":
            outcomes["filled"] += 1
        elif submitted and order.status == "rejected":
            outcomes["rejected_by_broker"] += 1
        elif submitted and order.status in ("canceled", "expired"):
            outcomes[f"{order.status}_after_submission"] += 1
        elif submitted and order.status in _SUBMITTED_OPEN:
            outcomes[f"open_{order.status}"] += 1
        else:
            outcomes[f"other_{order.status}"] += 1
    return {
        "proposals": len(proposals),
        "by_source": dict(Counter(_source(order) for order in proposals)),
        "approved": sum(1 for order in proposals if order.approved_at is not None),
        "submitted": sum(1 for order in proposals if order.submission_started_at is not None),
        "outcomes": dict(sorted(outcomes.items())),
        "orders_without_proposal": len(orders) - len(proposals),
    }


def _decimal(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


def build_report(
    session: Session,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    marks: Optional[dict[str, Decimal]] = None,
) -> dict[str, object]:
    order_query = select(Order)
    if since is not None:
        order_query = order_query.where(Order.created_at >= since)
    if until is not None:
        order_query = order_query.where(Order.created_at < until)
    orders = list(session.scalars(order_query))
    fill_query = select(Fill)
    if until is not None:
        fill_query = fill_query.where(Fill.filled_at < until)
    fills = [
        FillRecord(
            ticker=fill.ticker,
            side=fill.side,
            qty=Decimal(fill.qty),
            price=Decimal(fill.price),
            filled_at=fill.filled_at,
            trusted=fill_has_trusted_identity(fill),
        )
        for fill in session.scalars(fill_query)
    ]
    trusted = [fill for fill in fills if fill.trusted]
    pnl = fifo_pnl(trusted, since=since)
    wins = [trade.pnl for trade in pnl.trades if trade.pnl > 0]
    losses = [trade.pnl for trade in pnl.trades if trade.pnl < 0]
    realized = sum((trade.pnl for trade in pnl.trades), _ZERO)
    open_positions = []
    unrealized_total: Optional[Decimal] = _ZERO
    for ticker, book in sorted(pnl.open_lots.items()):
        qty = sum((lot.qty for lot in book), _ZERO)
        cost = sum((lot.qty * lot.price for lot in book), _ZERO)
        mark = (marks or {}).get(ticker)
        unrealized = None if mark is None else mark * qty - cost
        if unrealized is None:
            unrealized_total = None
        elif unrealized_total is not None:
            unrealized_total += unrealized
        open_positions.append(
            {
                "ticker": ticker,
                "qty": str(qty),
                "avg_cost": _decimal(cost / qty) if qty else "0.00",
                "mark": None if mark is None else str(mark),
                "unrealized": None if unrealized is None else _decimal(unrealized),
            }
        )
    return {
        "window": {
            "since": None if since is None else since.isoformat(),
            "until": None if until is None else until.isoformat(),
        },
        "funnel": _funnel(orders),
        "fills": {
            "trusted": len(trusted),
            "excluded_untrusted": len(fills) - len(trusted),
        },
        "pnl": {
            "realized": _decimal(realized),
            "closed_trades": len(pnl.trades),
            "wins": len(wins),
            "losses": len(losses),
            "average_winner": None if not wins else _decimal(sum(wins, _ZERO) / len(wins)),
            "average_loser": None if not losses else _decimal(sum(losses, _ZERO) / len(losses)),
            "max_drawdown_realized": _decimal(max_drawdown(pnl.curve)),
            "turnover": _decimal(pnl.turnover),
            "peak_gross_exposure_at_cost": _decimal(pnl.peak_gross_exposure),
            "unrealized": None if unrealized_total is None else _decimal(unrealized_total),
        },
        "realized_curve": [
            {"at": at.isoformat(), "cumulative_realized": _decimal(value)}
            for at, value in pnl.curve
        ],
        "open_positions": open_positions,
        "trades": [
            {
                "ticker": trade.ticker,
                "qty": str(trade.qty),
                "entry": _decimal(trade.entry_price),
                "exit": _decimal(trade.exit_price),
                "pnl": _decimal(trade.pnl),
                "opened_at": trade.opened_at.isoformat(),
                "closed_at": trade.closed_at.isoformat(),
            }
            for trade in pnl.trades
        ],
        "caveats": [
            "Alpaca paper fills are simulated; no live execution quality is implied.",
            "Realized P/L pairs trusted fills first-in, first-out; fees are not modeled.",
            "Drawdown is on realized P/L only; account equity history is not recorded.",
            "A handful of trades cannot distinguish skill from chance.",
        ],
    }


def autopilot_summary(cycles: list[object]) -> dict[str, object]:
    """What the autopilot decided per session, from its (decrypted) evidence."""
    sessions = []
    reasons: Counter[str] = Counter()
    results: Counter[str] = Counter()
    for record in sorted(cycles, key=lambda r: str(getattr(r, "detail", {}).get("session"))):
        detail = record.detail
        results[record.result_code] += 1
        for decision in detail.get("decisions", []):
            reasons[str(decision.get("reason"))] += 1
        sessions.append(
            {
                "session": detail.get("session"),
                "mode": detail.get("mode"),
                "result": record.result_code,
                "intended": detail.get("intended_actions", []),
                "orders": detail.get("orders", []),
            }
        )
    return {
        "cycles": len(cycles),
        "results": dict(sorted(results.items())),
        "decision_reasons": dict(sorted(reasons.items())),
        "sessions": sessions,
    }


def render_text(report: dict[str, object]) -> str:
    funnel = report["funnel"]
    pnl = report["pnl"]
    lines = [
        "PAPER EVALUATION REPORT (Alpaca paper; simulated fills)",
        f"window: {report['window']['since'] or 'start'} .. {report['window']['until'] or 'now'}",
        "",
        "Funnel",
        f"  proposals {funnel['proposals']} by source {funnel['by_source']}",
        f"  approved {funnel['approved']}  submitted {funnel['submitted']}",
        f"  outcomes {funnel['outcomes']}",
        f"  orders without a proposal row {funnel['orders_without_proposal']}",
        "",
        "Fills and P/L",
        f"  trusted fills {report['fills']['trusted']}"
        f" (excluded untrusted {report['fills']['excluded_untrusted']})",
        f"  realized {pnl['realized']} over {pnl['closed_trades']} closed trades:"
        f" {pnl['wins']} wins, {pnl['losses']} losses",
        f"  average winner {pnl['average_winner']}  average loser {pnl['average_loser']}",
        f"  max drawdown (realized) {pnl['max_drawdown_realized']}  turnover {pnl['turnover']}",
        f"  peak gross exposure at cost {pnl['peak_gross_exposure_at_cost']}",
        f"  unrealized {pnl['unrealized'] if pnl['unrealized'] is not None else 'not computed (pass --mark SYMBOL=PRICE)'}",
    ]
    if report["open_positions"]:
        lines.append("  open positions:")
        for position in report["open_positions"]:
            lines.append(
                f"    {position['ticker']} qty {position['qty']} avg cost {position['avg_cost']}"
                f" mark {position['mark']} unrealized {position['unrealized']}"
            )
    if report["trades"]:
        lines.append("  closed trades:")
        for trade in report["trades"]:
            lines.append(
                f"    {trade['closed_at'][:10]} {trade['ticker']} {trade['qty']} @"
                f" {trade['entry']} -> {trade['exit']} pnl {trade['pnl']}"
            )
    autopilot = report.get("autopilot")
    if autopilot is not None:
        lines += [
            "",
            "Autopilot evidence",
            f"  cycles {autopilot['cycles']} results {autopilot['results']}",
            f"  decision reasons {autopilot['decision_reasons']}",
        ]
        for item in autopilot["sessions"]:
            lines.append(
                f"    {item['session']} {item['mode']} {item['result']}"
                f" intended={item['intended']} orders={item['orders']}"
            )
    lines += ["", "Caveats"] + [f"  - {caveat}" for caveat in report["caveats"]]
    return "\n".join(lines)


def _day(value: str) -> datetime:
    return datetime.combine(date.fromisoformat(value), time.min, tzinfo=timezone.utc)


def _mark(value: str) -> tuple[str, Decimal]:
    symbol, _, price = value.partition("=")
    if not symbol or not price:
        raise argparse.ArgumentTypeError("use SYMBOL=PRICE")
    return symbol.upper(), Decimal(price)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--since", type=_day)
    parser.add_argument("--until", type=_day)
    parser.add_argument("--mark", type=_mark, action="append", default=[])
    parser.add_argument("--autopilot-evidence", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)

    from ..config import load_config
    from ..installation import InstallationError, require_chosen_database
    from ..security.secrets import load_role_secrets, secret_value

    config = load_config()
    role = "validate-analyst" if args.autopilot_evidence else "watchdog"
    secrets = load_role_secrets(role, config=config)
    try:
        database = require_chosen_database(secret_value(secrets.database_url))
    except InstallationError as error:
        print(f"paper report refused: {error.code}", file=sys.stderr)
        return 1
    factory = read_only_session_factory(database)
    if args.autopilot_evidence:
        from ..security.crypto import build_sensitive_data_cipher
        from ..security.sensitive_fields import bind_sensitive_cipher

        bind_sensitive_cipher(
            factory, build_sensitive_data_cipher(config.encryption, secrets)
        )
    with factory() as session:
        report = build_report(
            session, since=args.since, until=args.until, marks=dict(args.mark)
        )
    if args.autopilot_evidence:
        from ..autopilot.evidence import EvidenceStore

        store = EvidenceStore(factory, actor="paper-report")
        report["autopilot"] = autopilot_summary(store.cycles())
    print(render_text(report))
    if args.json is not None:
        try:
            write_private_json(args.json, report)
        except FileExistsError:
            print(f"paper report: {args.json} exists; not overwritten", file=sys.stderr)
            return 1
    return 0


def write_private_json(path: Path, payload: dict[str, object]) -> None:
    """Create ``path`` owner-only (0600) from the start; never follow a link or overwrite."""
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
