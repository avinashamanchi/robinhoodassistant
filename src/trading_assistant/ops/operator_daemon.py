"""Exact-child supervision for the local paper-monitor daemon."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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
_BREAKER_SCOPES = frozenset(
    {"account", "equity", "crypto", "liquidity"}
)
_QUARANTINE_SCOPES = frozenset(
    {"received", "summarized", "rejected", "failed"}
)
_RULE_SCOPES = frozenset({"rules", "rule_groups"})


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
            raise ValueError("posture_invalid")
        observed_at = cls._timestamp(report.get("observed_at"))
        raw_checks = report.get("checks")
        if (
            not isinstance(raw_checks, list)
            or not raw_checks
            or len(raw_checks) > 100
            or report.get("can_trade", False) is not False
        ):
            raise ValueError("posture_invalid")
        checks: dict[str, list[dict[str, object]]] = {}
        for raw_check in raw_checks:
            if not isinstance(raw_check, dict):
                raise ValueError("posture_invalid")
            name = raw_check.get("name")
            status = raw_check.get("status")
            detail_code = raw_check.get("detail_code")
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(status, str)
                or not status
                or not isinstance(detail_code, str)
                or not detail_code
                or cls._timestamp(raw_check.get("observed_at"))
                != observed_at
            ):
                raise ValueError("posture_invalid")
            checks.setdefault(name, []).append(raw_check)
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

    @classmethod
    def _posture_block(
        cls,
        posture: object,
    ) -> tuple[str | None, datetime | None]:
        try:
            observed_at, checks = cls._report_checks(posture)
        except (TypeError, ValueError, OverflowError):
            return "posture_invalid", None
        if not cls._observation_is_current(observed_at):
            return "posture_not_fresh", None

        broker = cls._one(checks, "broker_mode")
        if (
            broker is None
            or broker.get("status") != "paper"
            or broker.get("detail_code") != "broker_paper_mode"
        ):
            return "posture_broker_not_paper", None

        for name in ("loopback_https", "tls", "sensitive_encryption"):
            check = cls._one(checks, name)
            if (
                check is None
                or check.get("status") != "pass"
                or check.get("detail_code") != "ok"
            ):
                return "posture_structural_evidence_unsafe", None

        reconciliation = cls._one(checks, "startup_reconciliation")
        if (
            reconciliation is None
            or reconciliation.get("status") != "pass"
            or reconciliation.get("detail_code")
            != "reconciliation_current"
        ):
            return "posture_reconciliation_unsafe", None

        breakers_clear = cls._scoped_clear(
            checks,
            "circuit_breaker",
            _BREAKER_SCOPES,
            "breaker_clear",
        )
        if not breakers_clear:
            return "posture_breaker_unsafe", None

        quarantine_safe = cls._quarantine_safe(checks)
        orders = cls._one(checks, "unsafe_orders")
        fills = cls._one(checks, "unsafe_fills")
        interlocks = cls._one(checks, "uncertain_interlocks")
        rules_clear = cls._scoped_clear(
            checks,
            "unsafe_rules",
            _RULE_SCOPES,
            "unsafe_state_clear",
        )
        if (
            not quarantine_safe
            or orders is None
            or orders.get("status") != "clear"
            or orders.get("detail_code") != "unsafe_state_clear"
            or not cls._zero_count(orders)
            or fills is None
            or fills.get("status") != "clear"
            or fills.get("detail_code") != "unsafe_state_clear"
            or not cls._zero_count(fills)
            or not rules_clear
            or interlocks is None
            or interlocks.get("status") != "clear"
            or interlocks.get("detail_code")
            != "uncertain_interlocks_clear"
            or not cls._zero_count(interlocks)
        ):
            return "posture_database_evidence_unsafe", None

        tenures = checks.get("runtime_tenure", [])
        if not tenures:
            return "posture_database_evidence_unsafe", None
        for tenure in tenures:
            scope = tenure.get("scope")
            status = tenure.get("status")
            if (
                scope not in {
                    "app",
                    "daemon",
                    "mcp",
                    "validation",
                    "maintenance",
                }
                or status not in {"held", "released", "fenced", "stale"}
            ):
                return "posture_database_evidence_unsafe", None
            if scope == "daemon" and status == "held":
                return "daemon_tenure_conflict", None

        heartbeat = cls._one(checks, "daemon_heartbeat")
        if heartbeat is None:
            return "posture_heartbeat_unsafe", None
        heartbeat_state = (
            heartbeat.get("status"),
            heartbeat.get("detail_code"),
        )
        if heartbeat_state == (
            "fresh",
            "daemon_heartbeat_fresh",
        ):
            return "daemon_heartbeat_current", None
        if heartbeat_state == (
            "unknown",
            "daemon_heartbeat_missing",
        ):
            if heartbeat.get("evidence_at") is not None:
                return "posture_heartbeat_unsafe", None
            return None, observed_at
        if heartbeat_state == (
            "stale",
            "daemon_heartbeat_stale",
        ):
            try:
                evidence_at = cls._timestamp(
                    heartbeat.get("evidence_at")
                )
            except (TypeError, ValueError, OverflowError):
                return "posture_heartbeat_unsafe", None
            if evidence_at > observed_at:
                return "posture_heartbeat_unsafe", None
            return None, observed_at
        return "posture_heartbeat_unsafe", None

    @classmethod
    def _has_new_heartbeat(
        cls,
        report: object,
        *,
        after: datetime,
    ) -> bool:
        try:
            observed_at, checks = cls._report_checks(report)
            if not cls._observation_is_current(observed_at):
                return False
            heartbeat = cls._one(checks, "daemon_heartbeat")
            if (
                heartbeat is None
                or heartbeat.get("status") != "fresh"
                or heartbeat.get("detail_code")
                != "daemon_heartbeat_fresh"
            ):
                return False
            evidence_at = cls._timestamp(
                heartbeat.get("evidence_at")
            )
        except (TypeError, ValueError, OverflowError):
            return False
        return after < evidence_at <= observed_at

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
            if self._has_new_heartbeat(report, after=observed_at):
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

        try:
            child.send_signal(signal.SIGINT)
            child.wait(timeout=_STARTUP_TIMEOUT_SECONDS)
        except Exception:
            self._status = DaemonStatus(
                "stop_unconfirmed",
                pid,
                "daemon_heartbeat_timeout_stop_unconfirmed",
            )
            return self._status
        self._release_child()
        self._status = DaemonStatus(
            "start_blocked",
            None,
            "daemon_heartbeat_timeout",
        )
        return self._status

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
