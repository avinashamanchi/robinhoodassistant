"""MarketStack (equities) + CoinGecko (crypto) data-source parsing & caching."""

from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pandas as pd
import pytest

from trading_assistant.backtest.coingecko import CoinGeckoClient, coin_id
from trading_assistant.backtest.data import download_alpaca_bars
from trading_assistant.backtest.marketstack import MarketStackClient


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


# ── MarketStack ─────────────────────────────────────────────────
_EOD = {
    "data": [
        {"date": "2023-01-04T00:00:00+0000", "open": 101, "high": 102, "low": 100,
         "close": 101.5, "volume": 1200, "adj_close": 101.5},
        {"date": "2023-01-03T00:00:00+0000", "open": 100, "high": 101, "low": 99,
         "close": 100.5, "volume": 1000, "adj_close": 100.5},
    ]
}


def test_marketstack_eod_parses_and_sorts(tmp_path):
    http = _HTTP(lambda url, params: _EOD)
    client = MarketStackClient("key", http=http, cache_dir=tmp_path)
    df = client.eod("AAPL")
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df.index.is_monotonic_increasing          # ascending by date
    assert df["close"].iloc[-1] == 101.5


def test_marketstack_uses_cache(tmp_path):
    http = _HTTP(lambda url, params: _EOD)
    client = MarketStackClient("key", http=http, cache_dir=tmp_path)
    client.eod("AAPL")
    client.eod("AAPL")                               # second call served from parquet
    assert http.calls == 1


def test_marketstack_splits_dividends(tmp_path):
    http = _HTTP(lambda url, params: {"data": [{"date": "2023-06-01", "split_factor": "4/1"}]})
    client = MarketStackClient("key", http=http, cache_dir=tmp_path)
    assert client.splits("AAPL")[0]["split_factor"] == "4/1"
    assert client.dividends("AAPL")[0]["date"] == "2023-06-01"


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


@pytest.mark.parametrize(
    "client_factory",
    [
        lambda http, tmp_path: MarketStackClient(
            "test-key", http=http, cache_dir=tmp_path, max_response_bytes=4
        ),
        lambda http, tmp_path: CoinGeckoClient(
            http=http, cache_dir=tmp_path, max_response_bytes=4
        ),
    ],
)
def test_direct_marketdata_response_read_is_bounded_without_content_leak(
    client_factory, tmp_path
):
    """Removing bounded reads would let a provider body exhaust memory or enter errors."""
    from trading_assistant.security.outbound import OutboundResponseTooLarge

    secret = "provider-secret-body"
    client = client_factory(_HTTP(lambda _url, _params: {"data": secret}), tmp_path)

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


@pytest.mark.parametrize(
    "client",
    [
        lambda http, tmp_path: MarketStackClient("test-key", http=http, cache_dir=tmp_path),
        lambda http, tmp_path: CoinGeckoClient(http=http, cache_dir=tmp_path),
    ],
)
def test_direct_marketdata_redirect_is_rejected_without_second_request(client, tmp_path):
    """A response redirect must not be followed or expose its Location in errors."""
    from trading_assistant.security.outbound import OutboundRedirectDenied

    http = _RedirectHTTP()
    with pytest.raises(OutboundRedirectDenied) as raised:
        client(http, tmp_path)._get("/eod", {})

    assert http.calls == 1
    assert str(raised.value) == "outbound redirect rejected"
    assert "provider-secret" not in str(raised.value)


def test_marketstack_transport_error_redacts_access_key(tmp_path):
    """A direct-client exception must not expose MarketStack's query-string key."""
    from trading_assistant.security.outbound import OutboundRequestFailed

    secret = "test-only-marketstack-key"

    class FailingHTTP:
        def stream(self, _method, _url, *, params):
            raise RuntimeError(f"provider failure with {params['access_key']}")

    with pytest.raises(OutboundRequestFailed) as raised:
        MarketStackClient(
            secret,
            http=FailingHTTP(),
            cache_dir=tmp_path,
        )._get("/eod", {})

    assert str(raised.value) == "outbound request failed"
    assert secret not in str(raised.value)


def test_alpaca_cache_miss_gates_only_the_network_attempt(tmp_path):
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

        def get_stock_bars(self, _request):
            self.calls += 1
            return type("Bars", (), {"df": frame.copy()})()

    client = FakeAlpacaHistory()
    attempts = 0

    def gate(operation):
        nonlocal attempts
        attempts += 1
        return operation()

    download_alpaca_bars(
        "AAPL",
        "fake-key",
        "fake-secret",
        cache_dir=tmp_path,
        client_factory=lambda *_args: client,
        attempt_gate=gate,
    )
    download_alpaca_bars(
        "AAPL",
        "fake-key",
        "fake-secret",
        cache_dir=tmp_path,
        client_factory=lambda *_args: client,
        attempt_gate=lambda _operation: pytest.fail(
            "cache hit must not consume provider allowance"
        ),
    )

    assert attempts == 1
    assert client.calls == 1
