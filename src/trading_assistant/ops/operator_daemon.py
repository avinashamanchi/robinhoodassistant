"""Exact-child supervision for the local paper-monitor daemon."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import BinaryIO, Callable, Literal


_STARTUP_TIMEOUT_SECONDS = 15.0
_STARTUP_POLL_SECONDS = 0.1
_MAX_POSTURE_AGE_SECONDS = 30.0
_MAX_POSTURE_FUTURE_SKEW_SECONDS = 5.0
_RECONCILIATION_AGE_TOLERANCE_SECONDS = 1e-6
_BREAKER_SCOPES = frozenset(
    {"account", "equity", "crypto", "liquidity"}
)
_QUARANTINE_SCOPES = frozenset(
    {"received", "summarized", "rejected", "failed"}
)
_RULE_SCOPES = frozenset({"rules", "rule_groups"})
_REQUEST_BUDGET_SCOPES = frozenset(
    {
        "login",
        "session_read",
        "broker_read",
        "mutation",
        "approval",
        "privileged",
        "chat",
        "analysis",
        "backtest",
        "provider_read",
        "panic",
    }
)
_PROVIDER_BUDGET_SCOPES = frozenset(
    {"anthropic", "gemini", "groq"}
)
_TENURE_SCOPES = frozenset(
    {"app", "daemon", "mcp", "validation", "maintenance"}
)
_SINGLETON_CHECK_NAMES = frozenset(
    {
        "broker_mode",
        "loopback_https",
        "tls",
        "secret_provider",
        "sensitive_encryption",
        "webhook_receiver",
        "composio_integration",
        "daemon_heartbeat",
        "startup_reconciliation",
        "quote_freshness",
        "unsafe_orders",
        "unsafe_fills",
        "uncertain_interlocks",
    }
)
_SCOPED_CHECKS = {
    "request_budget": _REQUEST_BUDGET_SCOPES,
    "provider_budget": _PROVIDER_BUDGET_SCOPES,
    "quarantine": _QUARANTINE_SCOPES,
    "circuit_breaker": _BREAKER_SCOPES,
    "unsafe_rules": _RULE_SCOPES,
}
_KNOWN_CHECK_NAMES = frozenset(
    {
        *_SINGLETON_CHECK_NAMES,
        *_SCOPED_CHECKS,
        "runtime_tenure",
    }
)
_PAST_EVIDENCE_FIELDS = (
    "evidence_at",
    "started_at",
    "completed_at",
    "updated_at",
)


class _PostureValidationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DaemonStatus:
    state: Literal[
        "off",
        "starting",
        "running",
        "exited",
        "start_blocked",
        "stop_unconfirmed",
    ]
    pid: int | None
    detail_code: str


class DaemonSupervisor:
    """Own at most the one child explicitly created by this instance."""

    def __init__(
        self,
        project_root: Path,
        *,
        process_factory=subprocess.Popen,
        monotonic=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self._project_root = Path(project_root)
        self._process_factory = process_factory
        self._monotonic = monotonic
        self._sleep = sleep
        self._child = None
        self._log_handle: BinaryIO | None = None
        self._status = DaemonStatus("off", None, "daemon_off")

    @property
    def owns_child(self) -> bool:
        return self._child is not None

    def status(self) -> DaemonStatus:
        return self._status

    @staticmethod
    def _timestamp(value: object) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value:
            parsed = datetime.fromisoformat(value)
        else:
            raise ValueError("timestamp_invalid")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timestamp_invalid")
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _report_checks(
        cls,
        report: object,
    ) -> tuple[datetime, dict[str, list[dict[str, object]]]]:
        if not isinstance(report, dict):
            raise _PostureValidationError("posture_invalid")
        try:
            observed_at = cls._timestamp(report.get("observed_at"))
        except (TypeError, ValueError, OverflowError) as error:
            raise _PostureValidationError(
                "posture_invalid"
            ) from error
        raw_checks = report.get("checks")
        if (
            not isinstance(raw_checks, list)
            or not raw_checks
            or len(raw_checks) > 100
        ):
            raise _PostureValidationError("posture_invalid")
        checks: dict[str, list[dict[str, object]]] = {}
        for raw_check in raw_checks:
            if not isinstance(raw_check, dict):
                raise _PostureValidationError("posture_invalid")
            name = raw_check.get("name")
            status = raw_check.get("status")
            detail_code = raw_check.get("detail_code")
            try:
                check_observed_at = cls._timestamp(
                    raw_check.get("observed_at")
                )
            except (TypeError, ValueError, OverflowError) as error:
                raise _PostureValidationError(
                    "posture_invalid"
                ) from error
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(status, str)
                or not status
                or not isinstance(detail_code, str)
                or not detail_code
                or check_observed_at != observed_at
            ):
                raise _PostureValidationError("posture_invalid")
            if name not in _KNOWN_CHECK_NAMES:
                raise _PostureValidationError(
                    "posture_schema_invalid"
                )
            for field in _PAST_EVIDENCE_FIELDS:
                value = raw_check.get(field)
                if value is None:
                    continue
                try:
                    evidence_at = cls._timestamp(value)
                except (TypeError, ValueError, OverflowError) as error:
                    raise _PostureValidationError(
                        "posture_invalid"
                    ) from error
                if evidence_at > observed_at:
                    raise _PostureValidationError(
                        "posture_evidence_time_invalid"
                    )
            checks.setdefault(name, []).append(raw_check)
        if set(checks) != _KNOWN_CHECK_NAMES:
            raise _PostureValidationError("posture_schema_invalid")
        for name in _SINGLETON_CHECK_NAMES:
            matches = checks[name]
            if (
                len(matches) != 1
                or matches[0].get("scope") is not None
            ):
                raise _PostureValidationError(
                    "posture_schema_invalid"
                )
        for name, expected_scopes in _SCOPED_CHECKS.items():
            matches = checks[name]
            scopes = [item.get("scope") for item in matches]
            if (
                len(matches) != len(expected_scopes)
                or any(not isinstance(scope, str) for scope in scopes)
                or len(set(scopes)) != len(scopes)
                or set(scopes) != expected_scopes
            ):
                raise _PostureValidationError(
                    "posture_schema_invalid"
                )
        tenures = checks["runtime_tenure"]
        tenure_scopes = [item.get("scope") for item in tenures]
        missing_tenure = (
            len(tenures) == 1 and tenure_scopes == [None]
        )
        if not missing_tenure and (
            any(
                not isinstance(scope, str)
                or scope not in _TENURE_SCOPES
                for scope in tenure_scopes
            )
            or len(set(tenure_scopes)) != len(tenure_scopes)
        ):
            raise _PostureValidationError("posture_schema_invalid")
        return observed_at, checks

    @staticmethod
    def _one(
        checks: dict[str, list[dict[str, object]]],
        name: str,
    ) -> dict[str, object] | None:
        matches = checks.get(name, [])
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _zero_count(check: dict[str, object]) -> bool:
        return type(check.get("count")) is int and check["count"] == 0

    @staticmethod
    def _observation_is_current(observed_at: datetime) -> bool:
        age_seconds = (
            datetime.now(timezone.utc) - observed_at
        ).total_seconds()
        return (
            age_seconds <= _MAX_POSTURE_AGE_SECONDS
            and age_seconds >= -_MAX_POSTURE_FUTURE_SKEW_SECONDS
        )

    @classmethod
    def _scoped_clear(
        cls,
        checks: dict[str, list[dict[str, object]]],
        name: str,
        scopes: frozenset[str],
        detail_code: str,
    ) -> bool:
        matches = checks.get(name, [])
        if len(matches) != len(scopes):
            return False
        found_scopes = {
            item.get("scope")
            for item in matches
            if isinstance(item.get("scope"), str)
        }
        return (
            found_scopes == scopes
            and all(
                item.get("status") == "clear"
                and item.get("detail_code") == detail_code
                and cls._zero_count(item)
                for item in matches
            )
        )

    @staticmethod
    def _quarantine_safe(
        checks: dict[str, list[dict[str, object]]],
    ) -> bool:
        matches = checks.get("quarantine", [])
        if (
            len(matches) != len(_QUARANTINE_SCOPES)
            or {
                item.get("scope")
                for item in matches
                if isinstance(item.get("scope"), str)
            }
            != _QUARANTINE_SCOPES
        ):
            return False
        for item in matches:
            count = item.get("count")
            if type(count) is not int or count < 0:
                return False
            if item.get("scope") == "failed":
                if (
                    item.get("status") != "clear"
                    or item.get("detail_code") != "quarantine_empty"
                    or count != 0
                ):
                    return False
            elif count == 0:
                if (
                    item.get("status") != "clear"
                    or item.get("detail_code") != "quarantine_empty"
                ):
                    return False
            elif (
                item.get("status") != "present"
                or item.get("detail_code")
                != "quarantine_items_present"
            ):
                return False
        return True

    @staticmethod
    def _required_timestamp(
        check: dict[str, object],
        field: str,
    ) -> datetime | None:
        value = check.get(field)
        if value is None:
            return None
        try:
            return DaemonSupervisor._timestamp(value)
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _exact_nonnegative_ints(
        check: dict[str, object],
        fields: tuple[str, ...],
    ) -> bool:
        return all(
            type(check.get(field)) is int and check[field] >= 0
            for field in fields
        )

    @staticmethod
    def _exact_nonnegative_floats(
        check: dict[str, object],
        fields: tuple[str, ...],
    ) -> bool:
        return all(
            type(check.get(field)) is float
            and math.isfinite(check[field])
            and check[field] >= 0
            for field in fields
        )

    @classmethod
    def _budget_shape_safe(
        cls,
        check: dict[str, object],
        *,
        provider: bool,
    ) -> bool:
        groups = [
            ("budget_used", "budget_remaining", "budget_limit"),
        ]
        if provider:
            groups.extend(
                [
                    (
                        "input_tokens_used",
                        "input_tokens_remaining",
                        "input_tokens_limit",
                    ),
                    (
                        "output_tokens_used",
                        "output_tokens_remaining",
                        "output_tokens_limit",
                    ),
                ]
            )
        for used, remaining, limit in groups:
            if (
                not cls._exact_nonnegative_ints(
                    check,
                    (used, remaining, limit),
                )
                or check[limit] <= 0
                or check[remaining]
                != max(0, check[limit] - check[used])
            ):
                return False
        if provider and not cls._exact_nonnegative_ints(
            check,
            ("count",),
        ):
            return False
        reset_at = cls._required_timestamp(check, "reset_at")
        observed_at = cls._required_timestamp(check, "observed_at")
        return (
            reset_at is not None
            and observed_at is not None
            and reset_at > observed_at
        )

    @classmethod
    def _budget_evidence_safe(
        cls,
        checks: dict[str, list[dict[str, object]]],
    ) -> bool:
        for check in checks["request_budget"]:
            state = (
                check.get("status"),
                check.get("detail_code"),
            )
            if state not in {
                ("pass", "request_budget_available"),
                ("blocked", "request_budget_exhausted"),
            } or not cls._budget_shape_safe(
                check,
                provider=False,
            ):
                return False
            if (
                state[0] == "pass"
                and check["budget_remaining"] == 0
            ) or (
                state[0] == "blocked"
                and check["budget_remaining"] != 0
            ):
                return False
        for check in checks["provider_budget"]:
            state = (
                check.get("status"),
                check.get("detail_code"),
            )
            if state not in {
                ("pass", "provider_budget_available"),
                ("blocked", "provider_budget_exhausted"),
                (
                    "blocked",
                    "provider_reconciliation_required",
                ),
            } or not cls._budget_shape_safe(
                check,
                provider=True,
            ):
                return False
            remaining = (
                check["budget_remaining"],
                check["input_tokens_remaining"],
                check["output_tokens_remaining"],
            )
            if state[0] == "pass" and (
                0 in remaining or check["count"] != 0
            ):
                return False
            if (
                state[1] == "provider_budget_exhausted"
                and 0 not in remaining
            ):
                return False
        return True

    @classmethod
    def _common_safety_block(
        cls,
        checks: dict[str, list[dict[str, object]]],
        *,
        observed_at: datetime,
    ) -> str | None:
        broker = cls._one(checks, "broker_mode")
        if (
            broker is None
            or broker.get("status") != "paper"
            or broker.get("detail_code") != "broker_paper_mode"
        ):
            return "posture_broker_not_paper"

        for name in ("loopback_https", "tls"):
            check = cls._one(checks, name)
            if (
                check is None
                or check.get("status") != "pass"
                or check.get("detail_code") != "ok"
                or cls._required_timestamp(check, "evidence_at") is None
            ):
                return "posture_structural_evidence_unsafe"

        secret = cls._one(checks, "secret_provider")
        if (
            secret is None
            or secret.get("status") != "pass"
            or secret.get("detail_code") != "macos_keychain"
            or cls._required_timestamp(secret, "evidence_at") is None
        ):
            return "posture_secret_provider_unsafe"

        encryption = cls._one(checks, "sensitive_encryption")
        encryption_started = (
            cls._required_timestamp(encryption, "started_at")
            if encryption is not None
            else None
        )
        encryption_completed = (
            cls._required_timestamp(encryption, "completed_at")
            if encryption is not None
            else None
        )
        encryption_updated = (
            cls._required_timestamp(encryption, "updated_at")
            if encryption is not None
            else None
        )
        if (
            encryption is None
            or encryption.get("status") != "pass"
            or encryption.get("detail_code") != "ok"
            or encryption.get("migration_state") != "complete"
            or type(encryption.get("schema_version")) is not int
            or encryption["schema_version"] <= 0
            or not cls._exact_nonnegative_ints(
                encryption,
                ("rows_total", "rows_completed"),
            )
            or encryption["rows_total"]
            != encryption["rows_completed"]
            or encryption_started is None
            or encryption_completed is None
            or encryption_updated is None
            or not (
                encryption_started
                <= encryption_completed
                <= encryption_updated
            )
        ):
            return "posture_structural_evidence_unsafe"

        for name in ("webhook_receiver", "composio_integration"):
            check = cls._one(checks, name)
            if (
                check is None
                or check.get("status") != "disabled"
                or check.get("detail_code")
                != "integration_disabled"
            ):
                return "posture_structural_evidence_unsafe"

        quote = cls._one(checks, "quote_freshness")
        if (
            quote is None
            or quote.get("status") != "unknown"
            or quote.get("detail_code")
            != "quote_evidence_unavailable"
        ):
            return "posture_schema_invalid"

        if not cls._budget_evidence_safe(checks):
            return "posture_budget_evidence_unsafe"

        reconciliation = cls._one(checks, "startup_reconciliation")
        reconciliation_started = (
            cls._required_timestamp(reconciliation, "started_at")
            if reconciliation is not None
            else None
        )
        reconciliation_completed = (
            cls._required_timestamp(reconciliation, "completed_at")
            if reconciliation is not None
            else None
        )
        reconciliation_updated = (
            cls._required_timestamp(reconciliation, "updated_at")
            if reconciliation is not None
            else None
        )
        reconciliation_age = (
            (observed_at - reconciliation_completed).total_seconds()
            if reconciliation_completed is not None
            else None
        )
        if (
            reconciliation is None
            or reconciliation.get("status") != "pass"
            or reconciliation.get("detail_code")
            != "reconciliation_current"
            or not cls._exact_nonnegative_ints(
                reconciliation,
                ("generation", "completed_generation"),
            )
            or reconciliation["generation"] <= 0
            or reconciliation["generation"]
            != reconciliation["completed_generation"]
            or not cls._exact_nonnegative_floats(
                reconciliation,
                ("age_seconds", "max_age_seconds"),
            )
            or reconciliation["max_age_seconds"] <= 0
            or reconciliation_started is None
            or reconciliation_completed is None
            or reconciliation_updated is None
            or reconciliation_age is None
            or not math.isfinite(reconciliation_age)
            or reconciliation_age < 0
            or not math.isclose(
                reconciliation["age_seconds"],
                reconciliation_age,
                rel_tol=0.0,
                abs_tol=(
                    _RECONCILIATION_AGE_TOLERANCE_SECONDS
                ),
            )
            or reconciliation_age
            > reconciliation["max_age_seconds"]
            or not (
                reconciliation_started
                <= reconciliation_completed
                <= reconciliation_updated
            )
        ):
            return "posture_reconciliation_unsafe"

        if not cls._scoped_clear(
            checks,
            "circuit_breaker",
            _BREAKER_SCOPES,
            "breaker_clear",
        ) or any(
            type(check.get("generation")) is not int
            or check["generation"] < 0
            for check in checks["circuit_breaker"]
        ):
            return "posture_breaker_unsafe"

        orders = cls._one(checks, "unsafe_orders")
        fills = cls._one(checks, "unsafe_fills")
        interlocks = cls._one(checks, "uncertain_interlocks")
        if (
            not cls._quarantine_safe(checks)
            or orders is None
            or orders.get("status") != "clear"
            or orders.get("detail_code") != "unsafe_state_clear"
            or not cls._zero_count(orders)
            or fills is None
            or fills.get("status") != "clear"
            or fills.get("detail_code") != "unsafe_state_clear"
            or not cls._zero_count(fills)
            or not cls._scoped_clear(
                checks,
                "unsafe_rules",
                _RULE_SCOPES,
                "unsafe_state_clear",
            )
            or interlocks is None
            or interlocks.get("status") != "clear"
            or interlocks.get("detail_code")
            != "uncertain_interlocks_clear"
            or not cls._zero_count(interlocks)
        ):
            return "posture_database_evidence_unsafe"
        return None

    @classmethod
    def _tenure_block(
        cls,
        checks: dict[str, list[dict[str, object]]],
        *,
        observed_at: datetime,
        poststart: bool,
    ) -> tuple[str | None, bool]:
        tenures = checks["runtime_tenure"]
        daemon_held = False
        detail_for_status = {
            "held": "runtime_tenure_held",
            "released": "runtime_tenure_released",
            "fenced": "runtime_tenure_fenced",
            "stale": "runtime_tenure_stale",
        }
        for tenure in tenures:
            scope = tenure.get("scope")
            status = tenure.get("status")
            if (
                scope not in _TENURE_SCOPES
                or status not in detail_for_status
                or tenure.get("detail_code")
                != detail_for_status[status]
                or type(tenure.get("generation")) is not int
                or tenure["generation"] <= 0
            ):
                return "posture_database_evidence_unsafe", False
            started_at = cls._required_timestamp(
                tenure,
                "started_at",
            )
            updated_at = cls._required_timestamp(
                tenure,
                "updated_at",
            )
            expires_at = cls._required_timestamp(
                tenure,
                "expires_at",
            )
            if (
                started_at is None
                or updated_at is None
                or expires_at is None
                or not started_at <= updated_at <= expires_at
            ):
                return "posture_database_evidence_unsafe", False
            completed_at = cls._required_timestamp(
                tenure,
                "completed_at",
            )
            if status in {"released", "fenced"} and (
                completed_at is None or completed_at != expires_at
            ):
                return "posture_database_evidence_unsafe", False
            if status in {"held", "stale"} and completed_at is not None:
                return "posture_database_evidence_unsafe", False
            if status == "held" and expires_at <= observed_at:
                return "posture_database_evidence_unsafe", False
            if status == "stale" and expires_at > observed_at:
                return "posture_database_evidence_unsafe", False
            if scope == "maintenance" and status in {"held", "stale"}:
                return "maintenance_tenure_conflict", False
            if scope == "daemon" and status == "held":
                daemon_held = True
                if not poststart:
                    return "daemon_tenure_conflict", False
        return None, daemon_held

    @classmethod
    def _validated_common(
        cls,
        posture: object,
    ) -> tuple[
        str | None,
        datetime | None,
        dict[str, list[dict[str, object]]] | None,
    ]:
        try:
            observed_at, checks = cls._report_checks(posture)
        except _PostureValidationError as error:
            return error.code, None, None
        except (TypeError, ValueError, OverflowError):
            return "posture_invalid", None, None
        if not cls._observation_is_current(observed_at):
            return "posture_not_fresh", None, None
        block_code = cls._common_safety_block(
            checks,
            observed_at=observed_at,
        )
        if block_code is not None:
            return block_code, None, None
        return None, observed_at, checks

    @classmethod
    def _heartbeat_state(
        cls,
        heartbeat: dict[str, object],
        *,
        observed_at: datetime,
    ) -> tuple[str, datetime | None]:
        state = (
            heartbeat.get("status"),
            heartbeat.get("detail_code"),
        )
        if state == ("unknown", "daemon_heartbeat_missing"):
            if (
                heartbeat.get("evidence_at") is None
                and heartbeat.get("age_seconds") is None
                and heartbeat.get("max_age_seconds") is None
            ):
                return "missing", None
            return "unsafe", None
        if state not in {
            ("fresh", "daemon_heartbeat_fresh"),
            ("stale", "daemon_heartbeat_stale"),
        }:
            return "unsafe", None
        evidence_at = cls._required_timestamp(
            heartbeat,
            "evidence_at",
        )
        if (
            evidence_at is None
            or evidence_at > observed_at
            or not cls._exact_nonnegative_floats(
                heartbeat,
                ("age_seconds", "max_age_seconds"),
            )
            or heartbeat["max_age_seconds"] <= 0
        ):
            return "unsafe", None
        age = (observed_at - evidence_at).total_seconds()
        if abs(heartbeat["age_seconds"] - age) > 0.001:
            return "unsafe", None
        if state[0] == "fresh":
            return (
                ("fresh", evidence_at)
                if age <= heartbeat["max_age_seconds"]
                else ("unsafe", None)
            )
        return (
            ("stale", evidence_at)
            if age > heartbeat["max_age_seconds"]
            else ("unsafe", None)
        )

    @classmethod
    def _posture_block(
        cls,
        posture: object,
    ) -> tuple[str | None, datetime | None]:
        block_code, observed_at, checks = cls._validated_common(posture)
        if (
            block_code is not None
            or observed_at is None
            or checks is None
        ):
            return block_code or "posture_invalid", None
        tenure_code, _daemon_held = cls._tenure_block(
            checks,
            observed_at=observed_at,
            poststart=False,
        )
        if tenure_code is not None:
            return tenure_code, None
        heartbeat = cls._one(checks, "daemon_heartbeat")
        if heartbeat is None:
            return "posture_schema_invalid", None
        heartbeat_state, _evidence_at = cls._heartbeat_state(
            heartbeat,
            observed_at=observed_at,
        )
        if heartbeat_state == "fresh":
            return "daemon_heartbeat_current", None
        if heartbeat_state not in {"missing", "stale"}:
            return "posture_heartbeat_unsafe", None
        return None, observed_at

    @classmethod
    def _poststart_state(
        cls,
        posture: object,
        *,
        after: datetime,
    ) -> str:
        block_code, observed_at, checks = cls._validated_common(posture)
        if (
            block_code is not None
            or observed_at is None
            or checks is None
        ):
            return "unsafe"
        tenure_code, daemon_held = cls._tenure_block(
            checks,
            observed_at=observed_at,
            poststart=True,
        )
        if tenure_code is not None:
            return "unsafe"
        heartbeat = cls._one(checks, "daemon_heartbeat")
        if heartbeat is None:
            return "unsafe"
        heartbeat_state, evidence_at = cls._heartbeat_state(
            heartbeat,
            observed_at=observed_at,
        )
        if heartbeat_state == "unsafe":
            return "unsafe"
        if (
            heartbeat_state == "fresh"
            and evidence_at is not None
            and evidence_at > after
            and daemon_held
        ):
            return "confirmed"
        return "pending"

    def _open_log(self) -> BinaryIO:
        log_directory = self._project_root / "logs"
        log_directory.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            log_directory / "daemon.operator.log",
            flags,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            return os.fdopen(descriptor, "ab", buffering=0)
        except Exception:
            os.close(descriptor)
            raise

    def _close_log(self) -> None:
        handle = self._log_handle
        self._log_handle = None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    @staticmethod
    def _pid(child: object) -> int | None:
        pid = getattr(child, "pid", None)
        return pid if type(pid) is int and pid > 0 else None

    def _release_child(self) -> int | None:
        child = self._child
        pid = self._pid(child) if child is not None else None
        self._child = None
        self._close_log()
        return pid

    def _abort_start(
        self,
        child: object,
        *,
        pid: int | None,
        detail_code: str,
        unconfirmed_code: str,
    ) -> DaemonStatus:
        try:
            child.send_signal(signal.SIGINT)
            child.wait(timeout=_STARTUP_TIMEOUT_SECONDS)
        except Exception:
            self._status = DaemonStatus(
                "stop_unconfirmed",
                pid,
                unconfirmed_code,
            )
            return self._status
        self._release_child()
        self._status = DaemonStatus(
            "start_blocked",
            None,
            detail_code,
        )
        return self._status

    def start(
        self,
        *,
        posture: dict[str, object],
        heartbeat_loader: Callable[[], dict[str, object]],
    ) -> DaemonStatus:
        if self._child is not None:
            self._status = DaemonStatus(
                "start_blocked",
                self._pid(self._child),
                "daemon_child_already_owned",
            )
            return self._status

        block_code, observed_at = self._posture_block(posture)
        if block_code is not None or observed_at is None:
            self._status = DaemonStatus(
                "start_blocked",
                None,
                block_code or "posture_invalid",
            )
            return self._status

        try:
            log_handle = self._open_log()
            try:
                child = self._process_factory(
                    [
                        str(self._project_root / ".venv/bin/python"),
                        "-m",
                        "trading_assistant.daemon.main",
                    ],
                    cwd=self._project_root,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=log_handle,
                    shell=False,
                    start_new_session=False,
                )
            except BaseException:
                log_handle.close()
                raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            self._status = DaemonStatus(
                "start_blocked",
                None,
                "daemon_spawn_failed",
            )
            return self._status

        self._child = child
        self._log_handle = log_handle
        pid = self._pid(child)
        self._status = DaemonStatus(
            "starting",
            pid,
            "daemon_heartbeat_pending",
        )
        deadline = self._monotonic() + _STARTUP_TIMEOUT_SECONDS
        while True:
            try:
                return_code = child.poll()
            except Exception:
                return_code = None
            if return_code is not None:
                exited_pid = self._release_child()
                self._status = DaemonStatus(
                    "exited",
                    exited_pid,
                    "daemon_exited_before_heartbeat",
                )
                return self._status

            try:
                report = heartbeat_loader()
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                report = None
            poststart_state = self._poststart_state(
                report,
                after=observed_at,
            )
            if poststart_state == "unsafe":
                return self._abort_start(
                    child,
                    pid=pid,
                    detail_code="poststart_posture_unsafe",
                    unconfirmed_code=(
                        "poststart_posture_stop_unconfirmed"
                    ),
                )
            if poststart_state == "confirmed":
                try:
                    return_code = child.poll()
                except Exception:
                    return self._abort_start(
                        child,
                        pid=pid,
                        detail_code="poststart_child_unconfirmed",
                        unconfirmed_code=(
                            "poststart_child_stop_unconfirmed"
                        ),
                    )
                if return_code is not None:
                    exited_pid = self._release_child()
                    self._status = DaemonStatus(
                        "exited",
                        exited_pid,
                        "daemon_exited_after_heartbeat",
                    )
                    return self._status
                self._status = DaemonStatus(
                    "running",
                    pid,
                    "daemon_heartbeat_confirmed",
                )
                return self._status

            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            self._sleep(min(_STARTUP_POLL_SECONDS, remaining))

        return self._abort_start(
            child,
            pid=pid,
            detail_code="daemon_heartbeat_timeout",
            unconfirmed_code=(
                "daemon_heartbeat_timeout_stop_unconfirmed"
            ),
        )

    def stop(self, timeout_seconds: float = 15.0) -> DaemonStatus:
        child = self._child
        if child is None:
            self._status = DaemonStatus("off", None, "daemon_off")
            return self._status
        pid = self._pid(child)
        try:
            return_code = child.poll()
        except Exception:
            return_code = None
        if return_code is not None:
            self._release_child()
            self._status = DaemonStatus(
                "exited",
                pid,
                "daemon_already_exited",
            )
            return self._status
        try:
            child.send_signal(signal.SIGINT)
            child.wait(timeout=timeout_seconds)
        except Exception:
            self._status = DaemonStatus(
                "stop_unconfirmed",
                pid,
                "daemon_stop_unconfirmed",
            )
            return self._status
        self._release_child()
        self._status = DaemonStatus("off", None, "daemon_stopped")
        return self._status
