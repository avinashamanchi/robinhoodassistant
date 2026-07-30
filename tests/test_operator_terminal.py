"""Human-gate contract tests for the line-oriented paper operator menu."""

from __future__ import annotations

import ast
from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading_assistant.ops import operator_terminal
from trading_assistant.ops.operator_api import OperatorApiError
from trading_assistant.ops.operator_terminal import (
    MAX_RENDERED_CHARS,
    PAPER_BANNER,
    InputRejected,
    OperatorMenu,
    confirm_exact,
    parse_identifier,
    parse_positive_int,
    render_json_summary,
    require_reason,
)


TERMINAL_MODULE = (
    Path(__file__).resolve().parent.parent
    / "src/trading_assistant/ops/operator_terminal.py"
)
LOGIN_SECRET = "login-secret-never-print"
REAUTH_SECRET = "reauth-secret-never-print"


class FakeApi:
    """In-memory API boundary with no broker, database, or runtime behavior."""

    def __init__(self) -> None:
        self.gets: list[str] = []
        self.mutations: list[tuple[str, dict[str, object], bool]] = []
        self.login_secrets: list[str] = []
        self.reauth_secrets: list[str] = []
        self.logout_calls = 0
        self._gets: dict[str, deque[object]] = defaultdict(deque)
        self._mutation_results: deque[object] = deque()

    def queue_get(self, path: str, *results: object) -> None:
        self._gets[path].extend(results)

    def queue_mutation(self, *results: object) -> None:
        self._mutation_results.extend(results)

    @staticmethod
    def _resolve(result: object) -> dict[str, object]:
        if isinstance(result, BaseException):
            raise result
        assert isinstance(result, dict)
        return deepcopy(result)

    def login(self, secret: str) -> SimpleNamespace:
        self.login_secrets.append(secret)
        return SimpleNamespace(
            actor="operator:local",
            csrf_token="csrf-never-print",
            expires_at=None,
        )

    def reauthenticate(self, secret: str) -> SimpleNamespace:
        self.reauth_secrets.append(secret)
        return SimpleNamespace(
            actor="operator:local",
            csrf_token="csrf-never-print",
            expires_at=None,
        )

    def logout(self) -> None:
        self.logout_calls += 1

    def get(self, path: str) -> dict[str, object]:
        self.gets.append(path)
        assert self._gets[path], f"unexpected GET {path}"
        return self._resolve(self._gets[path].popleft())

    def mutate(
        self,
        path: str,
        payload: dict[str, object],
        *,
        idempotent: bool,
    ) -> dict[str, object]:
        self.mutations.append((path, deepcopy(payload), idempotent))
        if not self._mutation_results:
            return {"status": "ok"}
        return self._resolve(self._mutation_results.popleft())


@dataclass
class FakeDaemon:
    owns_child: bool = False
    stop_state: str = "off"
    stop_calls: int = 0

    def stop(self) -> SimpleNamespace:
        self.stop_calls += 1
        return SimpleNamespace(
            state=self.stop_state,
            pid=None,
            detail_code=self.stop_state,
        )


class InputFeeder:
    def __init__(self, values: list[object]) -> None:
        self.values = deque(values)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.values:
            raise EOFError
        value = self.values.popleft()
        if isinstance(value, BaseException):
            raise value
        assert isinstance(value, str)
        return value


class SecretFeeder(InputFeeder):
    pass


def build_menu(
    api: FakeApi,
    inputs: list[object],
    *,
    secrets: list[object] | None = None,
    daemon: FakeDaemon | None = None,
) -> tuple[OperatorMenu, list[str], InputFeeder, SecretFeeder, FakeDaemon]:
    output: list[str] = []
    input_fn = InputFeeder(inputs)
    secret_fn = SecretFeeder(
        [LOGIN_SECRET] if secrets is None else secrets
    )
    owned_daemon = daemon or FakeDaemon()
    menu = OperatorMenu(
        api,
        owned_daemon,
        input_fn=input_fn,
        secret_fn=secret_fn,
        output=output.append,
    )
    return menu, output, input_fn, secret_fn, owned_daemon


