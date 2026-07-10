"""Asset-class abstraction (Phase 7).

Crypto is a first-class live asset class: it has an independent kill switch, an
always-open clock, and a UTC-midnight daily P&L boundary. This enum threads
through the risk layer. Everything defaults to EQUITY so all pre-Phase-7
behavior is unchanged.
"""

from __future__ import annotations

import enum


class AssetClass(str, enum.Enum):
    EQUITY = "equity"
    CRYPTO = "crypto"

    @staticmethod
    def for_symbol(symbol: str) -> "AssetClass":
        """Crypto pairs carry a '/' (e.g. BTC/USD); everything else is equity.

        The crypto allowlist in config is the authoritative gate; this is only a
        fast structural classifier for routing kill switch / clock / P&L boundary.
        """
        return AssetClass.CRYPTO if "/" in symbol else AssetClass.EQUITY
