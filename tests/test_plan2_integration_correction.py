"""Whole-Plan-2 integration regressions use only local fakes."""

from __future__ import annotations

from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from functools import partial
import logging
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


def test_public_build_container_has_no_ambient_app_default():
    from trading_assistant import bootstrap

    with pytest.raises(TypeError):
        bootstrap.build_container()


def test_public_build_container_requires_receipt_for_explicit_app(
    app_config,
    monkeypatch,
):
    from trading_assistant import bootstrap

    monkeypatch.setattr(
        bootstrap,
        "_build_container",
        lambda *_args, **_kwargs: pytest.fail(
            "unguarded app authority was constructed"
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="^app_startup_guard_required$",
    ):
        bootstrap.build_container(
            app_config,
            Secrets(app_api_token="plan2-app-boundary-token"),
            runtime_role="app",
        )


def test_create_test_app_rejects_ordinary_build_container_result(
    app_config,
    monkeypatch,
):
    from trading_assistant import bootstrap
    from trading_assistant.app import main as app_main

    ordinary = SimpleNamespace()
    monkeypatch.setattr(
        bootstrap,
        "_build_container",
        lambda *_args, **_kwargs: ordinary,
    )
    container = bootstrap.build_container(
        app_config,
        Secrets(app_api_token="plan2-ordinary-container-token"),
        runtime_role="daemon",
    )

    with pytest.raises(
        RuntimeError,
        match="^test_container_required$",
    ):
        app_main.create_test_app(
            container=container,
            planning=None,
        )


def test_create_test_app_accepts_only_marked_fake_container(
    app_config,
    make_service,
):
    from trading_assistant.app import main as app_main
    from tests.app_factory import build_test_app_container

    class Agent:
        def chat(self, message, **context):
            return {"reply": message, "context": context}

    service = make_service()
    agent = Agent()
    secrets = Secrets(
        app_api_token="plan2-marked-test-container-token"
    )
    container = build_test_app_container(
        service,
        agent,
        secrets=secrets,
    )

    app = app_main.create_test_app(
        container=container,
        planning=None,
    )

    assert app.state.container is container
    assert app.state.trading_service is service
    assert app.state.agent is agent


def test_test_container_builder_rejects_production_broker_capability(
    app_config,
    session_factory,
):
    from trading_assistant import bootstrap
    from trading_assistant.broker.alpaca import AlpacaBroker
    from trading_assistant.risk.clock import FakeClock
    from trading_assistant.service import TradingService

    class Agent:
        def chat(self, message, **context):
            return {"reply": message, "context": context}

    broker = AlpacaBroker(SimpleNamespace(), SimpleNamespace())
    clock = FakeClock(is_open=True)
    service = TradingService(
        broker,
        session_factory,
        app_config,
        clock,
    )

    with pytest.raises(
        RuntimeError,
        match="^production_test_capability_forbidden$",
    ):
        bootstrap.build_test_container(
            app_config,
            Secrets(
                app_api_token="plan2-production-capability-token"
            ),
            broker=broker,
            clock=clock,
            service=service,
            agent=Agent(),
        )


def test_test_container_builder_rejects_nested_production_clock_capability(
    app_config,
    make_service,
):
    from trading_assistant import bootstrap
    from trading_assistant.assets import AssetClass
    from trading_assistant.broker.alpaca import AlpacaClock

    class Agent:
        def chat(self, message, **context):
            return {"reply": message, "context": context}

    service = make_service()
    service._clocks[AssetClass.CRYPTO] = AlpacaClock(
        SimpleNamespace()
    )

    with pytest.raises(
        RuntimeError,
        match="^production_test_capability_forbidden$",
    ):
        bootstrap.build_test_container(
            app_config,
            Secrets(
                app_api_token="plan2-nested-capability-token"
            ),
            broker=service.broker,
            clock=service.clock,
            service=service,
            agent=Agent(),
        )


def test_test_container_builder_rejects_wrapped_production_broker(
    app_config,
    session_factory,
):
    from trading_assistant.broker.alpaca import AlpacaBroker
    from trading_assistant.ops.safety_drill import (
        _CrashAfterAcceptanceOnceBroker,
    )
    from trading_assistant.risk.clock import FakeClock
    from trading_assistant.service import TradingService
    from tests.app_factory import build_test_app_container

    class Agent:
        def chat(self, message, **context):
            return {"reply": message, "context": context}

    broker = _CrashAfterAcceptanceOnceBroker(
        AlpacaBroker(SimpleNamespace(), SimpleNamespace())
    )
    service = TradingService(
        broker,
        session_factory,
        app_config,
        FakeClock(is_open=True),
    )

    with pytest.raises(
        RuntimeError,
        match="^production_test_capability_forbidden$",
    ):
        build_test_app_container(
            service,
            Agent(),
            secrets=Secrets(
                app_api_token="plan2-wrapped-capability-token"
            ),
        )


def test_test_container_builder_rejects_shallow_mock_broker_replacement(
    app_config,
    session_factory,
):
    from trading_assistant.app import main as app_main
    from trading_assistant.broker.alpaca import AlpacaBroker
    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.risk.clock import FakeClock
    from trading_assistant.service import TradingService
    from tests.app_factory import build_test_app_container

    class Agent:
        def chat(self, message, **context):
            return {"reply": message, "context": context}

    production_broker = AlpacaBroker(
        SimpleNamespace(),
        SimpleNamespace(),
    )
    service = TradingService(
        production_broker,
        session_factory,
        app_config,
        FakeClock(is_open=True),
    )
    service.broker = MockBroker()

    assert service.snapshot_service.broker is production_broker
    assert service.order_submission.broker is production_broker
    assert service.reconciliation.broker is production_broker

    with pytest.raises(
        RuntimeError,
        match="^production_test_capability_forbidden$",
    ):
        container = build_test_app_container(
            service,
            Agent(),
            secrets=Secrets(
                app_api_token="plan2-shallow-broker-swap-token"
            ),
        )
        app_main.create_test_app(
            container=container,
            planning=None,
        )


@pytest.mark.parametrize(
    "broker_owner",
    (
        "snapshot_service",
        "order_submission",
        "reconciliation",
    ),
)
def test_create_test_app_rejects_post_issuance_nested_broker_tampering(
    broker_owner,
    make_service,
):
    from trading_assistant.app import main as app_main
    from trading_assistant.broker.alpaca import AlpacaBroker
    from tests.app_factory import build_test_app_container

    class Agent:
        def chat(self, message, **context):
            return {"reply": message, "context": context}

    service = make_service()
    container = build_test_app_container(
        service,
        Agent(),
        secrets=Secrets(
            app_api_token="plan2-nested-broker-tamper-token"
        ),
    )
    getattr(service, broker_owner).broker = AlpacaBroker(
        SimpleNamespace(),
        SimpleNamespace(),
    )

    with pytest.raises(
        RuntimeError,
        match="^test_container_required$",
    ):
        app_main.create_test_app(
            container=container,
            planning=None,
        )


def test_test_container_rejects_mock_subclass_with_production_delegate(
    app_config,
    session_factory,
):
    from trading_assistant.broker.alpaca import AlpacaBroker
    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.risk.clock import FakeClock
    from trading_assistant.service import TradingService
    from tests.app_factory import build_test_app_container

    class ProductionDelegatingMock(MockBroker):
        def __init__(self, delegate):
            super().__init__()
            self.delegate = delegate

        def submit_order(self, order):
            return self.delegate.submit_order(order)

    broker = ProductionDelegatingMock(
        AlpacaBroker(SimpleNamespace(), SimpleNamespace())
    )
    service = TradingService(
        broker,
        session_factory,
        app_config,
        FakeClock(is_open=True),
    )

    with pytest.raises(
        RuntimeError,
        match="^production_test_capability_forbidden$",
    ):
        build_test_app_container(
            service,
            SimpleNamespace(chat=lambda *_args, **_kwargs: None),
            secrets=Secrets(
                app_api_token="plan2-direct-delegate-token"
            ),
        )


def test_test_container_rejects_nested_owned_production_delegate(
    make_service,
):
    from trading_assistant.broker.alpaca import AlpacaBroker
    from tests.app_factory import build_test_app_container

    service = make_service()
    service.broker.owned_state = {
        "adapters": [
            SimpleNamespace(
                delegate=AlpacaBroker(
                    SimpleNamespace(),
                    SimpleNamespace(),
                )
            )
        ]
    }

    with pytest.raises(
        RuntimeError,
        match="^production_test_capability_forbidden$",
    ):
        build_test_app_container(
            service,
            SimpleNamespace(chat=lambda *_args, **_kwargs: None),
            secrets=Secrets(
                app_api_token="plan2-nested-delegate-token"
            ),
        )


def test_test_container_rejects_delegate_in_plain_holder(
    make_service,
):
    from trading_assistant.broker.alpaca import AlpacaBroker
    from tests.app_factory import build_test_app_container

    class Holder:
        def __init__(self, delegate):
            self.delegate = delegate

    service = make_service()
    service.broker.owned_state = Holder(
        AlpacaBroker(SimpleNamespace(), SimpleNamespace())
    )

    with pytest.raises(
        RuntimeError,
        match="^production_test_capability_forbidden$",
    ):
        build_test_app_container(
            service,
            SimpleNamespace(chat=lambda *_args, **_kwargs: None),
            secrets=Secrets(
                app_api_token="plan2-plain-holder-token"
            ),
        )


def test_create_test_app_rejects_plain_holder_delegate_inserted_after_issuance(
    make_service,
):
    from trading_assistant.app import main as app_main
    from trading_assistant.broker.alpaca import AlpacaBroker
    from tests.app_factory import build_test_app_container

    class Holder:
        pass

    service = make_service()
    holder = Holder()
    service.broker.owned_state = holder
    container = build_test_app_container(
        service,
        SimpleNamespace(chat=lambda *_args, **_kwargs: None),
        secrets=Secrets(
            app_api_token="plan2-holder-consumption-token"
        ),
    )
    holder.delegate = AlpacaBroker(
        SimpleNamespace(),
        SimpleNamespace(),
    )

    with pytest.raises(
        RuntimeError,
        match="^test_container_required$",
    ):
        app_main.create_test_app(
            container=container,
            planning=None,
        )


def test_test_container_rejects_class_owned_production_delegate(
    make_service,
):
    from trading_assistant.broker.alpaca import AlpacaBroker
    from tests.app_factory import build_test_app_container

    class Holder:
        delegate = AlpacaBroker(
            SimpleNamespace(),
            SimpleNamespace(),
        )

    service = make_service()
    service.broker.owned_state = Holder()

    with pytest.raises(
        RuntimeError,
        match="^production_test_capability_forbidden$",
    ):
        build_test_app_container(
            service,
            SimpleNamespace(chat=lambda *_args, **_kwargs: None),
            secrets=Secrets(
                app_api_token="plan2-class-holder-token"
            ),
        )


def test_test_container_rejects_delegate_owned_by_retained_holder_class(
    make_service,
):
    from trading_assistant.broker.alpaca import AlpacaBroker
    from tests.app_factory import build_test_app_container

    class Holder:
        delegate = AlpacaBroker(
            SimpleNamespace(),
            SimpleNamespace(),
        )

    service = make_service()
    service.broker.owned_state = Holder

    with pytest.raises(
        RuntimeError,
        match="^production_test_capability_forbidden$",
    ):
        build_test_app_container(
            service,
            SimpleNamespace(chat=lambda *_args, **_kwargs: None),
            secrets=Secrets(
                app_api_token="plan2-retained-holder-class-token"
            ),
        )


@pytest.mark.parametrize(
    "descriptor_kind",
    ("property", "staticmethod", "classmethod"),
)
def test_test_container_rejects_delegate_captured_by_class_descriptor(
    descriptor_kind,
    make_service,
):
    from trading_assistant.broker.alpaca import AlpacaBroker
    from tests.app_factory import build_test_app_container

    production_broker = AlpacaBroker(
        SimpleNamespace(),
        SimpleNamespace(),
    )

    if descriptor_kind == "property":
        descriptor = property(lambda _self: production_broker)
    elif descriptor_kind == "staticmethod":
        descriptor = staticmethod(lambda: production_broker)
    else:
        descriptor = classmethod(lambda _cls: production_broker)

    class Holder:
        delegate = descriptor

    service = make_service()
    service.broker.owned_state = Holder()

    with pytest.raises(
        RuntimeError,
        match="^production_test_capability_forbidden$",
    ):
        build_test_app_container(
            service,
            SimpleNamespace(chat=lambda *_args, **_kwargs: None),
            secrets=Secrets(
                app_api_token=(
                    f"plan2-{descriptor_kind}-delegate-token"
                )
            ),
        )


def test_test_container_rejects_slotted_holder_without_invoking_descriptor(
    make_service,
):
    from trading_assistant.broker.alpaca import AlpacaBroker
    from tests.app_factory import build_test_app_container

    class Holder:
        __slots__ = ("delegate",)

        def __init__(self, delegate):
            self.delegate = delegate

    service = make_service()
    service.broker.owned_state = Holder(
        AlpacaBroker(SimpleNamespace(), SimpleNamespace())
    )

    with pytest.raises(
        RuntimeError,
        match="^production_test_capability_forbidden$",
    ):
        build_test_app_container(
            service,
            SimpleNamespace(chat=lambda *_args, **_kwargs: None),
            secrets=Secrets(
                app_api_token="plan2-slotted-holder-token"
            ),
        )


def test_test_container_rejects_builtin_container_subclass_without_iteration(
    make_service,
):
    from trading_assistant.broker.alpaca import AlpacaBroker
    from tests.app_factory import build_test_app_container

    iteration_attempted = []

    class HolderList(list):
        def __iter__(self):
            iteration_attempted.append(True)
            raise AssertionError("custom iteration invoked")

    owned_state = HolderList()
    list.append(
        owned_state,
        AlpacaBroker(SimpleNamespace(), SimpleNamespace()),
    )
    service = make_service()
    service.broker.owned_state = owned_state

    with pytest.raises(
        RuntimeError,
        match="^production_test_capability_forbidden$",
    ):
        build_test_app_container(
            service,
            SimpleNamespace(chat=lambda *_args, **_kwargs: None),
            secrets=Secrets(
                app_api_token="plan2-container-subclass-token"
            ),
        )
    assert iteration_attempted == []


def test_test_container_validation_uses_only_static_holder_state(
    make_service,
):
    from trading_assistant.app import main as app_main
    from tests.app_factory import build_test_app_container

    instance_lookups = []
    metaclass_lookups = []

    class ProbeMeta(type):
        def __getattribute__(cls, name):
            if name == "__dataclass_fields__":
                metaclass_lookups.append(name)
                raise AssertionError("dynamic dataclass lookup")
            return type.__getattribute__(cls, name)

    class Probe(metaclass=ProbeMeta):
        def __init__(self):
            object.__setattr__(self, "marker", "safe")

        def __getattribute__(self, name):
            instance_lookups.append(name)
            if name == "__class__":
                return object.__getattribute__(self, name)
            raise AssertionError("dynamic instance lookup")

        @property
        def delegate(self):
            raise AssertionError("property invoked")

    service = make_service()
    service.broker.probe = Probe()
    container = build_test_app_container(
        service,
        SimpleNamespace(chat=lambda *_args, **_kwargs: None),
        secrets=Secrets(
            app_api_token="plan2-static-holder-token"
        ),
    )

    app = app_main.create_test_app(
        container=container,
        planning=None,
    )

    assert app.state.trading_service is service
    assert instance_lookups == []
    assert metaclass_lookups == []


@pytest.mark.parametrize(
    "forwarder_kind",
    ("bound_method", "partial", "closure"),
)
def test_test_container_rejects_production_callable_capture(
    forwarder_kind,
    make_service,
):
    from trading_assistant.broker.alpaca import AlpacaBroker
    from tests.app_factory import build_test_app_container

    service = make_service()
    production_broker = AlpacaBroker(
        SimpleNamespace(),
        SimpleNamespace(),
    )
    if forwarder_kind == "bound_method":
        forwarder = production_broker.submit_order
    elif forwarder_kind == "partial":
        forwarder = partial(production_broker.submit_order)
    else:
        def forwarder(order):
            return production_broker.submit_order(order)

    service.broker.submit_order = forwarder

    with pytest.raises(
        RuntimeError,
        match="^production_test_capability_forbidden$",
    ):
        build_test_app_container(
            service,
            SimpleNamespace(chat=lambda *_args, **_kwargs: None),
            secrets=Secrets(
                app_api_token="plan2-callable-delegate-token"
            ),
        )


def test_create_test_app_rejects_production_delegate_inserted_after_issuance(
    make_service,
):
    from trading_assistant.app import main as app_main
    from trading_assistant.broker.alpaca import AlpacaBroker
    from trading_assistant.broker.mock import MockBroker
    from tests.app_factory import build_test_app_container

    class ProductionDelegatingMock(MockBroker):
        def submit_order(self, order):
            return self.delegate.submit_order(order)

    service = make_service(broker=ProductionDelegatingMock())
    container = build_test_app_container(
        service,
        SimpleNamespace(chat=lambda *_args, **_kwargs: None),
        secrets=Secrets(
            app_api_token="plan2-post-issuance-delegate-token"
        ),
    )
    service.broker.delegate = AlpacaBroker(
        SimpleNamespace(),
        SimpleNamespace(),
    )

    with pytest.raises(
        RuntimeError,
        match="^test_container_required$",
    ):
        app_main.create_test_app(
            container=container,
            planning=None,
        )


def test_test_container_rejects_unbounded_owned_state(
    make_service,
):
    from tests.app_factory import build_test_app_container

    service = make_service()
    nested = []
    for _ in range(64):
        nested = [nested]
    service.broker.owned_state = nested

    with pytest.raises(
        RuntimeError,
        match="^production_test_capability_forbidden$",
    ):
        build_test_app_container(
            service,
            SimpleNamespace(chat=lambda *_args, **_kwargs: None),
            secrets=Secrets(
                app_api_token="plan2-bounded-state-token"
            ),
        )


def test_test_container_accepts_spy_broker_and_cycle_safe_state(
    make_service,
):
    from threading import Event, Lock

    from trading_assistant.app import main as app_main
    from tests.app_factory import build_test_app_container

    service = make_service()
    cycle = []
    cycle.append(cycle)
    service.broker.cycle = cycle
    service.broker.test_event = Event()
    service.broker.test_lock = Lock()
    service.broker.call_counts = Counter({"submit": 1})
    container = build_test_app_container(
        service,
        SimpleNamespace(chat=lambda *_args, **_kwargs: None),
        secrets=Secrets(
            app_api_token="plan2-legitimate-spy-token"
        ),
    )

    app = app_main.create_test_app(
        container=container,
        planning=None,
    )

    assert type(service.broker).__name__ == "SpyBroker"
    assert app.state.trading_service is service


def test_non_app_test_composition_is_not_an_app_test_authority(
    app_config,
    monkeypatch,
):
    from trading_assistant import bootstrap
    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.risk.clock import FakeClock

    monkeypatch.setattr(
        bootstrap,
        "_build_container",
        lambda *_args, **_kwargs: SimpleNamespace(
            runtime_tenure_guard=None,
        ),
    )

    container = bootstrap.build_test_container(
        app_config,
        Secrets(app_api_token="plan2-non-app-test-token"),
        broker=MockBroker(),
        clock=FakeClock(is_open=True),
    )

    assert bootstrap._is_test_application_container(container) is False


def test_marked_test_container_revalidates_fake_capabilities_at_use(
    app_config,
    make_service,
):
    from trading_assistant.app import main as app_main
    from trading_assistant.broker.alpaca import AlpacaBroker
    from tests.app_factory import build_test_app_container

    class Agent:
        def chat(self, message, **context):
            return {"reply": message, "context": context}

    service = make_service()
    container = build_test_app_container(
        service,
        Agent(),
        secrets=Secrets(
            app_api_token="plan2-revalidate-test-token"
        ),
    )
    service.broker = AlpacaBroker(
        SimpleNamespace(),
        SimpleNamespace(),
    )

    with pytest.raises(
        RuntimeError,
        match="^test_container_required$",
    ):
        app_main.create_test_app(
            container=container,
            planning=None,
        )


def test_app_prebuild_failure_logs_stable_marker_and_preserves_exception(
    app_config,
    caplog,
    monkeypatch,
):
    from trading_assistant import bootstrap
    from trading_assistant import logging as runtime_logging
    from trading_assistant.app import main as app_main

    secret = "plan2-prebuild-sensitive-token"
    secrets = Secrets(app_api_token=secret)
    receipt = _issue_app_receipt(app_config, secrets)
    failure = RuntimeError(f"prebuild failed with {secret}")

    monkeypatch.setattr(
        runtime_logging,
        "configure_runtime_logging",
        lambda role, loaded: None,
    )
    monkeypatch.setattr(
        bootstrap,
        "build_container",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )

    with caplog.at_level(
        logging.ERROR,
        logger="trading_assistant.startup",
    ):
        with pytest.raises(RuntimeError) as raised:
            app_main.create_app(
                config=app_config,
                secrets=secrets,
                startup_guard_receipt=receipt,
            )

    assert raised.value is failure
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "trading_assistant.startup"
    ]
    assert messages == ["startup_failed role=app"]
    assert secret not in caplog.text


