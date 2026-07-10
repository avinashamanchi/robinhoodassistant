"""Build the configured external source (or None). Wrapped with TTL caching +
graceful degradation. Robinhood is only constructed when explicitly enabled."""

from __future__ import annotations

from typing import Optional

from ..config import AppConfig, Secrets
from .base import ExternalAccountSource
from .caching import CachingExternalSource


def build_external_source(
    config: AppConfig, secrets: Secrets
) -> Optional[ExternalAccountSource]:
    ext = config.external_accounts
    if ext is None or not ext.robinhood.enabled:
        return None
    from .robinhood import RobinhoodSource  # lazy: keeps robin_stocks optional

    inner = RobinhoodSource(
        username=secrets.rh_username,
        password=secrets.rh_password,
        totp_secret=secrets.rh_totp_secret,
        token_path=ext.robinhood.token_path,
    )
    return CachingExternalSource(inner, ext.robinhood.cache_ttl_seconds)
