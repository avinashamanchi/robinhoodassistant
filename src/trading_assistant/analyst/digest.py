"""Morning digest (D2): a plain-text summary sent at market open if Telegram is on.

Pulls only from already-computed state (no LLM), so it is cheap and safe to send
on a schedule.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select


def compose_digest(service, *, shadow=None, screen_source=None) -> str:
    lines = ["📈 Trading Assistant — morning digest"]

    acct = service.broker.get_account()
    lines.append(f"Equity ${acct.equity} · buying power ${acct.buying_power}")

    positions = service.get_positions()
    if positions:
        lines.append("Positions:")
        for p in positions:
            lines.append(f"  {p['ticker']} {p['qty']} @ {p['current_price']} (val {p['market_value']})")
    else:
        lines.append("Positions: none")

    pending = service.get_pending()
    lines.append(f"Pending approvals: {len(pending)}")

    from ..db.models import Rule
    with service.session_factory() as s:
        active = s.execute(select(func.count()).select_from(Rule).where(Rule.state == "active")).scalar_one()
    lines.append(f"Active rules: {active}")

    health = service.health()
    ks = health.get("killswitch", {})
    lines.append(f"Kill switch — equity: {'TRIPPED' if ks.get('equity') else 'ok'} · crypto: {'TRIPPED' if ks.get('crypto') else 'ok'}")

    if screen_source is not None:
        from . import screener
        universe = service.config.screener.universe or service.config.risk.ticker_allowlist
        top = screener.screen_source(screen_source, [s.upper() for s in universe], spy_symbol="SPY", top_n=3)
        if top:
            lines.append("Top screener candidates:")
            for c in top:
                lines.append(f"  {c['symbol']} score {c['score']} ({c['regime']})")

    if shadow is not None:
        pend = shadow.pending()
        lines.append(f"Shadow calls pending grade: {len(pend)}")

    from ..analyst.store import promotion_status
    with service.session_factory() as s:
        status = promotion_status(s, version=service.config.analyst.version)
    sc = status["scorecard"]
    lines.append(f"Scorecard: {sc['n_calls']} graded, {sc['accuracy']:.0%} accuracy — {status['reason']}")

    return "\n".join(lines)


def send_digest(service, notifier, *, shadow=None, screen_source=None) -> bool:
    return notifier.send(compose_digest(service, shadow=shadow, screen_source=screen_source))