def test_app_postbuild_failure_logs_and_closes_once_without_replacement(
    app_config,
    caplog,
    monkeypatch,
):
    from trading_assistant import logging as runtime_logging
    from trading_assistant.app import main as app_main

    class Guard:
        close_calls = 0

        def close(self):
            self.close_calls += 1
            return True

    secret = "plan2-postbuild-sensitive-token"
    secrets = Secrets(app_api_token=secret)
    receipt = _issue_app_receipt(app_config, secrets)
    guard = Guard()
    container = SimpleNamespace(runtime_tenure_guard=guard)
    failure = RuntimeError(f"app failed with {secret}")

    monkeypatch.setattr(
        runtime_logging,
        "configure_runtime_logging",
        lambda role, loaded: None,
    )
    monkeypatch.setattr(
        app_main,
        "build_default_container",
        lambda **_kwargs: container,
    )
    monkeypatch.setattr(
        app_main,
        "_create_guarded_app",
        lambda **_kwargs: (_ for _ in ()).throw(failure),
    )

    with caplog.at_level(
        logging.ERROR,
        logger="trading_assistant.startup",
    ):
        with pytest.raises(RuntimeError) as raised:
            app_main.create_app(
                config=app_config,
                secrets=secrets,
                startup_guard_receipt=receipt,
            )

    assert raised.value is failure
    assert guard.close_calls == 1
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "trading_assistant.startup"
    ]
    assert messages == ["startup_failed role=app"]
    assert secret not in caplog.text