def imported_modules(tree: ast.AST) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def plan_payload(
    plan_id: int = 7,
    *,
    review_token: str = "plan-review-token-never-print",
    authority_digest: str = "a" * 64,
    status: str = "proposed",
) -> dict[str, object]:
    return {
        "plan_id": plan_id,
        "symbol": "AAPL",
        "status": status,
        "paper_only": True,
        "authority_digest": authority_digest,
        "review_token": review_token,
        "plan": {"action": "buy", "thesis": "test thesis"},
        "sized": {"total_shares": "2"},
    }


def confirmation_payload(
    order_id: int = 9,
    *,
    order_type: str = "limit",
    limit_price: object = "101.50",
    quantity: object = "2",
    notional: object = None,
    side: str = "buy",
    resulting_signed_notional: object = "203.00",
    order_estimated_notional: object = "203.00",
) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "complete": True,
        "missing_proof": [],
        "broker": "Alpaca",
        "mode": "paper",
        "order": {
            "order_id": order_id,
            "symbol": "AAPL",
            "side": side,
            "order_type": order_type,
            "quantity": quantity,
            "notional": notional,
            "limit_price": limit_price,
        },
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "breaker_state": {
            "tripped": False,
            "active_scopes": [],
        },
        "reconciliation": {
            "broker_reconciled": True,
            "pending_exposure_complete": True,
        },
        "exposure": {
            "currency": "USD",
            "current_position_quantity": "0",
            "current_signed_notional": "0",
            "resulting_signed_notional": resulting_signed_notional,
            "order_estimated_notional": order_estimated_notional,
            "quote_observed_at": (
                now - timedelta(seconds=2)
            ).isoformat(),
        },
    }


def test_eof_before_confirmation_performs_no_mutation():
    api = FakeApi()
    menu, _output, _input, _secret, _daemon = build_menu(
        api,
        ["3", "2", "research batch"],
    )

    assert menu.run() == 0
    assert api.mutations == []


def test_terminal_has_no_direct_authority_imports():
    tree = ast.parse(TERMINAL_MODULE.read_text(encoding="utf-8"))
    forbidden = {
        "trading_assistant.bootstrap",
        "trading_assistant.broker",
        "trading_assistant.db",
        "trading_assistant.orders",
        "trading_assistant.service",
        "trading_assistant.llm",
    }
    imported = imported_modules(tree)
    assert not any(
        name == prefix or name.startswith(f"{prefix}.")
        for name in imported
        for prefix in forbidden
    )


@pytest.mark.parametrize("value", ["1", " 20 "])
def test_positive_integer_parser_accepts_only_bounded_decimal_input(value):
    assert parse_positive_int(value, maximum=20) == int(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "0",
        "21",
        "-1",
        "+1",
        "1.0",
        "1/2",
        "../1",
        "1\n",
        "１２",
        "9" * 40,
    ],
)
def test_positive_integer_parser_rejects_ambiguous_or_unbounded_input(value):
    with pytest.raises(InputRejected, match="invalid_positive_integer"):
        parse_positive_int(value, maximum=20)


@pytest.mark.parametrize("value", ["1", "9223372036854775807"])
def test_identifier_parser_accepts_positive_sqlite_identifiers(value):
    assert parse_identifier(value) == int(value)


@pytest.mark.parametrize(
    "value",
    ["", "0", "-2", "+2", "2.0", "2/3", "../2", "2\t", "１２", "9" * 20],
)
def test_identifier_parser_rejects_paths_signs_floats_controls_and_overflow(value):
    with pytest.raises(InputRejected, match="invalid_identifier"):
        parse_identifier(value)


def test_reason_parser_trims_and_bounds_human_text():
    assert require_reason("  reviewed paper intent  ") == "reviewed paper intent"
    assert require_reason("x" * 2_000) == "x" * 2_000


@pytest.mark.parametrize("value", ["", "   ", "line\nbreak", "tab\tvalue", "x" * 2_001])
def test_reason_parser_rejects_blank_control_or_overlong_text(value):
    with pytest.raises(InputRejected, match="invalid_reason"):
        require_reason(value)


def test_confirmation_is_exact_ascii_and_never_normalized():
    assert confirm_exact("GENERATE 2", input_fn=lambda _prompt: "GENERATE 2")
    assert not confirm_exact("GENERATE 2", input_fn=lambda _prompt: " generate 2 ")
    assert not confirm_exact("GENERATE 2", input_fn=lambda _prompt: "GENERATE ２")
    assert not confirm_exact("GENERATE 2", input_fn=lambda _prompt: "GENERATE 2\n")


