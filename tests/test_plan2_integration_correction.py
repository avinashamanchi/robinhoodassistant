"""Whole-Plan-2 integration regressions use only local fakes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from trading_assistant.config import Secrets


def _issue_app_receipt(config, secrets):
    from trading_assistant.operations import security_posture as posture

    observed_at = datetime.now(timezone.utc)
    return posture._issue_startup_guard_receipt(
        config=config,
        secrets=secrets,
        checks=(
            SimpleNamespace(
                name="runtime_configuration",
                passed=True,
                code="ok",
            ),
            SimpleNamespace(
                name="loopback_https",
                passed=True,
                code="ok",
            ),
            SimpleNamespace(name="tls", passed=True, code="ok"),
            SimpleNamespace(name="database", passed=True, code="ok"),
            SimpleNamespace(name="encryption", passed=True, code="ok"),
        ),
        observed_at=observed_at,
        secret_loaded_at=observed_at - timedelta(seconds=1),
        runtime_role="app",
    )


def test_public_automatic_app_factory_refuses_before_ambient_or_authority_build(
    monkeypatch,
):
    from trading_assistant import bootstrap
    from trading_assistant import config as config_module
    from trading_assistant.app import main as app_main
    from trading_assistant.security import secrets as secrets_module

    touched: list[str] = []

    def forbidden(label):
        def fail(*_args, **_kwargs):
            touched.append(label)
            raise AssertionError(label)

        return fail

    monkeypatch.setattr(
        config_module,
        "load_config",
        forbidden("config"),
    )
    monkeypatch.setattr(
        secrets_module,
        "load_role_secrets",
        forbidden("keychain"),
    )
    monkeypatch.setattr(
        bootstrap,
        "_build_guarded_container",
        forbidden("container"),
    )

    with pytest.raises(
        RuntimeError,
        match="^production_startup_guard_required$",
    ):
        app_main.create_app()

    assert touched == []


def test_public_default_container_consumes_exact_receipt_once(
    app_config,
    monkeypatch,
):
    from trading_assistant import bootstrap
    from trading_assistant.app import main as app_main

    secrets = Secrets(app_api_token="plan2-receipt-test-token")
    receipt = _issue_app_receipt(app_config, secrets)
    built: list[dict[str, object]] = []
    sentinel = object()

    def fake_build(config, loaded, **kwargs):
        assert config is app_config
        assert loaded is secrets
        built.append(kwargs)
        return sentinel

    monkeypatch.setattr(bootstrap, "_build_container", fake_build)

    assert (
        app_main.build_default_container(
            config=app_config,
            secrets=secrets,
            startup_guard_receipt=receipt,
        )
        is sentinel
    )
    with pytest.raises(
        RuntimeError,
        match="^startup_guard_receipt_consumed$",
    ):
        app_main.build_default_container(
            config=app_config,
            secrets=secrets,
            startup_guard_receipt=receipt,
        )

    assert len(built) == 1
    assert built[0]["runtime_role"] == "app"
    assert "_consumed_startup_guard" in built[0]


def test_public_production_factory_consumes_receipt_before_app_creation(
    app_config,
    monkeypatch,
):
    from trading_assistant import bootstrap
    from trading_assistant.app import main as app_main

    secrets = Secrets(app_api_token="plan2-public-factory-token")
    receipt = _issue_app_receipt(app_config, secrets)
    container = SimpleNamespace(runtime_tenure_guard=None)
    created: list[object] = []

    monkeypatch.setattr(
        bootstrap,
        "_build_container",
        lambda *_args, **_kwargs: container,
    )
    monkeypatch.setattr(
        app_main,
        "_create_guarded_app",
        lambda *, container: created.append(container) or "app",
    )

    assert (
        app_main.create_app(
            config=app_config,
            secrets=secrets,
            startup_guard_receipt=receipt,
        )
        == "app"
    )
    with pytest.raises(
        RuntimeError,
        match="^startup_guard_receipt_consumed$",
    ):
        app_main.create_app(
            config=app_config,
            secrets=secrets,
            startup_guard_receipt=receipt,
        )

    assert created == [container]


def test_explicit_unguarded_stack_requires_named_test_factory(
    make_service,
):
    from trading_assistant.app import main as app_main

    class Agent:
        def chat(self, message, **context):
            return {"reply": message, "context": context}

    service = make_service()
    with pytest.raises(
        RuntimeError,
        match="^explicit_stack_requires_test_factory$",
    ):
        app_main.create_app(
            service=service,
            agent=Agent(),
            api_token="plan2-explicit-test-token",
            planning=None,
        )

    test_app = app_main.create_test_app(
        service=service,
        agent=Agent(),
        api_token="plan2-explicit-test-token",
        planning=None,
    )
    assert test_app.state.trading_service is service
    assert test_app.state.startup_evidence is None


@pytest.mark.parametrize(
    "runtime_role",
    ["mcp", "paper-drill", "safety-drill"],
)
@pytest.mark.parametrize(
    "provider",
    ["anthropic", "gemini", "groq"],
)
def test_unused_news_roots_construct_no_llm_adapter(
    app_config,
    monkeypatch,
    runtime_role,
    provider,
):
    from trading_assistant import bootstrap
    from trading_assistant.llm import factory as llm_factory

    config = app_config.model_copy(
        update={
            "analyst": app_config.analyst.model_copy(
                update={"news_enabled": True}
            ),
            "llm": app_config.llm.model_copy(
                update={"provider": provider}
            ),
        }
    )
    constructed: list[str] = []
    monkeypatch.setattr(
        llm_factory,
        "build_llm_backend",
        lambda *_args, **_kwargs: constructed.append(runtime_role)
        or object(),
    )

    summarizer = bootstrap.build_quarantine_summarizer(
        config,
        Secrets(),
        object(),
        runtime_role=runtime_role,
    )

    assert summarizer is None
    assert constructed == []


@pytest.mark.parametrize("runtime_role", ["app", "daemon"])
@pytest.mark.parametrize(
    "provider",
    ["anthropic", "gemini", "groq"],
)
def test_supported_news_adapters_keep_exact_runtime_role(
    app_config,
    monkeypatch,
    runtime_role,
    provider,
):
    from trading_assistant import bootstrap
    from trading_assistant.llm import factory as llm_factory

    config = app_config.model_copy(
        update={
            "analyst": app_config.analyst.model_copy(
                update={"news_enabled": True}
            ),
            "llm": app_config.llm.model_copy(
                update={"provider": provider}
            ),
        }
    )
    backend = object()
    observed: list[tuple[str, str]] = []

    monkeypatch.setattr(
        llm_factory,
        "build_llm_backend",
        lambda *_args, **kwargs: (
            observed.append(
                (
                    kwargs["category"],
                    kwargs["runtime_role"],
                )
            )
            or backend
        ),
    )
    monkeypatch.setattr(
        bootstrap,
        "QuarantineSummarizer",
        lambda supplied: supplied,
    )

    assert (
        bootstrap.build_quarantine_summarizer(
            config,
            Secrets(),
            object(),
            runtime_role=runtime_role,
        )
        is backend
    )
    assert observed == [("untrusted", runtime_role)]


def test_paper_drill_container_never_borrows_app_role(
    app_config,
    monkeypatch,
):
    from trading_assistant import bootstrap
    from trading_assistant.ops import paper_drill

    service = object()
    roles: list[str | None] = []

    def build_container(_config, _secrets, *, runtime_role):
        roles.append(runtime_role)
        return SimpleNamespace(
            service=service,
            runtime_tenure_guard=None,
        )

    monkeypatch.setattr(bootstrap, "build_container", build_container)

    with paper_drill.build_paper_service(
        app_config,
        Secrets(),
    ) as built:
        assert built is service

    assert roles == ["paper-drill"]


def test_paper_drill_composition_uses_maintenance_tenure_and_exact_role(
    app_config,
    monkeypatch,
):
    from trading_assistant import bootstrap

    runtime = SimpleNamespace(
        engine=object(),
        session_factory=object(),
    )
    guard = object()
    observed: list[tuple[str, object]] = []
    sentinel = object()

    monkeypatch.setattr(
        bootstrap,
        "require_configured_role_origins",
        lambda _config, role: observed.append(("origins", role)),
    )
    monkeypatch.setattr(
        bootstrap,
        "_guard_runtime",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bootstrap,
        "prepare_database_runtime",
        lambda _secrets, *, runtime_role: (
            observed.append(("database", runtime_role)) or runtime
        ),
    )
    monkeypatch.setattr(
        bootstrap,
        "build_sensitive_data_cipher",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        bootstrap,
        "bind_sensitive_cipher",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bootstrap,
        "acquire_runtime_guard",
        lambda *_args, **_kwargs: pytest.fail(
            "paper drill must not borrow runtime tenure"
        ),
    )
    monkeypatch.setattr(
        bootstrap,
        "acquire_maintenance_guard",
        lambda *_args, **_kwargs: guard,
    )

    def finish(_config, _secrets, **kwargs):
        observed.append(("finish_role", kwargs["runtime_role"]))
        assert kwargs["runtime_tenure_guard"] is guard
        return sentinel

    monkeypatch.setattr(bootstrap, "_finish_container", finish)

    assert (
        bootstrap._build_container(
            app_config,
            Secrets(),
            runtime_role="paper-drill",
        )
        is sentinel
    )
    assert observed == [
        ("origins", "paper-drill"),
        ("database", "paper-drill"),
        ("finish_role", "paper-drill"),
    ]


def test_safety_drill_role_is_restricted_to_explicit_test_container(
    app_config,
):
    from trading_assistant import bootstrap

    with pytest.raises(ValueError, match="^runtime_role_invalid$"):
        bootstrap.build_container(
            app_config,
            Secrets(),
            runtime_role="safety-drill",
        )