def test_canonical_launcher_prebuild_failure_logs_and_preserves_cleanup(
    app_config,
    caplog,
    monkeypatch,
):
    from trading_assistant import logging as runtime_logging
    from trading_assistant.ops import serve

    class Control:
        close_calls = 0

        def close(self):
            self.close_calls += 1

    secret = "plan2-launcher-prebuild-sensitive-token"
    secrets = Secrets(app_api_token=secret)
    provider = SimpleNamespace(
        last_successful_role_load_at=datetime.now(timezone.utc)
    )
    control = Control()
    failure = RuntimeError(f"launcher failed with {secret}")

    monkeypatch.setattr(serve, "load_config", lambda: app_config)
    monkeypatch.setattr(
        serve,
        "MacOSKeychainSecretProvider",
        lambda: provider,
    )
    monkeypatch.setattr(
        serve,
        "load_role_secrets",
        lambda *_args, **_kwargs: secrets,
    )
    monkeypatch.setattr(
        serve,
        "run_startup_guard",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        serve,
        "start_app_control",
        lambda _path: control,
    )
    monkeypatch.setattr(
        serve,
        "_build_guarded_container",
        lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(failure)
        ),
    )
    monkeypatch.setattr(
        serve.uvicorn,
        "Server",
        lambda *_args, **_kwargs: pytest.fail(
            "uvicorn must follow guarded composition"
        ),
    )
    monkeypatch.setattr(
        runtime_logging,
        "configure_runtime_logging",
        lambda role, loaded: None,
    )

    with caplog.at_level(
        logging.ERROR,
        logger="trading_assistant.startup",
    ):
        with pytest.raises(RuntimeError) as raised:
            serve.main()

    assert raised.value is failure
    assert control.close_calls == 1
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "trading_assistant.startup"
    ]
    assert messages == ["startup_failed role=app"]
    assert secret not in caplog.text


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
    from trading_assistant import logging as runtime_logging
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
    monkeypatch.setattr(
        runtime_logging,
        "runtime_startup",
        lambda *_args, **_kwargs: nullcontext(),
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
    from tests.app_factory import create_app as create_fake_app

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

    with pytest.raises(
        RuntimeError,
        match="^test_container_required$",
    ):
        app_main.create_test_app(
            service=service,
            agent=Agent(),
            api_token="plan2-explicit-test-token",
            planning=None,
        )
    test_app = create_fake_app(
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
