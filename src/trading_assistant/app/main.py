"""FastAPI host: chat, pending-approval queue, approve/reject, positions, log.

``create_app`` accepts an injected service + agent (tests use mocks). With none
provided it builds the real stack from config/secrets. The approval endpoint is
the only path that can execute — and it runs the risk engine one final time
inside TradingService.approve_order.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..config import Secrets, load_config
from ..db.models import create_all
from ..db.session import create_db_engine, make_session_factory
from ..service import TradingService
from .agent import Agent, AnthropicBackend
from .ratelimit import RateLimiter

_STATIC = Path(__file__).parent / "static"


class ChatIn(BaseModel):
    message: str


class BacktestRunIn(BaseModel):
    symbols: list[str] = []


def build_default_stack() -> tuple[TradingService, Agent]:
    from ..broker.factory import build_broker, build_clock

    config = load_config()
    secrets = Secrets()
    engine = create_db_engine(secrets.database_url)
    create_all(engine)
    session_factory = make_session_factory(engine)
    broker = build_broker(config, secrets)
    clock = build_clock(config, secrets)
    service = TradingService(broker, session_factory, config, clock)
    backend = AnthropicBackend(
        secrets.anthropic_api_key, config.llm.model, config.llm.max_tokens
    )
    agent = Agent(
        backend, service, session_factory, config.llm.model, config.llm.max_tokens
    )
    return service, agent


def create_app(
    service: Optional[TradingService] = None,
    agent: Optional[Agent] = None,
    *,
    chat_rate: RateLimiter | None = None,
    approve_rate: RateLimiter | None = None,
) -> FastAPI:
    if service is None or agent is None:
        service, agent = build_default_stack()

    chat_rate = chat_rate or RateLimiter(max_requests=20, window_seconds=60)
    approve_rate = approve_rate or RateLimiter(max_requests=30, window_seconds=60)

    app = FastAPI(title="Trading Assistant")

    def _client(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (_STATIC / "index.html").read_text(encoding="utf-8")

    @app.post("/chat")
    def chat(body: ChatIn, request: Request):
        if not chat_rate.allow(_client(request)):
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        return agent.chat(body.message)

    @app.get("/pending")
    def pending():
        return {"pending": service.get_pending()}

    @app.post("/approve/{order_id}")
    def approve(order_id: int, request: Request):
        if not approve_rate.allow(_client(request)):
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        result = service.approve_order(order_id)
        # Surface a conflict (already-decided) as HTTP 409 for the atomic guarantee.
        if result.get("error", "").startswith("order not in PROPOSED"):
            raise HTTPException(status_code=409, detail=result)
        return result

    @app.post("/reject/{order_id}")
    def reject(order_id: int):
        return service.reject_order(order_id)

    @app.get("/positions")
    def positions():
        return {"positions": service.get_positions()}

    @app.get("/log")
    def log():
        return service.get_log()

    @app.post("/killswitch/reset")
    def killswitch_reset():
        return service.reset_killswitch()

    @app.post("/orders/{order_id}/cancel")
    def cancel_order(order_id: int):
        return service.cancel_live_order(order_id)

    @app.post("/reconcile")
    def reconcile():
        return service.reconcile_positions()

    @app.get("/analyst/scorecard")
    def analyst_scorecard():
        from ..analyst.store import promotion_status

        with service.session_factory() as s:
            return promotion_status(s)

    # ── backtests (Phase 7) ────────────────────────────────────
    @app.get("/backtests")
    def list_backtests():
        return {"backtests": _list_backtests(service.session_factory)}

    @app.post("/backtests/run")
    def run_backtest_endpoint(body: BacktestRunIn):
        from ..backtest.runner import run_synthetic_backtest

        run_id, report = run_synthetic_backtest(
            service.session_factory, symbols=body.symbols or None
        )
        return {"run_id": run_id, "report": report.to_dict()}

    @app.get("/backtests/{run_id}/report")
    def backtest_report(run_id: int):
        report = _load_backtest_report(service.session_factory, run_id)
        if report is None:
            raise HTTPException(status_code=404, detail="run not found")
        return report

    @app.get("/backtests/ui", response_class=HTMLResponse)
    def backtests_ui() -> str:
        return (_STATIC / "backtests.html").read_text(encoding="utf-8")

    return app


# ── backtest DB helpers ────────────────────────────────────────
def _list_backtests(session_factory) -> list[dict]:
    from sqlalchemy import select

    from ..db.models import BacktestRun

    with session_factory() as s:
        runs = s.execute(select(BacktestRun).order_by(BacktestRun.id.desc())).scalars().all()
        return [
            {
                "run_id": r.id,
                "label": r.label,
                "created_at": r.created_at.isoformat(),
                "holdout_start": r.holdout_start.isoformat() if r.holdout_start else None,
            }
            for r in runs
        ]


def _load_backtest_report(session_factory, run_id: int) -> Optional[dict]:
    import json

    from sqlalchemy import select

    from ..backtest.report import SIMULATED_LABEL
    from ..db.models import BacktestMetricRow, BacktestRun

    with session_factory() as s:
        run = s.get(BacktestRun, run_id)
        if run is None:
            return None
        rows = s.execute(
            select(BacktestMetricRow).where(BacktestMetricRow.run_id == run_id)
        ).scalars().all()
        return {
            "run_id": run.id,
            "label": run.label,
            "holdout_start": run.holdout_start.isoformat() if run.holdout_start else None,
            "disclaimer": SIMULATED_LABEL,
            "rows": [json.loads(r.metrics_json) for r in rows],
        }
