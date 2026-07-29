"""Asset-class abstraction (Phase 7).

Crypto is a first-class paper-runtime asset class: it has an independent kill
switch, an always-open clock, and a UTC-midnight daily P&L boundary. It is not a
live-trading asset class in this release. This enum threads through the risk
layer. Everything defaults to EQUITY so all pre-Phase-7 behavior is unchanged.
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


def _normalized_symbol(symbol: object) -> str:
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be a nonblank string")
    return symbol.strip().upper()


def _compact_pair(symbol: str) -> str | None:
    if "/" not in symbol:
        return symbol
    parts = symbol.split("/")
    if (
        len(parts) != 2
        or not all(parts)
        or parts[1] != "USD"
    ):
        return None
    return "".join(parts)


def broker_symbol_matches_local(
    broker_symbol: object,
    local_symbol: object,
) -> bool:
    """Whether a broker spelling identifies one trusted local symbol.

    Direction matters: an exact spelling always matches, while a compact
    broker spelling is accepted only when the local symbol is a canonical
    slash-form USD crypto pair.
    """
    broker = _normalized_symbol(broker_symbol)
    local = _normalized_symbol(local_symbol)
    if broker == local:
        return True
    if "/" not in local or "/" in broker:
        return False
    local_compact = _compact_pair(local)
    return local_compact is not None and broker == local_compact


def canonicalize_broker_symbol(
    symbol: object,
    *,
    asset_class: object | None = None,
    local_symbol: str | None = None,
) -> str:
    """Return the local spelling for an identified broker symbol.

    Compact symbols are never guessed to be crypto based on their suffix alone.
    Alpaca asset metadata authorizes conversion at adapter boundaries. Exact
    fill reconciliation can instead use the already matched local order as its
    authority, but only when a compact broker spelling identifies that
    canonical slash-form pair.
    """
    normalized = _normalized_symbol(symbol)
    if local_symbol is not None:
        local = _normalized_symbol(local_symbol)
        if broker_symbol_matches_local(normalized, local):
            return local
        return normalized

    raw_asset_class = getattr(asset_class, "value", asset_class)
    if str(raw_asset_class).strip().lower() != AssetClass.CRYPTO.value:
        return normalized
    if "/" in normalized:
        if _compact_pair(normalized) is None:
            raise ValueError("invalid broker crypto pair")
        return normalized
    if normalized.endswith("USD") and len(normalized) > len("USD"):
        return f"{normalized[:-3]}/USD"
    raise ValueError("unsupported compact broker crypto pair")