def test_json_summary_is_deterministic_bounded_and_recursively_redacted():
    payload = {
        "z": 1,
        "secret": "secret-value",
        "nested": {
            "review_token": "review-value",
            "reviewToken": "camel-review-value",
            "csrf": "csrf-value",
            "items": [
                {"cookie": "cookie-value"},
                {"provider_body": "provider-value"},
                {"request_json": "request-value"},
            ],
        },
        "a": "x" * (MAX_RENDERED_CHARS * 2),
    }

    rendered = render_json_summary(payload)

    assert len(rendered) <= MAX_RENDERED_CHARS
    assert rendered == render_json_summary(dict(reversed(payload.items())))
    for value in (
        "secret-value",
        "review-value",
        "camel-review-value",
        "csrf-value",
        "cookie-value",
        "provider-value",
        "request-value",
    ):
        assert value not in rendered


def test_login_uses_secret_prompt_and_prints_exact_paper_banner():
    api = FakeApi()
    menu, output, input_fn, secret_fn, _daemon = build_menu(api, ["0"])

    assert menu.run() == 0

    assert api.login_secrets == [LOGIN_SECRET]
    assert secret_fn.prompts == ["Operator secret: "]
    assert all("secret" not in prompt.lower() for prompt in input_fn.prompts)
    assert PAPER_BANNER in output
    assert PAPER_BANNER == (
        "ALPACA PAPER OPERATOR\n"
        "No action is automatic. Every order requires fresh human approval."
    )
    assert LOGIN_SECRET not in "\n".join(output)
    assert not hasattr(menu, "_secret")


def test_login_rejects_unbounded_secret_before_the_api_boundary():
    api = FakeApi()
    menu, output, _input, _secret, _daemon = build_menu(
        api,
        [],
        secrets=["x" * 4_097],
    )

    assert menu.run() == 0
    assert api.login_secrets == []
    assert "operator_secret_invalid" in output


@pytest.mark.parametrize("interrupt", [EOFError(), KeyboardInterrupt()])
def test_eof_and_interrupt_logout_and_stop_only_the_owned_daemon(interrupt):
    api = FakeApi()
    daemon = FakeDaemon(owns_child=True)
    menu, _output, _input, _secret, _daemon = build_menu(
        api,
        [interrupt],
        daemon=daemon,
    )

    assert menu.run() == 0
    assert daemon.stop_calls == 1
    assert api.logout_calls == 1


@pytest.mark.parametrize(
    "stop_state",
    ["stop_unconfirmed", "running", "starting"],
)
def test_unconfirmed_owned_daemon_cleanup_is_the_only_nonzero_menu_exit(
    stop_state,
):
    api = FakeApi()
    daemon = FakeDaemon(owns_child=True, stop_state=stop_state)
    menu, output, _input, _secret, _daemon = build_menu(
        api,
        ["0"],
        daemon=daemon,
    )

    assert menu.run() == 1
    assert daemon.stop_calls == 1
    assert api.logout_calls == 1
    assert "daemon_cleanup_unconfirmed" in output


def test_unowned_daemon_is_never_stopped_on_exit():
    api = FakeApi()
    daemon = FakeDaemon(owns_child=False)
    menu, _output, _input, _secret, _daemon = build_menu(
        api,
        ["0"],
        daemon=daemon,
    )

    assert menu.run() == 0
    assert daemon.stop_calls == 0


def test_unknown_top_level_choice_is_deterministic():
    api = FakeApi()
    menu, output, _input, _secret, _daemon = build_menu(api, ["?", "0"])

    assert menu.run() == 0
    assert "Invalid choice" in output


def test_all_future_top_level_actions_are_dedicated_no_authority_stubs():
    api = FakeApi()
    menu, output, _input, _secret, _daemon = build_menu(
        api,
        ["1", "2", "7", "8", "9", "0"],
    )

    assert menu.run() == 0
    assert {
        "system_status_not_available_in_task_3",
        "paper_account_not_available_in_task_3",
        "monitoring_not_available_in_task_3",
        "operations_not_available_in_task_3",
        "emergency_safety_not_available_in_task_3",
    }.issubset(output)
    assert api.gets == []
    assert api.mutations == []


