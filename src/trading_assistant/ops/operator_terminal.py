"""Bounded, human-gated terminal menu for the Alpaca paper operator API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import getpass
import json
from pathlib import Path
import re
import sys
import unicodedata
from typing import Any, Callable

from .operator_api import OperatorApiClient, OperatorApiError


CANONICAL_PROJECT_ROOT = Path(
    "/Users/avi/Desktop/robinhood/trading-assistant"
)
PAPER_BANNER = (
    "ALPACA PAPER OPERATOR\n"
    "No action is automatic. Every order requires fresh human approval."
)
SENSITIVE_KEYS = {
    "secret",
    "token",
    "csrf",
    "cookie",
    "authorization",
    "api_key",
    "key",
    "credential",
}
ORDER_CONFIRMATION_FIELDS = (
    "order_id",
    "ticker",
    "side",
    "qty",
    "order_type",
    "estimated_exposure",
    "quote_observed_at",
    "expires_at",
    "breaker_state",
    "reconciliation",
)

MAX_RENDERED_CHARS = 32_768
_MAX_RENDER_DEPTH = 8
_MAX_COLLECTION_ITEMS = 100
_MAX_RENDERED_STRING_CHARS = 2_000
_MAX_CONFIRMATION_CHARS = 200
_MAX_SECRET_CHARS = 4_096
_MAX_IDENTIFIER = 2**63 - 1
_MAX_QUOTE_AGE_SECONDS = 300.0
_MAX_CLOCK_SKEW_SECONDS = 5.0
_REDACTED = "<redacted>"
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TICKER = re.compile(r"^[A-Z0-9][A-Z0-9./-]{0,19}$")
_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_SAFE_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
_UNSAFE_EVIDENCE_STATES = frozenset(
    {
        "blocked",
        "conflict",
        "conflicting",
        "error",
        "failed",
        "failure",
        "missing",
        "partial",
        "stale",
        "tripped",
        "unconfirmed",
        "unknown",
        "unreconciled",
    }
)

_TOP_LEVEL_MENU = (
    "1 System status | 2 Alpaca paper account | "
    "3 Generate analyst proposals | 4 Plans | 5 Rules | "
    "6 Pending orders | 7 Monitoring | 8 Operations | "
    "9 Emergency safety | 0 Log out and exit"
)
_PLAN_MENU = (
    "Plans: 1 List | 2 Review | 3 Approve reviewed plan | "
    "4 Cancel | 0 Back"
)
_RULE_MENU = "Rules: 1 List | 2 Cancel | 0 Back"
_PENDING_MENU = (
    "Pending orders: 1 List | 2 Approve | 3 Reject | 4 Cancel | 0 Back"
)


class InputRejected(ValueError):
    """Stable local validation failure that never includes raw input."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class ReviewedPlan:
    plan_id: int
    review_token: str
    authority_digest: str
    status: str

    def __repr__(self) -> str:
        return (
            "ReviewedPlan("
            f"plan_id={self.plan_id}, status={self.status!r}, "
            "review_token=<redacted>, authority_digest=<redacted>)"
        )


@dataclass(frozen=True)
class _OrderConfirmation:
    order_id: int
    quote_observed_at: datetime
    expires_at: datetime
    rendered: dict[str, object]


def _has_control(value: str) -> bool:
    return any(
        unicodedata.category(character).startswith("C")
        for character in value
    )


def _bounded_secret(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_SECRET_CHARS
        or _has_control(value)
    ):
        raise InputRejected("operator_secret_invalid")
    return value


def parse_positive_int(value: str, maximum: int = 20) -> int:
    """Parse one canonical positive ASCII integer under a fixed maximum."""
    if (
        isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or maximum <= 0
    ):
        raise ValueError("maximum must be a positive integer")
    if not isinstance(value, str) or _has_control(value):
        raise InputRejected("invalid_positive_integer")
    normalized = value.strip(" ")
    if (
        not normalized
        or not normalized.isascii()
        or not normalized.isdecimal()
        or (len(normalized) > 1 and normalized.startswith("0"))
        or len(normalized) > len(str(maximum))
    ):
        raise InputRejected("invalid_positive_integer")
    parsed = int(normalized)
    if parsed <= 0 or parsed > maximum:
        raise InputRejected("invalid_positive_integer")
    return parsed


