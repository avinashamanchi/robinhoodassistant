"""MarketStack (equities) + CoinGecko (crypto) data-source parsing & caching."""

from __future__ import annotations

from trading_assistant.backtest.coingecko import CoinGeckoClient, coin_id
from trading_assistant.backtest.marketstack import MarketStackClient


class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


class _HTTP:
    def __init__(self, router):
        self._router = router
        self.calls = 0

    def get(self, url, params):
        self.calls += 1
        return _Resp(self._router(url, params))


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