def test_generate_requires_exact_phrase_and_performs_no_side_effect_on_mismatch():
    api = FakeApi()
    menu, _output, _input, _secret, _daemon = build_menu(
        api,
        ["3", "2", "research batch", "GENERATE 3", "0"],
    )

    assert menu.run() == 0
    assert api.gets == []
    assert api.mutations == []


def test_generate_calls_only_propose_once_and_labels_every_result_unproven():
    api = FakeApi()
    api.queue_mutation(
        {
            "proposed": [
                {"plan_id": 1, "symbol": "AAPL"},
                {"plan_id": 2, "symbol": "MSFT"},
            ]
        }
    )
    menu, output, _input, _secret, _daemon = build_menu(
        api,
        ["3", "2", "research batch", "GENERATE 2", "0"],
    )

    assert menu.run() == 0
    assert api.gets == []
    assert api.mutations == [
        ("/propose", {"n": 2, "reason": "research batch"}, True)
    ]
    assert output.count("UNPROVEN ANALYST: this may use paid model calls.") == 1
    rendered = [line for line in output if line.startswith("UNPROVEN: ")]
    assert len(rendered) == 2


def test_generate_rejects_an_unbounded_result_list_without_rendering_it():
    api = FakeApi()
    api.queue_mutation(
        {
            "proposed": [
                {"plan_id": index, "symbol": "AAPL"}
                for index in range(21)
            ]
        }
    )
    menu, output, _input, _secret, _daemon = build_menu(
        api,
        ["3", "1", "research batch", "GENERATE 1", "0"],
    )

    assert menu.run() == 0
    assert len(api.mutations) == 1
    assert "proposal_response_invalid" in output
    assert not any(line.startswith("UNPROVEN: ") for line in output)


def test_plan_approval_without_current_process_review_is_blocked_locally():
    api = FakeApi()
    menu, output, _input, _secret, _daemon = build_menu(
        api,
        ["4", "3", "7", "0"],
    )

    assert menu.run() == 0
    assert "plan_review_required" in output
    assert api.gets == []
    assert api.reauth_secrets == []
    assert api.mutations == []


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("review_token", "changed-review-token"),
        ("authority_digest", "b" * 64),
        ("status", "approved"),
        ("plan_id", 8),
    ],
)
def test_plan_approval_invalidates_review_when_any_authority_field_changes(
    changed_field,
    changed_value,
):
    api = FakeApi()
    fresh = plan_payload()
    fresh[changed_field] = changed_value
    api.queue_get("/plans/7", plan_payload(), fresh)
    menu, output, _input, _secret, _daemon = build_menu(
        api,
        ["4", "2", "7", "4", "3", "7", "0"],
    )

    assert menu.run() == 0
    assert api.gets == ["/plans/7", "/plans/7"]
    assert "plan_review_stale" in output
    assert api.reauth_secrets == []
    assert api.mutations == []
    assert menu.reviewed_plans == ()


def test_plan_review_reauth_exact_phrase_and_one_approval():
    api = FakeApi()
    api.queue_get("/plans/7", plan_payload(), plan_payload())
    api.queue_mutation({"status": "approved", "review_token": "must-not-print"})
    menu, output, _input, secret_fn, _daemon = build_menu(
        api,
        [
            "4",
            "2",
            "7",
            "4",
            "3",
            "7",
            "approve reviewed paper plan",
            "APPROVE PAPER PLAN 7",
            "0",
        ],
        secrets=[LOGIN_SECRET, REAUTH_SECRET],
    )

    assert menu.run() == 0
    assert secret_fn.prompts == [
        "Operator secret: ",
        "Operator secret (reauthentication): ",
    ]
    assert api.reauth_secrets == [REAUTH_SECRET]
    assert api.mutations == [
        (
            "/plans/7/approve",
            {
                "reason": "approve reviewed paper plan",
                "review_token": "plan-review-token-never-print",
            },
            True,
        )
    ]
    transcript = "\n".join(output)
    assert "plan-review-token-never-print" not in transcript
    assert "must-not-print" not in transcript
    assert REAUTH_SECRET not in transcript


def test_plan_cancel_requires_reason_and_uses_only_cancel_route():
    api = FakeApi()
    menu, _output, _input, _secret, _daemon = build_menu(
        api,
        ["4", "4", "7", "operator canceled plan", "0"],
    )

    assert menu.run() == 0
    assert api.mutations == [
        (
            "/plans/7/cancel",
            {"reason": "operator canceled plan"},
            True,
        )
    ]


