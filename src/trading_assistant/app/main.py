"""FastAPI host: chat, pending-approval queue, approve/reject, positions, log.

``create_app`` accepts an injected service + agent (tests use mocks). With none
provided it builds the real stack from config/secrets. The approval endpoint is
the only path that can execute — and it runs the risk engine one final time
inside TradingService.approve_order.
"""

from __future__ import annotations

import hmac
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..config import Secrets, load_config
from ..db.models import create_all
from ..db.session import create_db_engine, make_session_factory
from ..service import TradingService
from .agent import Agent
from .ratelimit import RateLimiter

_STATIC = Path(__file__).parent / "static"


class ChatIn(BaseModel):
    message: str


class BacktestRunIn(BaseModel):
    symbols: list[str] = []


class AnalyzeIn(BaseModel):
    symbol: str


class ProposeIn(BaseModel):
    n: int = 3


def build_default_stack() -> tuple[TradingService, Agent]:
    from ..broker.factory import build_broker, build_clock
    from ..external_accounts.factory import build_external_source
    from ..logging import register_all_secrets

    config = load_config()
    secrets = Secrets()
    register_all_secrets(secrets)
    engine = create_db_engine(secrets.database_url)
    create_all(engine)
    session_factory = make_session_factory(engine)
    broker = build_broker(config, secrets)
    clock = build_clock(config, secrets)
    service = TradingService(
        broker, session_factory, config, clock,
        external_source=build_external_source(config, secrets),
    )
    from ..llm.factory import build_llm_backend

    backend = build_llm_backend(config, secrets)
    model_label = getattr(config.llm, f"{config.llm.provider}_model", config.llm.model)
    agent = Agent(backend, service, session_factory, model_label, config.llm.max_tokens)
    return service, agent


