"""Canonical durable identities shared by runtime provenance boundaries.

Externally supplied request IDs are never truncated or hashed. Raw input must
contain only printable ASCII; controls and non-ASCII are rejected before any
transformation. Only outer ASCII SPACE characters are trimmed. Canonical IDs
must be 1-64 characters from ``A-Z a-z 0-9 . _ : -``.

Persisted market symbols trim only outer ASCII SPACE and then use 1-16
uppercase characters from
``A-Z 0-9 . _ : / -``. Analyst versions use 1-16 lowercase characters from
``a-z 0-9 . _ : -``. These policies match the existing database columns.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone


REQUEST_ID_MAX_LENGTH = 64
SYMBOL_MAX_LENGTH = 16
ANALYST_VERSION_MAX_LENGTH = 16

_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]+\Z", re.ASCII)
_SYMBOL = re.compile(r"[A-Z0-9._:/-]+\Z", re.ASCII)
_ANALYST_VERSION = re.compile(r"[a-z0-9._:-]+\Z", re.ASCII)


def _canonical_ascii(
    field: str,
    value: str,
    *,
    pattern: re.Pattern[str],
    max_length: int,
    case: str | None = None,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    for character in value:
        codepoint = ord(character)
        if codepoint > 0x7F:
            raise ValueError(f"{field} must contain only ASCII")
        if codepoint < 0x20 or codepoint == 0x7F:
            raise ValueError(f"{field} must not contain controls")
    canonical = value.strip(" ")
    if case == "upper":
        canonical = canonical.upper()
    elif case == "lower":
        canonical = canonical.lower()
    if not canonical:
        raise ValueError(f"{field} must be non-empty")
    if len(canonical) > max_length:
        raise ValueError(
            f"{field} must be at most {max_length} characters"
        )
    if pattern.fullmatch(canonical) is None:
        raise ValueError(f"{field} contains unsupported characters")
    return canonical


def canonical_request_id(value: str) -> str:
    """Return the sole durable representation of an HTTP/audit request ID."""

    return _canonical_ascii(
        "request_id",
        value,
        pattern=_REQUEST_ID,
        max_length=REQUEST_ID_MAX_LENGTH,
    )


def canonical_symbol(value: str) -> str:
    """Return a stable uppercase symbol suitable for 16-character columns."""

    return _canonical_ascii(
        "symbol",
        value,
        pattern=_SYMBOL,
        max_length=SYMBOL_MAX_LENGTH,
        case="upper",
    )


def canonical_analyst_version(value: str) -> str:
    """Return a stable lowercase analyst-version tag."""

    return _canonical_ascii(
        "analyst_version",
        value,
        pattern=_ANALYST_VERSION,
        max_length=ANALYST_VERSION_MAX_LENGTH,
        case="lower",
    )


def canonical_utc_datetime(value: datetime, *, field: str) -> datetime:
    """Require an aware datetime and return its equivalent UTC instant."""

    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def canonical_utc_timestamp(value: datetime, *, field: str) -> str:
    """Serialize an aware instant as fixed-microsecond UTC with a ``Z`` suffix."""

    return (
        canonical_utc_datetime(value, field=field)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