def test_plan_and_pending_list_actions_are_read_only():
    api = FakeApi()
    api.queue_get("/plans", {"plans": []})
    api.queue_get("/pending", {"pending": []})
    menu, _output, _input, _secret, _daemon = build_menu(
        api,
        ["4", "1", "6", "1", "0"],
    )

    assert menu.run() == 0
    assert api.gets == ["/plans", "/pending"]
    assert api.mutations == []


def test_rule_list_and_cancel_use_only_guarded_rule_routes():
    api = FakeApi()
    api.queue_get(
        "/rules",
        {
            "rules": [
                {
                    "rule_id": 4,
                    "status": "active",
                    "cookie": "cookie-never-print",
                }
            ]
        },
    )
    menu, output, _input, _secret, _daemon = build_menu(
        api,
        [
            "5",
            "1",
            "5",
            "2",
            "4",
            "operator canceled rule",
            "0",
        ],
    )

    assert menu.run() == 0
    assert api.gets == ["/rules"]
    assert api.mutations == [
        (
            "/rules/4/cancel",
            {"reason": "operator canceled rule"},
            True,
        )
    ]
    assert "cookie-never-print" not in "\n".join(output)


@pytest.mark.parametrize(
    ("section", "missing_field"),
    [
        (None, "complete"),
        (None, "missing_proof"),
        (None, "broker"),
        (None, "mode"),
        (None, "order"),
        (None, "expires_at"),
        (None, "breaker_state"),
        (None, "reconciliation"),
        (None, "exposure"),
        ("order", "order_id"),
        ("order", "symbol"),
        ("order", "side"),
        ("order", "quantity"),
        ("order", "notional"),
        ("order", "order_type"),
        ("order", "limit_price"),
        ("breaker_state", "tripped"),
        ("breaker_state", "active_scopes"),
        ("reconciliation", "broker_reconciled"),
        ("reconciliation", "pending_exposure_complete"),
        ("exposure", "currency"),
        ("exposure", "current_position_quantity"),
        ("exposure", "current_signed_notional"),
        ("exposure", "resulting_signed_notional"),
        ("exposure", "order_estimated_notional"),
        ("exposure", "quote_observed_at"),
    ],
)
def test_order_approval_blocks_when_each_required_confirmation_field_is_missing(
    section,
    missing_field,
):
    api = FakeApi()
    payload = confirmation_payload()
    container = payload if section is None else payload[section]
    assert isinstance(container, dict)
    container.pop(missing_field)
    api.queue_get("/pending/9/confirmation", payload)
    menu, output, _input, _secret, _daemon = build_menu(api, [])

    menu.approve_pending_order(9)

    assert "pending_confirmation_invalid" in output
    assert api.reauth_secrets == []
    assert api.mutations == []


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("complete",), False),
        (("missing_proof",), ["quote"]),
        (("broker",), "Mock"),
        (("mode",), "live"),
        (("order", "order_id"), True),
        (("order", "symbol"), "AAPL\n"),
        (("order", "side"), "hold"),
        (("order", "quantity"), "NaN"),
        (("order", "order_type"), "stop"),
        (("exposure", "current_position_quantity"), "NaN"),
        (("exposure", "current_signed_notional"), "unknown"),
        (("exposure", "resulting_signed_notional"), "Infinity"),
        (("exposure", "order_estimated_notional"), "unknown"),
        (("exposure", "quote_observed_at"), "not-a-time"),
        (("expires_at",), "not-a-time"),
        (
            ("breaker_state",),
            {"status": "clear", "tripped": False},
        ),
        (
            ("breaker_state",),
            {"tripped": False, "active_scopes": ()},
        ),
        (
            ("reconciliation",),
            {
                "broker_reconciled": True,
                "pending_exposure_complete": "true",
            },
        ),
    ],
)
def test_order_approval_blocks_malformed_or_unknown_confirmation_values(
    path,
    value,
):
    api = FakeApi()
    payload = confirmation_payload()
    container = payload
    for component in path[:-1]:
        nested = container[component]
        assert isinstance(nested, dict)
        container = nested
    container[path[-1]] = value
    api.queue_get("/pending/9/confirmation", payload)
    menu, output, _input, _secret, _daemon = build_menu(api, [])

    menu.approve_pending_order(9)

    assert "pending_confirmation_invalid" in output
    assert api.reauth_secrets == []
    assert api.mutations == []


