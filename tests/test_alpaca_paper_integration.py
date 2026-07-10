"""Real Alpaca paper-sandbox smoke test. Skipped unless credentials are present.

Run with ALPACA_API_KEY / ALPACA_SECRET_KEY set to exercise the live paper API.
This never submits an order — it only reads the account and a quote.
"""

from __future__ import annotations

import os

import pytest

_HAS_KEYS = bool(os.getenv("ALPACA_API_KEY") and os.getenv("ALPACA_SECRET_KEY"))
pytestmark = pytest.mark.skipif(not _HAS_KEYS, reason="Alpaca paper credentials not set")


def test_paper_account_and_quote():
    from trading_assistant.broker.alpaca import AlpacaBroker

    broker = AlpacaBroker.from_credentials(
        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True
    )
    account = broker.get_account()
    assert account.equity >= 0

    quote = broker.get_quote("AAPL")
    assert quote.last > 0
