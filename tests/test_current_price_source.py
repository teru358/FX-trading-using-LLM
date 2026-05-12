"""CurrentPrice.source フィールド検証 (各 fetcher 別)。"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.data.price_fetcher import CurrentPrice, fetch_current_price


def test_current_price_source_default_is_yfinance() -> None:
    cp = CurrentPrice(price=100.0, timestamp=datetime.now())
    assert cp.source == "yfinance"


def test_current_price_source_explicit() -> None:
    cp = CurrentPrice(price=100.0, timestamp=datetime.now(), source="mt5")
    assert cp.source == "mt5"


def test_yfinance_fetcher_returns_source_yfinance() -> None:
    with patch("src.data.price_fetcher.yf.Ticker") as mock_ticker:
        mock_t = MagicMock()
        mock_t.fast_info = {"last_price": 150.5}
        mock_ticker.return_value = mock_t
        cp = fetch_current_price("USDJPY=X")
        assert cp.source == "yfinance"
        assert cp.price == 150.5


def test_twelvedata_fetcher_returns_source_twelvedata() -> None:
    from src.data.twelvedata_fetcher import TwelveDataFetcher

    fetcher = TwelveDataFetcher.__new__(TwelveDataFetcher)
    fetcher._api_key = "test"           # type: ignore[attr-defined]
    fetcher._timeout = 5.0              # type: ignore[attr-defined]
    fake_quote = {
        "close": "150.5",
        "datetime": "2026-05-12 09:00:00",
        "percent_change": "0.5",
        "fifty_two_week": {},
    }
    with patch.object(fetcher, "_get_json", return_value=fake_quote):
        cp = fetcher.fetch_current_price("USDJPY=X")
        assert cp.source == "twelvedata"
        assert cp.price == 150.5


def test_mt5_fetcher_returns_source_mt5() -> None:
    from src.data.mt5_ohlcv_fetcher import Mt5OhlcvFetcher

    fetcher = Mt5OhlcvFetcher.__new__(Mt5OhlcvFetcher)
    fetcher._url = "http://test"  # type: ignore[attr-defined]
    fetcher._headers = {}                # type: ignore[attr-defined]
    fetcher._timeout = 2.0               # type: ignore[attr-defined]
    df = pd.DataFrame({"Close": [150.5]}, index=[pd.Timestamp("2026-05-12")])
    with patch.object(fetcher, "_fetch_dataframe", return_value=df):
        cp = fetcher.fetch_current_price("USDJPY")
        assert cp.source == "mt5"
        assert cp.price == 150.5