def test_actual_legacy_route_confirmation_shape_is_rejected():
    now = datetime.now(timezone.utc)
    legacy_route_payload = {
        "complete": True,
        "missing_proof": [],
        "broker": "Alpaca",
        "mode": "paper",
        "order": {
            "order_id": 9,
            "symbol": "AAPL",
            "side": "buy",
            "order_type": "limit",
            "quantity": "2",
            "notional": None,
            "limit_price": "101.50",
        },
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "exposure": {
            "currency": "USD",
            "current_position_quantity": "0",
            "current_signed_notional": "0",
            "resulting_signed_notional": "203.00",
            "as_of": (now - timedelta(seconds=2)).isoformat(),
        },
    }

    with pytest.raises(
        InputRejected,
        match="pending_confirmation_invalid",
    ):
        OperatorMenu._validate_confirmation(
            legacy_route_payload,
            expected_order_id=9,
        )


@pytest.mark.parametrize(
    ("breaker_state", "reconciliation"),
    [
        (
            {"tripped": False, "active_scopes": ["liquidity:AAPL"]},
            {
                "broker_reconciled": True,
                "pending_exposure_complete": True,
            },
        ),
        (
            {"tripped": False, "active_scopes": []},
            {
                "broker_reconciled": True,
                "pending_exposure_complete": False,
            },
        ),
    ],
)
def test_order_approval_rejects_contradictory_safety_proof_before_prompt(
    breaker_state,
    reconciliation,
):
    api = FakeApi()
    payload = confirmation_payload()
    payload["breaker_state"] = breaker_state
    payload["reconciliation"] = reconciliation
    api.queue_get("/pending/9/confirmation", payload)
    menu, output, input_fn, _secret, _daemon = build_menu(api, [])

    menu.approve_pending_order(9)

    assert output == ["pending_confirmation_invalid"]
    assert input_fn.prompts == []
    assert api.reauth_secrets == []
    assert api.mutations == []


@pytest.mark.parametrize(
    ("quantity", "notional", "valid"),
    [
        ("2", None, True),
        (None, "203.00", True),
        ("2", "203.00", False),
        (None, None, False),
    ],
)
def test_order_approval_requires_exactly_one_positive_size(
    quantity,
    notional,
    valid,
):
    payload = confirmation_payload(
        quantity=quantity,
        notional=notional,
    )

    if valid:
        confirmation = OperatorMenu._validate_confirmation(
            payload,
            expected_order_id=9,
        )
        order = confirmation.rendered["order"]
        assert isinstance(order, dict)
        assert order["quantity"] == quantity
        assert order["notional"] == notional
    else:
        with pytest.raises(
            InputRejected,
            match="pending_confirmation_invalid",
        ):
            OperatorMenu._validate_confirmation(
                payload,
                expected_order_id=9,
            )


def test_notional_order_is_rendered_as_notional_not_quantity():
    payload = confirmation_payload(
        quantity=None,
        notional="203.00",
    )

    confirmation = OperatorMenu._validate_confirmation(
        payload,
        expected_order_id=9,
    )

    order = confirmation.rendered["order"]
    assert isinstance(order, dict)
    assert order["quantity"] is None
    assert order["notional"] == "203.00"
    assert "qty" not in order


def test_sell_to_flat_zero_resulting_exposure_is_valid():
    payload = confirmation_payload(
        side="sell",
        resulting_signed_notional="0",
        order_estimated_notional="200",
    )

    confirmation = OperatorMenu._validate_confirmation(
        payload,
        expected_order_id=9,
    )

    exposure = confirmation.rendered["exposure"]
    assert isinstance(exposure, dict)
    assert exposure["resulting_signed_notional"] == "0"


def test_order_approval_blocks_changed_id_stale_quote_and_expired_proposal():
    now = datetime.now(timezone.utc)
    cases = [
        (("order", "order_id"), 10),
        (
            ("exposure", "quote_observed_at"),
            (now - timedelta(minutes=10)).isoformat(),
        ),
        (
            ("expires_at",),
            (now - timedelta(seconds=1)).isoformat(),
        ),
    ]

    for path, value in cases:
        api = FakeApi()
        payload = confirmation_payload()
        container = payload
        for component in path[:-1]:
            nested = container[component]
            assert isinstance(nested, dict)
            container = nested
        container[path[-1]] = value
        api.queue_get("/pending/9/confirmation", payload)
        menu, output, _input, _secret, _daemon = build_menu(api, [])

        menu.approve_pending_order(9)

        assert "pending_confirmation_invalid" in output
        assert api.reauth_secrets == []
        assert api.mutations == []