def parse_identifier(value: str) -> int:
    """Parse one canonical positive SQLite-sized route identifier."""
    try:
        return parse_positive_int(value, maximum=_MAX_IDENTIFIER)
    except InputRejected:
        raise InputRejected("invalid_identifier") from None


def require_reason(value: str, maximum: int = 2_000) -> str:
    """Return bounded nonblank human text without terminal controls."""
    if (
        isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or maximum <= 0
    ):
        raise ValueError("maximum must be a positive integer")
    if not isinstance(value, str) or _has_control(value):
        raise InputRejected("invalid_reason")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise InputRejected("invalid_reason")
    return normalized


def confirm_exact(
    expected: str,
    *,
    input_fn: Callable[[str], str] = input,
) -> bool:
    """Require one exact printable ASCII phrase without normalization."""
    if (
        not isinstance(expected, str)
        or not expected
        or len(expected) > _MAX_CONFIRMATION_CHARS
        or not expected.isascii()
        or _has_control(expected)
    ):
        raise ValueError("confirmation phrase is invalid")
    provided = input_fn("Confirmation: ")
    return (
        isinstance(provided, str)
        and len(provided) <= _MAX_CONFIRMATION_CHARS
        and provided.isascii()
        and not _has_control(provided)
        and provided == expected
    )


def _sensitive_key(key: str) -> bool:
    separated = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", key)
    separated = re.sub(
        r"([a-z0-9])([A-Z])",
        r"\1_\2",
        separated,
    )
    normalized = re.sub(
        r"[^a-z0-9]+",
        "_",
        separated.lower(),
    ).strip("_")
    if normalized in SENSITIVE_KEYS:
        return True
    components = frozenset(normalized.split("_"))
    if components.intersection(SENSITIVE_KEYS):
        return True
    return normalized in {
        "headers",
        "idempotency_key",
        "provider_body",
        "raw_body",
        "raw_response",
        "request_body",
        "request_json",
        "response_body",
        "set_cookie",
    }


def _bounded_redacted(value: object, *, depth: int = 0) -> object:
    if depth >= _MAX_RENDER_DEPTH:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not Decimal(str(value)).is_finite():
            return "unknown"
        return value
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else "unknown"
    if isinstance(value, str):
        if len(value) <= _MAX_RENDERED_STRING_CHARS:
            return value
        return (
            value[: _MAX_RENDERED_STRING_CHARS - 12]
            + "<truncated>"
        )
    if isinstance(value, dict):
        result: dict[str, object] = {}
        string_keys = sorted(key for key in value if isinstance(key, str))
        for key in string_keys[:_MAX_COLLECTION_ITEMS]:
            if _sensitive_key(key):
                result[key] = _REDACTED
            else:
                result[key] = _bounded_redacted(
                    value[key],
                    depth=depth + 1,
                )
        if len(string_keys) > _MAX_COLLECTION_ITEMS:
            result["<truncated>"] = (
                len(string_keys) - _MAX_COLLECTION_ITEMS
            )
        invalid_keys = len(value) - len(string_keys)
        if invalid_keys:
            result["<invalid-keys>"] = invalid_keys
        return result
    if isinstance(value, (list, tuple)):
        items = [
            _bounded_redacted(item, depth=depth + 1)
            for item in value[:_MAX_COLLECTION_ITEMS]
        ]
        if len(value) > _MAX_COLLECTION_ITEMS:
            items.append("<truncated>")
        return items
    return "<unsupported>"


def render_json_summary(value: object) -> str:
    """Render redacted JSON with deterministic ordering and a hard bound."""
    rendered = json.dumps(
        _bounded_redacted(value),
        ensure_ascii=True,
        sort_keys=True,
    )
    if len(rendered) > MAX_RENDERED_CHARS:
        return json.dumps(
            {"summary": "output_too_large"},
            ensure_ascii=True,
            sort_keys=True,
        )
    return rendered


