"""FastAPI host: chat, pending-approval queue, approve/reject, positions, log.

``create_app`` accepts an injected service + agent (tests use mocks). With none
provided it builds the real stack from config/secrets. The approval endpoint is
the only path that can execute — and it runs the risk engine one final time
inside TradingService.approve_order.
"""

from __future__ import annotations

import ipaddress
from datetime import timedelta
from pathlib import Path
from typing import Callable, Optional

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from ..assets import AssetClass
from ..broker.models import OrderStatus
from ..config import Secrets, load_config
from ..db.schema import require_current_schema
from ..db.session import create_db_engine, make_session_factory
from ..orders.safety_state import enumerate_unsafe_local_state
from ..service import TradingService
from .agent import Agent
from .auth import SessionAuth, SessionPrincipal
from .errors import ApiError
from .ratelimit import RateLimiter
from .routers.auth import router as auth_router
from .security import (
    csrf_protected,
    current_principal,
    install_security,
    rate_limit_key,
    recent_principal,
)

_STATIC = Path(__file__).parent / "static"
def _panic_exception_receipt(service: TradingService) -> dict:
    local_state = enumerate_unsafe_local_state(
        service.session_factory
    )
    return {
        "safe": False,
        "local_enumeration": local_state.enumeration,
        "remote_enumeration": "unknown",
        "confirmed_canceled": [],
        "unconfirmed_order_ids": list(
            local_state.unsafe_order_ids
        ),
        "remote_open_order_ids": [],
        "unsafe_local_state": local_state.as_dict(),
        "message": "panic incomplete: safety could not be confirmed",
    }


