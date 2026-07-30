"""Stable API failures that do not disclose provider or domain internals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ApiError(Exception):
    code: str
    status_code: int
    message: str
    receipt: dict[str, Any] | None = None