def _auth_dependency(token: str):
    """Require X-API-Key on mutating endpoints (constant-time). If no token is
    configured, auth is disabled (dev/test) — preflight flags that as a FAIL."""

    def dep(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
        if not token:
            return
        if x_api_key is None or not hmac.compare_digest(str(x_api_key), token):
            raise HTTPException(status_code=401, detail="missing or invalid API key")

    return dep


def create_app(
    service: Optional[TradingService] = None,
    agent: Optional[Agent] = None,
    *,
    planning=None,
    screen_source=None,
    api_token: Optional[str] = None,
    chat_rate: RateLimiter | None = None,
    approve_rate: RateLimiter | None = None,
) -> FastAPI:
    if service is None or agent is None:
        service, agent = build_default_stack()
    if api_token is None:
        api_token = Secrets().app_api_token
    from ..logging import register_secret

    register_secret(api_token)
    auth = Depends(_auth_dependency(api_token))

    _secrets_holder: dict = {}
    if planning is None:
        try:
            from ..analyst.analyst import Analyst
            from ..analyst.live_features import build_live_feature_provider
            from ..analyst.planning import PlanningService
            from ..llm.factory import build_llm_backend

            sec = Secrets()
            _secrets_holder["s"] = sec
            analyst = Analyst(
                build_llm_backend(service.config, sec),
                max_tokens=service.config.llm.max_tokens,
                suppress_ranging=service.config.analyst.suppress_ranging,
            )
            planning = PlanningService(
                service, analyst, build_live_feature_provider(service.config, sec), sec
            )
        except Exception:  # keep the app up; plan endpoints return 503
            planning = None

    chat_rate = chat_rate or RateLimiter(max_requests=20, window_seconds=60)
    approve_rate = approve_rate or RateLimiter(max_requests=30, window_seconds=60)

    app = FastAPI(title="Trading Assistant")
    # Same-origin only. Cross-origin requests carrying the custom X-API-Key header
    # must CORS-preflight; disallowed origins fail preflight -> CSRF vector closed.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["X-API-Key", "Content-Type"],
    )

    def _client(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (_STATIC / "index.html").read_text(encoding="utf-8")

    @app.post("/chat", dependencies=[auth])
    def chat(body: ChatIn, request: Request):
        if not chat_rate.allow(_client(request)):
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        return agent.chat(body.message)

    @app.get("/health")
    def health():  # no auth — for watchdogs/uptime checks
        return service.health()

    @app.get("/pending")
    def pending():
        return {"pending": service.get_pending()}

    @app.post("/approve/{order_id}", dependencies=[auth])
    def approve(order_id: int, request: Request):
        if not approve_rate.allow(_client(request)):
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        result = service.approve_order(order_id)
        # Surface a conflict (already-decided) as HTTP 409 for the atomic guarantee.
        if result.get("error", "").startswith("order not in PROPOSED"):
            raise HTTPException(status_code=409, detail=result)
        return result

    @app.post("/reject/{order_id}", dependencies=[auth])
    def reject(order_id: int):
        return service.reject_order(order_id)

    @app.get("/positions")
    def positions():
        return {"positions": service.get_positions()}

    @app.get("/log")
    def log():
        return service.get_log()

    @app.post("/killswitch/reset", dependencies=[auth])
    def killswitch_reset():
        return service.reset_killswitch()

    @app.post("/orders/{order_id}/cancel", dependencies=[auth])
    def cancel_order(order_id: int):
        return service.cancel_live_order(order_id)

    @app.post("/reconcile", dependencies=[auth])
    def reconcile():
        return service.reconcile_positions()

    @app.post("/sync", dependencies=[auth])
    def sync():  # pull fills/status from the broker (also runs each daemon loop)
        return service.sync_open_orders()

    @app.post("/panic", dependencies=[auth])
    def panic():
        return service.panic()

    @app.get("/analyst/scorecard")
    def analyst_scorecard():
        from ..analyst.store import promotion_status

        with service.session_factory() as s:
            return promotion_status(s, version=service.config.analyst.version)

    # ── plans + screener (Phase 8) ─────────────────────────────
    def _require_planning():
        if planning is None:
            raise HTTPException(status_code=503, detail="analyst/planning not configured (needs LLM + market data)")
        return planning

    @app.post("/analyze", dependencies=[auth])
    def analyze(body: AnalyzeIn):
        return _require_planning().analyze(body.symbol)

    @app.get("/plans")
    def list_plans():
        return {"plans": _require_planning().get_plans()}

    @app.get("/plans/ui", response_class=HTMLResponse)
    def plans_ui() -> str:
        return (_STATIC / "plans.html").read_text(encoding="utf-8")

    @app.get("/plans/{plan_id}")
    def get_plan(plan_id: int):
        plan = _require_planning().get_plan(plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="plan not found")
        return plan

    @app.post("/plans/{plan_id}/approve", dependencies=[auth])
    def approve_plan(plan_id: int):
        result = _require_planning().approve_plan(plan_id)
        if "error" in result and "promotion gate" in result["error"]:
            raise HTTPException(status_code=409, detail=result)
        return result

    @app.post("/plans/{plan_id}/cancel", dependencies=[auth])
    def cancel_plan(plan_id: int):
        return _require_planning().cancel_plan(plan_id)

    def _screen_candidates(top_n: int):
        nonlocal screen_source
        from ..analyst import screener

        universe = service.config.screener.universe or service.config.risk.ticker_allowlist
        if screen_source is None:  # lazily build the live source on first call
            sec = _secrets_holder.get("s")
            if sec is None:
                raise HTTPException(status_code=503, detail="screener source not configured")
            from ..analyst.live_features import build_screen_source

            screen_source = build_screen_source([s.upper() for s in universe], sec)
        return screener.screen_source(
            screen_source, [s.upper() for s in universe], spy_symbol="SPY", top_n=top_n,
        )

    @app.post("/screen", dependencies=[auth])
    def screen():
        return {"candidates": _screen_candidates(service.config.screener.top_n)}

    @app.post("/propose", dependencies=[auth])
    def propose(body: ProposeIn):
        """Screen the market and run the analyst on the top N candidates, creating
        sized plans you can approve. The analyst is UNPROVEN — these are suggestions
        the risk engine still gates; you approve each one."""
        planning = _require_planning()
        candidates = _screen_candidates(max(body.n, service.config.screener.top_n))
        created = []
        for c in candidates[: body.n]:
            try:
                out = planning.analyze(c["symbol"])
                created.append({
                    "plan_id": out["plan_id"], "symbol": c["symbol"],
                    "action": out["plan"]["action"], "score": c["score"],
                    "sized_shares": out["sized"]["total_shares"],
                })
            except Exception as exc:  # skip a bad candidate, keep going
                created.append({"symbol": c["symbol"], "error": type(exc).__name__})
        return {"proposed": created, "note": "analyst v2 is UNPROVEN — review before approving"}

    # ── external (read-only) accounts ──────────────────────────
    @app.get("/holdings")
    def holdings():
        """Combined Alpaca + external holdings, labeled by source (read-only external)."""
        return service.get_combined_holdings()

    @app.get("/external/positions")
    def external_positions():
        return service.get_external_positions()

    @app.get("/external/summary")
    def external_summary():
        return service.get_external_account_summary()

    # ── backtests (Phase 7) ────────────────────────────────────
    @app.get("/backtests")
    def list_backtests():
        return {"backtests": _list_backtests(service.session_factory)}

    @app.post("/backtests/run", dependencies=[auth])
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
