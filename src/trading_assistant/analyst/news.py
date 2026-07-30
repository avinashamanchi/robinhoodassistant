"""Pinned Alpaca news ingestion through the untrusted-content quarantine."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..security.outbound import (
    OutboundPolicy,
    install_pinned_session,
    require_origin,
)
from .untrusted import (
    UntrustedContent,
    UntrustedContentError,
    UntrustedContentGateway,
)

_ALPACA_DATA_URL = "https://data.alpaca.markets"
_ALPACA_NEWS_POLICY = OutboundPolicy(_ALPACA_DATA_URL)


class NewsFetchResult(BaseModel):
    """Typed provider result; failure details are stable and redacted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["available", "unavailable"]
    items: tuple[UntrustedContent, ...] = ()
    code: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[a-z0-9_]+$",
    )

    @model_validator(mode="after")
    def state_matches_payload(self) -> NewsFetchResult:
        if self.state == "available" and self.code is not None:
            raise ValueError("available news cannot have an error code")
        if self.state == "unavailable" and (self.items or self.code is None):
            raise ValueError("unavailable news requires only an error code")
        return self


class AlpacaNewsProvider:
    """Read-only Alpaca news fetcher with deterministic quarantine."""

    def __init__(
        self,
        *,
        gateway: UntrustedContentGateway,
        api_key: str = "",
        secret_key: str = "",
        client: Any = None,
        clock: Callable[[], datetime] | None = None,
        runtime_role: str = "app",
    ) -> None:
        self._gateway = gateway
        self._api_key = api_key
        self._secret_key = secret_key
        self._client = client
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._runtime_role = runtime_role

    def _get_client(self):
        if self._client is None:
            from alpaca.data.historical.news import NewsClient

            require_origin(
                self._runtime_role,
                "alpaca.historical",
                _ALPACA_DATA_URL,
            )
            self._client = NewsClient(
                self._api_key,
                self._secret_key,
                url_override=_ALPACA_DATA_URL,
            )
            install_pinned_session(
                self._client,
                _ALPACA_NEWS_POLICY,
                read_timeout=10.0,
            )
        return self._client

    def fetch(self, symbol: str, limit: int = 10) -> NewsFetchResult:
        if (
            not isinstance(symbol, str)
            or not symbol.strip()
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 20
        ):
            return NewsFetchResult(
                state="unavailable",
                code="news_request_invalid",
            )
        try:
            from alpaca.data.requests import NewsRequest

            resp = self._get_client().get_news(
                NewsRequest(
                    symbols=symbol.strip().upper(),
                    limit=limit,
                    include_content=False,
                )
            )
        except Exception:
            return NewsFetchResult(
                state="unavailable",
                code="news_provider_unavailable",
            )

        try:
            raw_items = self._extract_items(resp)
        except Exception:
            return NewsFetchResult(
                state="unavailable",
                code="news_response_invalid",
            )
        if raw_items is None:
            return NewsFetchResult(
                state="unavailable",
                code="news_response_invalid",
            )

        gateway_items: list[dict[str, Any]] = []
        try:
            received_at = self._clock()
            for item in raw_items:
                headline = getattr(item, "headline", "")
                summary = getattr(item, "summary", "")
                if not isinstance(headline, str) or not isinstance(summary, str):
                    raise UntrustedContentError("invalid_content")
                source_id = getattr(item, "id", None)
                if source_id is None:
                    raise UntrustedContentError("invalid_source_id")
                raw_text = "\n".join(
                    value for value in (headline, summary) if value
                )
                if not raw_text:
                    continue
                gateway_items.append(
                    {
                        "source_kind": "alpaca_news",
                        "source_id": str(source_id),
                        "source_name": getattr(item, "source", None),
                        "source_url": getattr(item, "url", None),
                        "published_at": getattr(item, "created_at", None),
                        "received_at": received_at,
                        "raw_text": raw_text,
                    }
                )
            quarantined = self._gateway.ingest_many(gateway_items)
        except UntrustedContentError:
            return NewsFetchResult(
                state="unavailable",
                code="news_content_rejected",
            )
        except Exception:
            return NewsFetchResult(
                state="unavailable",
                code="news_quarantine_unavailable",
            )

        return NewsFetchResult(state="available", items=quarantined)

    @staticmethod
    def _extract_items(response: Any) -> list[Any] | tuple[Any, ...] | None:
        if hasattr(response, "data"):
            data = response.data
            if not isinstance(data, dict) or "news" not in data:
                return None
            items = data["news"]
        elif hasattr(response, "news"):
            items = response.news
        else:
            return None
        if not isinstance(items, (list, tuple)) or len(items) > 20:
            return None
        return items


__all__ = [
    "AlpacaNewsProvider",
    "NewsFetchResult",
]
