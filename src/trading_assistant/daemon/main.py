"""Daemon entrypoint: build the stack from config/secrets and run the monitor loop.

    uv run python -m trading_assistant.daemon.main
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from ..ops.equity import EquitySnapshotRecorder
from ..app.limits import LimitStoreUnavailable
from ..config import load_config
from ..notifications.base import build_notifier
from ..security.secrets import RuntimeSecrets, load_role_secrets
from .backoff import (
    ScheduledMarketDataDenied,
    scheduled_market_data_read,
    trip_scheduled_market_data_breaker,
)
from .monitor import Monitor


def build_monitor() -> Monitor:
    from ..logging import runtime_startup

    config = load_config()
    secrets = load_role_secrets("daemon", config=config)
    with runtime_startup("daemon", secrets):
        return _build_monitor(config, secrets)


def _build_monitor(
    config,
    secrets: RuntimeSecrets,
    *,
    historical_alpaca_client_factory=None,
    historical_coingecko_http=None,
    historical_cache_dir=".cache/bars",
) -> Monitor:
    from .. import bootstrap

    container = bootstrap.build_container(
        config,
        secrets,
        runtime_role="daemon",
    )
    try:
        return _finish_monitor(
            config,
            secrets,
            container=container,
            historical_alpaca_client_factory=(
                historical_alpaca_client_factory
            ),
            historical_coingecko_http=historical_coingecko_http,
            historical_cache_dir=historical_cache_dir,
        )
    except BaseException:
        guard = getattr(container, "runtime_tenure_guard", None)
        if guard is not None and not guard.close():
            raise RuntimeError(
                "runtime_tenure_cleanup_uncertain"
            ) from None
        raise


def _finish_monitor(
    config,
    secrets: RuntimeSecrets,
    *,
    container,
    historical_alpaca_client_factory,
    historical_coingecko_http,
    historical_cache_dir,
) -> Monitor:
    service = container.service
    runtime_tenure_guard = getattr(
        container,
        "runtime_tenure_guard",
        None,
    )
    notifier = build_notifier(
        config,
        secrets,
        runtime_role="daemon",
    )
    container.rule_worker.notifier = notifier
    shadow = None
    screen_source = None
    if config.features.shadow_mode:
        from decimal import Decimal

        from ..analyst.analyst import Analyst
        from ..analyst.live_features import (
            RefreshingScreenSource,
            build_live_feature_provider,
            build_screen_source,
        )
        from ..analyst.planning import PlanningService
        from ..analyst.shadow import ShadowRunner
        from ..llm.factory import build_llm_backend

        analyst = Analyst(
            build_llm_backend(
                config,
                secrets,
                provider_budget=container.provider_budget,
                category="analysis",
                runtime_role="daemon",
            ),
            max_tokens=config.llm.max_tokens,
            suppress_ranging=config.analyst.suppress_ranging,
            max_attempts=(
                config.security.provider_budget.max_structured_attempts
            ),
        )
        historical_kwargs = {
            "scheduled_service": service,
            "rate_limiter": container.rate_limiter,
            "alpaca_client_factory": (
                historical_alpaca_client_factory
            ),
            "coingecko_http": historical_coingecko_http,
            "cache_dir": historical_cache_dir,
        }
        planning = PlanningService(
            service,
            analyst,
            build_live_feature_provider(
                config,
                secrets,
                runtime_role="daemon",
                **historical_kwargs,
            ),
            secrets,
        )
        universe = config.screener.universe or config.risk.ticker_allowlist
        screen_source = RefreshingScreenSource(
            lambda: build_screen_source(
                [s.upper() for s in universe],
                secrets,
                config=config,
                runtime_role="daemon",
                **historical_kwargs,
            )
        )

        def _price(sym: str):
            try:
                quote = scheduled_market_data_read(
                    lambda: service.broker.get_quote(sym),
                    rate_limiter=container.rate_limiter,
                    limit_config=(
                        config.security.rate_limits.provider_read
                    ),
                )
                return Decimal(str(quote.last))
            except (ScheduledMarketDataDenied, LimitStoreUnavailable):
                trip_scheduled_market_data_breaker(
                    service,
                    sym,
                    actor="daemon:shadow",
                    request_id=f"shadow-price:{uuid4().hex}",
                    audit_reason=(
                        "daemon scheduled shadow market data read"
                    ),
                )
                return None
            except Exception:
                return None

        shadow = ShadowRunner(service, planning, screen_source, _price, top_n=3)

    autopilot = _build_autopilot_runner(
        config,
        secrets,
        container=container,
        runtime_tenure_guard=runtime_tenure_guard,
        historical_alpaca_client_factory=historical_alpaca_client_factory,
        historical_cache_dir=historical_cache_dir,
    )

    return Monitor(
        service,
        notifier,
        auto_execute=config.features.auto_execute_preapproved_rules,
        poll_interval_seconds=config.daemon.poll_interval_seconds,
        max_quote_age_seconds=config.daemon.max_quote_age_seconds,
        cycle_timeout_seconds=config.daemon.cycle_timeout_seconds,
        daily_task_timeout_seconds=config.daemon.daily_task_timeout_seconds,
        shadow=shadow,
        digest_source=screen_source,
        rule_worker=container.rule_worker,
        rate_limiter=container.rate_limiter,
        leases=container.leases,
        provider_budget=container.provider_budget,
        policy_store_maintenance=getattr(
            container,
            "policy_store_maintenance",
            None,
        ),
        runtime_tenure_guard=runtime_tenure_guard,
        autopilot=autopilot,
        autopilot_timeout_seconds=config.autopilot.cycle_timeout_seconds,
        equity_recorder=EquitySnapshotRecorder(service),
        campaign_capture=_build_campaign_capture(config, service, runtime_tenure_guard, secrets=secrets),
    )


def _build_campaign_capture(config, service, guard, root=None, *, config_fingerprint=None, code_fingerprint=None, secrets=None):
    from ..assets import AssetClass
    from ..autopilot.evidence import EvidenceStore
    from ..autopilot.identity import config_fingerprint as config_identity, decision_code_fingerprint, code_identity
    from ..installation import source_root
    from ..ops.acceptance_campaign import CampaignCapture, read_reconciliation, read_daemon_order_reconciliation, read_position_reconciliation, read_backup_artifacts
    from ..ops.campaign_transitions import read_lifecycle, read_recovery, read_mock_drill
    from ..ops.release_evidence import clean_commit
    from ..ops.operational_state import StateStore
    config_fingerprint = config_fingerprint or config_identity
    code_fingerprint = code_fingerprint or decision_code_fingerprint
    scope = {"mode": config.autopilot.mode, "config_fingerprint": config_fingerprint(config),
             "code_fingerprint": code_fingerprint(config.autopilot.strategy)}
    campaign_root = root or source_root()
    runtime_code = code_identity(campaign_root)
    if secrets is not None:
        from ..security.crypto import build_sensitive_data_cipher
        try:
            cipher = build_sensitive_data_cipher(config.encryption, secrets)
        except Exception:
            # Campaign collection must not interrupt the trading safety loop or
            # create unauthenticated operational evidence when keys are absent.
            import logging
            logging.getLogger(__name__).warning("campaign_cipher_unavailable")
            return None
    else:
        cipher = None
    def order_reconciliation(at):
        if config.trading.broker.value != "alpaca":
            return "unknown", "0" * 64
        return read_daemon_order_reconciliation(service.session_factory, at, config.trading.reconciliation_max_age_seconds)

    def startup_reconciliation(at):
        if config.trading.broker.value != "alpaca":
            return "unknown", "0" * 64
        return read_reconciliation(service.session_factory, "alpaca", at, config.trading.reconciliation_max_age_seconds)

    def position_reconciliation(at):
        if config.trading.broker.value != "alpaca":
            return "unknown", "0" * 64
        return read_position_reconciliation(service.session_factory, at, config.trading.reconciliation_max_age_seconds)

    def transitions(cycles, start, at):
        real = config.trading.broker.value == "alpaca" and config.trading.mode.value == "paper"
        lifecycle = read_lifecycle(service.session_factory, cycles, scope, start, at) if real else {"outcome": "unknown"}
        recovery = read_recovery(service.session_factory, start, at) if real else {"outcome": "unknown"}
        try:
            mock = read_mock_drill(campaign_root, runtime_code, at) if runtime_code and clean_commit(campaign_root) == runtime_code else {"outcome": "unknown"}
        except Exception:
            mock = {"outcome": "unknown"}
        return {"lifecycle": lifecycle, "recovery": recovery, "safety_drill": mock}

    return CampaignCapture(StateStore(campaign_root), service.market_clock(AssetClass.EQUITY),
                           EvidenceStore(service.session_factory, actor="campaign"), scope, guard,
                           delay_minutes=getattr(config.autopilot, "run_after_open_minutes", 30),
                           reconciliation_reader=startup_reconciliation,
                           order_reconciliation_reader=order_reconciliation, position_reconciliation_reader=position_reconciliation,
                           backup_reader=lambda at: read_backup_artifacts(campaign_root, at),
                           runtime_code=runtime_code, transition_reader=transitions, cipher=cipher)


def _build_autopilot_runner(
    config,
    secrets: RuntimeSecrets,
    *,
    container,
    runtime_tenure_guard,
    historical_alpaca_client_factory,
    historical_cache_dir,
):
    """The daemon is the single owner of autopilot scheduling and orders."""
    if config.autopilot.mode == "off":
        return None
    from datetime import timedelta

    from ..analyst.live_features import build_live_feature_provider
    from ..autopilot import Autopilot, AutopilotDisabled, autopilot_config_problems
    from ..autopilot.decisions import exit_policy, require_paper, resolve_universe
    from ..autopilot.evidence import EvidenceStore
    from ..autopilot.runner import AutopilotRunner
    from ..installation import source_root

    require_paper(config)
    problems = autopilot_config_problems(config)
    if problems:
        raise AutopilotDisabled(
            "autopilot configuration cannot trade: " + "; ".join(problems)
        )
    service = container.service
    autopilot = Autopilot(
        service,
        build_live_feature_provider(
            config,
            secrets,
            scheduled_service=service,
            rate_limiter=container.rate_limiter,
            alpaca_client_factory=historical_alpaca_client_factory,
            cache_dir=historical_cache_dir,
            runtime_role="daemon",
            require_market_clock=True,
        ),
        universe=resolve_universe(config),
        notional_per_trade=config.autopilot.notional_per_trade,
        max_orders_per_day=config.autopilot.max_orders_per_day,
        strategy=config.autopilot.strategy,
        dry_run=True,
        max_feature_age=timedelta(hours=config.autopilot.max_feature_age_hours),
        exit_policy=exit_policy(config),
        retry_after_rejection_days=config.autopilot.retry_after_rejection_days,
        tenure_guard=runtime_tenure_guard,
    )
    return AutopilotRunner(
        config=config,
        service=service,
        autopilot=autopilot,
        store=EvidenceStore(
            container.session_factory,
            actor=f"autopilot:{config.autopilot.strategy}",
        ),
        root=source_root(),
        tenure_guard=runtime_tenure_guard,
    )


def main() -> None:
    from ..logging import runtime_startup

    config = load_config()
    secrets = load_role_secrets("daemon", config=config)
    with runtime_startup("daemon", secrets):
        monitor = _build_monitor(config, secrets)
        asyncio.run(monitor.run())


if __name__ == "__main__":
    main()
