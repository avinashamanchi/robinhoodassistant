"""Production external-account integrations are intentionally disabled."""

from __future__ import annotations

from ..config import AppConfig, Secrets
from .base import ExternalAccountSource


def build_external_source(
    config: AppConfig, secrets: Secrets
) -> ExternalAccountSource | None:
    del config, secrets
    return None
