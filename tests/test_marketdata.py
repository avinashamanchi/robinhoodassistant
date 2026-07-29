"""Alpaca equities and CoinGecko crypto data-source boundaries."""

from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trading_assistant.backtest import data as backtest_data
from trading_assistant.backtest.coingecko import CoinGeckoClient, coin_id
from trading_assistant.backtest.data import download_alpaca_bars
from trading_assistant.security.secrets import RuntimeSecrets


class _Resp:
    def __init__(self, data, url, *, status_code=200, headers=None):
        self._d = data
        self.status_code = status_code
        self.headers = headers or {}
        self.request = SimpleNamespace(url=url)

    def json(self):
        return self._d

    def iter_bytes(self):
        if isinstance(self._d, bytes):
            yield self._d
        else:
            yield json.dumps(self._d).encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("provider status")


class _HTTP:
    def __init__(self, router):
        self._router = router
        self.calls = 0

    def stream(self, method, url, *, params):
        self.calls += 1
        assert method == "GET"
        return nullcontext(_Resp(self._router(url, params), url))


def test_marketstack_runtime_and_secret_paths_are_removed():
    assert not Path("src/trading_assistant/backtest/marketstack.py").exists()
    assert "marketstack_api_key" not in RuntimeSecrets.model_fields


# ── CoinGecko ───────────────────────────────────────────────────
def test_coin_id_mapping():
    assert coin_id("BTC/USD") == "bitcoin"
    assert coin_id("ETH/USD") == "ethereum"


def test_coingecko_merges_ohlc_and_volume(tmp_path):
    def router(url, params):
        if "/ohlc" in url:
            return [[1672790400000, 100, 101, 99, 100.5]]
        return {"total_volumes": [[1672790400000, 5000]]}

    client = CoinGeckoClient(http=_HTTP(router), cache_dir=tmp_path)
    df = client.bars("BTC/USD")
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df["close"].iloc[0] == 100.5
    assert df["volume"].iloc[0] == 5000.0


def test_coingecko_cache_miss_gates_each_http_attempt(tmp_path):
    attempts = 0

    def gate(operation):
        nonlocal attempts
        attempts += 1
        return operation()

    def router(url, params):
        if "/ohlc" in url:
            return [[1672790400000, 100, 101, 99, 100.5]]
        return {"total_volumes": [[1672790400000, 5000]]}

    http = _HTTP(router)
    client = CoinGeckoClient(
        http=http,
        cache_dir=tmp_path,
        attempt_gate=gate,
    )

    client.bars("BTC/USD")
    client.bars("BTC/USD")

    assert attempts == 2
    assert http.calls == 2


def test_direct_marketdata_response_read_is_bounded_without_content_leak(
    tmp_path,
):
    """Removing bounded reads would let a provider body exhaust memory or enter errors."""
    from trading_assistant.security.outbound import OutboundResponseTooLarge

    secret = "provider-secret-body"
    client = CoinGeckoClient(
        http=_HTTP(lambda _url, _params: {"data": secret}),
        cache_dir=tmp_path,
        max_response_bytes=4,
    )

    with pytest.raises(OutboundResponseTooLarge) as raised:
        client._get("/eod", {})

    assert str(raised.value) == "outbound response too large"
    assert secret not in str(raised.value)


class _RedirectHTTP:
    def __init__(self):
        self.calls = 0

    def stream(self, _method, url, *, params):
        self.calls += 1
        return nullcontext(
            _Resp(
                {},
                url,
                status_code=302,
                headers={"Location": "https://evil.test/provider-secret"},
            )
        )


def test_direct_marketdata_redirect_is_rejected_without_second_request(tmp_path):
    """A response redirect must not be followed or expose its Location in errors."""
    from trading_assistant.security.outbound import OutboundRedirectDenied

    http = _RedirectHTTP()
    with pytest.raises(OutboundRedirectDenied) as raised:
        CoinGeckoClient(http=http, cache_dir=tmp_path)._get("/coins", {})

    assert http.calls == 1
    assert str(raised.value) == "outbound redirect rejected"
    assert "provider-secret" not in str(raised.value)


def test_alpaca_cache_miss_pins_origin_and_gates_only_network_attempt(
    tmp_path,
    monkeypatch,
):
    from alpaca.data import historical as alpaca_historical

    frame = pd.DataFrame(
        {
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
            "volume": [1_000.0],
        },
        index=pd.DatetimeIndex(
            ["2026-07-24T00:00:00Z"],
            name="ts",
        ),
    )

    class FakeAlpacaHistory:
        def __init__(self):
            self.calls = 0
            self._base_url = "https://data.alpaca.markets"

        def get_stock_bars(self, _request):
            self.calls += 1
            return type("Bars", (), {"df": frame.copy()})()

    client = FakeAlpacaHistory()
    attempts = 0
    installed = []

    monkeypatch.setattr(
        backtest_data,
        "install_pinned_session",
        lambda candidate, policy, *, read_timeout: installed.append(
            (candidate, policy.origin, read_timeout)
        ),
    )
    monkeypatch.setattr(
        alpaca_historical,
        "StockHistoricalDataClient",
        lambda *_args: client,
    )

    def gate(operation):
        nonlocal attempts
        attempts += 1
        return operation()

    download_alpaca_bars(
        "AAPL",
        "fake-key",
        "fake-secret",
        cache_dir=tmp_path,
        attempt_gate=gate,
    )
    download_alpaca_bars(
        "AAPL",
        "fake-key",
        "fake-secret",
        cache_dir=tmp_path,
        attempt_gate=lambda _operation: pytest.fail(
            "cache hit must not consume provider allowance"
        ),
    )

    assert attempts == 1
    assert client.calls == 1
    assert installed == [
        (client, "https://data.alpaca.markets", 30.0),
    ]


def test_injected_alpaca_history_fake_is_not_mutated_as_a_real_sdk_client(
    tmp_path,
    monkeypatch,
):
    frame = pd.DataFrame(
        {
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
            "volume": [1_000.0],
        },
        index=pd.DatetimeIndex(
            ["2026-07-24T00:00:00Z"],
            name="ts",
        ),
    )

    class FakeAlpacaHistory:
        def get_stock_bars(self, _request):
            return type("Bars", (), {"df": frame.copy()})()

    monkeypatch.setattr(
        backtest_data,
        "install_pinned_session",
        lambda *_args, **_kwargs: pytest.fail(
            "injected test fake must not receive production transport state"
        ),
    )

    result = download_alpaca_bars(
        "AAPL",
        "fake-key",
        "fake-secret",
        cache_dir=tmp_path,
        client_factory=lambda *_args: FakeAlpacaHistory(),
    )

    assert result["close"].iloc[-1] == 100.5
