"""twelvedata_fetcher のユニットテスト（API呼び出しはモック）。"""
from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.data.twelvedata_fetcher import (
    TwelveDataFetcher,
    _symbol_to_twelvedata,
    _symbol_from_twelvedata,
)


def test_symbol_to_twelvedata():
    assert _symbol_to_twelvedata("USDJPY=X") == "USD/JPY"
    assert _symbol_to_twelvedata("EURUSD=X") == "EUR/USD"
    assert _symbol_to_twelvedata("GBPUSD=X") == "GBP/USD"


def test_symbol_from_twelvedata():
    assert _symbol_from_twelvedata("USD/JPY") == "USDJPY=X"
    assert _symbol_from_twelvedata("EUR/USD") == "EURUSD=X"


def test_symbol_non_fx_passthrough():
    """FX以外のシンボルはそのまま返す。"""
    assert _symbol_to_twelvedata("^N225") == "^N225"


class TestTwelveDataFetcher:

    def test_init_no_key_raises(self):
        with pytest.raises(ValueError, match="TWELVEDATA_API_KEY"):
            TwelveDataFetcher(api_key="")

    def test_init_with_key(self):
        fetcher = TwelveDataFetcher(api_key="test_key_123")
        assert fetcher._api_key == "test_key_123"

    def test_fetch_current_price_parses_quote(self):
        fetcher = TwelveDataFetcher(api_key="test_key")
        mock_response = {
            "symbol": "USD/JPY",
            "close": "149.850",
            "previous_close": "150.200",
            "change": "-0.350",
            "percent_change": "-0.23",
            "is_market_open": True,
            "fifty_two_week": {
                "high": "161.950",
                "low": "133.080",
            },
            "timestamp": 1711872000,
        }
        with patch.object(fetcher, "_get_json", return_value=mock_response):
            cp = fetcher.fetch_current_price("USDJPY=X")
        assert cp.price == 149.85
        assert cp.percent_change == -0.23
        assert cp.fifty_two_week_high == 161.95
        assert cp.fifty_two_week_low == 133.08
        assert cp.is_market_open is True

    def test_fetch_ohlcv_parses_time_series(self):
        fetcher = TwelveDataFetcher(api_key="test_key")
        mock_response = {
            "meta": {"symbol": "USD/JPY", "interval": "1h"},
            "values": [
                {"datetime": "2026-03-31 12:00:00", "open": "149.5", "high": "150.0", "low": "149.3", "close": "149.8", "volume": "0"},
                {"datetime": "2026-03-31 11:00:00", "open": "149.0", "high": "149.6", "low": "148.9", "close": "149.5", "volume": "0"},
            ],
            "status": "ok",
        }
        with patch.object(fetcher, "_get_json", return_value=mock_response):
            price_data = fetcher.fetch_ohlcv("USDJPY=X", period="5d", interval="1h")
        assert price_data.symbol == "USDJPY=X"
        assert len(price_data.df) == 2
        assert price_data.current_price == 149.8
        assert list(price_data.df.columns) == ["Open", "High", "Low", "Close", "Volume"]
