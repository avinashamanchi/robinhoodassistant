"""Deterministic mock external source for tests. READ-ONLY (no write methods)."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .base import ExternalAccountSummary, ExternalPosition


class MockExternalSource:
    source_name = "mock_external"

    def __init__(
        self,
        positions: Optional[list[ExternalPosition]] = None,
        summary: Optional[ExternalAccountSummary] = None,
        fail: bool = False,
    ) -> None:
        self._positions = positions or [
            ExternalPosition("NVDA", Decimal("10"), Decimal("400"), Decimal("500"), "mock_external"),
        ]
        self._summary = summary or ExternalAccountSummary(
            total_equity=Decimal("10000"),
            cash=Decimal("2000"),
            buying_power=Decimal("2000"),
            source="mock_external",
        )
        self.fail = fail
        self.fetch_count = 0  # lets tests assert cache behavior

    def _maybe_fail(self) -> None:
        if self.fail:
            raise RuntimeError("mock external source is down")

    def get_positions(self) -> list[ExternalPosition]:
        self.fetch_count += 1
        self._maybe_fail()
        return list(self._positions)

    def get_account_summary(self) -> ExternalAccountSummary:
        self._maybe_fail()
        return self._summary

    def get_order_history(self, days: int = 30) -> list[dict]:
        self._maybe_fail()
        return [{"ticker": "NVDA", "side": "buy", "qty": "10", "source": "mock_external"}]

    def get_dividends(self, days: int = 90) -> list[dict]:
        self._maybe_fail()
        return [{"ticker": "NVDA", "amount": "5.00", "source": "mock_external"}]
