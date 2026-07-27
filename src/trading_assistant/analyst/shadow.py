"""Shadow mode (D1) — build a live graded track record at zero risk.

Each market morning: screen the universe, run the top-N candidates through the
analyst, store the fully-sized plans as SHADOW (never approved, never ordered),
and grade them once their horizon elapses. This accumulates the 50-graded-calls
track record on live data in parallel with manual paper trading — nothing trades.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable, Optional

from sqlalchemy import select

from . import screener
from .models import AnalysisReport, AnalystAction, TradePlan
from .store import grade_report, save_report

log = logging.getLogger(__name__)


def _base_report(plan: TradePlan) -> AnalysisReport:
    """Project a TradePlan onto the base report the scorecard grades (NO_TRADE->hold)."""
    action = plan.action.value if plan.action.value in ("buy", "sell", "hold") else "hold"
    return AnalysisReport(
        symbol=plan.symbol, as_of=plan.as_of, action=AnalystAction(action),
        confidence=plan.confidence, thesis=plan.thesis,
        cited_concepts=plan.cited_concepts, regime_note=plan.regime_note,
        earnings_note=plan.earnings_note, correlation_note=plan.correlation_note,
    )

DEFAULT_HORIZON_DAYS = 5


def _request_id_for_daily_call(
    scheduled_date: date,
    analyst_version: str,
    symbol: str,
) -> str:
    material = json.dumps(
        {
            "analyst_version": analyst_version,
            "scheduled_date": scheduled_date.isoformat(),
            "symbol": symbol.strip().upper(),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = base64.urlsafe_b64encode(
        hashlib.sha256(material.encode("utf-8")).digest()
    ).decode("ascii").rstrip("=")
    return f"shadow:{digest}"


class ShadowRunner:
    def __init__(
        self,
        service,
        planning,
        screen_source,
        price_lookup: Callable[[str], Optional[Decimal]],
        *,
        top_n: int = 3,
        spy_symbol: str = "SPY",
    ) -> None:
        self.service = service
        self.planning = planning
        self.screen_source = screen_source
        self.price_lookup = price_lookup
        self.top_n = top_n
        self.spy_symbol = spy_symbol

    def _base_horizon(self, plan: TradePlan) -> int:
        for s in plan.scenarios:
            if s.name == "base":
                return s.horizon_days
        return DEFAULT_HORIZON_DAYS

    def run_once(self) -> list[int]:
        """Screen + analyze the top candidates into shadow plans. No orders."""
        from ..db.models import AnalysisReportRow, ShadowCall, TradePlanRow, utcnow

        universe = self.service.config.screener.universe or self.service.config.risk.ticker_allowlist
        candidates = screener.screen_source(
            self.screen_source, [s.upper() for s in universe],
            spy_symbol=self.spy_symbol, top_n=self.top_n,
        )
        now = utcnow()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        version = self.service.config.analyst.version
        with self.service.session_factory() as s:
            completed_symbols = set(
                s.execute(
                    select(ShadowCall.symbol)
                    .join(
                        AnalysisReportRow,
                        AnalysisReportRow.id == ShadowCall.report_id,
                    )
                    .where(
                        ShadowCall.created_at >= day_start,
                        AnalysisReportRow.analyst_version == version,
                    )
                ).scalars()
            )
        plan_ids: list[int] = []
        for c in candidates:
            if c["symbol"] in completed_symbols:
                continue
            request_id = _request_id_for_daily_call(
                day_start.date(),
                version,
                c["symbol"],
            )
            try:
                # Stores a shadow plan only; it never creates or executes an order.
                out = self.planning.analyze(
                    c["symbol"],
                    actor="daemon:shadow",
                    reason="scheduled shadow plan analysis",
                    request_id=request_id,
                )
            except Exception:
                log.error(
                    "shadow analysis failed code=analysis_failed "
                    "symbol=%s; continuing batch",
                    c["symbol"],
                )
                continue
            plan_id = out["plan_id"]
            with self.service.session_factory() as s:
                row = s.get(TradePlanRow, plan_id)
                row.shadow = True
                plan = TradePlan.model_validate_json(row.plan_json)
                report_id = save_report(
                    s, _base_report(plan), version=self.service.config.analyst.version
                )
                s.add(ShadowCall(
                    report_id=report_id, symbol=plan.symbol,
                    reference_price=plan.reference_price,
                    grade_after=utcnow() + timedelta(days=self._base_horizon(plan)),
                ))
                s.commit()
            plan_ids.append(plan_id)
            completed_symbols.add(plan.symbol)
        return plan_ids

    def grade_due(self, now=None) -> int:
        """Grade matured shadow calls into the scorecard (forward return vs entry)."""
        from ..db.models import ShadowCall, utcnow

        now = now or utcnow()
        graded = 0
        with self.service.session_factory() as s:
            due = s.execute(
                select(ShadowCall).where(ShadowCall.graded == False, ShadowCall.grade_after <= now)  # noqa: E712
            ).scalars().all()
            for sc in due:
                price = self.price_lookup(sc.symbol)
                if price is None:
                    continue
                fwd = float((Decimal(str(price)) - sc.reference_price) / sc.reference_price * 100)
                grade_report(s, sc.report_id, fwd)
                sc.graded = True
                graded += 1
            s.commit()
        return graded

    def pending(self) -> list[dict]:
        from ..db.models import ShadowCall

        with self.service.session_factory() as s:
            rows = s.execute(select(ShadowCall).where(ShadowCall.graded == False)).scalars().all()  # noqa: E712
            return [{"symbol": r.symbol, "grade_after": r.grade_after.isoformat()} for r in rows]
