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
_CONFIRMATION_FIELDS = frozenset(
    {
        "complete",
        "missing_proof",
        "broker",
        "mode",
        "order",
        "expires_at",
        "breaker_state",
        "reconciliation",
        "exposure",
    }
)
_ORDER_FIELDS = frozenset(
    {
        "order_id",
        "symbol",
        "side",
        "order_type",
        "quantity",
        "notional",
        "limit_price",
    }
)
_BREAKER_FIELDS = frozenset({"tripped", "active_scopes"})
_RECONCILIATION_FIELDS = frozenset(
    {"broker_reconciled", "pending_exposure_complete"}
)
_EXPOSURE_FIELDS = frozenset(
    {
        "currency",
        "current_position_quantity",
        "current_signed_notional",
        "resulting_signed_notional",
        "order_estimated_notional",
        "quote_observed_at",
    }
)
_RENDERED_CONFIRMATION_FIELDS = (
    "complete",
    "missing_proof",
    "broker",
    "mode",
    "order",
    "expires_at",
    "breaker_state",
    "reconciliation",
    "exposure",
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
_MAX_EVIDENCE_AGE_SECONDS = 30.0
_MAX_EVIDENCE_FUTURE_SKEW_SECONDS = 5.0
_MAX_RESET_EVIDENCE_DELTA_SECONDS = 5.0
_REDACTED = "<redacted>"
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TICKER = re.compile(r"^[A-Z0-9][A-Z0-9./-]{0,19}$")
_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_SAFE_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")

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
_OPERATIONS_MENU = (
    "Operations: 1 Synchronize open orders | 2 Reconcile positions | "
    "3 Redacted logs | 0 Back"
)
_EMERGENCY_MENU = (
    "Emergency safety: 1 Panic | 2 Reset circuit breaker | 0 Back"
)
_BREAKER_CATEGORIES = ("account", "equity", "crypto", "liquidity")
_BREAKER_KINDS = frozenset(
    {
        "operator_global",
        "broker_drift",
        "data",
        "loss",
        "drawdown",
        "liquidity",
    }
)
_ASSET_BREAKER_KINDS = frozenset({"data", "loss", "drawdown"})
_LIQUIDITY_TARGET = re.compile(r"^[A-Z0-9][A-Z0-9./_-]{0,31}$")
_LOG_SENSITIVE_KEYS = frozenset(
    {
        "detail",
        "detail_json",
        "prompt",
        "reason",
        "reasoning_summary",
    }
)
_POSTURE_SENSITIVE_KEYS = frozenset(
    {
        "actor",
        "content_hash",
        "evidence_json",
        "prompt",
        "reason",
        "request_id",
        "source_hash",
        "tool_call",
        "value",
    }
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


@dataclass(frozen=True)
class _BreakerCandidate:
    scope: str
    kind: str
    target: str
    generation: int
    category: str


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


def _normalized_key(key: str) -> str:
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
    return normalized


def _sensitive_key(key: str) -> bool:
    normalized = _normalized_key(key)
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


def _redacted_named_value(
    value: object,
    *,
    sensitive_keys: frozenset[str],
    depth: int = 0,
) -> object:
    if depth >= _MAX_RENDER_DEPTH:
        return "<max-depth>"
    if isinstance(value, dict):
        result: dict[str, object] = {}
        string_keys = sorted(key for key in value if isinstance(key, str))
        for key in string_keys[:_MAX_COLLECTION_ITEMS]:
            result[key] = (
                _REDACTED
                if (
                    _sensitive_key(key)
                    or _normalized_key(key) in sensitive_keys
                )
                else _redacted_named_value(
                    value[key],
                    sensitive_keys=sensitive_keys,
                    depth=depth + 1,
                )
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
            _redacted_named_value(
                item,
                sensitive_keys=sensitive_keys,
                depth=depth + 1,
            )
            for item in value[:_MAX_COLLECTION_ITEMS]
        ]
        if len(value) > _MAX_COLLECTION_ITEMS:
            items.append("<truncated>")
        return items
    return value


def _redacted_log_value(value: object) -> object:
    return _redacted_named_value(
        value,
        sensitive_keys=_LOG_SENSITIVE_KEYS,
    )


def _redacted_posture_value(value: object) -> object:
    return _redacted_named_value(
        value,
        sensitive_keys=_POSTURE_SENSITIVE_KEYS,
    )


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

    @classmethod
    def _observation_state(
        cls,
        value: object,
        *,
        now: datetime,
    ) -> tuple[str, datetime | None]:
        try:
            observed_at = cls._evidence_timestamp(
                value,
                "evidence_timestamp_invalid",
            )
        except InputRejected:
            return "unknown", None
        age_seconds = (now - observed_at).total_seconds()
        if age_seconds > _MAX_EVIDENCE_AGE_SECONDS:
            return "stale", observed_at
        if age_seconds < -_MAX_EVIDENCE_FUTURE_SKEW_SECONDS:
            return "unknown", observed_at
        return "current", observed_at

    @classmethod
    def _health_observation_state(
        cls,
        payload: object,
        *,
        now: datetime,
    ) -> tuple[str, datetime | None]:
        if not isinstance(payload, dict):
            return "unknown", None
        state, observed_at = cls._observation_state(
            payload.get("observed_at"),
            now=now,
        )
        if state != "current" or observed_at is None:
            return state, observed_at
        safety = payload.get("safety")
        if not isinstance(safety, dict):
            return "unknown", observed_at
        try:
            safety_observed_at = cls._evidence_timestamp(
                safety.get("observed_at"),
                "evidence_timestamp_invalid",
            )
        except InputRejected:
            return "unknown", observed_at
        if safety_observed_at != observed_at:
            return "unknown", observed_at
        return "current", observed_at

    @classmethod
    def _posture_observation_state(
        cls,
        payload: object,
        *,
        now: datetime,
    ) -> tuple[str, datetime | None]:
        if not isinstance(payload, dict):
            return "unknown", None
        state, observed_at = cls._observation_state(
            payload.get("observed_at"),
            now=now,
        )
        if state != "current" or observed_at is None:
            return state, observed_at
        checks = payload.get("checks")
        if (
            not isinstance(checks, list)
            or len(checks) > _MAX_COLLECTION_ITEMS
        ):
            return "unknown", observed_at
        try:
            coherent = all(
                isinstance(check, dict)
                and cls._evidence_timestamp(
                    check.get("observed_at"),
                    "evidence_timestamp_invalid",
                )
                == observed_at
                for check in checks
            )
        except InputRejected:
            coherent = False
        if not coherent:
            return "unknown", observed_at
        return "current", observed_at

    def _system_status(self) -> bool:
        health = self.api.get("/health")
        posture = self.api.get("/security/posture")
        now = datetime.now(timezone.utc)
        health_state, health_observed_at = (
            self._health_observation_state(health, now=now)
        )
        posture_state, posture_observed_at = (
            self._posture_observation_state(posture, now=now)
        )
        health_current = health_state == "current"
        posture_current = posture_state == "current"

        def health_field(key: str) -> object:
            return (
                self._evidence_field(health, key)
                if health_current
                else "unknown"
            )

        health_summary = {
            "evidence_state": health_state,
            "observed_at": (
                self._evidence_field(health, "observed_at")
                if health_observed_at is not None
                else "unknown"
            ),
            "broker": health_field("broker"),
            "mode": health_field("mode"),
            "database_reachable": health_field(
                "database_reachable"
            ),
            "daemon": {
                "alive": health_field("daemon_alive"),
                "heartbeat_age_seconds": health_field(
                    "heartbeat_age_seconds"
                ),
            },
            "reconciliation": {
                "broker_contact_evidence_valid": health_field(
                    "broker_contact_evidence_valid"
                ),
                "last_confirmed_broker_contact": health_field(
                    "last_confirmed_broker_contact"
                ),
                "reconciliation_age_seconds": health_field(
                    "reconciliation_age_seconds"
                ),
                "reconciliation_max_age_seconds": health_field(
                    "reconciliation_max_age_seconds"
                ),
                "startup": health_field(
                    "startup_reconciliation"
                ),
            },
            "breakers": {
                "active": health_field("active_breakers"),
                "safety": health_field("safety"),
            },
        }
        posture_summary = {
            "evidence_state": posture_state,
            "observed_at": (
                self._evidence_field(posture, "observed_at")
                if posture_observed_at is not None
                else "unknown"
            ),
            "can_trade": (
                self._evidence_field(posture, "can_trade")
                if posture_current
                else "unknown"
            ),
            "checks": (
                _redacted_posture_value(
                    self._evidence_field(posture, "checks")
                )
                if posture_current
                else "unknown"
            ),
        }
        self._write(
            "System health evidence: "
            f"{render_json_summary(health_summary)}"
        )
        self._write(
            "Security posture evidence: "
            f"{render_json_summary(posture_summary)}"
        )
        return False

    def _paper_account(self) -> bool:
        account = self.api.get("/account")
        positions = self.api.get("/positions")
        account_summary = {
            "observed_at": self._evidence_field(
                account,
                "observed_at",
            ),
            "buying_power": self._evidence_field(
                account,
                "buying_power",
            ),
            "equity": self._evidence_field(account, "equity"),
            "cash": self._evidence_field(account, "cash"),
            "gross_exposure": self._evidence_field(
                account,
                "gross_exposure",
            ),
            "positions": self._evidence_field(
                account,
                "positions",
            ),
        }
        positions_summary = {
            "observed_at": self._evidence_field(
                positions,
                "observed_at",
            ),
            "source": self._evidence_field(positions, "source"),
            "positions": self._evidence_field(
                positions,
                "positions",
            ),
        }
        self._write(
            "Alpaca paper account evidence: "
            f"{render_json_summary(account_summary)}"
        )
        self._write(
            "Position evidence: "
            f"{render_json_summary(positions_summary)}"
        )
        return False

    @staticmethod
    def _evidence_field(payload: object, key: str) -> object:
        if not isinstance(payload, dict):
            return "unknown"
        value = payload.get(key)
        return "unknown" if value is None else value

    def _monitoring(self) -> bool:
        self._write("monitoring_not_available_in_task_3")
        return False

    def _operations(self) -> bool:
        self._write(_OPERATIONS_MENU)
        choice = self._menu_choice(
            self._input("Operations choice: ")
        )
        if choice == "0":
            return False
        if choice == "1":
            self.sync_open_orders()
        elif choice == "2":
            self.reconcile_positions()
        elif choice == "3":
            self.show_logs()
        else:
            self._write("Invalid choice")
        return False

    def _emergency_safety(self) -> bool:
        self._write(_EMERGENCY_MENU)
        choice = self._menu_choice(
            self._input("Emergency choice: ")
        )
        if choice == "0":
            return False
        if choice == "1":
            self.panic()
        elif choice == "2":
            self.reset_breaker()
        else:
            self._write("Invalid choice")
        return False

    def _mutate_once(
        self,
        path: str,
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        try:
            return self.api.mutate(
                path,
                payload,
                idempotent=True,
            )
        except OperatorApiError as error:
            if error.status != 409:
                raise
            self._show_api_error(error)
            self._write("mutation_not_retried")
            self._system_status()
            return None

    def sync_open_orders(self) -> None:
        reason = require_reason(self._input("Reason: "))
        result = self._mutate_once(
            "/sync",
            {"reason": reason},
        )
        if result is not None:
            self._write(
                f"Order synchronization: {render_json_summary(result)}"
            )

    def reconcile_positions(self) -> None:
        reason = require_reason(self._input("Reason: "))
        self._reauthenticate()
        result = self._mutate_once(
            "/reconcile",
            {"reason": reason},
        )
        if result is not None:
            self._write(
                f"Position reconciliation: {render_json_summary(result)}"
            )

    def show_logs(self) -> None:
        payload = self.api.get("/log")
        self._write(
            "Redacted logs: "
            f"{render_json_summary(_redacted_log_value(payload))}"
        )

    def panic(self) -> None:
        reason = require_reason(self._input("Reason: "))
        self._reauthenticate()
        if not self._confirm_exact("PANIC ALPACA PAPER"):
            self._write("Canceled")
            return
        result = self._mutate_once(
            "/panic",
            {"reason": reason},
        )
        if result is None:
            return
        if result.get("safe") is not True:
            self._write("panic_incomplete")
            self._write(
                f"Panic receipt: {render_json_summary(result)}"
            )
            return
        self._write(
            f"Panic complete: {render_json_summary(result)}"
        )

    @staticmethod
    def _evidence_timestamp(value: object, code: str) -> datetime:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 80
            or _has_control(value)
        ):
            raise InputRejected(code)
        try:
            parsed = datetime.fromisoformat(
                value[:-1] + "+00:00"
                if value.endswith("Z")
                else value
            )
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError
            return parsed.astimezone(timezone.utc)
        except (OverflowError, ValueError):
            raise InputRejected(code) from None

    @classmethod
    def _breaker_posture(
        cls,
        payload: object,
        *,
        now: datetime,
    ) -> tuple[datetime, dict[str, dict[str, object]]]:
        code = "breaker_posture_invalid"
        if (
            not isinstance(payload, dict)
            or payload.get("can_trade") is not False
        ):
            raise InputRejected(code)
        state, report_observed_at = cls._observation_state(
            payload.get("observed_at"),
            now=now,
        )
        if state != "current" or report_observed_at is None:
            raise InputRejected(code)
        checks = payload.get("checks")
        if (
            not isinstance(checks, list)
            or len(checks) > _MAX_COLLECTION_ITEMS
        ):
            raise InputRejected(code)
        broker_mode_checks = [
            check
            for check in checks
            if isinstance(check, dict)
            and check.get("name") == "broker_mode"
        ]
        if len(broker_mode_checks) != 1:
            raise InputRejected(code)
        broker_mode = broker_mode_checks[0]
        if (
            broker_mode.get("status") != "paper"
            or broker_mode.get("detail_code")
            != "broker_paper_mode"
            or broker_mode.get("scope") is not None
            or cls._evidence_timestamp(
                broker_mode.get("observed_at"),
                code,
            )
            != report_observed_at
        ):
            raise InputRejected(code)
        observed: dict[str, dict[str, object]] = {}
        for check in checks:
            if (
                not isinstance(check, dict)
                or check.get("name") != "circuit_breaker"
            ):
                continue
            scope = check.get("scope")
            if (
                not isinstance(scope, str)
                or scope not in _BREAKER_CATEGORIES
                or scope in observed
            ):
                raise InputRejected(code)
            if (
                cls._evidence_timestamp(
                    check.get("observed_at"),
                    code,
                )
                != report_observed_at
            ):
                raise InputRejected(code)
            status = check.get("status")
            count = check.get("count")
            generation = check.get("generation")
            if (
                type(count) is not int
                or type(generation) is not int
                or count < 0
                or generation < 0
            ):
                raise InputRejected(code)
            if status == "clear":
                if (
                    count != 0
                    or check.get("detail_code") != "breaker_clear"
                ):
                    raise InputRejected(code)
            elif status == "tripped":
                if (
                    count <= 0
                    or generation <= 0
                    or check.get("detail_code")
                    != "breaker_tripped"
                ):
                    raise InputRejected(code)
            else:
                raise InputRejected(code)
            observed[scope] = {
                "status": status,
                "count": count,
                "generation": generation,
            }
        if tuple(sorted(observed)) != tuple(
            sorted(_BREAKER_CATEGORIES)
        ):
            raise InputRejected(code)
        return report_observed_at, observed

    @staticmethod
    def _breaker_candidate(value: object) -> _BreakerCandidate:
        code = "breaker_evidence_invalid"
        fields = frozenset({"scope", "kind", "target", "generation"})
        if not isinstance(value, dict) or frozenset(value) != fields:
            raise InputRejected(code)
        scope = value.get("scope")
        kind = value.get("kind")
        target = value.get("target")
        generation = value.get("generation")
        if (
            not isinstance(scope, str)
            or not scope
            or len(scope) > 64
            or _has_control(scope)
            or not isinstance(kind, str)
            or kind not in _BREAKER_KINDS
            or not isinstance(target, str)
            or len(target) > 32
            or _has_control(target)
            or type(generation) is not int
            or generation <= 0
        ):
            raise InputRejected(code)
        if kind in {"operator_global", "broker_drift"}:
            if target or scope != kind:
                raise InputRejected(code)
            category = "account"
        elif kind in _ASSET_BREAKER_KINDS:
            if (
                target not in {"equity", "crypto"}
                or scope != f"{kind}:{target}"
            ):
                raise InputRejected(code)
            category = target
        elif kind == "liquidity":
            if (
                _LIQUIDITY_TARGET.fullmatch(target) is None
                or scope != f"liquidity:{target}"
            ):
                raise InputRejected(code)
            category = "liquidity"
        else:
            raise InputRejected(code)
        return _BreakerCandidate(
            scope=scope,
            kind=kind,
            target=target,
            generation=generation,
            category=category,
        )

    @classmethod
    def _breaker_surface(
        cls,
        value: object,
    ) -> tuple[_BreakerCandidate, ...]:
        code = "breaker_evidence_invalid"
        if (
            not isinstance(value, list)
            or len(value) > _MAX_COLLECTION_ITEMS
        ):
            raise InputRejected(code)
        candidates: list[_BreakerCandidate] = []
        seen: set[str] = set()
        for item in value:
            candidate = cls._breaker_candidate(item)
            if candidate.scope in seen:
                raise InputRejected(code)
            seen.add(candidate.scope)
            candidates.append(candidate)
        return tuple(
            sorted(candidates, key=lambda candidate: candidate.scope)
        )

    @classmethod
    def _concrete_breakers(
        cls,
        health: object,
        posture: dict[str, dict[str, object]],
        *,
        posture_observed_at: datetime,
        now: datetime,
    ) -> tuple[_BreakerCandidate, ...]:
        code = "breaker_evidence_invalid"
        if (
            not isinstance(health, dict)
            or health.get("broker") != "Alpaca"
            or health.get("mode") != "paper"
        ):
            raise InputRejected(code)
        health_state, health_observed_at = cls._observation_state(
            health.get("observed_at"),
            now=now,
        )
        if health_state != "current" or health_observed_at is None:
            raise InputRejected(code)
        safety = health.get("safety")
        if not isinstance(safety, dict):
            raise InputRejected(code)
        safety_state, safety_observed_at = cls._observation_state(
            safety.get("observed_at"),
            now=now,
        )
        if (
            safety.get("complete") is not True
            or safety_state != "current"
            or safety_observed_at != health_observed_at
        ):
            raise InputRejected(code)
        request_delta = (
            health_observed_at - posture_observed_at
        ).total_seconds()
        if (
            request_delta < 0
            or request_delta > _MAX_RESET_EVIDENCE_DELTA_SECONDS
        ):
            raise InputRejected(code)
        unknown_categories = safety.get("unknown_categories")
        if (
            not isinstance(unknown_categories, list)
            or unknown_categories
        ):
            raise InputRejected(code)
        top_level_candidates = cls._breaker_surface(
            health.get("active_breakers")
        )
        safety_candidates = cls._breaker_surface(
            safety.get("active_breakers")
        )
        if top_level_candidates != safety_candidates:
            raise InputRejected(code)
        candidates = top_level_candidates
        category_counts = {
            category: 0 for category in _BREAKER_CATEGORIES
        }
        category_generations = {
            category: 0 for category in _BREAKER_CATEGORIES
        }
        for candidate in candidates:
            category_counts[candidate.category] += 1
            category_generations[candidate.category] = max(
                category_generations[candidate.category],
                candidate.generation,
            )
        for category in _BREAKER_CATEGORIES:
            aggregate = posture[category]
            count = category_counts[category]
            expected_status = "tripped" if count else "clear"
            if (
                aggregate["status"] != expected_status
                or aggregate["count"] != count
                or (
                    count
                    and aggregate["generation"]
                    < category_generations[category]
                )
            ):
                raise InputRejected(code)
        return candidates

    def reset_breaker(self) -> None:
        posture_payload = self.api.get("/security/posture")
        try:
            posture_observed_at, posture = self._breaker_posture(
                posture_payload,
                now=datetime.now(timezone.utc),
            )
        except InputRejected:
            self._write("breaker_posture_invalid")
            return
        health = self.api.get("/health")
        try:
            candidates = self._concrete_breakers(
                health,
                posture,
                posture_observed_at=posture_observed_at,
                now=datetime.now(timezone.utc),
            )
        except InputRejected:
            self._write("breaker_evidence_invalid")
            return
        self._write(
            "Fresh breaker posture: "
            + render_json_summary(
                {
                    "observed_at": posture_payload["observed_at"],
                    "breakers": posture,
                }
            )
        )
        if not candidates:
            self._write("breaker_reset_unavailable")
            return
        by_scope = {candidate.scope: candidate for candidate in candidates}
        for candidate in candidates:
            self._write(
                "Tripped breaker: "
                f"scope={candidate.scope} "
                f"generation={candidate.generation}"
            )
        selected_scope = self._input("Breaker scope: ")
        if (
            not isinstance(selected_scope, str)
            or len(selected_scope) > 64
            or _has_control(selected_scope)
            or selected_scope not in by_scope
        ):
            self._write("breaker_selection_invalid")
            return
        selected = by_scope[selected_scope]
        reason = require_reason(self._input("Reason: "))
        self._reauthenticate()
        phrase = (
            f"RESET BREAKER {selected.scope} "
            f"GENERATION {selected.generation}"
        )
        if not self._confirm_exact(phrase):
            self._write("Canceled")
            return
        result = self._mutate_once(
            "/killswitch/reset",
            {
                "scope": selected.scope,
                "expected_generation": selected.generation,
                "reason": reason,
            },
        )
        if result is not None:
            self._write(
                f"Breaker reset: {render_json_summary(result)}"
            )

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
    def _exact_dict(
        value: object,
        fields: frozenset[str],
    ) -> dict[str, object]:
        if (
            not isinstance(value, dict)
            or frozenset(value) != fields
        ):
            raise InputRejected("pending_confirmation_invalid")
        return value

    @staticmethod
    def _finite_decimal(
        value: object,
        *,
        positive: bool = False,
    ) -> Decimal:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 80
            or not value.isascii()
            or value != value.strip()
            or _has_control(value)
        ):
            raise InputRejected("pending_confirmation_invalid")
        try:
            parsed = Decimal(value)
        except (InvalidOperation, ValueError):
            raise InputRejected(
                "pending_confirmation_invalid"
            ) from None
        if (
            not parsed.is_finite()
            or (positive and parsed <= 0)
        ):
            raise InputRejected("pending_confirmation_invalid")
        return parsed

    @classmethod
    def _positive_decimal(cls, value: object) -> Decimal:
        return cls._finite_decimal(value, positive=True)

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
    def _validate_confirmation(
        cls,
        payload: object,
        *,
        expected_order_id: int,
    ) -> _OrderConfirmation:
        confirmation = cls._exact_dict(
            payload,
            _CONFIRMATION_FIELDS,
        )
        if confirmation["complete"] is not True:
            raise InputRejected("pending_confirmation_invalid")
        missing_proof = confirmation["missing_proof"]
        if (
            not isinstance(missing_proof, list)
            or missing_proof != []
        ):
            raise InputRejected("pending_confirmation_invalid")
        if confirmation["broker"] != "Alpaca":
            raise InputRejected("pending_confirmation_invalid")
        if confirmation["mode"] != "paper":
            raise InputRejected("pending_confirmation_invalid")

        breaker_state = cls._exact_dict(
            confirmation["breaker_state"],
            _BREAKER_FIELDS,
        )
        active_scopes = breaker_state["active_scopes"]
        if (
            breaker_state["tripped"] is not False
            or not isinstance(active_scopes, list)
            or active_scopes != []
        ):
            raise InputRejected("pending_confirmation_invalid")

        reconciliation = cls._exact_dict(
            confirmation["reconciliation"],
            _RECONCILIATION_FIELDS,
        )
        if (
            reconciliation["broker_reconciled"] is not True
            or reconciliation[
                "pending_exposure_complete"
            ] is not True
        ):
            raise InputRejected("pending_confirmation_invalid")

        order = cls._exact_dict(
            confirmation["order"],
            _ORDER_FIELDS,
        )
        order_id = order["order_id"]
        if (
            isinstance(order_id, bool)
            or not isinstance(order_id, int)
            or order_id != expected_order_id
        ):
            raise InputRejected("pending_confirmation_invalid")
        ticker = order["symbol"]
        if (
            not isinstance(ticker, str)
            or _TICKER.fullmatch(ticker) is None
        ):
            raise InputRejected("pending_confirmation_invalid")
        if order["side"] not in {"buy", "sell"}:
            raise InputRejected("pending_confirmation_invalid")

        quantity = order["quantity"]
        notional = order["notional"]
        if (quantity is None) == (notional is None):
            raise InputRejected("pending_confirmation_invalid")
        if quantity is not None:
            cls._positive_decimal(quantity)
        else:
            parsed_notional = cls._positive_decimal(notional)

        order_type = order["order_type"]
        limit_price = order["limit_price"]
        if order_type == "market":
            if limit_price is not None:
                raise InputRejected("pending_confirmation_invalid")
        elif order_type == "limit":
            cls._positive_decimal(limit_price)
        else:
            raise InputRejected("pending_confirmation_invalid")

        exposure = cls._exact_dict(
            confirmation["exposure"],
            _EXPOSURE_FIELDS,
        )
        if exposure["currency"] != "USD":
            raise InputRejected("pending_confirmation_invalid")
        cls._finite_decimal(exposure["current_position_quantity"])
        cls._finite_decimal(exposure["current_signed_notional"])
        cls._finite_decimal(exposure["resulting_signed_notional"])
        order_estimated_notional = cls._positive_decimal(
            exposure["order_estimated_notional"]
        )
        if (
            notional is not None
            and order_estimated_notional != parsed_notional
        ):
            raise InputRejected("pending_confirmation_invalid")

        quote_observed_at = cls._timestamp(
            exposure["quote_observed_at"]
        )
        expires_at = cls._timestamp(
            confirmation["expires_at"]
        )
        now = datetime.now(timezone.utc)
        quote_age = (now - quote_observed_at).total_seconds()
        if (
            quote_age < -_MAX_CLOCK_SKEW_SECONDS
            or quote_age > _MAX_QUOTE_AGE_SECONDS
            or expires_at <= now
        ):
            raise InputRejected("pending_confirmation_invalid")

        rendered = {
            field: confirmation[field]
            for field in _RENDERED_CONFIRMATION_FIELDS
        }
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