class OperatorMenu:
    """Presentation-only menu over the guarded local operator API."""

    def __init__(
        self,
        api: OperatorApiClient,
        daemon: object,
        *,
        input_fn: Callable[[str], str] = input,
        secret_fn: Callable[[str], str] = getpass.getpass,
        output: Callable[[str], Any] = print,
    ) -> None:
        self.api = api
        self.daemon = daemon
        self._input_fn = input_fn
        self._secret_fn = secret_fn
        self._output = output
        self._reviewed: dict[int, ReviewedPlan] = {}
        self._acceptance_unknown_order_ids: set[int] = set()
        self._actions: dict[str, Callable[[], bool]] = {
            "1": self._system_status,
            "2": self._paper_account,
            "3": self._generate_proposals,
            "4": self._plans,
            "5": self._rules,
            "6": self._pending_orders,
            "7": self._monitoring,
            "8": self._operations,
            "9": self._emergency_safety,
            "0": self._logout_and_exit,
        }

    @property
    def reviewed_plans(self) -> tuple[int, ...]:
        """Expose only non-sensitive process-local review identities."""
        return tuple(sorted(self._reviewed))

    def _input(self, prompt: str) -> str:
        return self._input_fn(prompt)

    def _write(self, value: str) -> None:
        self._output(value)

    @staticmethod
    def _menu_choice(value: object) -> str:
        if (
            not isinstance(value, str)
            or len(value) > 8
            or _has_control(value)
        ):
            return ""
        return value.strip(" ")

    def run(self) -> int:
        """Log in, run bounded dispatch, and clean up owned state on exit."""
        authenticated = False
        try:
            try:
                secret = _bounded_secret(
                    self._secret_fn("Operator secret: ")
                )
                try:
                    self.api.login(secret)
                    authenticated = True
                finally:
                    secret = None
            except (EOFError, KeyboardInterrupt):
                pass
            except InputRejected as error:
                self._write(error.code)
            except OperatorApiError as error:
                self._show_api_error(error)
            except Exception:
                self._write("operator_login_failed")

            if authenticated:
                self._write(PAPER_BANNER)
                while True:
                    self._write(_TOP_LEVEL_MENU)
                    try:
                        choice = self._menu_choice(
                            self._input("Choice: ")
                        )
                        action = self._actions.get(choice)
                        if action is None:
                            self._write("Invalid choice")
                            continue
                        if action():
                            break
                    except (EOFError, KeyboardInterrupt):
                        break
                    except InputRejected as error:
                        self._write(error.code)
                    except OperatorApiError as error:
                        self._show_api_error(error)
                        if error.status == 401:
                            break
                    except Exception:
                        self._write("operation_failed")
        finally:
            cleanup_confirmed = self._cleanup_owned_daemon()
            if authenticated:
                try:
                    self.api.logout()
                except OperatorApiError as error:
                    self._show_api_error(error)
                except Exception:
                    self._write("operator_logout_failed")
        return 0 if cleanup_confirmed else 1

    def _cleanup_owned_daemon(self) -> bool:
        try:
            if getattr(self.daemon, "owns_child", False) is not True:
                return True
            result = self.daemon.stop()
            if getattr(result, "state", None) not in {"off", "exited"}:
                self._write("daemon_cleanup_unconfirmed")
                return False
            return True
        except Exception:
            self._write("daemon_cleanup_unconfirmed")
            return False

    def _show_api_error(self, error: OperatorApiError) -> None:
        status = (
            str(error.status)
            if isinstance(error.status, int)
            and not isinstance(error.status, bool)
            else "none"
        )
        code = (
            error.code
            if isinstance(error.code, str)
            and _SAFE_ERROR_CODE.fullmatch(error.code)
            else "unknown"
        )
        fields = [f"api_error status={status}", f"code={code}"]
        if (
            isinstance(error.request_id, str)
            and _SAFE_REQUEST_ID.fullmatch(error.request_id)
        ):
            fields.append(f"request_id={error.request_id}")
        if (
            isinstance(error.retry_after, int)
            and not isinstance(error.retry_after, bool)
            and 0 <= error.retry_after <= 3_600
        ):
            fields.append(f"retry_after={error.retry_after}")
        if code == "acceptance_unknown":
            fields.append("retry=prohibited")
        self._write(" ".join(fields))

    def _confirm_exact(self, expected: str) -> bool:
        self._write(f"Type exactly: {expected}")
        return confirm_exact(expected, input_fn=self._input)

    def _reauthenticate(self) -> None:
        secret = _bounded_secret(
            self._secret_fn(
                "Operator secret (reauthentication): "
            )
        )
        try:
            self.api.reauthenticate(secret)
        finally:
            secret = None

    def _system_status(self) -> bool:
        self._write("system_status_not_available_in_task_3")
        return False

    def _paper_account(self) -> bool:
        self._write("paper_account_not_available_in_task_3")
        return False

    def _monitoring(self) -> bool:
        self._write("monitoring_not_available_in_task_3")
        return False

    def _operations(self) -> bool:
        self._write("operations_not_available_in_task_3")
        return False

    def _emergency_safety(self) -> bool:
        self._write("emergency_safety_not_available_in_task_3")
        return False

    @staticmethod
    def _logout_and_exit() -> bool:
        return True

    def _generate_proposals(self) -> bool:
        self.generate_proposals()
        return False

    def generate_proposals(self) -> None:
        count = parse_positive_int(
            self._input("Proposal count (1-20): "),
            maximum=20,
        )
        reason = require_reason(self._input("Reason: "))
        self._write(
            "UNPROVEN ANALYST: this may use paid model calls."
        )
        if not self._confirm_exact(f"GENERATE {count}"):
            self._write("Canceled")
            return
        payload = self.api.mutate(
            "/propose",
            {"n": count, "reason": reason},
            idempotent=True,
        )
        proposed = payload.get("proposed")
        if isinstance(proposed, list):
            if (
                len(proposed) > count
                or any(
                    not isinstance(result, dict)
                    for result in proposed
                )
            ):
                self._write("proposal_response_invalid")
                return
            for result in proposed:
                self._write(
                    f"UNPROVEN: {render_json_summary(result)}"
                )
            return
        self._write(f"UNPROVEN: {render_json_summary(payload)}")

    def _plans(self) -> bool:
        self._write(_PLAN_MENU)
        choice = self._menu_choice(self._input("Plan choice: "))
        if choice == "0":
            return False
        if choice == "1":
            self.list_plans()
        elif choice == "2":
            self.review_plan(
                parse_identifier(self._input("Plan ID: "))
            )
        elif choice == "3":
            self.approve_plan(
                parse_identifier(self._input("Plan ID: "))
            )
        elif choice == "4":
            self.cancel_plan(
                parse_identifier(self._input("Plan ID: "))
            )
        else:
            self._write("Invalid choice")
        return False

    def list_plans(self) -> None:
        payload = self.api.get("/plans")
        self._write(
            f"Plans: {render_json_summary(payload)}"
        )

    @staticmethod
    def _identifier(value: int | str) -> int:
        if isinstance(value, bool):
            raise InputRejected("invalid_identifier")
        if isinstance(value, int):
            value = str(value)
        return parse_identifier(value)

    @staticmethod
    def _review_from_payload(
        payload: object,
        *,
        expected_plan_id: int,
    ) -> ReviewedPlan:
        if not isinstance(payload, dict):
            raise InputRejected("plan_review_invalid")
        plan_id = payload.get("plan_id")
        review_token = payload.get("review_token")
        authority_digest = payload.get("authority_digest")
        status = payload.get("status")
        if (
            isinstance(plan_id, bool)
            or not isinstance(plan_id, int)
            or plan_id != expected_plan_id
            or not isinstance(review_token, str)
            or not review_token
            or len(review_token) > 2_000
            or _has_control(review_token)
            or not isinstance(authority_digest, str)
            or _HEX_DIGEST.fullmatch(authority_digest) is None
            or not isinstance(status, str)
            or not status
            or len(status) > 64
            or _has_control(status)
        ):
            raise InputRejected("plan_review_invalid")
        return ReviewedPlan(
            plan_id=plan_id,
            review_token=review_token,
            authority_digest=authority_digest,
            status=status,
        )

    def review_plan(
        self,
        plan_id: int | str,
    ) -> ReviewedPlan | None:
        canonical_id = self._identifier(plan_id)
        payload = self.api.get(f"/plans/{canonical_id}")
        self._write(
            f"Plan review: {render_json_summary(payload)}"
        )
        try:
            reviewed = self._review_from_payload(
                payload,
                expected_plan_id=canonical_id,
            )
        except InputRejected:
            self._reviewed.pop(canonical_id, None)
            self._write("plan_review_invalid")
            return None
        self._reviewed[canonical_id] = reviewed
        return reviewed

    def approve_plan(self, plan_id: int | str) -> None:
        canonical_id = self._identifier(plan_id)
        reviewed = self._reviewed.get(canonical_id)
        if reviewed is None:
            self._write("plan_review_required")
            return
        try:
            payload = self.api.get(f"/plans/{canonical_id}")
            refreshed = self._review_from_payload(
                payload,
                expected_plan_id=canonical_id,
            )
        except (InputRejected, KeyError, TypeError):
            self._reviewed.pop(canonical_id, None)
            self._write("plan_review_stale")
            return
        if (
            refreshed != reviewed
            or reviewed.status != "proposed"
        ):
            self._reviewed.pop(canonical_id, None)
            self._write("plan_review_stale")
            return
        self._write(
            "Plan approval refresh: "
            f"{render_json_summary(payload)}"
        )
        reason = require_reason(self._input("Reason: "))
        self._reauthenticate()
        if not self._confirm_exact(
            f"APPROVE PAPER PLAN {canonical_id}"
        ):
            self._write("Canceled")
            return
        self._reviewed.pop(canonical_id, None)
        result = self.api.mutate(
            f"/plans/{canonical_id}/approve",
            {
                "reason": reason,
                "review_token": reviewed.review_token,
            },
            idempotent=True,
        )
        self._write(
            f"Plan approval: {render_json_summary(result)}"
        )

    def cancel_plan(self, plan_id: int | str) -> None:
        canonical_id = self._identifier(plan_id)
        reason = require_reason(self._input("Reason: "))
        self._reviewed.pop(canonical_id, None)
        result = self.api.mutate(
            f"/plans/{canonical_id}/cancel",
            {"reason": reason},
            idempotent=True,
        )
        self._write(
            f"Plan cancellation: {render_json_summary(result)}"
        )

    def _rules(self) -> bool:
        self._write(_RULE_MENU)
        choice = self._menu_choice(self._input("Rule choice: "))
        if choice == "0":
            return False
        if choice == "1":
            self.list_rules()
        elif choice == "2":
            self.cancel_rule(
                parse_identifier(self._input("Rule ID: "))
            )
        else:
            self._write("Invalid choice")
        return False

    def list_rules(self) -> None:
        payload = self.api.get("/rules")
        self._write(
            f"Rules: {render_json_summary(payload)}"
        )

    def cancel_rule(self, rule_id: int | str) -> None:
        canonical_id = self._identifier(rule_id)
        reason = require_reason(self._input("Reason: "))
        result = self.api.mutate(
            f"/rules/{canonical_id}/cancel",
            {"reason": reason},
            idempotent=True,
        )
        self._write(
            f"Rule cancellation: {render_json_summary(result)}"
        )

    def _pending_orders(self) -> bool:
        self._write(_PENDING_MENU)
        choice = self._menu_choice(
            self._input("Pending-order choice: ")
        )
        if choice == "0":
            return False
        if choice == "1":
            self.list_pending_orders()
        elif choice == "2":
            self.approve_pending_order(
                parse_identifier(self._input("Order ID: "))
            )
        elif choice == "3":
            self.reject_pending_order(
                parse_identifier(self._input("Order ID: "))
            )
        elif choice == "4":
            self.cancel_order(
                parse_identifier(self._input("Order ID: "))
            )
        else:
            self._write("Invalid choice")
        return False

    def list_pending_orders(self) -> None:
        payload = self.api.get("/pending")
        self._write(
            f"Pending orders: {render_json_summary(payload)}"
        )

    @staticmethod
    def _positive_decimal(value: object) -> Decimal:
        if isinstance(value, bool) or value is None:
            raise InputRejected("pending_confirmation_invalid")
        if isinstance(value, str):
            if (
                not value
                or len(value) > 80
                or _has_control(value)
            ):
                raise InputRejected(
                    "pending_confirmation_invalid"
                )
            raw = value
        elif isinstance(value, (int, float, Decimal)):
            raw = str(value)
        else:
            raise InputRejected("pending_confirmation_invalid")
        try:
            parsed = Decimal(raw)
        except (InvalidOperation, ValueError):
            raise InputRejected(
                "pending_confirmation_invalid"
            ) from None
        if not parsed.is_finite() or parsed <= 0:
            raise InputRejected("pending_confirmation_invalid")
        return parsed

    @staticmethod
    def _timestamp(value: object) -> datetime:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 80
            or _has_control(value)
        ):
            raise InputRejected("pending_confirmation_invalid")
        canonical = (
            value[:-1] + "+00:00"
            if value.endswith("Z")
            else value
        )
        try:
            parsed = datetime.fromisoformat(canonical)
        except ValueError:
            raise InputRejected(
                "pending_confirmation_invalid"
            ) from None
        if (
            parsed.tzinfo is None
            or parsed.utcoffset() is None
        ):
            raise InputRejected("pending_confirmation_invalid")
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _evidence_has_unknown(cls, value: object) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            normalized = re.sub(
                r"[^a-z0-9]+",
                "_",
                value.lower(),
            ).strip("_")
            if normalized in {
                "clean",
                "clear",
                "current",
                "not_tripped",
                "ok",
                "ready",
                "reconciled",
            }:
                return False
            return (
                normalized in _UNSAFE_EVIDENCE_STATES
                or bool(
                    set(normalized.split("_")).intersection(
                        _UNSAFE_EVIDENCE_STATES
                    )
                )
            )
        if isinstance(value, dict):
            return (
                not value
                or any(
                    (
                        isinstance(key, str)
                        and bool(
                            set(
                                re.sub(
                                    r"[^a-z0-9]+",
                                    "_",
                                    key.lower(),
                                )
                                .strip("_")
                                .split("_")
                            ).intersection(
                                {
                                    "failed",
                                    "missing",
                                    "partial",
                                    "stale",
                                    "unconfirmed",
                                    "unknown",
                                }
                            )
                        )
                    )
                    or cls._evidence_has_unknown(item)
                    for key, item in value.items()
                )
            )
        if isinstance(value, (list, tuple)):
            return (
                not value
                or any(
                    cls._evidence_has_unknown(item)
                    for item in value
                )
            )
        return False

    @classmethod
    def _validate_breaker_state(cls, value: object) -> None:
        if (
            not isinstance(value, dict)
            or not value
            or cls._evidence_has_unknown(value)
        ):
            raise InputRejected("pending_confirmation_invalid")
        if "tripped" in value and value["tripped"] is not False:
            raise InputRejected("pending_confirmation_invalid")
        status = value.get("status")
        if status is not None and status not in {
            "clear",
            "not_tripped",
            "ok",
            "ready",
        }:
            raise InputRejected("pending_confirmation_invalid")
        if "tripped" not in value and status is None:
            raise InputRejected("pending_confirmation_invalid")

    @classmethod
    def _validate_reconciliation(cls, value: object) -> None:
        if (
            not isinstance(value, dict)
            or not value
            or cls._evidence_has_unknown(value)
        ):
            raise InputRejected("pending_confirmation_invalid")
        status = value.get("status")
        if status is not None and status not in {
            "clean",
            "current",
            "ok",
            "reconciled",
        }:
            raise InputRejected("pending_confirmation_invalid")
        for key, item in value.items():
            normalized_key = (
                re.sub(
                    r"[^a-z0-9]+",
                    "_",
                    key.lower(),
                ).strip("_")
                if isinstance(key, str)
                else ""
            )
            if (
                item is False
                and (
                    normalized_key
                    in {
                        "clean",
                        "complete",
                        "current",
                        "reconciled",
                    }
                    or normalized_key.endswith("_reconciled")
                )
            ):
                raise InputRejected(
                    "pending_confirmation_invalid"
                )
        if status is None and not any(
            item is True
            or (
                isinstance(item, str)
                and item in {"clean", "current", "reconciled"}
            )
            for item in value.values()
        ):
            raise InputRejected("pending_confirmation_invalid")

    @staticmethod
    def _flatten_confirmation(
        payload: dict[str, object],
    ) -> dict[str, object]:
        flattened = dict(payload)
        order = payload.get("order")
        if isinstance(order, dict):
            aliases = {
                "order_id": "order_id",
                "ticker": "symbol",
                "side": "side",
                "qty": "quantity",
                "order_type": "order_type",
                "limit_price": "limit_price",
            }
            for target, source in aliases.items():
                if target not in flattened and source in order:
                    flattened[target] = order[source]
        exposure = payload.get("exposure")
        if isinstance(exposure, dict):
            if (
                "estimated_exposure" not in flattened
                and "resulting_signed_notional" in exposure
            ):
                flattened["estimated_exposure"] = exposure[
                    "resulting_signed_notional"
                ]
            if (
                "quote_observed_at" not in flattened
                and "as_of" in exposure
            ):
                flattened["quote_observed_at"] = exposure["as_of"]
        return flattened

    @classmethod
    def _validate_confirmation(
        cls,
        payload: object,
        *,
        expected_order_id: int,
    ) -> _OrderConfirmation:
        if not isinstance(payload, dict):
            raise InputRejected("pending_confirmation_invalid")
        if payload.get("complete", True) is not True:
            raise InputRejected("pending_confirmation_invalid")
        missing_proof = payload.get("missing_proof", [])
        if (
            not isinstance(missing_proof, list)
            or missing_proof
        ):
            raise InputRejected("pending_confirmation_invalid")
        if "broker" in payload and payload["broker"] != "Alpaca":
            raise InputRejected("pending_confirmation_invalid")
        if "mode" in payload and payload["mode"] != "paper":
            raise InputRejected("pending_confirmation_invalid")

        flattened = cls._flatten_confirmation(payload)
        if any(
            field not in flattened or flattened[field] is None
            for field in ORDER_CONFIRMATION_FIELDS
        ):
            raise InputRejected("pending_confirmation_invalid")

        order_id = flattened["order_id"]
        if (
            isinstance(order_id, bool)
            or not isinstance(order_id, int)
            or order_id != expected_order_id
        ):
            raise InputRejected("pending_confirmation_invalid")
        ticker = flattened["ticker"]
        if (
            not isinstance(ticker, str)
            or _TICKER.fullmatch(ticker) is None
        ):
            raise InputRejected("pending_confirmation_invalid")
        if flattened["side"] not in {"buy", "sell"}:
            raise InputRejected("pending_confirmation_invalid")
        cls._positive_decimal(flattened["qty"])
        cls._positive_decimal(flattened["estimated_exposure"])

        order_type = flattened["order_type"]
        limit_price = flattened.get("limit_price")
        if order_type == "market":
            if limit_price is not None:
                raise InputRejected("pending_confirmation_invalid")
        elif order_type == "limit":
            cls._positive_decimal(limit_price)
        else:
            raise InputRejected("pending_confirmation_invalid")

        cls._validate_breaker_state(flattened["breaker_state"])
        cls._validate_reconciliation(flattened["reconciliation"])
        quote_observed_at = cls._timestamp(
            flattened["quote_observed_at"]
        )
        expires_at = cls._timestamp(flattened["expires_at"])
        now = datetime.now(timezone.utc)
        quote_age = (now - quote_observed_at).total_seconds()
        if (
            quote_age < -_MAX_CLOCK_SKEW_SECONDS
            or quote_age > _MAX_QUOTE_AGE_SECONDS
            or expires_at <= now
        ):
            raise InputRejected("pending_confirmation_invalid")

        rendered = {
            field: flattened[field]
            for field in ORDER_CONFIRMATION_FIELDS
        }
        rendered["limit_price"] = limit_price
        return _OrderConfirmation(
            order_id=order_id,
            quote_observed_at=quote_observed_at,
            expires_at=expires_at,
            rendered=rendered,
        )

    @staticmethod
    def _confirmation_is_still_fresh(
        confirmation: _OrderConfirmation,
    ) -> bool:
        now = datetime.now(timezone.utc)
        quote_age = (
            now - confirmation.quote_observed_at
        ).total_seconds()
        return (
            -_MAX_CLOCK_SKEW_SECONDS
            <= quote_age
            <= _MAX_QUOTE_AGE_SECONDS
            and confirmation.expires_at > now
        )

    def approve_pending_order(
        self,
        order_id: int | str,
    ) -> None:
        canonical_id = self._identifier(order_id)
        if canonical_id in self._acceptance_unknown_order_ids:
            self._write("order_approval_retry_prohibited")
            return
        payload = self.api.get(
            f"/pending/{canonical_id}/confirmation"
        )
        try:
            confirmation = self._validate_confirmation(
                payload,
                expected_order_id=canonical_id,
            )
        except InputRejected:
            self._write("pending_confirmation_invalid")
            return
        self._write(
            "ALPACA PAPER ORDER confirmation: "
            f"{render_json_summary(confirmation.rendered)}"
        )
        reason = require_reason(self._input("Reason: "))
        self._reauthenticate()
        if not self._confirm_exact(
            f"APPROVE ALPACA PAPER ORDER {canonical_id}"
        ):
            self._write("Canceled")
            return
        if not self._confirmation_is_still_fresh(confirmation):
            self._write("pending_confirmation_invalid")
            return
        try:
            result = self.api.mutate(
                f"/approve/{canonical_id}",
                {"reason": reason},
                idempotent=True,
            )
        except OperatorApiError as error:
            if error.code == "acceptance_unknown":
                self._acceptance_unknown_order_ids.add(
                    canonical_id
                )
            raise
        if result.get("status") == "acceptance_unknown":
            self._acceptance_unknown_order_ids.add(canonical_id)
            self._write("acceptance_unknown")
            return
        self._write(
            f"Order approval: {render_json_summary(result)}"
        )

    def reject_pending_order(
        self,
        order_id: int | str,
    ) -> None:
        canonical_id = self._identifier(order_id)
        reason = require_reason(self._input("Reason: "))
        result = self.api.mutate(
            f"/reject/{canonical_id}",
            {"reason": reason},
            idempotent=True,
        )
        self._write(
            f"Order rejection: {render_json_summary(result)}"
        )

    def cancel_order(self, order_id: int | str) -> None:
        canonical_id = self._identifier(order_id)
        reason = require_reason(self._input("Reason: "))
        result = self.api.mutate(
            f"/orders/{canonical_id}/cancel",
            {"reason": reason},
            idempotent=True,
        )
        self._write(
            f"Order cancellation: {render_json_summary(result)}"
        )


class _NoOwnedDaemon:
    owns_child = False


def main(argv: list[str] | None = None) -> int:
    """Start the fixed-root terminal without URL or TLS override arguments."""
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments:
        print("operator_arguments_not_supported")
        return 2
    try:
        api = OperatorApiClient(CANONICAL_PROJECT_ROOT)
        return OperatorMenu(api, _NoOwnedDaemon()).run()
    except (KeyboardInterrupt, EOFError):
        return 0
    except Exception:
        print("operator_start_failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