@pytest.mark.parametrize(
    ("order_type", "limit_price", "valid"),
    [
        ("market", None, True),
        ("market", "100", False),
        ("limit", None, False),
        ("limit", "0", False),
        ("limit", "100.25", True),
    ],
)
def test_order_approval_enforces_market_versus_limit_price(
    order_type,
    limit_price,
    valid,
):
    api = FakeApi()
    api.queue_get(
        "/pending/9/confirmation",
        confirmation_payload(
            order_type=order_type,
            limit_price=limit_price,
        ),
    )
    inputs = (
        ["reviewed order", "not the phrase"]
        if valid
        else []
    )
    menu, output, _input, _secret, _daemon = build_menu(
        api,
        inputs,
        secrets=[REAUTH_SECRET],
    )

    menu.approve_pending_order(9)

    if valid:
        assert "pending_confirmation_invalid" not in output
        assert api.reauth_secrets == [REAUTH_SECRET]
    else:
        assert "pending_confirmation_invalid" in output
        assert api.reauth_secrets == []
    assert api.mutations == []


def test_order_review_reauth_exact_phrase_and_one_approval():
    api = FakeApi()
    api.queue_get("/pending/9/confirmation", confirmation_payload())
    menu, output, _input, secret_fn, _daemon = build_menu(
        api,
        [
            "6",
            "2",
            "9",
            "approve refreshed paper order",
            "APPROVE ALPACA PAPER ORDER 9",
            "0",
        ],
        secrets=[LOGIN_SECRET, REAUTH_SECRET],
    )

    assert menu.run() == 0
    assert api.gets == ["/pending/9/confirmation"]
    assert api.reauth_secrets == [REAUTH_SECRET]
    assert api.mutations == [
        (
            "/approve/9",
            {"reason": "approve refreshed paper order"},
            True,
        )
    ]
    assert secret_fn.prompts[-1] == "Operator secret (reauthentication): "
    assert "ALPACA PAPER ORDER" in "\n".join(output)


def test_acceptance_unknown_is_shown_once_and_never_retried():
    api = FakeApi()
    api.queue_get("/pending/9/confirmation", confirmation_payload())
    api.queue_mutation(
        OperatorApiError(
            status=409,
            code="acceptance_unknown",
            message="provider-body-never-print",
            request_id="request-json-never-print",
        )
    )
    menu, output, _input, _secret, _daemon = build_menu(
        api,
        [
            "6",
            "2",
            "9",
            "approve refreshed paper order",
            "APPROVE ALPACA PAPER ORDER 9",
            "0",
        ],
        secrets=[LOGIN_SECRET, REAUTH_SECRET],
    )

    assert menu.run() == 0
    assert len(api.mutations) == 1
    transcript = "\n".join(output)
    assert transcript.count("acceptance_unknown") == 1
    assert "provider-body-never-print" not in transcript
    assert "request-json-never-print" not in transcript


def test_acceptance_unknown_blocks_a_second_manual_attempt_in_same_process():
    api = FakeApi()
    api.queue_get("/pending/9/confirmation", confirmation_payload())
    api.queue_mutation(
        OperatorApiError(
            status=409,
            code="acceptance_unknown",
            message="stable message",
        )
    )
    menu, output, _input, _secret, _daemon = build_menu(
        api,
        [
            "6",
            "2",
            "9",
            "approve refreshed paper order",
            "APPROVE ALPACA PAPER ORDER 9",
            "6",
            "2",
            "9",
            "0",
        ],
        secrets=[LOGIN_SECRET, REAUTH_SECRET],
    )

    assert menu.run() == 0
    assert api.gets == ["/pending/9/confirmation"]
    assert len(api.mutations) == 1
    transcript = "\n".join(output)
    assert transcript.count("acceptance_unknown") == 1
    assert "order_approval_retry_prohibited" in output


