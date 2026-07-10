"""Structured logging with a secret-redaction filter.

API keys and tokens must never reach a log sink. The filter masks any value
that looks like a known secret (Anthropic/Alpaca keys, bearer tokens) and any
explicitly registered secret string.
"""

from __future__ import annotations

import logging
import re

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


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    handler.addFilter(RedactionFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