class ChatIn(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must be non-empty")
        return value.strip()


class BacktestRunIn(BaseModel):
    symbols: list[str] = []
    reason: str

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must be non-empty")
        return value.strip()


class AnalyzeIn(BaseModel):
    symbol: str
    reason: str

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must be non-empty")
        return value.strip()


class ProposeIn(BaseModel):
    n: int = 3
    reason: str

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must be non-empty")
        return value.strip()


class ApprovalIn(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must be non-empty")
        return value.strip()


class PanicIn(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must be non-empty")
        return value.strip()


class KillSwitchResetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_class: AssetClass = AssetClass.EQUITY
    reason: str
    expected_generation: int = Field(gt=0)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must be non-empty")
        return value.strip()


def build_default_stack() -> tuple[TradingService, Agent]:
    from ..broker.factory import build_broker, build_clock
    from ..external_accounts.factory import build_external_source
    from ..logging import register_all_secrets

    config = load_config()
    secrets = Secrets()
    register_all_secrets(secrets)
    engine = create_db_engine(secrets.database_url)
    require_current_schema(engine)
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


def _is_loopback_bind(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def create_app(
    service: Optional[TradingService] = None,
    agent: Optional[Agent] = None,
    *,
    planning=None,
    screen_source=None,
    api_token: Optional[str] = None,
    chat_rate: RateLimiter | None = None,
    approve_rate: RateLimiter | None = None,
    analysis_rate: RateLimiter | None = None,
    backtest_rate: RateLimiter | None = None,
    login_rate: RateLimiter | None = None,
    auth_now: Callable | None = None,
    bind_host: str | None = None,
) -> FastAPI:
    runtime_secrets = Secrets()
    if api_token is None:
        api_token = runtime_secrets.app_api_token
    if not api_token or not api_token.strip():
        raise RuntimeError("APP_API_TOKEN is required")
    if service is None or agent is None:
        service, agent = build_default_stack()
    from ..logging import register_secret

    register_secret(api_token)
    security_config = service.config.security
    configured_bind = bind_host or runtime_secrets.app_host
    if (
        not security_config.cookie_secure
        and not _is_loopback_bind(configured_bind)
    ):
        raise RuntimeError(
            "security.cookie_secure must be true for a non-loopback APP_HOST"
        )

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
    analysis_rate = analysis_rate or RateLimiter(max_requests=5, window_seconds=60)
    backtest_rate = backtest_rate or RateLimiter(max_requests=2, window_seconds=3600)
    login_rate = login_rate or RateLimiter(max_requests=5, window_seconds=60)

    app = FastAPI(
        title="Trading Assistant",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.operator_secret = api_token
    app.state.login_rate = login_rate
    session_kwargs = {
        "ttl": timedelta(hours=security_config.session_hours),
        "reauthentication_window": timedelta(
            minutes=security_config.reauthentication_minutes
        ),
        "cookie_secure": security_config.cookie_secure,
    }
    if auth_now is not None:
        session_kwargs["now"] = auth_now
    app.state.session_auth = SessionAuth(
        service.session_factory,
        application_secret=api_token,
        **session_kwargs,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["X-CSRF-Token", "Content-Type"],
    )
    install_security(app)
    app.include_router(auth_router)

    @app.get("/health/live")
    def liveness():
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_page() -> str:
        return (_STATIC / "login.html").read_text(encoding="utf-8")

    @app.get("/static/login.js", response_class=FileResponse)
    def login_script():
        return _STATIC / "login.js"

    @app.get("/", response_class=HTMLResponse)
    def index(
        principal: SessionPrincipal = Depends(current_principal),
    ) -> str:
        return (_STATIC / "index.html").read_text(encoding="utf-8")

    @app.post("/chat")
    def chat(
        body: ChatIn,
        request: Request,
        principal: SessionPrincipal = Depends(csrf_protected),
    ):
        if not chat_rate.allow(rate_limit_key(request, principal)):
            raise ApiError(
                "rate_limit_exceeded", 429, "Chat rate limit exceeded"
            )
        return agent.chat(
            body.message,
            actor=principal.actor,
            reason=body.message,
            request_id=request.state.request_id,
        )

    @app.get("/health")
    def health(
        principal: SessionPrincipal = Depends(current_principal),
    ):
        return service.health()

    @app.get("/pending")
    def pending(
        principal: SessionPrincipal = Depends(current_principal),
    ):
        return {"pending": service.get_pending()}

    @app.post("/approve/{order_id}")
    def approve(
        order_id: int,
        body: ApprovalIn,
        request: Request,
        principal: SessionPrincipal = Depends(recent_principal),
    ):
        if not approve_rate.allow(rate_limit_key(request, principal)):
            raise ApiError(
                "rate_limit_exceeded", 429, "Approval rate limit exceeded"
            )
        result = service.approve_order(
            order_id,
            actor=principal.actor,
            reason=body.reason,
            request_id=request.state.request_id,
        )
        if (
            result.get("error", "").startswith("order not in PROPOSED")
            or result.get("error") == "proposal expired"
            or result.get("status") == "expired"
        ):
            raise ApiError(
                "approval_conflict", 409, "Order approval is no longer current"
            )
        if result.get("error") == "not found":
            raise ApiError("order_not_found", 404, "Order not found")
        if result.get("status") == "acceptance_unknown":
            raise ApiError(
                "acceptance_unknown",
                409,
                "Broker acceptance is unknown; reconciliation is required",
            )
        if result.get("status") == "rejected":
            raise ApiError(
                "policy_denied", 403, "Order was denied by safety policy"
            )
        return result

    @app.post("/reject/{order_id}")
    def reject(
        order_id: int,
        body: ApprovalIn,
        request: Request,
        principal: SessionPrincipal = Depends(csrf_protected),
    ):
        result = service.reject_order(
            order_id,
            actor=principal.actor,
            reason=body.reason,
            request_id=request.state.request_id,
        )
        if result.get("error") == "not found":
            raise ApiError("order_not_found", 404, "Order not found")
        if "error" in result:
            raise ApiError(
                "order_conflict", 409, "Order is not rejectable"
            )
        return result

    @app.get("/positions")
    def positions(
        principal: SessionPrincipal = Depends(current_principal),
    ):
        return {"positions": service.get_positions()}

    @app.get("/log")
    def log(
        principal: SessionPrincipal = Depends(current_principal),
    ):
        return service.get_log()

    @app.post("/killswitch/reset")
    def killswitch_reset(
        body: KillSwitchResetIn,
        request: Request,
        principal: SessionPrincipal = Depends(recent_principal),
    ):
        from ..risk.breakers import BreakerResetConflict

        try:
            return service.reset_killswitch(
                body.asset_class,
                actor=principal.actor,
                reason=body.reason,
                expected_generation=body.expected_generation,
                request_id=request.state.request_id,
            )
        except BreakerResetConflict as exc:
            raise ApiError(
                "breaker_conflict",
                409,
                "Circuit-breaker state changed; refresh before resetting",
            ) from exc

    @app.post("/orders/{order_id}/cancel")
    def cancel_order(
        order_id: int,
        body: ApprovalIn,
        request: Request,
        principal: SessionPrincipal = Depends(csrf_protected),
    ):
        result = service.cancel_live_order(
            order_id,
            actor=principal.actor,
            reason=body.reason,
            request_id=request.state.request_id,
        )
        if result.get("error") == "not found":
            raise ApiError("order_not_found", 404, "Order not found")
        if "error" in result:
            raise ApiError(
                "order_conflict",
                409,
                "Order cancellation could not be confirmed",
            )
        return result

    @app.post("/reconcile")
    def reconcile(
        body: ApprovalIn,
        request: Request,
        principal: SessionPrincipal = Depends(recent_principal),
    ):
        return service.reconcile_positions(
            actor=principal.actor,
            reason=body.reason,
            request_id=request.state.request_id,
        )

    @app.post("/sync")
    def sync(
        body: ApprovalIn,
        request: Request,
        principal: SessionPrincipal = Depends(csrf_protected),
    ):  # pull fills/status from the broker (also runs each daemon loop)
        return service.sync_open_orders(
            actor=principal.actor,
            reason=body.reason,
            request_id=request.state.request_id,
        )

    @app.post("/panic")
    def panic(
        body: PanicIn,
        request: Request,
        principal: SessionPrincipal = Depends(recent_principal),
    ):
        try:
            receipt = service.panic(
                actor=principal.actor,
                reason=body.reason,
                request_id=request.state.request_id,
            )
        except Exception:
            receipt = _panic_exception_receipt(service)
        if receipt.get("safe") is not True:
            raise ApiError(
                "panic_incomplete",
                503,
                "Panic could not confirm a safe state",
                receipt=receipt,
            )
        return receipt

    @app.get("/analyst/scorecard")
    def analyst_scorecard(
        principal: SessionPrincipal = Depends(current_principal),
    ):
        from ..analyst.store import promotion_status

        with service.session_factory() as s:
            return promotion_status(s, version=service.config.analyst.version)

    # ── plans + screener (Phase 8) ─────────────────────────────
    def _require_planning():
        if planning is None:
            raise ApiError(
                "dependency_unavailable",
                503,
                "Analyst planning is unavailable",
            )
        return planning

    @app.post("/analyze")
    def analyze(
        body: AnalyzeIn,
        request: Request,
        principal: SessionPrincipal = Depends(csrf_protected),
    ):
        if not analysis_rate.allow(rate_limit_key(request, principal)):
            raise ApiError(
                "rate_limit_exceeded", 429, "Analysis rate limit exceeded"
            )
        try:
            return _require_planning().analyze(
                body.symbol,
                actor=principal.actor,
                reason=body.reason,
                request_id=request.state.request_id,
            )
        except ApiError:
            raise
        except Exception:
            raise ApiError(
                "analysis_failed",
                503,
                "Analysis could not be completed",
            ) from None

    @app.get("/plans")
    def list_plans(
        principal: SessionPrincipal = Depends(current_principal),
    ):
        return {"plans": _require_planning().get_plans()}

    @app.get("/plans/ui", response_class=HTMLResponse)
    def plans_ui(
        principal: SessionPrincipal = Depends(current_principal),
    ) -> str:
        return (_STATIC / "plans.html").read_text(encoding="utf-8")

    @app.get("/plans/{plan_id}")
    def get_plan(
        plan_id: int,
        principal: SessionPrincipal = Depends(current_principal),
    ):
        plan = _require_planning().get_plan(plan_id)
        if plan is None:
            raise ApiError("plan_not_found", 404, "Plan not found")
        return plan

    @app.post("/plans/{plan_id}/approve")
    def approve_plan(
        plan_id: int,
        body: ApprovalIn,
        request: Request,
        principal: SessionPrincipal = Depends(recent_principal),
    ):
        result = _require_planning().approve_plan(
            plan_id,
            actor=principal.actor,
            reason=body.reason,
            request_id=request.state.request_id,
        )
        if "error" in result and "promotion gate" in result["error"]:
            raise ApiError(
                "policy_denied",
                403,
                "Plan approval was denied by safety policy",
            )
        if result.get("error") == "not found":
            raise ApiError("plan_not_found", 404, "Plan not found")
        if "error" in result:
            raise ApiError(
                "approval_conflict", 409, "Plan approval is no longer current"
            )
        return result

    @app.post("/plans/{plan_id}/cancel")
    def cancel_plan(
        plan_id: int,
        body: ApprovalIn,
        request: Request,
        principal: SessionPrincipal = Depends(csrf_protected),
    ):
        result = _require_planning().cancel_plan(
            plan_id,
            actor=principal.actor,
            reason=body.reason,
            request_id=request.state.request_id,
        )
        if result.get("error") == "not found":
            raise ApiError("plan_not_found", 404, "Plan not found")
        if "error" in result:
            raise ApiError(
                "plan_conflict", 409, "Plan cancellation is not current"
            )
        return result

    def _screen_candidates(top_n: int):
        nonlocal screen_source
        from ..analyst import screener

        universe = service.config.screener.universe or service.config.risk.ticker_allowlist
        if screen_source is None:  # lazily build the live source on first call
            sec = _secrets_holder.get("s")
            if sec is None:
                raise ApiError(
                    "dependency_unavailable",
                    503,
                    "Screener source is unavailable",
                )
            from ..analyst.live_features import build_screen_source

            screen_source = build_screen_source([s.upper() for s in universe], sec)
        return screener.screen_source(
            screen_source, [s.upper() for s in universe], spy_symbol="SPY", top_n=top_n,
        )

    @app.post("/screen")
    def screen(
        request: Request,
        principal: SessionPrincipal = Depends(csrf_protected),
    ):
        if not analysis_rate.allow(rate_limit_key(request, principal)):
            raise ApiError(
                "rate_limit_exceeded", 429, "Analysis rate limit exceeded"
            )
        return {"candidates": _screen_candidates(service.config.screener.top_n)}

    @app.post("/propose")
    def propose(
        body: ProposeIn,
        request: Request,
        principal: SessionPrincipal = Depends(csrf_protected),
    ):
        """Screen the market and run the analyst on the top N candidates, creating
        sized plans you can approve. The analyst is UNPROVEN — these are suggestions
        the risk engine still gates; you approve each one."""
        if not analysis_rate.allow(rate_limit_key(request, principal)):
            raise ApiError(
                "rate_limit_exceeded", 429, "Analysis rate limit exceeded"
            )
        planning = _require_planning()
        candidates = _screen_candidates(max(body.n, service.config.screener.top_n))
        created = []
        for c in candidates[: body.n]:
            try:
                out = planning.analyze(
                    c["symbol"],
                    actor=principal.actor,
                    reason=body.reason,
                    request_id=request.state.request_id,
                )
                created.append({
                    "plan_id": out["plan_id"], "symbol": c["symbol"],
                    "action": out["plan"]["action"], "score": c["score"],
                    "sized_shares": out["sized"]["total_shares"],
                })
            except Exception:  # skip a bad candidate, keep going
                created.append(
                    {
                        "symbol": c["symbol"],
                        "error": "analysis_failed",
                    }
                )
        return {"proposed": created, "note": "analyst v2 is UNPROVEN — review before approving"}

    # ── external (read-only) accounts ──────────────────────────
    @app.get("/holdings")
    def holdings(
        principal: SessionPrincipal = Depends(current_principal),
    ):
        """Combined Alpaca + external holdings, labeled by source (read-only external)."""
        return service.get_combined_holdings()

    @app.get("/external/positions")
    def external_positions(
        principal: SessionPrincipal = Depends(current_principal),
    ):
        return service.get_external_positions()

    @app.get("/external/summary")
    def external_summary(
        principal: SessionPrincipal = Depends(current_principal),
    ):
        return service.get_external_account_summary()

    # ── backtests (Phase 7) ────────────────────────────────────
    @app.get("/backtests")
    def list_backtests(
        principal: SessionPrincipal = Depends(current_principal),
    ):
        return {"backtests": _list_backtests(service.session_factory)}

    @app.post("/backtests/run")
    def run_backtest_endpoint(
        body: BacktestRunIn,
        request: Request,
        principal: SessionPrincipal = Depends(csrf_protected),
    ):
        if not backtest_rate.allow(rate_limit_key(request, principal)):
            raise ApiError(
                "rate_limit_exceeded", 429, "Backtest rate limit exceeded"
            )
        from ..backtest.runner import run_synthetic_backtest

        run_id, report = run_synthetic_backtest(
            service.session_factory,
            symbols=body.symbols or None,
            actor=principal.actor,
            reason=body.reason,
            request_id=request.state.request_id,
        )
        return {"run_id": run_id, "report": report.to_dict()}

    @app.get("/backtests/{run_id}/report")
    def backtest_report(
        run_id: int,
        principal: SessionPrincipal = Depends(current_principal),
    ):
        report = _load_backtest_report(service.session_factory, run_id)
        if report is None:
            raise ApiError("backtest_not_found", 404, "Backtest run not found")
        return report

    @app.get("/backtests/ui", response_class=HTMLResponse)
    def backtests_ui(
        principal: SessionPrincipal = Depends(current_principal),
    ) -> str:
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