@pytest.mark.parametrize(
    ("submenu_choice", "path"),
    [
        ("3", "/reject/9"),
        ("4", "/orders/9/cancel"),
    ],
)
def test_order_reject_and_cancel_require_reason_and_mutate_once(
    submenu_choice,
    path,
):
    api = FakeApi()
    menu, _output, _input, _secret, _daemon = build_menu(
        api,
        [
            "6",
            submenu_choice,
            "9",
            "operator reviewed order",
            "0",
        ],
    )

    assert menu.run() == 0
    assert api.mutations == [
        (
            path,
            {"reason": "operator reviewed order"},
            True,
        )
    ]


@pytest.mark.parametrize("submenu_choice", ["3", "4"])
def test_order_reject_and_cancel_blank_reason_have_no_side_effect(
    submenu_choice,
):
    api = FakeApi()
    menu, output, _input, _secret, _daemon = build_menu(
        api,
        ["6", submenu_choice, "9", "   ", "0"],
    )

    assert menu.run() == 0
    assert "invalid_reason" in output
    assert api.mutations == []


@pytest.mark.parametrize("status", [401, 403, 409, 429, 503])
def test_api_statuses_remain_distinct_without_raw_error_leakage(status):
    api = FakeApi()
    api.queue_get(
        "/rules",
        OperatorApiError(
            status=status,
            code=f"stable_{status}",
            message="provider-body-never-print",
            request_id="request-json-never-print",
            retry_after=3,
        ),
    )
    menu, output, _input, _secret, _daemon = build_menu(
        api,
        ["5", "1", "0"],
    )

    assert menu.run() == 0
    transcript = "\n".join(output)
    assert f"status={status}" in transcript
    assert f"code=stable_{status}" in transcript
    assert "provider-body-never-print" not in transcript
    assert "request-json-never-print" not in transcript


def test_api_error_displays_only_a_canonical_server_request_id():
    api = FakeApi()
    api.queue_get(
        "/rules",
        OperatorApiError(
            status=503,
            code="dependency_unavailable",
            message="Stable message",
            request_id="a" * 32,
        ),
    )
    menu, output, _input, _secret, _daemon = build_menu(
        api,
        ["5", "1", "0"],
    )

    assert menu.run() == 0
    assert f"request_id={'a' * 32}" in "\n".join(output)


def test_terminal_output_never_leaks_sensitive_response_fields_or_secrets():
    api = FakeApi()
    api.queue_get(
        "/plans/7",
        {
            **plan_payload(),
            "secret": "secret-never-print",
            "nested": {
                "cookie": "cookie-never-print",
                "csrf_token": "csrf-never-print",
                "provider_body": "provider-body-never-print",
                "request_json": "request-json-never-print",
            },
        },
    )
    menu, output, _input, _secret, _daemon = build_menu(
        api,
        ["4", "2", "7", "0"],
    )

    assert menu.run() == 0
    transcript = "\n".join(output)
    for forbidden in (
        LOGIN_SECRET,
        "secret-never-print",
        "cookie-never-print",
        "csrf-never-print",
        "plan-review-token-never-print",
        "provider-body-never-print",
        "request-json-never-print",
    ):
        assert forbidden not in transcript
    json.dumps(output, ensure_ascii=True)


def test_main_constructs_only_the_fixed_root_client_and_injected_menu(
    monkeypatch,
):
    seen: dict[str, object] = {}

    class Client:
        def __init__(self, project_root):
            seen["project_root"] = project_root

    class Menu:
        def __init__(self, api, daemon):
            seen["api"] = api
            seen["daemon"] = daemon

        def run(self):
            return 0

    monkeypatch.setattr(operator_terminal, "OperatorApiClient", Client)
    monkeypatch.setattr(operator_terminal, "OperatorMenu", Menu)

    assert operator_terminal.main([]) == 0
    assert seen["project_root"] == Path(
        "/Users/avi/Desktop/robinhood/trading-assistant"
    )
    assert seen["api"].__class__ is Client
    assert seen["daemon"].owns_child is False


def test_main_rejects_all_arguments_without_echoing_them(
    monkeypatch,
    capsys,
):
    def forbidden_client(_project_root):
        raise AssertionError("client must not be constructed")

    monkeypatch.setattr(
        operator_terminal,
        "OperatorApiClient",
        forbidden_client,
    )

    assert operator_terminal.main(["--url", "https://secret.test"]) == 2
    output = capsys.readouterr().out
    assert output.strip() == "operator_arguments_not_supported"
    assert "secret.test" not in output
