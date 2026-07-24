"""Stable API failures that do not disclose provider or domain internals."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ApiError(Exception):
    code: str
    status_code: int
    message: str
