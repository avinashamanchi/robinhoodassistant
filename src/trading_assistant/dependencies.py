"""Typed, detail-free signals for required external evidence."""

from __future__ import annotations


class RequiredDependencyUnavailable(RuntimeError):
    """A required provider or health input could not be collected safely."""

    def __init__(self) -> None:
        super().__init__("required dependency unavailable")


class RequiredQuoteUnavailable(RequiredDependencyUnavailable):
    """A required quote is missing, invalid, stale, or otherwise unavailable."""
