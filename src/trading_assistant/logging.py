"""Structured logging with a secret-redaction filter.

API keys and tokens must never reach a log sink. The filter masks any value
that looks like a known secret (Anthropic/Alpaca keys, bearer tokens) and any
explicitly registered secret string.
"""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Patterns for secrets that must never appear in logs.
_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{6,}"),          # Anthropic keys
    re.compile(r"(?i)bearer\s+[A-Za-z0-9\.\-_]{10,}"),  # bearer tokens
    re.compile(r"(?i)(api[_-]?key|secret|token)\s*[=:]\s*\S+"),  # key=value
]

_REGISTERED: set[str] = set()
_MASK = "***REDACTED***"


def register_secret(value: str) -> None:
    """Register a concrete secret string so it is masked wherever it appears."""
    if value:
        _REGISTERED.add(value)


# Secret-bearing attribute names on the Secrets object.
_SECRET_ATTRS = (
    "anthropic_api_key",
    "gemini_api_key",
    "groq_api_key",
    "openrouter_api_key",
    "marketstack_api_key",
    "app_api_token",
    "alpaca_api_key",
    "alpaca_secret_key",
    "telegram_bot_token",
)


def register_all_secrets(secrets) -> None:
    """Register every known secret value before providers are constructed."""
    for attr in _SECRET_ATTRS:
        register_secret(str(getattr(secrets, attr, "") or ""))


def redact(message: str) -> str:
    for secret in _REGISTERED:
        message = message.replace(secret, _MASK)
    for pattern in _PATTERNS:
        message = pattern.sub(_MASK, message)
    return message


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()  # already interpolated by getMessage()
        except Exception:
            # Never let logging redaction crash the caller.
            pass
        return True


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """Rotating handler whose active and backup files remain owner-only."""

    def _open(self):
        descriptor = os.open(
            self.baseFilename,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        os.chmod(self.baseFilename, 0o600)
        return os.fdopen(
            descriptor,
            self.mode,
            encoding=self.encoding,
            errors=self.errors,
        )

    def doRollover(self) -> None:
        super().doRollover()
        os.chmod(self.baseFilename, 0o600)
        for number in range(1, self.backupCount + 1):
            backup = Path(f"{self.baseFilename}.{number}")
            if backup.exists():
                os.chmod(backup, 0o600)


def _formatter() -> logging.Formatter:
    return logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    )


def _attach_redaction(handler: logging.Handler) -> None:
    if not any(
        isinstance(item, RedactionFilter)
        for item in handler.filters
    ):
        handler.addFilter(RedactionFilter())


def configure_logging(
    level: int = logging.INFO,
    *,
    log_path: str | Path | None = None,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Install redacted handlers once and secure optional rotating file output."""
    root = logging.getLogger()
    for existing in root.handlers:
        _attach_redaction(existing)

    stream = next(
        (
            handler
            for handler in root.handlers
            if getattr(handler, "_trading_assistant_stream", False)
        ),
        None,
    )
    if stream is None:
        stream = logging.StreamHandler()
        stream._trading_assistant_stream = True  # type: ignore[attr-defined]
        stream.setFormatter(_formatter())
        _attach_redaction(stream)
        root.addHandler(stream)

    if log_path is not None:
        path = Path(log_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        os.close(descriptor)
        os.chmod(path, 0o600)
        resolved = str(path)
        file_handler = next(
            (
                handler
                for handler in root.handlers
                if getattr(
                    handler,
                    "_trading_assistant_path",
                    None,
                )
                == resolved
            ),
            None,
        )
        if file_handler is None:
            file_handler = _PrivateRotatingFileHandler(
                path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler._trading_assistant_path = resolved  # type: ignore[attr-defined]
            file_handler.setFormatter(_formatter())
            _attach_redaction(file_handler)
            root.addHandler(file_handler)
    root.setLevel(level)
